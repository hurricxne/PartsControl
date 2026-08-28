"""API del módulo Wasabil DTE — emisión de guías de despacho electrónicas (SII 52).

Prefijo: /wasabil (se monta con prefix=/api → /api/wasabil). Solo Grupo AM
(candado 'mineria', igual que los demás módulos de contabilidad).

Flujo (protocolo de seguridad — emitir al SII es IRREVERSIBLE):
  1. POST /despachos/{id}/preview   → arma el documento y lo muestra (NO toca el SII)
  2. POST /despachos/{id}/emitir    → con el OK explícito del usuario: reclama el
                                      "claim" anti doble emisión (bajo lock, sin red),
                                      llama a Wasabil con issue=true y persiste el
                                      uuid DE INMEDIATO
  3. GET  /despachos/{id}/estado    → sondeo; al quedar Emitido (3) graba el folio
                                      real en despacho.numero_guia + links PDF/XML
  4. POST /despachos/{id}/reintentar→ reintento SEGURO (verifica el estado real en
                                      Wasabil ANTES de re-crear; si no puede
                                      verificar, ABORTA: nunca re-crea a ciegas)
  5. POST /despachos/{id}/registrar-folio (y su gemela de facturas) → salida del
                                      callejón "EMITIDA sin folio": registra a mano
                                      el folio REAL leído en app.wasabil.com, con
                                      confirmación y rastro. NUNCA emite nada.

Disciplina anti doble emisión (diseñada tras la mesa redonda de revisión):
  - La fila `wasabil_dte` es el ancla: única por despacho (índice) y con un claim
    `en_vuelo_desde` que se marca BAJO LOCK antes de la llamada HTTP y bloquea a
    cualquier otro request mientras esté fresco (CLAIM_TTL_SEGUNDOS).
  - Los locks (SELECT ... FOR UPDATE) son SIEMPRE cortos y sin red adentro: las
    llamadas a Wasabil ocurren fuera de toda transacción con locks.
  - La máquina de estados es explícita (_estado_dte_bloquea): nada de filtrar
    mensajes por texto.

Los precios de las líneas usan el MISMO cálculo que Contabilidad al facturar
(routers.contabilidad._precios_de_cotizacion): la guía y su futura factura cuadran.
"""
import json
from datetime import date, datetime
from typing import List, NamedTuple, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import (
    User, Despacho, DespachoItem, OcCliente, Cotizacion, ConfiguracionCotizador,
)
from routers.contabilidad import _cfg_to_dict, _precios_de_cotizacion

from . import client as wasabil
from .models import (
    WasabilDte, STATUS_EMITIDO, STATUS_FALLIDO, STATUS_PROCESANDO, STATUS_PENDIENTE,
)
from .service import (
    TIPO_DOC_GUIA, FOLIO_REF_MAX, TIPOS_TRASLADO, TIPO_TRASLADO_DEFAULT,
    armar_lineas, armar_guia, payload_a_rest, parse_fecha_oc, cuadratura,
    total_neto_lineas, serialize_dte, claim_vigente, _folio_dte_valido, _f,
    advertencia_lineas_sii_gratuito,
)

# Módulo SOLO MachParts (Grupo AM = 'mineria'): Wasabil emite con el RUT de
# GRUPO AM SPA, por lo que usuarios de otra empresa quedan denegados (403).
router = APIRouter(
    prefix="/wasabil",
    tags=["wasabil-dte"],
    dependencies=[Depends(require_empresa("mineria"))],
)


# ─── Helpers ────────────────────────────────────────────────────────────────────
def _cargar_contexto(db: Session, despacho_id: int) -> Tuple[Despacho, OcCliente, Cotizacion]:
    """Despacho + su OC + su cotización, con 404 claros. SIN locks (las
    validaciones con red van fuera de transacciones con locks)."""
    despacho = (
        db.query(Despacho)
        .options(joinedload(Despacho.items))
        .filter(Despacho.id == despacho_id)
        .first()
    )
    if not despacho:
        raise HTTPException(404, "Despacho no encontrado")
    oc = db.query(OcCliente).filter(OcCliente.id == despacho.oc_cliente_id).first()
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "La OC de cliente de este despacho no existe")
    return despacho, oc, oc.cotizacion


def _dte_de_despacho(db: Session, despacho_id: int, lock: bool = False) -> Optional[WasabilDte]:
    q = db.query(WasabilDte).filter(WasabilDte.despacho_id == despacho_id,
                                    WasabilDte.tipo_dte == TIPO_DOC_GUIA)
    if lock:
        # populate_existing es OBLIGATORIO: sin él, si la fila ya está en el identity
        # map de la sesión, SQLAlchemy DESCARTA los valores frescos que devuelve el
        # SELECT ... FOR UPDATE y la re-validación vería datos viejos (p.ej. el claim
        # que otro request acaba de commitear) → doble emisión.
        q = q.populate_existing().with_for_update()
    return q.first()


def _precios(db: Session, cot: Cotizacion) -> dict:
    """{item_cotizacion_id: {"precio_venta_clp": ...}} — mismo cálculo que facturación."""
    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    _items, pmap, _tot = _precios_de_cotizacion(db, cot.id, cfg_dict)
    return pmap


def _estado_dte_bloquea(dte: Optional[WasabilDte], para_reintento: bool,
                        sustantivo: str = "este despacho",
                        documento: str = "guía electrónica emitida") -> Optional[str]:
    """Máquina de estados EXPLÍCITA del DTE previo: devuelve el problema que
    bloquea la emisión, o None si se puede proceder. Cubre TODOS los estados:

      sin fila            → emitir OK · reintentar no aplica (404 antes)
      emitido (3)         → bloquea siempre
      claim en vuelo      → bloquea siempre (hay una emisión HTTP en curso)
      uuid + procesando/sin status → bloquea siempre (consultar estado)
      uuid + pendiente (6)→ bloquea siempre (borrador en Wasabil: resolver allá)
      fallido (4)         → bloquea emitir (usar Reintentar) · reintentar OK
      sin uuid + error    → bloquea emitir (usar Reintentar) · reintentar OK

    `sustantivo`/`documento` parametrizan SOLO el texto: la misma máquina la usa la
    Fase B para facturas 33, y el usuario que reintentaba una FACTURA leía un mensaje
    que hablaba de despachos. La lógica es idéntica; cambia el sustantivo.
    """
    if not dte:
        return None
    if dte.status_id == STATUS_EMITIDO:
        return f"{sustantivo.capitalize()} YA tiene {documento} (folio {dte.folio})"
    if claim_vigente(dte):
        return (f"Hay una emisión EN CURSO para {sustantivo} (otro usuario o pestaña). "
                "Espera unos minutos y consulta el estado")
    if dte.uuid and (dte.status_id == STATUS_PROCESANDO or dte.status_id is None):
        return "Hay una emisión EN PROCESO en el SII: consulta su estado antes de intentar otra"
    if dte.uuid and dte.status_id == STATUS_PENDIENTE:
        return ("El documento quedó como BORRADOR en Wasabil (no llegó al SII): "
                "revísalo/emítelo en app.wasabil.com o elimínalo allá antes de reintentar")
    if dte.status_id == STATUS_FALLIDO:
        return None if para_reintento else \
            "La emisión anterior FALLÓ en el SII: usa el botón Reintentar"
    if dte.uuid is None:
        return None if para_reintento else \
            "Hay un intento previo que no llegó a Wasabil: usa el botón Reintentar"
    # Default-deny: un documento con uuid en un estado que no reconocemos NUNCA
    # habilita otra emisión (ante la duda, bloquear — emitir es irreversible)
    return (f"El documento tiene un estado desconocido en Wasabil (status {dte.status_id}): "
            "consulta el estado o resuélvelo en app.wasabil.com")


def _preparar_emision(db: Session, despacho_id: int, para_reintento: bool = False) -> dict:
    """Arma y valida TODO lo necesario para emitir (SIN locks: puede llamar a
    Wasabil para resolver la ficha del cliente). Devuelve contexto + `problemas`
    (bloqueantes) + `advertencias`. Única fuente de verdad de validación."""
    despacho, oc, cot = _cargar_contexto(db, despacho_id)
    problemas: List[str] = []
    advertencias: List[str] = []

    # ── Estado del despacho ──
    if despacho.estado == "anulado":
        problemas.append("El despacho está anulado")
    elif despacho.estado != "en_preparacion":
        problemas.append(
            f"Solo se emite la guía con el despacho EN PREPARACIÓN (estado actual: "
            f"{despacho.estado}). Si este despacho ya salió con guía manual, no corresponde emitir."
        )

    # ── Ancla interna: el N° de despacho es el invoiceReference (anti doble emisión) ──
    if not (despacho.numero_despacho or "").strip():
        problemas.append("El despacho no tiene N° interno: sin él no hay ancla de "
                         "referencia para la guía electrónica (el reintento la usa para "
                         "reencontrar el documento en Wasabil y no re-crearlo)")

    # ── Guía electrónica previa (máquina de estados explícita) ──
    dte = _dte_de_despacho(db, despacho.id)
    problema_dte = _estado_dte_bloquea(dte, para_reintento)
    if problema_dte:
        problemas.append(problema_dte)
    if dte and dte.status_id == STATUS_FALLIDO and para_reintento:
        advertencias.append(f"Emisión anterior rechazada por el SII: {dte.error or 'sin detalle'}")

    # ── Datos del cliente / OC (lo que exige el SII) ──
    rut = (cot.rut_cliente or "").strip()
    if not rut:
        problemas.append("La cotización no tiene RUT de cliente: complétalo antes de emitir "
                         "(el SII exige el RUT del receptor)")
    numero_oc = (oc.numero_oc or "").strip()
    if not numero_oc:
        problemas.append("La OC de cliente no tiene número: la guía debe referenciarla (tipo 801)")
    elif len(numero_oc) > FOLIO_REF_MAX:
        # El SII limita el folio de una referencia a 18 caracteres; truncarlo cambiaría
        # la referencia legal a la OC del cliente, así que se BLOQUEA para que el
        # operador acorte el N° real (mejor detectarlo aquí que en el rechazo al emitir).
        problemas.append(
            f"El N° de OC del cliente ('{numero_oc}') tiene {len(numero_oc)} caracteres; "
            f"el SII permite máximo {FOLIO_REF_MAX} en la referencia. Acorta el N° de OC en la venta."
        )
    fecha_oc = parse_fecha_oc(oc.fecha_oc)
    if not fecha_oc:
        problemas.append(
            f"No se pudo interpretar la fecha de la OC ('{oc.fecha_oc or 'vacía'}'): "
            "corrígela en la venta (la referencia 801 lleva la fecha real de la OC)"
        )

    # ── Líneas (cantidades del despacho × precios de la cotización) ──
    lineas, problemas_lineas = armar_lineas(despacho.items, _precios(db, cot))
    problemas.extend(problemas_lineas)
    # ADVERTENCIA (jamás bloqueo): la vía SII gratuito por la que emite la cuenta
    # rechaza documentos con más de 10 ítems (los 3 únicos fallidos históricos).
    # El operador puede dividir el despacho ANTES de emitir — o emitir igual si
    # la cuenta ya no depende de esa vía (por eso no va en `problemas`).
    aviso_tope = advertencia_lineas_sii_gratuito(len(lineas), "guía")
    if aviso_tope:
        advertencias.append(aviso_tope)

    # ── Receptor: ficha del cliente en Wasabil (autocompleta datos ante el SII) ──
    receptor = {
        "rut": rut or None,
        "razon_social": cot.cliente or None,
        "giro": None, "direccion": None, "comuna": None, "ciudad": None,
        "fuente": "cotizacion",
    }
    client_id = None
    if not wasabil.esta_configurado():
        problemas.append("Wasabil no está configurado (falta WASABIL_API_TOKEN en backend/.env): "
                         "puedes previsualizar, pero no emitir")
    elif rut:
        try:
            cli = wasabil.buscar_cliente_por_rut(rut)
            if not cli:
                problemas.append(
                    f"El cliente RUT {rut} no existe en Wasabil: créalo en app.wasabil.com "
                    "(con giro y dirección) y vuelve a intentar"
                )
            else:
                client_id = cli.get("id")
                receptor = {
                    "rut": cli.get("rut") or rut,
                    "razon_social": cli.get("name") or cli.get("razon_social") or cot.cliente,
                    "giro": cli.get("giro") or cli.get("activity"),
                    "direccion": cli.get("address") or cli.get("direccion"),
                    "comuna": cli.get("comuna") or cli.get("commune"),
                    "ciudad": cli.get("city") or cli.get("ciudad"),
                    "fuente": "wasabil",
                }
                # El SII exige receptor completo en la guía 52 de venta: si la ficha
                # de Wasabil viene sin giro/dirección/comuna, avisar ANTES de emitir
                # (emitir con la ficha incompleta termina en rechazo del SII)
                faltantes = [
                    nombre for nombre, valor in (
                        ("giro", receptor["giro"]),
                        ("dirección", receptor["direccion"]),
                        ("comuna", receptor["comuna"]),
                    ) if not (valor or "").strip()
                ]
                if faltantes:
                    advertencias.append(
                        f"La ficha del cliente en Wasabil no tiene {', '.join(faltantes)}: "
                        "el SII puede rechazar la guía — complétala en app.wasabil.com antes de emitir"
                    )
                # La guía sale con la dirección REGISTRADA en Wasabil; si el despacho
                # trae otra dirección de entrega, avisar (se corrige en la ficha Wasabil)
                dir_wasabil = (receptor["direccion"] or "").strip().lower()
                dir_despacho = (despacho.direccion_entrega or "").strip().lower()
                if dir_despacho and dir_wasabil and dir_despacho[:15] not in dir_wasabil:
                    advertencias.append(
                        "La dirección de entrega del despacho difiere de la registrada en "
                        f"Wasabil ('{receptor['direccion']}'): la guía saldrá con la de Wasabil"
                    )
        except wasabil.WasabilError as e:
            problemas.append(f"No se pudo consultar el cliente en Wasabil: {e}")

    # ── Avisos no bloqueantes ──
    if despacho.numero_guia and not (dte and dte.folio):
        advertencias.append(
            f"El despacho tiene N° de guía manual '{despacho.numero_guia}': al emitir, "
            "se REEMPLAZARÁ por el folio real del SII"
        )

    neto, iva, total = cuadratura(total_neto_lineas(lineas)) if lineas else (0, 0, 0)
    return {
        "despacho": despacho, "oc": oc, "cot": cot, "dte": dte,
        "lineas": lineas, "fecha_oc": fecha_oc, "client_id": client_id,
        "receptor": receptor, "problemas": problemas, "advertencias": advertencias,
        "neto": neto, "iva": iva, "total": total,
    }


def _reclamar_emision(db: Session, despacho_id: int, para_reintento: bool,
                      usuario_id: Optional[int], empresa: str) -> WasabilDte:
    """Transacción CORTA y bajo lock (sin red) que deja el claim anti doble emisión:

    1. FOR UPDATE sobre el despacho (serializa claims del mismo despacho).
    2. FOR UPDATE sobre la fila wasabil_dte (si existe) y RE-VALIDACIÓN del estado
       con datos frescos — lo que otro request haya hecho ya es visible aquí.
    3. Marca `en_vuelo_desde` (o crea la fila) y COMMITEA: el claim queda visible
       y los locks se liberan ANTES de cualquier llamada HTTP.
    """
    despacho = (
        db.query(Despacho)
        .filter(Despacho.id == despacho_id)
        .populate_existing()  # ver nota en _dte_de_despacho: sin esto se validaría con datos viejos
        .with_for_update()
        .first()
    )
    if not despacho:
        raise HTTPException(404, "Despacho no encontrado")
    if despacho.estado != "en_preparacion":
        db.rollback()
        raise HTTPException(409, f"El despacho ya no está en preparación (estado: {despacho.estado})")

    dte = _dte_de_despacho(db, despacho_id, lock=True)
    problema = _estado_dte_bloquea(dte, para_reintento)
    if problema:
        db.rollback()
        raise HTTPException(409, problema)

    ahora = datetime.utcnow()  # UTC naive, igual que claim_vigente (inmune a cambios de hora)
    if dte:
        # Reintento: se reutiliza la MISMA fila (el índice único no permite otra)
        dte.en_vuelo_desde = ahora
        dte.status_id = STATUS_PENDIENTE
        dte.uuid = None
        dte.error = None
        dte.usuario_id = usuario_id or dte.usuario_id
    else:
        dte = WasabilDte(
            empresa=empresa, tipo_dte=TIPO_DOC_GUIA, despacho_id=despacho_id,
            status_id=STATUS_PENDIENTE, en_vuelo_desde=ahora, usuario_id=usuario_id,
        )
        db.add(dte)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Ya existe una emisión para este despacho (refresca la página)")
    db.refresh(dte)
    return dte


def _vacio(valor) -> bool:
    """None, cadena vacía o solo espacios (criterio único de 'dato ausente')."""
    return valor is None or (isinstance(valor, str) and not valor.strip())


def _fusionar_respuesta(base: dict, extra: dict) -> dict:
    """Fusiona la respuesta original con la enriquecida SIN DEGRADAR ningún dato.

    `{**base, **extra}` conserva las claves AUSENTES de `extra`, pero NO las que llegan
    presentes-con-null: un documento completo que respondiera `{"uuid": None,
    "status_id": 3, "folio": None}` BORRABA el uuid que sí traía el POST y dejaba la fila
    en el callejón 'status 3 · folio NULL · uuid NULL' — es decir, el rescate CREABA el
    estado que existe para evitar (sin rescate, el uuid del POST se persistía y el sondeo
    curaba solo). Acá un valor vacío del enriquecido nunca pisa uno útil del original."""
    fusion = dict(base or {})
    for clave, valor in (extra or {}).items():
        if _vacio(valor) and not _vacio(fusion.get(clave)):
            continue  # el documento completo no sabe: se conserva lo que ya teníamos
        fusion[clave] = valor
    return fusion


def _coincide_referencia(doc: dict, referencia_interna: str, compat_v1: bool) -> bool:
    """¿Este documento de Wasabil lleva ESTA referencia interna? Match EXACTO; `compat_v1`
    acepta además el formato viejo "OC <n> · <N° despacho>" de las guías ya emitidas
    (folio 136), con sufijo "· " y NO substring (DSP-0001 ≠ DSP-00012)."""
    inv = str(doc.get("invoice_reference") or "")
    return inv == referencia_interna or (compat_v1 and inv.endswith(f"· {referencia_interna}"))


def _status_de(doc: dict) -> int:
    """status_id del documento como int (0 = desconocido). Un dato ilegible NUNCA debe
    reventar el rescate: 0 no es EMITIDO, así que cae del lado conservador."""
    try:
        return int(doc.get("status_id") or 0)
    except (TypeError, ValueError):
        return 0


# Clave interna (no del API) con la que el rescate deja dicho que hay algo que un HUMANO
# tiene que mirar: _actualizar_desde_wasabil la conserva en `dte.error` incluso cuando el
# documento quedó EMITIDO (donde el error se limpia), porque es el único canal por el que
# el operador/el sondeo ven la ambigüedad.
CLAVE_AVISO = "_aviso_rescate"


def _msg_rescate_ambiguo(referencia_interna: str, folios: List[str]) -> str:
    return (f"Wasabil tiene {len(folios)} documentos EMITIDOS con la MISMA referencia "
            f"interna '{referencia_interna}' (folios: {', '.join(folios)}). No se elige "
            "ninguno automáticamente: cualquiera de esos folios es un documento tributario "
            "REAL y quedarse con el otro perdería un folio para siempre. NO se emite nada "
            "nuevo. Revísalos en app.wasabil.com y pide soporte para dejar registrado el "
            "folio correcto (probablemente haya que anular uno con nota de crédito).")


def _documentos_de_referencia(referencia_interna: str,
                              compat_v1: bool = False) -> List[dict]:
    """TODOS los documentos que Wasabil tiene con esta referencia interna (SOLO LECTURA).

    Después de un reintento el estado NORMAL son DOS documentos con la misma referencia
    (`_reclamar_emision` reutiliza la fila y reusa el ancla): el del intento rechazado y
    el nuevo. Por eso el rescate tiene que ver la lista COMPLETA y decidir, en vez de
    quedarse con el primero que pase.

    Una lista truncada por paginación NUNCA se interpreta: "no encontré un emitido" no
    prueba que no exista, y el documento que falta puede ser justo el que tiene el folio.
    Se convierte en WasabilError(ambiguo) y el llamador aborta (jamás re-emitir a ciegas).
    """
    documentos, busqueda_completa = wasabil.buscar_documentos(referencia_interna)
    coincidencias = [d for d in (documentos or [])
                     if isinstance(d, dict)
                     and _coincide_referencia(d, referencia_interna, compat_v1)]
    if not busqueda_completa:
        raise wasabil.WasabilError(
            "La búsqueda en Wasabil quedó incompleta (lista paginada): no se puede "
            "concluir qué documentos existen con esta referencia", ambiguo=True)
    return coincidencias


def _es_rechazo_confirmado(doc: dict) -> bool:
    """¿CONSTA que este documento no va a tener folio nunca?

    Es la única lectura que autoriza a re-emitir, así que se exige que el dato sea
    LEGIBLE: un `status_id` que no se puede interpretar no prueba nada. Ojo con el
    atajo tentador de reusar `_status_de`, que devuelve 0 ante un dato ilegible: 0 no
    es FALLIDO, pero tampoco es "consta que fue rechazado", y esa confusión es
    exactamente el agujero que se cerró acá.
    """
    try:
        return int(doc.get("status_id")) == STATUS_FALLIDO
    except (TypeError, ValueError):
        return False


class ClasificacionRef(NamedTuple):
    """Los documentos de una referencia, partidos por lo que pueden hacerle a un folio."""
    emitidos: List[dict]        # status 3 legible: CONSTA que hay un folio real
    pueden_tener_folio: List[dict]  # ni emitidos ni rechazo confirmado: PODRÍAN quedárselo


def _clasificar_referencia(referencia_interna: str,
                           compat_v1: bool = False) -> ClasificacionRef:
    """Parte los documentos de Wasabil en "ya tiene folio" y "todavía podría tenerlo".

    La pregunta del cinturón NO es "¿hay un status 3?" sino "¿puedo PROBAR que no existe
    ningún documento capaz de quedarse con un folio?". Un documento `procesando` (2), uno
    en cola, o uno con un `status_id` que este código no sabe leer, todavía pueden
    convertirse en un DTE real: cuentan como "no se puede concluir", no como "no hay nada".
    Contarlos como "no hay nada" habilitaba re-emitir con el listado SANO — se reprodujeron
    7 dobles emisiones reales (52 y 33, en las dos marcas) por esta sola diferencia.

    Es además la lectura que `_rescatar_por_referencia` YA hacía (trata como vivo todo lo
    que no es EMITIDO ni FALLIDO): el cinturón y el rescate preguntan a la misma fuente y
    tienen que leerla igual, o uno bloquea mientras el otro deja pasar.
    """
    documentos = _documentos_de_referencia(referencia_interna, compat_v1)
    emitidos = [d for d in documentos if _status_de(d) == STATUS_EMITIDO]
    pueden = [d for d in documentos
              if _status_de(d) != STATUS_EMITIDO and not _es_rechazo_confirmado(d)]
    return ClasificacionRef(emitidos, pueden)


def _rescatar_por_referencia(referencia_interna: str,
                             compat_v1: bool = False) -> Optional[dict]:
    """Documento de Wasabil con esta referencia interna que describe el estado REAL, o
    None si no hay ninguno. SOLO LECTURA — es la única vía de rescate cuando la respuesta
    no trajo uuid (sin uuid no hay a quién consultar por id) y JAMÁS crea nada.

    Orden de preferencia EXPLÍCITO (el bug era "el primero que pase"):
      1. Hay MÁS DE UN documento EMITIDO con la misma referencia → se ABORTA pidiendo
         intervención humana. Elegir uno sería decidir en silencio qué folio real se
         registra y cuál se pierde.
      2. Hay EXACTAMENTE UNO emitido → ése, completado por uuid (folio + PDF/XML). El
         status 3 es el ÚNICO estado que trae folio, así que es el único cuya pérdida es
         irreversible: gana siempre, esté donde esté en la lista.
      3. Ninguno emitido, pero hay alguno vivo (procesando/borrador/estado raro) → ése:
         el documento puede todavía nacer con folio, así que el llamador debe bloquear.
      4. Solo rechazados → el primero. Ninguno tiene folio que perder, así que son
         equivalentes para la decisión ("no hay documento emitido con esta referencia").
    """
    coincidencias = _documentos_de_referencia(referencia_interna, compat_v1)
    emitidos = [d for d in coincidencias if _status_de(d) == STATUS_EMITIDO]
    if len(emitidos) > 1:
        raise wasabil.WasabilError(
            _msg_rescate_ambiguo(referencia_interna,
                                 [str(d.get("folio") or "sin folio") for d in emitidos]),
            ambiguo=True)
    if emitidos:
        doc = emitidos[0]
        if doc.get("uuid"):
            # El documento COMPLETO garantiza folio + PDF/XML
            return _fusionar_respuesta(doc, wasabil.obtener_documento(doc["uuid"]) or {})
        return doc
    vivos = [d for d in coincidencias
             if _status_de(d) not in (STATUS_EMITIDO, STATUS_FALLIDO)]
    if vivos:
        return vivos[0]
    return coincidencias[0] if coincidencias else None


def _completar_documento_emitido(data: dict, referencia_interna: Optional[str] = None,
                                 compat_v1: bool = False) -> dict:
    """Si la respuesta de Wasabil dice EMITIDO (3) pero NO trae folio, re-consulta el
    documento completo y devuelve la respuesta enriquecida.

    POR QUÉ: el POST /documents puede volver con {"uuid": ..., "status_id": 3} SIN la
    clave `folio` (forma que el README del módulo declara PENDIENTE de confirmar contra
    el API real). Sin esto, el DTE queda 'emitido' con folio NULL y el estado es
    PERMANENTE: el sondeo no volvía a consultar, /reintentar responde 409 'ya está
    emitida' y el N° manual no se puede editar (guard anti-pisado de despachos.py).
    Consecuencias medidas: el despacho nunca recibe su N° de guía y la factura 33 salía
    al SII referenciando el N° TECLEADO a mano — un folio 52 que el SII no reconoce,
    irreversible salvo nota de crédito.

    La variante SIN uuid es el mismo callejón y llega igual de fácil (una respuesta
    `{"status_id": 3}` pelada): ahí no hay documento que consultar por id, así que el
    rescate va por la REFERENCIA INTERNA (el ancla anti doble emisión: N° de despacho
    para la 52, FACT-<id> para la 33). Sigue siendo solo lectura.

    Falla ABIERTO a propósito: un error de CONSULTA jamás debe convertirse en el
    fracaso de una emisión que SÍ salió (el documento ya existe ante el SII). Si la
    consulta falla se devuelve la respuesta original y el sondeo la rescata después
    (ver la condición de estado_guia / estado_factura_sii), con el guard de la 33
    bloqueando mientras tanto para que nunca se cite el N° tecleado a mano."""
    try:
        if not (int(data.get("status_id") or 0) == STATUS_EMITIDO
                and _vacio(data.get("folio"))):
            return data
        if data.get("uuid"):
            return _fusionar_respuesta(data, wasabil.obtener_documento(data["uuid"]) or {})
        if referencia_interna and referencia_interna.strip():
            doc = _rescatar_por_referencia(referencia_interna.strip(), compat_v1=compat_v1)
            if doc:
                return _fusionar_respuesta(data, doc)
    except wasabil.WasabilError as e:
        # El rescate AMBIGUO (dos documentos emitidos con la misma referencia) no puede
        # perderse en silencio: viaja como aviso hasta `dte.error`, que es lo que ve el
        # operador y el sondeo. Sigue siendo fail open para la EMISIÓN (el documento ya
        # existe ante el SII), y el guard de la 33 bloquea mientras el folio no esté.
        if e.ambiguo:
            return {**data, CLAVE_AVISO: str(e)}
    except (TypeError, ValueError):
        pass
    return data


def _referencia_interna_guia(db: Session, despacho_id: int) -> str:
    """Ancla anti doble emisión de la guía: el N° de despacho (formato v2). Cadena vacía
    si el despacho no tiene N° — nunca se busca en Wasabil con `search=""`."""
    despacho = db.query(Despacho).filter(Despacho.id == despacho_id).first()
    return (despacho.numero_despacho or "").strip() if despacho else ""


# ─── Cinturón anti doble emisión por REFERENCIA — los TRES veredictos ───────────
# EL BUG QUE ESTO CIERRA (reproducido por endpoints, con dos documentos REALES saliendo
# con issue=True): el cinturón tenía DOS veredictos —"consta que hay un emitido" y "todo
# lo demás"— y el "no pude preguntar" caía en el segundo (`except WasabilError: return`).
# Con el listado caído, TRUNCADO por paginación, o INEXISTENTE (el `GET /documents` del
# API REAL responde 405, ver client.py) el reintento re-emitía: salió una SEGUNDA guía 52
# y una SEGUNDA factura 33 por lo mismo.
#
# Un guard que protege un documento IRREVERSIBLE y que NO PUEDE CONCLUIR tiene que FALLAR
# CERRADO. "Best effort" vale para una comodidad; no vale para lo único que impide emitir
# dos veces al SII. Y "solo puede agregar bloqueos" NUNCA puede significar "puede quitarlos
# cuando no ve nada": un guard inerte es PEOR que ninguno, porque da confianza falsa.
VERD_SIN_EMITIDO = "sin_emitido"        # (a) CONSTA que Wasabil NO tiene ninguno emitido
VERD_HAY_EMITIDO = "hay_emitido"        # (b) CONSTA que sí — con folio(s) nombrados
VERD_INDETERMINADO = "indeterminado"    # (c) NO se puede concluir → bloquear


class VeredictoEmitido(NamedTuple):
    """Respuesta a "¿ya existe un documento tributario REAL con esta referencia?".

    `folios` solo viene en (b). `motivo` solo en (c), y viaja hasta el texto del 409:
    el operador tiene que leer QUÉ falló, no solo que falló."""
    veredicto: str
    folios: List[str]
    motivo: str


_MOTIVO_SIN_ANCLA = (
    "este documento no tiene referencia interna con la que buscar en Wasabil (el despacho "
    "no tiene N° interno, o la factura no está identificada): sin ancla no hay nada que "
    "verificar, y buscar con el texto vacío traería documentos de OTRAS ventas")


def _veredicto_documento_emitido(referencia_interna: str,
                                 compat_v1: bool = False) -> VeredictoEmitido:
    """¿Wasabil ya tiene un documento EMITIDO con esta referencia interna? SOLO LECTURA.

    Fuente ÚNICA de los tres veredictos, y el único lugar donde se decide qué cuenta como
    "no se puede concluir": WasabilError (incluido el 405 del API real), lista truncada por
    paginación (la convierte en error `_documentos_de_referencia`), cualquier respuesta con
    una forma que no se puede leer, y —esto es lo que faltaba— **todo documento que todavía
    podría quedarse con un folio**: uno `procesando`, uno en cola, o uno cuyo `status_id`
    este código no sabe interpretar. Ninguno de esos casos devuelve SIN_EMITIDO.

    SIN_EMITIDO exige la prueba positiva de que TODOS los documentos de esa referencia son
    rechazos CONFIRMADOS y LEGIBLES. Es el único veredicto que autoriza re-emitir, y emitir
    dos veces al SII no se deshace con un botón: se deshace con una nota de crédito."""
    ref = (referencia_interna or "").strip()
    if not ref:
        return VeredictoEmitido(VERD_INDETERMINADO, [], _MOTIVO_SIN_ANCLA)
    try:
        clasificacion = _clasificar_referencia(ref, compat_v1=compat_v1)
    except wasabil.WasabilError as e:
        return VeredictoEmitido(VERD_INDETERMINADO, [], str(e))
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        return VeredictoEmitido(
            VERD_INDETERMINADO, [],
            f"la respuesta de Wasabil no se pudo interpretar ({type(e).__name__}: {e})")
    if clasificacion.emitidos:
        return VeredictoEmitido(
            VERD_HAY_EMITIDO,
            [str(d.get("folio") or "sin folio") for d in clasificacion.emitidos], "")
    if clasificacion.pueden_tener_folio:
        n = len(clasificacion.pueden_tener_folio)
        estados = sorted({str(d.get("status_id")) for d in clasificacion.pueden_tener_folio})
        return VeredictoEmitido(
            VERD_INDETERMINADO, [],
            f"Wasabil tiene {n} documento(s) con esta referencia que NO son un rechazo "
            f"confirmado (estado: {', '.join(estados)}): todavía pueden quedarse con un "
            "folio REAL, así que re-emitir ahora arriesga dos documentos tributarios por lo "
            "mismo. Revisa en app.wasabil.com en qué terminaron antes de reintentar")
    return VeredictoEmitido(VERD_SIN_EMITIDO, [], "")


# Nombre del parámetro con el que el operador deja registrada su verificación humana
# (la salida del caso (c)). Va en el mensaje del 409: un remedio que no dice cómo
# ejecutarse no es un remedio.
_PARAM_VERIF = "verificado_sin_emitido"


def _msg_ya_hay_emitido(referencia: str, folios: List[str], sustantivo: str) -> str:
    return (f"Wasabil ya tiene {len(folios)} documento(s) EMITIDO(S) con la referencia "
            f"'{referencia}' (folio(s): {', '.join(folios)}): re-emitir la {sustantivo} "
            "crearía un SEGUNDO documento tributario REAL por lo mismo, y eso no se "
            "deshace. NO se re-emite. Revísalo en app.wasabil.com y pide soporte para "
            "dejar registrado el folio que corresponde.")


def _msg_no_se_pudo_concluir(referencia: str, motivo: str, sustantivo: str,
                             parametro: str) -> str:
    return (
        f"NO se pudo verificar en Wasabil si ya existe un documento EMITIDO con la "
        f"referencia '{referencia}', así que la {sustantivo} NO se re-emite: {motivo}. "
        f"QUÉ REVISAR: entra a app.wasabil.com y busca los documentos cuya referencia "
        f"interna sea EXACTAMENTE '{referencia}'. Si alguno está EMITIDO, NO reintentes: "
        f"pide soporte para dejar registrado ese folio. Si CONSTA que no hay ninguno "
        f"emitido, vuelve a pedir el reintento agregando {parametro}='{referencia}' — tu "
        f"verificación queda registrada en el documento y autoriza la re-emisión. "
        f"POR QUÉ SE BLOQUEA: esta verificación por referencia es la SEGUNDA línea de "
        f"defensa, no la primera. La primera es el candado LOCAL (la fila wasabil_dte es "
        f"única por despacho y por factura, y su claim se guarda ANTES de llamar al SII); "
        f"la segunda es la única que ve un documento que exista allá y no acá — y hoy "
        f"depende de un listado que el API real no expone. Cuando no puede ver, bloquea: "
        f"antes seguía adelante y salieron dos documentos tributarios reales.")


def _linea_auditoria_verificacion(referencia: str, motivo: str,
                                  usuario_id: Optional[int]) -> str:
    """Rastro que deja la salida humana del caso (c). Se escribe en el documento ANTES de
    lo irreversible: si aparece un duplicado, esta línea dice quién lo autorizó y con qué
    verificación."""
    return (f"VERIFICACIÓN HUMANA {datetime.utcnow():%Y-%m-%d %H:%M} UTC · usuario "
            f"{usuario_id if usuario_id is not None else 'no identificado'}: declaró que en "
            f"Wasabil NO existe ningún documento EMITIDO con la referencia '{referencia}' y "
            f"autorizó la re-emisión. El cinturón por referencia no pudo concluir solo "
            f"({motivo}). Si aparece un documento duplicado, ésta es la autorización que lo "
            f"permitió.")


def _abortar_si_puede_haber_documento_emitido(
        referencia_interna: str, sustantivo: str, compat_v1: bool = False,
        declaracion: Optional[str] = None,
        usuario_id: Optional[int] = None) -> Optional[str]:
    """Cinturón que se cruza JUSTO ANTES de re-emitir. FALLA CERRADO.

    Es la pregunta correcta (por REFERENCIA, no por uuid): `estado_documento(uuid)`
    contesta por ESE documento, y el uuid de la fila puede ser el del intento rechazado
    mientras el documento bueno vive con otro uuid y la MISMA referencia (después de un
    reintento, dos documentos con la misma referencia es el estado normal).

    LOS TRES CASOS, explícitos, porque el mapeo viejo de (c) sobre (a) era el peor posible:
      (a) VERD_SIN_EMITIDO   → consta que no hay ninguno emitido: se sigue.
      (b) VERD_HAY_EMITIDO   → consta que sí: 409 nombrando el/los folio(s). NO se repara
          la fila a propósito (estado local y remoto se contradicen: lo mira un humano) y
          NINGUNA declaración humana levanta este bloqueo.
      (c) VERD_INDETERMINADO → no se puede concluir: 409 pidiendo verificación humana.

    QUÉ DEFIENDE DE VERDAD, EN ORDEN (para que nadie vuelva a confiar en el orden
    equivocado):
      1ª línea, la que funciona hoy en producción: el ancla LOCAL — la fila `wasabil_dte`
        es ÚNICA por despacho (uq_wasabil_dte_despacho) y por factura
        (uq_wasabil_dte_factura), y su claim `en_vuelo_desde` se COMMITEA bajo lock ANTES
        de la llamada HTTP. No depende de ningún endpoint de Wasabil.
      2ª línea, ESTE cinturón: lo único capaz de ver un documento que exista en Wasabil y
        no en la fila local. Depende de `buscar_documentos` → `GET /documents`, que en el
        API REAL responde 405 (client.py). MIENTRAS ESE ENDPOINT NO EXISTA, este cinturón
        NO PUEDE CONCLUIR NUNCA y por eso BLOQUEA con la salida humana. Antes devolvía
        `return` y era teatro: no bloqueaba jamás en producción.

    SALIDA HUMANA del caso (c), auditada y explícita —nunca "reintentar a ciegas"—: el
    operador mira app.wasabil.com y, si CONSTA que no hay ningún documento emitido con esa
    referencia, repite la referencia EXACTA en `declaracion`. Escribirla es la prueba de
    que la miró (no sirve un `true` genérico). Devuelve entonces la línea de auditoría para
    que el llamador la persista con `_anotar_auditoria` ANTES de emitir; en (a) devuelve
    None. Confirmar el endpoint de listado real con Wasabil es lo que vuelve automática
    esta puerta."""
    ref = (referencia_interna or "").strip()
    veredicto = _veredicto_documento_emitido(ref, compat_v1=compat_v1)
    if veredicto.veredicto == VERD_HAY_EMITIDO:
        raise HTTPException(409, _msg_ya_hay_emitido(ref, veredicto.folios, sustantivo))
    if veredicto.veredicto == VERD_INDETERMINADO:
        if ref and (declaracion or "").strip() == ref:
            return _linea_auditoria_verificacion(ref, veredicto.motivo, usuario_id)
        raise HTTPException(409, _msg_no_se_pudo_concluir(
            ref or "(este documento no tiene referencia interna)",
            veredicto.motivo, sustantivo, _PARAM_VERIF))
    return None


def _anotar_auditoria(db: Session, dte: Optional[WasabilDte], linea: Optional[str],
                      tolerante: bool = False) -> None:
    """Persiste (y COMMITEA) la línea de auditoría de la verificación humana en
    `dte.error`, que es el campo que el operador ve en pantalla.

    Se llama DOS veces a propósito: ANTES de la llamada HTTP —para que quede escrita aunque
    el proceso muera con el documento ya creado— y DESPUÉS, porque
    `_actualizar_desde_wasabil` limpia `error` cuando el documento queda EMITIDO, que es
    justo el caso en que hay que saber quién autorizó. Idempotente: no duplica la línea.

    `tolerante` es para la SEGUNDA escritura: ahí el documento ya salió (o ya falló) y un
    problema de base al anotar no puede convertir esa respuesta en un 500 — la escritura
    que importa para la auditoría es la primera, que sí es estricta (si no se puede
    guardar, no se emite)."""
    if not linea or dte is None:
        return
    previo = (dte.error or "").strip()
    if previo and linea in previo:
        return
    dte.error = (linea if not previo else f"{linea} · {previo}")[:2000]
    if not tolerante:
        db.commit()
        return
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()


# Mensaje ÚNICO del rescate que no pudo resolverse: el documento EXISTE ante el SII
# (Wasabil lo dio por emitido) y su folio no llegó. Fail closed: se bloquea y se pide
# intervención humana — re-emitir sería un SEGUNDO documento tributario REAL, y Grupo AM
# es la marca que ya emite de verdad. `sustantivo` mantiene el texto en el idioma del
# documento (el operador de una FACTURA no debe leer un mensaje sobre despachos).
def _msg_rescate_sin_folio(dte: WasabilDte, sustantivo: str) -> str:
    return (f"El documento de {sustantivo} quedó EMITIDO en el SII pero su folio todavía "
            f"no llega desde Wasabil (uuid {dte.uuid or 'no registrado'}): NO se re-emite, "
            "porque sería un segundo documento tributario REAL. Consulta el estado en unos "
            "minutos; si el folio no aparece, búscalo en app.wasabil.com y pide soporte "
            "para registrarlo.")


# ─── MEDIO-6 · salida OPERABLE del callejón "EMITIDA sin folio" ────────────────
# El documento existe ante el SII y su folio no llegó. El sistema hacía lo correcto
# (bloquear: re-emitir sería un segundo documento REAL) pero mandaba a una acción que el
# producto NO OFRECÍA ("pide soporte para registrarlo"): los únicos escritores de
# `dte.folio` viven dentro de _actualizar_desde_wasabil, y el PUT del N° de guía lo
# rechaza el guard anti-pisado de despachos.py. El único remedio real era un UPDATE a mano
# en la base. Estos dos endpoints son esa salida: registran el folio que el humano LEE en
# app.wasabil.com, con confirmación explícita y rastro — y NUNCA emiten nada.
def _folio_confirmado_por_wasabil(dte: WasabilDte, referencia_interna: str,
                                  compat_v1: bool) -> Tuple[Optional[str], str, bool]:
    """(folio que Wasabil confirma, motivo por el que no se pudo obtener, contradice).

    SOLO LECTURA. La máquina manda sobre el humano CUANDO PUEDE CONCLUIR: primero por uuid
    (`GET /documents/{uuid}`, el endpoint que sí existe en el API real) y después por
    referencia. `contradice=True` significa que Wasabil dice que NO hay ningún documento
    emitido con esta referencia mientras acá figura EMITIDO: eso no lo resuelve un folio
    tecleado, lo resuelve una persona."""
    if dte.uuid:
        try:
            doc = wasabil.obtener_documento(dte.uuid) or {}
            if not _vacio(doc.get("folio")):
                return str(doc["folio"]).strip(), "", False
        except wasabil.WasabilError as e:
            return None, f"la consulta por uuid falló ({e})", False
        except (TypeError, ValueError, AttributeError) as e:
            return None, f"la respuesta por uuid no se pudo interpretar ({e})", False
    veredicto = _veredicto_documento_emitido(referencia_interna, compat_v1=compat_v1)
    if veredicto.veredicto == VERD_HAY_EMITIDO:
        folios = [f for f in veredicto.folios if f != "sin folio"]
        if len(folios) == 1:
            return folios[0], "", False
        return None, ("Wasabil tiene documentos emitidos con esta referencia pero no un "
                      f"folio único (folios: {', '.join(veredicto.folios)})"), True
    if veredicto.veredicto == VERD_SIN_EMITIDO:
        return None, (f"Wasabil NO tiene ningún documento EMITIDO con la referencia "
                      f"'{referencia_interna}', pero aquí el documento figura EMITIDO: los "
                      "dos estados se contradicen"), True
    return None, veredicto.motivo, False


def _registrar_folio_a_mano(db: Session, dte: WasabilDte, folio: str, confirmo_folio: str,
                            sustantivo: str, referencia_interna: str, compat_v1: bool,
                            usuario_id: Optional[int]) -> dict:
    """Registra a mano el folio de un documento que YA está EMITIDO y llegó sin folio.

    Reglas, todas fail-closed y ninguna capaz de emitir:
      1. Sólo sobre el callejón exacto: status 3 y folio vacío. Cualquier otro estado 409
         (nunca se pisa un folio ya registrado; folio idéntico = idempotente, 200).
      2. El operador repite el folio en `confirmo_folio` y el folio tiene que ser un
         correlativo del SII (numérico ASCII, la misma regla del FolioRef de la 52).
      3. La MÁQUINA manda cuando puede concluir: si Wasabil devuelve un folio para este
         documento y NO es el tecleado, 409 nombrando los dos. Si Wasabil dice que no hay
         ningún emitido con esta referencia (contradicción), 409: eso lo mira una persona.
      4. Se escribe por el MISMO camino que la emisión (_actualizar_desde_wasabil), así el
         folio llega a `despacho.numero_guia` / `numero_factura` igual que siempre, y el
         rastro (quién, cuándo, con qué motivo) queda en `respuesta_json`."""
    if dte.status_id != STATUS_EMITIDO:
        raise HTTPException(
            409, f"Este documento no está EMITIDO (estado {dte.status_id}): registrar un "
                 "folio a mano sólo corresponde cuando el SII YA lo aceptó y el folio no "
                 "llegó. Consulta el estado o usa Reintentar.")
    folio = (folio or "").strip()
    if not _vacio(dte.folio):
        if str(dte.folio).strip() == folio:
            return {**serialize_dte(dte), "registro_manual": "ya estaba registrado"}
        raise HTTPException(
            409, f"Esta {sustantivo} ya tiene registrado el folio {dte.folio}: no se pisa "
                 f"con {folio or '(vacío)'}. Son dos folios reales distintos — revísalo en "
                 "app.wasabil.com y pide soporte.")
    if (confirmo_folio or "").strip() != folio:
        raise HTTPException(
            400, "Repite EXACTAMENTE el mismo folio en `confirmo_folio`: es la confirmación "
                 "de que lo leíste del documento en app.wasabil.com.")
    if not _folio_dte_valido(folio):
        raise HTTPException(
            400, f"'{folio or '(vacío)'}' no sirve como folio de un documento tributario: "
                 "el folio del SII es un correlativo numérico.")
    folio_maquina, motivo, contradice = _folio_confirmado_por_wasabil(
        dte, referencia_interna, compat_v1)
    if contradice:
        raise HTTPException(
            409, f"No se registra el folio {folio}: {motivo}. Resuélvelo en "
                 "app.wasabil.com antes de tocar nada acá — registrar un folio sobre un "
                 "estado contradictorio puede tapar un documento duplicado.")
    if folio_maquina and folio_maquina != folio:
        raise HTTPException(
            409, f"Wasabil dice que el folio de este documento es {folio_maquina} y tú "
                 f"escribiste {folio}: no se elige por cuenta propia. Verifica cuál "
                 "corresponde en app.wasabil.com.")
    # Re-lectura BAJO LOCK con datos frescos: entre la consulta a Wasabil (con red, sin
    # locks — regla de la casa) y la escritura, el sondeo o el registro de otra pestaña
    # pueden haber puesto el folio. La transacción es CORTA y no tiene red adentro.
    db.rollback()
    fresca = (db.query(WasabilDte).filter(WasabilDte.id == dte.id)
              .populate_existing().with_for_update().first())
    if fresca is None or fresca.status_id != STATUS_EMITIDO or not _vacio(fresca.folio):
        db.rollback()
        raise HTTPException(
            409, "El estado del documento cambió mientras se verificaba en Wasabil (el "
                 "sondeo o otro usuario ya registró el folio): refresca la página y "
                 "revísalo antes de volver a intentarlo.")
    dte = fresca
    origen = ("confirmado por Wasabil" if folio_maquina
              else f"declarado por el operador (Wasabil no pudo confirmarlo: {motivo})")
    data = {
        "status_id": STATUS_EMITIDO, "folio": folio, "uuid": dte.uuid,
        "_registro_manual_de_folio": {
            "folio": folio, "usuario_id": usuario_id,
            "fecha_utc": datetime.utcnow().isoformat(timespec="seconds"),
            "referencia_interna": referencia_interna, "origen": origen,
        },
    }
    _actualizar_desde_wasabil(db, dte, data)
    db.commit()
    db.refresh(dte)
    return {**serialize_dte(dte), "registro_manual": origen}


def _msg_degradacion(status_remoto: int) -> str:
    return (f"Wasabil respondió status {status_remoto} para un documento que en este "
            "sistema YA estaba EMITIDO (status 3) ante el SII: NO se degrada el estado ni "
            "se pisa el folio (hacerlo perdería el folio de un documento tributario REAL "
            "y habilitaría una segunda emisión). Verifica el documento en app.wasabil.com "
            "antes de tocar nada.")


def _msg_folio_distinto(folio_local: str, folio_remoto: str) -> str:
    return (f"Wasabil respondió el folio {folio_remoto} para un documento que aquí ya está "
            f"registrado con el folio {folio_local}: se CONSERVA el folio local y no se "
            "elige por cuenta propia. Son dos folios reales distintos — revísalo en "
            "app.wasabil.com y pide soporte.")


def _actualizar_desde_wasabil(db: Session, dte: WasabilDte, data: dict) -> None:
    """Vuelca la respuesta de Wasabil en la fila (uuid/estado/folio/PDF).

    El folio SOLO se registra cuando el documento queda EMITIDO (status 3) — y en
    ese momento se copia a despacho.numero_guia (única escritura de este módulo
    sobre una tabla existente). Con uuid conocido, el claim en_vuelo se libera:
    el uuid pasa a ser el candado.

    PISO MONÓTONO DEL ESTADO (la regla que faltaba): un DTE que YA está EMITIDO ante el
    SII NO se degrada nunca. Sin esto, una respuesta que contradice la emisión —el rescate
    por referencia trayendo el documento RECHAZADO hermano, o un `estado_documento` que
    responde 4— dejaba la fila en status 4 sin folio: el documento REAL perdía su folio
    para siempre, el guard de la 33 dejaba de bloquear (para él ya no había guía
    electrónica) y la factura salía citando el N° de guía TECLEADO a mano. Encima el
    reintento veía "fallido" y re-emitía: DOBLE documento tributario REAL.

    Ante la contradicción no se fusiona NADA de esa respuesta (ni uuid, ni status, ni
    folio): se anota el problema en `dte.error` y lo resuelve un humano. Mismo criterio
    para un folio distinto del ya registrado. Enriquecer sí está permitido (folio/PDF/XML
    que faltaban, o subir de fallido a emitido: eso no pierde nada)."""
    ya_emitido = dte.status_id == STATUS_EMITIDO
    status_remoto, status_ilegible = None, None
    if data.get("status_id") is not None:
        try:
            status_remoto = int(data["status_id"])
        except (TypeError, ValueError):
            # Un status ilegible no cambia el estado (y antes reventaba en 500 a mitad
            # del volcado): se conserva lo que había y se anota. Cualquier estado que no
            # sea 3 bloquea la referencia 52, así que conservar es el lado seguro.
            status_ilegible = str(data.get("status_id"))[:100]
    degrada = ya_emitido and status_remoto is not None and status_remoto != STATUS_EMITIDO
    # `nota` se escribe al FINAL: tiene que sobrevivir al `error = None` del emitido,
    # porque es justo el caso en que un humano tiene que mirar.
    nota = str(data.get(CLAVE_AVISO)) if data.get(CLAVE_AVISO) else None

    dte.respuesta_json = json.dumps(data, ensure_ascii=False, default=str)[:60000]
    if degrada:
        # Respuesta CONTRADICTORIA: no se toca la fila (el documento existe ante el SII).
        dte.error = (nota or _msg_degradacion(status_remoto))[:2000]
        return

    if data.get("uuid"):
        dte.uuid = data["uuid"]
        dte.en_vuelo_desde = None
    if status_remoto is not None:
        dte.status_id = status_remoto
    if data.get("display_error") or data.get("error"):
        dte.error = str(data.get("display_error") or data.get("error"))[:2000]

    if dte.status_id == STATUS_EMITIDO:
        folio_remoto = str(data["folio"]) if data.get("folio") else None
        folio_distinto = bool(folio_remoto) and not _vacio(dte.folio) \
            and str(dte.folio).strip() != folio_remoto.strip()
        if folio_remoto and not folio_distinto:
            dte.folio = folio_remoto
        if data.get("document_pdf_url"):
            dte.pdf_url = data["document_pdf_url"]
        if data.get("document_xml_url"):
            dte.xml_url = data["document_xml_url"]
        dte.error = None
        dte.en_vuelo_desde = None
        if folio_distinto and not nota:
            nota = _msg_folio_distinto(str(dte.folio), folio_remoto)
        if dte.folio and dte.despacho_id:
            despacho = db.query(Despacho).filter(Despacho.id == dte.despacho_id).first()
            if despacho:
                despacho.numero_guia = dte.folio
    if status_ilegible and not nota:
        nota = (f"Wasabil respondió un status ilegible ('{status_ilegible}'): no se cambió "
                "el estado de este documento. Consúltalo en app.wasabil.com")
    if nota:
        dte.error = nota[:2000]


def _emitir_en_wasabil(db: Session, ctx: dict, dte: WasabilDte,
                       tipo_traslado: int = TIPO_TRASLADO_DEFAULT) -> WasabilDte:
    """Llama a Wasabil con issue=true. El claim YA está commiteado y no hay locks:
    si la red muere después de crear el documento, el claim sigue bloqueando a
    otros mientras esté fresco, y el reintento verificará en Wasabil antes de
    re-crear (por uuid o por la referencia interna del despacho)."""
    doc = armar_guia(
        numero_oc=ctx["oc"].numero_oc.strip(),
        fecha_oc=ctx["fecha_oc"],
        numero_despacho=ctx["despacho"].numero_despacho,
        lineas=ctx["lineas"],
        client_id=ctx["client_id"],
        contacto=ctx["despacho"].contacto_destinatario,
        issue=True,
        tipo_traslado=tipo_traslado,
    )
    dte.payload_json = json.dumps(doc, ensure_ascii=False)[:60000]
    dte.monto_neto, dte.iva, dte.monto_total = ctx["neto"], ctx["iva"], ctx["total"]
    db.commit()

    try:
        data = wasabil.crear_documento(payload_a_rest(doc))
    except wasabil.WasabilError as e:
        dte.error = (str(e) + (f" · {e.detalle[:500]}" if e.detalle else ""))[:2000]
        if not e.ambiguo:
            # Seguro que NO se creó documento (conexión rechazada / 4xx):
            # liberar el claim para que el reintento quede disponible de inmediato…
            dte.en_vuelo_desde = None
            # …y dejar el ESTADO diciendo la verdad. `_reclamar_emision` dejó
            # status PENDIENTE, que en el vocabulario del módulo significa "hay un
            # BORRADOR en Wasabil"; con la creación confirmadamente fallida no hay
            # nada allá, y otras capas leen ese estado para decidir si un documento
            # PUDO existir (p.ej. el guard de borrado de Contabilidad, que bloquea
            # PENDIENTE/PROCESANDO). Dejarlo en PENDIENTE convertía un fallo
            # confirmado en un callejón: ni se puede completar ni se puede borrar.
            dte.status_id = STATUS_FALLIDO
        # Ambiguo (timeout/5xx): el claim queda puesto y expira solo — mientras
        # tanto nadie puede duplicar, y el reintento posterior verifica en Wasabil
        db.commit()
        raise HTTPException(502, f"No se pudo emitir en Wasabil: {e}")

    # Si la respuesta viene EMITIDA pero sin folio, se trae el documento completo ANTES
    # de dar la emisión por cerrada (si no, el despacho se quedaría sin N° de guía y la
    # factura 33 referenciaría el N° tecleado a mano). Sin uuid el rescate va por el N°
    # de despacho, que es el invoiceReference de la guía.
    data = _completar_documento_emitido(
        data, referencia_interna=ctx["despacho"].numero_despacho, compat_v1=True)
    _actualizar_desde_wasabil(db, dte, data)
    db.commit()
    db.refresh(dte)
    return dte


# ─── Endpoints ──────────────────────────────────────────────────────────────────
def _validar_tipo_traslado(tipo_traslado: int) -> int:
    """Rechaza (400) un dispatchTypeCode fuera de la tabla del SII antes de emitir."""
    if tipo_traslado not in TIPOS_TRASLADO:
        raise HTTPException(400, f"Tipo de traslado inválido: {tipo_traslado}")
    return tipo_traslado


# Lista {codigo, label} para el selector del frontend (orden estable por código)
TIPOS_TRASLADO_OPCIONES = [
    {"codigo": codigo, "label": label} for codigo, label in sorted(TIPOS_TRASLADO.items())
]


@router.get("/config")
def estado_configuracion(current_user: User = Depends(get_current_user)):
    """Si Wasabil está configurado (token presente). El frontend lo usa para avisar
    ANTES de que el usuario arme el despacho (no expone el token)."""
    return {"configurado": wasabil.esta_configurado()}


@router.post("/despachos/{despacho_id}/preview")
def preview_guia(
    despacho_id: int,
    tipo_traslado: int = Query(TIPO_TRASLADO_DEFAULT,
                               description="dispatchTypeCode del SII (ver TIPOS_TRASLADO)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Previsualización de la guía 52: documento armado + validaciones. NO toca el SII.
    `puede_emitir` es True solo si no hay ningún problema bloqueante."""
    _validar_tipo_traslado(tipo_traslado)
    ctx = _preparar_emision(db, despacho_id)
    doc_preview = None
    # Las MISMAS condiciones que el cinturón de armar_guia (fecha de OC, N° de OC
    # dentro del límite del SII y N° de despacho como ancla): si faltara alguna, el
    # armado revienta con ValueError y el preview moriría en 500 en vez de mostrar el
    # problema bloqueante que ya viene en ctx["problemas"].
    if (ctx["fecha_oc"] and ctx["lineas"] and (ctx["oc"].numero_oc or "").strip()
            and len((ctx["oc"].numero_oc or "").strip()) <= FOLIO_REF_MAX
            and (ctx["despacho"].numero_despacho or "").strip()):
        doc_preview = armar_guia(
            numero_oc=ctx["oc"].numero_oc.strip(),
            fecha_oc=ctx["fecha_oc"],
            numero_despacho=ctx["despacho"].numero_despacho,
            lineas=ctx["lineas"],
            client_id=ctx["client_id"],
            contacto=ctx["despacho"].contacto_destinatario,
            issue=False,  # el preview JAMÁS emite
            tipo_traslado=tipo_traslado,
        )
    return {
        "puede_emitir": not ctx["problemas"],
        "problemas": ctx["problemas"],
        "advertencias": ctx["advertencias"],
        "receptor": ctx["receptor"],
        "lineas": ctx["lineas"],
        "totales": {"neto": ctx["neto"], "iva": ctx["iva"], "total": ctx["total"]},
        "referencias": ([{"tipo": "801", "folio": ctx["oc"].numero_oc,
                          "fecha": ctx["fecha_oc"].isoformat() if ctx["fecha_oc"] else None,
                          "descripcion": "Orden de compra del cliente"}]),
        "tipo_traslado": tipo_traslado,
        "tipos_traslado": TIPOS_TRASLADO_OPCIONES,
        "documento": doc_preview,
        "dte": serialize_dte(ctx["dte"]) if ctx["dte"] else None,
    }


@router.post("/despachos/{despacho_id}/emitir")
def emitir_guia(
    despacho_id: int,
    tipo_traslado: int = Query(TIPO_TRASLADO_DEFAULT,
                               description="dispatchTypeCode del SII (ver TIPOS_TRASLADO)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE la guía al SII vía Wasabil (IRREVERSIBLE). Requiere que el usuario haya
    visto la previsualización: el frontend solo habilita este botón tras el preview."""
    _validar_tipo_traslado(tipo_traslado)
    ctx = _preparar_emision(db, despacho_id)
    if ctx["problemas"]:
        raise HTTPException(409, " · ".join(ctx["problemas"]))

    dte = _reclamar_emision(
        db, despacho_id, para_reintento=False,
        usuario_id=getattr(current_user, "id", None),
        empresa=getattr(current_user, "empresa", None) or "mineria",
    )
    dte = _emitir_en_wasabil(db, ctx, dte, tipo_traslado=tipo_traslado)
    return serialize_dte(dte)


@router.get("/despachos/{despacho_id}/estado")
def estado_guia(
    despacho_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Estado del DTE del despacho. Si sigue en proceso, consulta a Wasabil y
    actualiza (sondeo del frontend cada pocos segundos hasta Emitido/Fallido)."""
    dte = _dte_de_despacho(db, despacho_id)
    if not dte:
        raise HTTPException(404, "Este despacho no tiene guía electrónica")
    # 'Emitido SIN folio' TAMBIÉN se re-consulta: con la condición frenando en cuanto el
    # status era 3, una emisión que volvió sin folio quedaba en callejón sin salida (el
    # despacho nunca recibía su N° de guía y /reintentar responde 409 'ya está emitida').
    # Con esto el sondeo se autocura solo.
    emitida_sin_folio = dte.status_id == STATUS_EMITIDO and _vacio(dte.folio)
    if dte.uuid and (dte.status_id not in (STATUS_EMITIDO, STATUS_FALLIDO)
                     or emitida_sin_folio):
        try:
            data = wasabil.estado_documento(dte.uuid)
            if int(data.get("status_id") or 0) == STATUS_EMITIDO:
                # El status trae lo esencial; el documento completo trae folio + PDF/XML
                data = wasabil.obtener_documento(dte.uuid)
            _actualizar_desde_wasabil(db, dte, data)
            db.commit()
            db.refresh(dte)
        except wasabil.WasabilError as e:
            # No romper el sondeo por un error transitorio: se informa y se reintenta
            return {**serialize_dte(dte), "error_consulta": str(e)}
    elif emitida_sin_folio:
        # Emitida sin folio y SIN uuid: no hay documento que consultar por id, así que el
        # sondeo se autocura por la REFERENCIA INTERNA (N° de despacho). Solo lectura: si
        # Wasabil no lo devuelve la fila queda igual y el guard de la 33 sigue bloqueando
        # — jamás se cae al N° de guía tecleado a mano.
        despacho = db.query(Despacho).filter(Despacho.id == despacho_id).first()
        referencia = (despacho.numero_despacho or "").strip() if despacho else ""
        if referencia:
            try:
                doc = _rescatar_por_referencia(referencia, compat_v1=True)
            except wasabil.WasabilError as e:
                return {**serialize_dte(dte), "error_consulta": str(e)}
            if doc:
                _actualizar_desde_wasabil(db, dte, doc)
                db.commit()
                db.refresh(dte)
    return serialize_dte(dte)


@router.post("/despachos/{despacho_id}/reintentar")
def reintentar_guia(
    despacho_id: int,
    tipo_traslado: int = Query(TIPO_TRASLADO_DEFAULT,
                               description="dispatchTypeCode del SII (ver TIPOS_TRASLADO)"),
    verificado_sin_emitido: Optional[str] = Query(
        None, description="Salida humana AUDITADA del cinturón por referencia cuando "
                          "Wasabil no permite concluir si ya existe un documento emitido: "
                          "el N° de despacho EXACTO, tecleado por el operador después de "
                          "revisarlo en app.wasabil.com (ver "
                          "_abortar_si_puede_haber_documento_emitido)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reintento SEGURO de una emisión fallida (o que no llegó a Wasabil).

    Anti doble emisión: (1) si hay uuid, consulta el estado real; (2) si no hay
    uuid, busca en Wasabil por la referencia interna (OC + N° despacho) por si el
    documento SÍ se creó y la respuesta se perdió. Si la verificación FALLA se
    ABORTA (nunca se re-crea a ciegas). Solo si el documento está confirmado
    fallido/inexistente se re-emite — reclamando el claim bajo lock.

    Caso 'EMITIDA SIN folio' (el callejón): el documento EXISTE ante el SII, así que acá
    el reintento NO es una re-emisión sino un RESCATE de solo lectura del folio (por uuid
    o por referencia interna). Antes respondía 409 'ya está emitida (folio None)' y no
    quedaba ninguna salida: ni sondeo, ni reintento, ni corrección manual del N°.
    """
    _validar_tipo_traslado(tipo_traslado)
    dte = _dte_de_despacho(db, despacho_id)
    if not dte:
        raise HTTPException(404, "Este despacho no tiene emisión que reintentar")
    rescate_de_folio = dte.status_id == STATUS_EMITIDO and _vacio(dte.folio)
    if dte.status_id == STATUS_EMITIDO and not rescate_de_folio:
        raise HTTPException(409, f"La guía ya está emitida (folio {dte.folio})")
    if claim_vigente(dte):
        raise HTTPException(409, "Hay una emisión EN CURSO para este despacho: "
                                 "espera unos minutos y consulta el estado")

    # 1) ¿El documento existe en Wasabil aunque acá no tengamos respuesta?
    if dte.uuid:
        try:
            data = wasabil.estado_documento(dte.uuid)
            if int(data.get("status_id") or 0) == STATUS_EMITIDO:
                data = wasabil.obtener_documento(dte.uuid)
            _actualizar_desde_wasabil(db, dte, data)
            db.commit()
            db.refresh(dte)
        except wasabil.WasabilError:
            raise HTTPException(502, "No se pudo verificar el estado real del documento en "
                                     "Wasabil; reintenta en unos minutos (no se re-crea a ciegas)")
        if dte.status_id in (STATUS_EMITIDO, STATUS_PROCESANDO, STATUS_PENDIENTE):
            # Emitido/en proceso/borrador: NO corresponde re-crear
            if rescate_de_folio and _vacio(dte.folio):
                raise HTTPException(409, _msg_rescate_sin_folio(dte, "este despacho"))
            return serialize_dte(dte)
    else:
        despacho_prev, _oc_prev, _cot_prev = _cargar_contexto(db, despacho_id)
        ref = (despacho_prev.numero_despacho or "").strip()
        try:
            # Fuente ÚNICA de "qué tiene Wasabil con esta referencia": prefiere SIEMPRE el
            # documento EMITIDO (el único con folio que perder) y ABORTA si hay más de uno.
            # El bucle que había acá se quedaba con el PRIMERO que coincidiera — después de
            # un reintento eso es el intento RECHAZADO, y con él la fila terminaba en
            # 'fallido' aunque existiera una guía real: el siguiente reintento re-emitía.
            # Sin ancla (N° de despacho vacío) no se busca: `search=""` traería documentos
            # de otros despachos. Ese caso lo bloquea _preparar_emision más abajo.
            doc = _rescatar_por_referencia(ref, compat_v1=True) if ref else None
        except wasabil.WasabilError as e:
            raise HTTPException(502, "No se pudo verificar en Wasabil si el documento ya existe; "
                                     f"reintenta en unos minutos (no se re-crea a ciegas). {e}")
        if doc:
            _actualizar_desde_wasabil(db, dte, doc)
            db.commit()
            db.refresh(dte)
            if rescate_de_folio and _vacio(dte.folio):
                raise HTTPException(409, _msg_rescate_sin_folio(dte, "este despacho"))
            return serialize_dte(dte)

    # 1-bis) 'EMITIDA sin folio' que el rescate no pudo resolver: el documento YA existe
    # ante el SII (status 3), así que este camino JAMÁS puede seguir a re-emitir — sería
    # una segunda guía 52 REAL por la misma mercadería. Fail closed con el remedio humano.
    if rescate_de_folio:
        raise HTTPException(409, _msg_rescate_sin_folio(dte, "este despacho"))

    # 1-ter) CINTURÓN ANTI DOBLE EMISIÓN por REFERENCIA, justo antes de lo irreversible.
    # El uuid que tenemos puede ser el del intento RECHAZADO mientras Wasabil conserva OTRO
    # documento EMITIDO con la MISMA referencia (el estado normal tras un reintento son dos
    # documentos con la misma referencia). `estado_documento(uuid)` responde por ESE
    # documento, no por la referencia, así que confirmaba "fallido" y se re-emitía: dos
    # guías 52 REALES por la misma mercadería. FALLA CERRADO: si no puede concluir, 409 con
    # la verificación humana como única salida (ver el docstring del cinturón).
    auditoria = _abortar_si_puede_haber_documento_emitido(
        _referencia_interna_guia(db, despacho_id), "guía", compat_v1=True,
        declaracion=verificado_sin_emitido,
        usuario_id=getattr(current_user, "id", None))

    # 2) Documento confirmado fallido (o inexistente): revalidar y re-emitir.
    ctx = _preparar_emision(db, despacho_id, para_reintento=True)
    if ctx["problemas"]:
        raise HTTPException(409, " · ".join(ctx["problemas"]))
    dte = _reclamar_emision(
        db, despacho_id, para_reintento=True,
        usuario_id=getattr(current_user, "id", None),
        empresa=getattr(current_user, "empresa", None) or "mineria",
    )
    # La autorización humana se persiste ANTES del HTTP (el claim ya limpió `error`) y otra
    # vez después, porque la emisión exitosa vuelve a limpiarlo.
    _anotar_auditoria(db, dte, auditoria)
    try:
        dte = _emitir_en_wasabil(db, ctx, dte, tipo_traslado=tipo_traslado)
    finally:
        _anotar_auditoria(db, dte, auditoria, tolerante=True)
    return serialize_dte(dte)


@router.post("/despachos/{despacho_id}/registrar-folio")
def registrar_folio_guia(
    despacho_id: int,
    folio: str = Query(..., description="Folio REAL del SII, leído en app.wasabil.com"),
    confirmo_folio: str = Query(..., description="Repite el folio (confirmación explícita)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SALIDA del callejón "guía EMITIDA sin folio" (MEDIO-6). NO emite nada.

    El documento ya existe ante el SII y su folio no llegó: el reintento responde 409 (bien:
    re-emitir sería una segunda guía 52 REAL) y el N° manual no se puede editar (guard
    anti-pisado de despachos.py). Sin esto la única salida era un UPDATE a mano en la base y
    el despacho se quedaba sin N° de guía para siempre. Reglas y rastro: ver
    _registrar_folio_a_mano."""
    dte = _dte_de_despacho(db, despacho_id)
    if not dte:
        raise HTTPException(404, "Este despacho no tiene emisión electrónica")
    if claim_vigente(dte):
        raise HTTPException(409, "Hay una emisión EN CURSO para este despacho: espera su "
                                 "resultado antes de registrar un folio")
    return _registrar_folio_a_mano(
        db, dte, folio, confirmo_folio, "guía",
        _referencia_interna_guia(db, despacho_id), True,
        getattr(current_user, "id", None))


@router.get("/despachos/estado-batch")
def estado_batch(
    ids: str = Query(..., description="IDs de despachos separados por coma"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Estado (solo BD, sin llamar a Wasabil) de los DTE de varios despachos —
    para pintar los badges de folio/PDF en las tarjetas sin N llamadas."""
    try:
        despacho_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids debe ser una lista de enteros separados por coma")
    if not despacho_ids:
        return {}
    if len(despacho_ids) > 200:
        raise HTTPException(400, "Máximo 200 despachos por consulta")
    dtes = (
        db.query(WasabilDte)
        .filter(WasabilDte.despacho_id.in_(despacho_ids),
                WasabilDte.tipo_dte == TIPO_DOC_GUIA)
        .all()
    )
    return {d.despacho_id: serialize_dte(d) for d in dtes}


# ═══════════════════════════════════════════════════════════════════════════════
# FASE B — FACTURAS ELECTRÓNICAS (DTE 33)
#
# Mismo protocolo que las guías (preview → emitir con OK explícito → sondeo →
# reintento seguro) y misma ancla anti doble emisión (fila wasabil_dte, ahora
# única por factura_id). La diferencia estructural: la factura LOCAL se crea
# PRIMERO (sin folio — numero_factura queda NULL) reutilizando la persistencia
# de Contabilidad (_persistir_factura, la MISMA de crear_factura), y el folio
# del SII se escribe al confirmarse la emisión. La aplicación de adelantos como
# cobranza se DIFIERE hasta ese momento: una factura rechazada no movió plata.
# ═══════════════════════════════════════════════════════════════════════════════
from routers.contabilidad import (  # noqa: E402  (mismo patrón del import de arriba)
    FacturaCreate, _construir_factura, _construir_factura_anticipo,
    _persistir_factura, _aplicar_adelantos_pendientes,
)
from models.models import ContFacturaCliente, ContAdelanto  # noqa: E402
from .service import (  # noqa: E402
    TIPO_DOC_FACTURA, NETO_MINIMO_DTE, armar_factura, armar_lineas_factura,
    armar_referencias_factura, hoy_chile,
)
# _parse_date: MISMO parseo tolerante que usa Contabilidad para `fecha_emision`, para que
# el guard mida contra la fecha que realmente se va a persistir en la factura.
from routers.contabilidad import _parse_date  # noqa: E402

# Sustantivos para los mensajes de _estado_dte_bloquea en el camino de facturas (la
# misma máquina de estados, pero el usuario que reintenta una FACTURA no debe leer un
# mensaje que habla de despachos).
_SUST_FACTURA = "esta factura"
_DOC_FACTURA = "factura electrónica emitida"


def _dte_de_factura(db: Session, factura_id: int, lock: bool = False) -> Optional[WasabilDte]:
    """Fila DTE de la factura. Se filtra por tipo_dte 33: el día que una factura tenga
    además una nota de crédito electrónica (61), esta consulta seguiría devolviendo la
    factura correcta. Falla CERRADO: si por alguna razón hubiera otra fila con ese
    factura_id, el INSERT del claim choca con el UNIQUE y responde 409 en vez de
    duplicar."""
    q = db.query(WasabilDte).filter(WasabilDte.factura_id == factura_id,
                                    WasabilDte.tipo_dte == TIPO_DOC_FACTURA)
    if lock:
        q = q.populate_existing().with_for_update()
    return q.first()


def _referencia_interna_factura(factura_id: int) -> str:
    """Ancla anti doble emisión de la factura (formato v2: Wasabil la imprime,
    así que NO lleva la OC — única por factura local)."""
    return f"FACT-{factura_id}"


# Textos de los tres motivos por los que el N° de guía del despacho NO sirve como
# referencia 52. Todos llevan "EN PROCESO" a propósito: es el vocabulario con el que el
# operador ya conoce este bloqueo (y el que usan preview/emisión/reintento).
_MSG_GUIA_EN_PROCESO = (
    "La guía electrónica de este despacho está EN PROCESO en el SII: espera a "
    "que quede emitida (con su folio) antes de facturarla — si no, la factura "
    "referenciaría un N° de guía que el SII no reconoce")
_MSG_GUIA_SIN_FOLIO = (
    "La guía electrónica de este despacho quedó EMITIDA en el SII pero su folio todavía "
    "no llegó: cuenta como EN PROCESO hasta que el sondeo lo rescate. Facturar ahora "
    "referenciaría el N° de guía tecleado a mano, que el SII no reconoce")
_MSG_GUIA_AMBIGUA = (
    "No se pudo CONFIRMAR si la guía electrónica de este despacho llegó al SII (la "
    "comunicación se cortó sin respuesta): el documento PUDO nacer con folio real, así "
    "que cuenta como EN PROCESO. Resuélvela primero (consulta el estado o Reintentar); "
    "facturar ahora referenciaría un N° de guía que el SII podría no reconocer")


def _guia_no_referenciable(db: Session, despacho_id: int) -> Optional[str]:
    """Motivo por el que el N° de guía de este despacho NO se puede usar como referencia
    52 de una factura, o None si sí se puede. Fuente ÚNICA del bloqueo (preview, emisión
    y reintento usan este mismo texto).

    En la ventana en que el folio del SII no ha llegado, `despacho.numero_guia` conserva
    el N° manual viejo — el módulo lo pisa recién al confirmarse la emisión —, así que
    caer al fallback manual haría que la factura saliera al SII referenciando un folio
    que el SII NO conoce. Y eso es irreversible. Mejor bloquear y esperar el folio.

    Los tres estados que bloquean:
      · EN PROCESO / BORRADOR: uuid conocido, o claim de emisión vigente.
      · EMITIDA pero SIN folio: Wasabil puede responder al POST con status 3 y sin folio
        (con o sin uuid). Con el criterio viejo (status == 3 → False) la factura 33 caía
        al fallback manual y salía al SII citando el N° TECLEADO a mano. Se decide SIN
        mirar el uuid: ese estado nunca es uno en que el N° manual sea legítimo. Queda
        alineado con _rechazar_si_pisa_folio (routers/despachos.py), que ya exigía folio
        no vacío: antes se contradecían.
      · AMBIGUA: uuid NULL pero `en_vuelo_desde` puesto y el claim ya VENCIDO por TTL —
        el POST salió y nadie confirmó el resultado (timeout/5xx: _emitir_en_wasabil
        conserva el timestamp a propósito). Es el MISMO `incluir_ambiguo` con el que
        routers/despachos.py protege el anular, y por este agujero ya se emitió una
        segunda guía real en el pasado. Decidir por TTL dejaba la 33 saliendo con el N°
        tecleado a mano.

    ORDEN DE LOS CHEQUEOS = el de routers/despachos.py:_guia_electronica_activa
    (incluir_ambiguo=True), y ahora de verdad. El orden anterior era
    EMITIDO → uuid/claim → FALLIDO → en_vuelo, y por eso:
      · `status 4 · uuid NULL · en_vuelo puesto` (lo que deja un timeout, y también un
        rescate que eligió el documento equivocado) salía por el `return None` del FALLIDO
        ANTES de llegar al criterio AMBIGUO: despachos.py la trataba como guía VIVA (no
        deja anular) y este guard la daba por inofensiva (dejaba facturar citando el N°
        tecleado a mano). El docstring afirmaba que estaban alineados y no lo estaban.
      · un rechazo CONFIRMADO del SII (status 4 CON uuid — la forma normal de un rechazo)
        entraba por `dte.uuid or …` y bloqueaba la 33 PARA SIEMPRE con un mensaje falso
        ("EN PROCESO"): con la guía en papel, ese despacho no se podía facturar nunca.

    Invariante que sostiene la alineación (probado estado por estado contra la función
    real de despachos.py en wasabil_dte/tests/test_ra_sii_bloquear.py): si despachos.py
    considera la guía VIVA y NO hay folio del SII disponible, este guard BLOQUEA. La
    dirección contraria no es simétrica a propósito: la guía EMITIDA CON folio es "viva"
    para despachos.py (no se pisa a mano) y es exactamente la que sí se puede referenciar.
    Donde este guard es MÁS estricto que despachos.py (uuid con status desconocido o NULL)
    se conserva, porque _estado_dte_bloquea ya trata ese estado como bloqueante: la
    cobertura es un superconjunto, nunca un subconjunto."""
    dte = (db.query(WasabilDte)
           .filter(WasabilDte.despacho_id == despacho_id,
                   WasabilDte.tipo_dte == TIPO_DOC_GUIA)
           .first())
    if dte is None:
        return None
    if dte.status_id == STATUS_EMITIDO:
        return None if not _vacio(dte.folio) else _MSG_GUIA_SIN_FOLIO
    if claim_vigente(dte):
        return _MSG_GUIA_EN_PROCESO
    if dte.uuid is not None and dte.status_id != STATUS_FALLIDO:
        # Documento con uuid que el SII no ha rechazado: procesando (2), borrador (6),
        # status desconocido o NULL → todavía puede nacer con folio.
        return _MSG_GUIA_EN_PROCESO
    if dte.uuid is None and dte.en_vuelo_desde is not None:
        # AMBIGUA. Se decide SIN mirar el status: el 4 local puede venir de una respuesta
        # que contradice una emisión real (ver _actualizar_desde_wasabil).
        return _MSG_GUIA_AMBIGUA
    # Rechazo CONFIRMADO (status 4 con uuid, o sin rastro de POST en vuelo): no existe
    # guía electrónica que referenciar y el N° de la guía en papel es el legítimo — que
    # además tiene que ser un folio numérico del SII (lo valida armar_referencias_factura).
    return None


def _guia_electronica_en_proceso(db: Session, despacho_id: int) -> bool:
    """¿El folio del SII de la guía de este despacho TODAVÍA no está disponible? (bool
    sobre _guia_no_referenciable, que es donde vive el criterio y el mensaje)."""
    return _guia_no_referenciable(db, despacho_id) is not None


# Parámetro con el que el operador declara —después de mirar Wasabil— que la mercadería
# salió con la guía en PAPEL y que no hay guía electrónica emitida por este despacho.
_PARAM_VERIF_52 = "verificado_sin_guia_electronica"


def _problema_52_de_papel(db: Session, despacho_id: int, numero_papel: str,
                          declaracion: Optional[str] = None
                          ) -> Tuple[Optional[str], Optional[str]]:
    """¿Puede esta factura citar como referencia 52 el N° de guía TECLEADO a mano?
    Devuelve `(problema | None, línea de auditoría | None)`. SOLO LECTURA.

    Cierra el ÚNICO estado con uuid que dejaba citar el N° del papel: `status 4 (rechazo
    confirmado) · uuid presente · sin claim vigente`. Que el SII haya rechazado el
    documento U no dice NADA sobre otro documento con la misma referencia: la fila puede
    decir "4 con uuid" mientras Wasabil tiene una 52 EMITIDA con esa referencia, y
    entonces la 33 sale citando el folio del papel en vez del real — irreversible, y la
    validación numérica del folio no lo frena (el N° del papel es numérico y legítimo a
    primera vista).

    NO vive dentro de `_guia_no_referenciable` a propósito: ese predicado se mantiene PURO
    (solo BD) porque su alineación estado-por-estado con
    routers/despachos.py:_guia_electronica_activa es lo que sostiene el invariante, y se
    prueba comparando las dos funciones. Éste es el umbral extra del camino que ARMA la 33.

      · Sin fila DTE, o con la fila SIN uuid → nunca nació un documento electrónico para
        este despacho: el N° del papel es el único que existe y es el legítimo.
      · (b) hay un EMITIDO con esta referencia → BLOQUEA nombrando el folio real. Ninguna
        declaración humana levanta este bloqueo.
      · (c) no se puede concluir → BLOQUEA (fail closed) con la salida humana AUDITADA.
      · (a) consta que no hay ninguno emitido → el N° del papel es legítimo (que un
        rechazo CONFIRMADO no deje al despacho preso sigue valiendo).

    COSTO OPERATIVO, sin adornos: mientras `GET /documents` responda 405 en el API real, el
    veredicto de un despacho con guía electrónica RECHAZADA será siempre (c), así que
    facturar citando la guía de PAPEL exigirá la declaración explícita del operador. Es el
    precio de que una 33 REAL no cite un folio que el SII no reconoce."""
    dte = _dte_de_despacho(db, despacho_id)
    if dte is None or dte.uuid is None:
        return None, None
    ref = _referencia_interna_guia(db, despacho_id)
    veredicto = _veredicto_documento_emitido(ref, compat_v1=True)
    papel = (numero_papel or "").strip() or "(sin N°)"
    if veredicto.veredicto == VERD_HAY_EMITIDO:
        return (
            f"Esta factura iba a citar como guía 52 el N° tecleado a mano ('{papel}'), "
            f"pero Wasabil tiene {len(veredicto.folios)} documento(s) EMITIDO(S) con la "
            f"referencia interna '{ref}' (folio(s): {', '.join(veredicto.folios)}): ése es "
            "el folio REAL de la guía de esta mercadería. Emitir así mandaría al SII una "
            "33 citando un folio que no la ampara, y eso no se deshace. Consulta el estado "
            "de la guía (o Reintentar) para que el folio real quede registrado, y factura "
            "después."), None
    if veredicto.veredicto == VERD_INDETERMINADO:
        if ref and (declaracion or "").strip() == ref:
            return None, _linea_auditoria_verificacion(ref, veredicto.motivo, None)
        return (
            f"Este despacho SÍ tiene un documento en Wasabil (uuid {dte.uuid}) y NO se pudo "
            f"verificar allá si hay una guía 52 EMITIDA con la referencia '{ref}' "
            f"({veredicto.motivo}), así que la factura no puede citar el N° de guía "
            f"tecleado a mano ('{papel}'): si esa 52 existe, la 33 saldría al SII con el "
            "folio equivocado. QUÉ REVISAR: en app.wasabil.com, los documentos cuya "
            f"referencia interna sea EXACTAMENTE '{ref}'. Si hay una 52 emitida, registra "
            "su folio (Estado / Reintentar) y factura después. Si CONSTA que la mercadería "
            f"salió con la guía en PAPEL N° {papel} y no hay ninguna 52 emitida, repite "
            f"'{ref}' en {_PARAM_VERIF_52} para dejar registrada tu verificación."), None
    return None, None


def _fecha_guia_papel(desp) -> Tuple[Optional[date], Optional[str]]:
    """(fecha, problema) de EMISIÓN de una guía EN PAPEL, para el FchRef de la ref 52.

    Fuente ÚNICA: `despacho.fecha_guia`, que teclea el operador en Despachos → Editar.

    POR QUÉ EXISTE Y POR QUÉ BLOQUEA
    Antes esta fecha se sacaba de `despacho.fecha_despacho`, que NO es la fecha de la guía:
    es el instante en que se cerró el despacho en PartsControl, puesto por el reloj del
    servidor. Cuando la guía se emite en el portal del SII un día y el despacho se cierra
    en el sistema otro (lo habitual), el DTE 33 salía REAL citando la guía con una fecha
    que esa guía no tiene. El SII no permite corregir un documento emitido, así que el
    error se descubría con la factura ya en la contabilidad del cliente.

    Sustituir una fecha desconocida por otra "parecida" es exactamente lo que causó el
    problema: sin el dato, se BLOQUEA. Es el mismo criterio del resto del módulo (un guard
    que falla ABIERTO es peor que ninguno) y lo confirmó el dueño al pedir el cambio.

    OJO: esto es sólo para la guía en PAPEL. Con guía ELECTRÓNICA la fecha sale del
    `documentDate` del propio DTE 52 y este helper no se llama.
    """
    fecha = getattr(desp, "fecha_guia", None)
    if fecha:
        return fecha, None
    numero = (getattr(desp, "numero_guia", "") or "").strip()
    return None, (
        f"La guía en papel N° {numero} no tiene registrada su FECHA DE EMISIÓN: "
        "cárgala en Despachos → Editar (botón del transportista) y vuelve a facturar. "
        "La referencia a la guía que lleva la factura ante el SII debe ir con la fecha en "
        "que se EMITIÓ la guía — no la de la firma del cliente ni la del cierre del "
        "despacho en el sistema, que es lo que se usaba antes y salía equivocado.")


def _guia_referencia_de_factura(
        db: Session, factura, declaracion_52_papel: Optional[str] = None
) -> Tuple[Optional[str], Optional[object], Optional[str], Optional[str]]:
    """(folio, fecha, problema, auditoría) de la guía a referenciar (tipo 52) en la
    factura.

    Preferencia: la guía ELECTRÓNICA emitida del despacho (folio SII + fecha
    tributaria del payload) → guía EN PAPEL (despacho.numero_guia + despacho.fecha_guia,
    ver _fecha_guia_papel) → problema BLOQUEANTE. Una factura de ANTICIPO no ampara
    traslado: no lleva 52 y tampoco es un problema.

    La tercera componente es la clave: antes se devolvía (None, None) en TRES caminos
    distintos sin reportar nada, y armar_referencias_factura omitía la 52 con
    `problemas` vacío → el DTE 33 salía REAL, con issue=True, sin la referencia a la
    guía que ampara la mercadería. Quien arma el documento inyecta este problema.

    La CUARTA es la línea de auditoría de la verificación humana cuando el N° de guía en
    PAPEL se usa habiendo un documento electrónico en Wasabil (ver _problema_52_de_papel):
    el llamador la persiste sólo en el camino que EMITE, nunca en el preview."""
    if not factura.despacho_id:
        if getattr(factura, "es_anticipo", 0):
            # Factura de ANTICIPO: respalda plata recibida por adelantado, no un
            # traslado — sale con la sola referencia 801 de la venta.
            return None, None, None, None
        return None, None, (
            "Esta factura no está ligada a una guía de despacho: no se puede armar la "
            "referencia 52 y emitirla sin ella dejaría mercadería trasladada sin el "
            "documento que la ampara. Emítela desde una guía despachada y firmada"), None
    motivo_guia = _guia_no_referenciable(db, factura.despacho_id)
    if motivo_guia:
        return None, None, motivo_guia, None
    dte_guia = (db.query(WasabilDte)
                .filter(WasabilDte.despacho_id == factura.despacho_id,
                        WasabilDte.tipo_dte == TIPO_DOC_GUIA,
                        WasabilDte.status_id == STATUS_EMITIDO)
                .first())
    if dte_guia and dte_guia.folio:
        fecha = None
        try:
            payload = json.loads(dte_guia.payload_json or "{}")
            if payload.get("documentDate"):
                fecha = datetime.fromisoformat(payload["documentDate"]).date()
        except (ValueError, TypeError):
            fecha = None
        if not fecha and dte_guia.created_at:
            fecha = dte_guia.created_at.date()
        return dte_guia.folio, fecha, None, None
    desp = factura.despacho
    if desp and (desp.numero_guia or "").strip():
        # N° de guía EN PAPEL: sólo es citable si consta que no hay una 52 electrónica
        # EMITIDA por este despacho (ver _problema_52_de_papel).
        problema_papel, auditoria = _problema_52_de_papel(
            db, factura.despacho_id, desp.numero_guia, declaracion_52_papel)
        if problema_papel:
            return None, None, problema_papel, None
        fecha, problema_fecha = _fecha_guia_papel(desp)
        if problema_fecha:
            return None, None, problema_fecha, None
        return desp.numero_guia.strip(), fecha, None, auditoria
    return None, None, (
        "La guía del despacho no tiene folio registrado: emite la guía SII (o registra "
        "el N° manual) antes de facturarla"), None


def _despacho_items_ajenos(db: Session, despacho_id: Optional[int],
                           despacho_item_ids) -> List[int]:
    """De los `despacho_item_id` declarados en las líneas, los que NO pertenecen al
    despacho de la factura (lista ordenada; vacía si todo está en su guía).

    POR QUÉ: `_construir_factura` (routers/contabilidad.py) valida el despacho_item_id
    contra TODOS los despacho_items de la OC, nunca contra el `despacho_id` declarado —
    y la referencia 52 se arma DESDE ese `despacho_id`. Reproducido: 4 unidades salidas en
    la guía B, facturadas declarando la guía A → el DTE 33 salía citando el folio de A.
    El invariante "una 33 nunca sale sin su 52 válida" se cumplía en la forma y se violaba
    en el fondo: mercadería amparada por una guía que no la trasladó. Sin despacho_id (modo
    `items`, anticipo) no hay 52 que citar y el bloqueo vive en otro lado."""
    ids = {int(i) for i in (despacho_item_ids or []) if i}
    if not ids or not despacho_id:
        return []
    propios = {fila[0] for fila in db.query(DespachoItem.id).filter(
        DespachoItem.despacho_id == despacho_id, DespachoItem.id.in_(ids)).all()}
    return sorted(ids - propios)


def _problema_lineas_de_la_guia(db: Session, despacho_id: Optional[int],
                                despacho_item_ids: List[Optional[int]]) -> Optional[str]:
    """Motivo por el que estas líneas NO están respaldadas por ESTE despacho, o None.

    Puerta ÚNICA del invariante "una 33 con referencia 52 solo puede facturar mercadería
    que salió en ESA guía", usada por los dos caminos que arman el DTE 33 (preview/emisión
    y reintento desde la factura congelada).

    Dos motivos, en orden:
      1. FALTA el `despacho_item_id`. Es OPCIONAL en el payload (FacturaItemIn) y el tope
         por guía de _construir_factura solo se aplica "si se indicó despacho_item_id":
         omitiéndolo, el tope pasa a ser el de la OC COMPLETA y se facturan unidades que
         salieron en OTRA guía citando la 52 de ésta. El chequeo de ítems ajenos quedaba
         vacío (no hay ids que comparar) y `_persistir_factura` grababa
         `despacho_item_id=NULL`, así que el cinturón del reintento nacía igual de ciego.
         Se EXIGE declararlo en la vía electrónica —donde el documento es irreversible—
         en vez de recalcular el tope acá: (a) activa el tope por guía que Contabilidad ya
         tiene (no hay una segunda fuente de verdad sobre cantidades), (b) deja la línea
         persistida con su guía, que es lo único que hace verificable el reintento, y
         (c) el único cliente real (FacturasPage) NUNCA manda `items`: deriva las líneas
         del despacho y ahí el id viene siempre. La vía manual de Contabilidad → Facturas
         sigue aceptando líneas sueltas: ahí no se emite nada al SII.
      2. El `despacho_item_id` declarado pertenece a OTRA guía (el hallazgo original).

    Sin `despacho_id` no hay 52 que citar (modo `items` puro y anticipos): ese camino se
    bloquea en otro lado (o no lleva mercadería) y acá no aplica.

    HONESTIDAD SOBRE LAS CAPAS: la causa raíz se cerró en la MISMA ronda en
    routers/contabilidad.py (`_ligar_lineas_a_su_guia`, archivo de otro reparador), que liga
    la línea suelta al ítem de ESTA guía y con eso activa el tope por guía. Con esa capa
    viva, el motivo 1 casi nunca se dispara en el preview/emisión — pero SÍ es la única
    defensa en el camino del REINTENTO, que arma el documento desde la factura ya
    persistida: ahí la línea puede tener `despacho_item_id = NULL` (facturas legadas, o
    creadas por cualquier camino que no sea el modal) y ya no hay nada que ligar. Este
    módulo es el último umbral antes del SII: no delega el invariante en otra capa."""
    if not despacho_id:
        return None
    ids = list(despacho_item_ids or [])
    if not ids:
        return None  # sin líneas de mercadería no hay nada que amparar (anticipo)
    sin_guia = sum(1 for i in ids if i is None or not i)
    if sin_guia:
        return (f"{sin_guia} de las {len(ids)} líneas no dicen de qué ítem de esta guía de "
                "despacho salieron (despacho_item_id): la factura electrónica referencia la "
                "guía 52 y no se puede confirmar que la mercadería facturada sea la que esa "
                "guía trasladó. Emítela desde la guía (el modal lo hace solo) o registra la "
                "factura por la vía manual en Contabilidad → Facturas")
    ajenas = _despacho_items_ajenos(db, despacho_id, ids)
    if ajenas:
        return (f"Hay líneas que salieron en OTRA guía de despacho (ítem de despacho "
                f"{', '.join(str(i) for i in ajenas)}): la referencia 52 citaría una guía "
                "que no trasladó esa mercadería. Factura cada guía por separado")
    return None


def _anticipos_referenciados(db: Session, factura) -> List[dict]:
    """[{folio, fecha}] de las facturas de ANTICIPO descontadas en esta factura
    (líneas negativas con anticipo_factura_id) — referencias tipo 33. El armado
    de referencias bloquea si algún anticipo no tiene folio SII registrado."""
    ids = {it.anticipo_factura_id for it in factura.items if it.anticipo_factura_id}
    if not ids:
        return []
    out = []
    for fa in db.query(ContFacturaCliente).filter(ContFacturaCliente.id.in_(ids)).all():
        out.append({"folio": fa.numero_factura or f"#{fa.id}", "fecha": fa.fecha_emision})
    return out


def _receptor_factura(db: Session, rut: str, razon_social_local: Optional[str],
                      problemas: List[str], advertencias: List[str]):
    """Resuelve la ficha del cliente en Wasabil (client_id). La factura 33 es más
    estricta que la guía: ficha inexistente o SIN giro/dirección/comuna BLOQUEA
    (el SII exige receptor completo; emitir incompleto termina en rechazo)."""
    receptor = {"rut": rut or None, "razon_social": razon_social_local,
                "giro": None, "direccion": None, "comuna": None, "ciudad": None,
                "fuente": "local"}
    client_id = None
    if not wasabil.esta_configurado():
        problemas.append("Wasabil no está configurado (falta WASABIL_API_TOKEN en backend/.env): "
                         "puedes previsualizar, pero no emitir")
        return receptor, client_id
    if not rut:
        return receptor, client_id  # el RUT faltante ya lo reportó _construir_factura
    try:
        cli = wasabil.buscar_cliente_por_rut(rut)
    except wasabil.WasabilError as e:
        problemas.append(f"No se pudo consultar el cliente en Wasabil: {e}")
        return receptor, client_id
    if not cli:
        problemas.append(
            f"El cliente RUT {rut} no existe en Wasabil: créalo en app.wasabil.com "
            "(con giro, dirección y comuna) y vuelve a intentar")
        return receptor, client_id
    client_id = cli.get("id")
    receptor = {
        "rut": cli.get("rut") or rut,
        "razon_social": cli.get("name") or cli.get("razon_social") or razon_social_local,
        "giro": cli.get("giro") or cli.get("activity"),
        "direccion": cli.get("address") or cli.get("direccion"),
        "comuna": cli.get("comuna") or cli.get("commune"),
        "ciudad": cli.get("city") or cli.get("ciudad"),
        "fuente": "wasabil",
    }
    faltantes = [n for n, v in (("giro", receptor["giro"]),
                                ("dirección", receptor["direccion"]),
                                ("comuna", receptor["comuna"])) if not (v or "").strip()]
    if faltantes:
        problemas.append(
            f"La ficha del cliente en Wasabil no tiene {', '.join(faltantes)}: "
            "la factura 33 exige receptor completo — complétala en app.wasabil.com")
    return receptor, client_id


def _payment_method(plazo_dias, condicion_pago: Optional[str] = None) -> str:
    """paymentMethod del DTE 33 (OBLIGATORIO en el esquema): contado si el plazo es
    0 días o la condición lo dice; crédito en el resto (default del negocio)."""
    if plazo_dias is not None and int(plazo_dias) == 0:
        return "contado"
    if "contado" in (condicion_pago or "").lower():
        return "contado"
    return "credito"


def _preparar_emision_factura(db: Session, payload: FacturaCreate,
                              declaracion_52_papel: Optional[str] = None) -> dict:
    """Arma y valida TODO para emitir una factura NUEVA (SIN persistir ni locks;
    puede llamar a Wasabil para la ficha del cliente). Única fuente de verdad de
    la validación del preview — la emisión re-valida bajo lock con las mismas
    funciones de Contabilidad.

    `declaracion_52_papel`: verificación humana del N° de guía en papel (ver
    _problema_52_de_papel). El contexto devuelve `auditoria_52` para que SOLO el camino
    que emite la persista."""
    problemas: List[str] = []
    advertencias: List[str] = []
    if (payload.numero_factura or "").strip():
        problemas.append("El folio lo asigna el SII al emitir: deja el N° de factura vacío "
                         "(para registrar una factura ya emitida usa Contabilidad → Facturas)")
    if (payload.tipo_doc or "factura") != "factura":
        problemas.append("La emisión electrónica es para tipo 'factura' (DTE 33); "
                         "las boletas se registran por la vía manual")

    oc = db.query(OcCliente).filter(OcCliente.id == payload.oc_cliente_id).first()
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "OC Cliente no encontrada")
    cot = oc.cotizacion
    empresa = "mineria"

    if payload.es_anticipo:
        datos = _construir_factura_anticipo(db, payload, oc, cot, empresa)
    else:
        datos = _construir_factura(db, payload, oc, cot, empresa)
    problemas.extend(datos["problemas"])
    advertencias.extend(datos["advertencias"])
    # ADVERTENCIA (jamás bloqueo): la vía SII gratuito rechaza >10 ítems por documento
    # (los 3 únicos fallidos históricos de la cuenta). Se cuentan las líneas VALIDADAS
    # — las que viajan como details del DTE 33 —: los descuentos de anticipo no suman
    # (van como `discount` porcentual, no como línea) y la factura de anticipo lleva
    # 1 sola línea, así que nunca gatilla el aviso.
    aviso_tope = advertencia_lineas_sii_gratuito(len(datos.get("validadas") or []), "factura")
    if aviso_tope:
        advertencias.append(aviso_tope)
    # Mismo piso que aplicar_descuento_lineas: sin esto el preview decía "puede emitir"
    # y el emitir moría en 409 DESPUÉS de haber creado la factura local (zombi).
    if datos.get("neto") is not None and 0 <= float(datos["neto"]) < NETO_MINIMO_DTE:
        problemas.append(
            "El descuento del anticipo deja la factura en $0: el SII no acepta un "
            "DTE en cero — ajusta el descuento o registra la factura por la vía manual")

    # Receptor: ficha REAL en Wasabil (client_id) — bloqueo si está incompleta
    rut = datos["receptor"].get("rut_normalizado") or (datos["receptor"].get("rut") or "")
    receptor, client_id = _receptor_factura(
        db, rut, datos["receptor"].get("razon_social"), problemas, advertencias)

    # Referencias según la matriz de negocio (OC 801 + guía 52 + anticipos 33)
    guia_folio, guia_fecha, auditoria_52 = None, None, None
    if not payload.es_anticipo and not payload.despacho_id and datos.get("validadas"):
        # Modo `items` SIN despacho_id: la mercadería SÍ está despachada (_construir_factura
        # lo exige por ítem), pero no hay guía identificable, así que no se puede armar la
        # referencia 52 y la factura salía al SII con la sola 801 — mercadería trasladada
        # sin el documento que la ampara, en silencio. Se BLOQUEA: la vía electrónica de
        # MachParts factura DESDE una guía firmada.
        problemas.append(
            "No se pudo determinar la guía de despacho a referenciar (tipo 52): emite la "
            "factura electrónica desde una guía despachada y firmada (para facturar por "
            "ítems suelto usa la vía manual en Contabilidad → Facturas)")
    if not payload.es_anticipo and payload.despacho_id and datos.get("desp"):
        desp = datos["desp"]
        # Las líneas tienen que ser de ESTA guía: _construir_factura valida el
        # despacho_item_id contra TODOS los despacho_items de la OC, así que declarando
        # `despacho_id` = guía A se podían facturar unidades salidas en la guía B — la 52
        # citaba la guía que NO trasladó esa mercadería. Se bloquea acá, antes de persistir
        # (y el id es OBLIGATORIO: sin él el guard no tiene nada que comparar — ver
        # _problema_lineas_de_la_guia).
        problema_lineas = _problema_lineas_de_la_guia(
            db, desp.id, [ln.despacho_item_id for _it, ln, *_r in (datos.get("validadas") or [])])
        if problema_lineas:
            problemas.append(problema_lineas)
        dte_guia = (db.query(WasabilDte)
                    .filter(WasabilDte.despacho_id == desp.id,
                            WasabilDte.tipo_dte == TIPO_DOC_GUIA,
                            WasabilDte.status_id == STATUS_EMITIDO).first())
        motivo_guia_desp = _guia_no_referenciable(db, desp.id)
        if dte_guia and dte_guia.folio:
            guia_folio = dte_guia.folio
            try:
                p = json.loads(dte_guia.payload_json or "{}")
                guia_fecha = (datetime.fromisoformat(p["documentDate"]).date()
                              if p.get("documentDate") else None)
            except (ValueError, TypeError, KeyError):
                guia_fecha = None
            if not guia_fecha and dte_guia.created_at:
                guia_fecha = dte_guia.created_at.date()
        elif motivo_guia_desp:
            # El folio del SII no está disponible (en proceso, emitida sin folio o emisión
            # ambigua sin confirmar) y `desp.numero_guia` todavía tiene el N° manual viejo:
            # referenciarlo mandaría la factura al SII apuntando a un folio inexistente, y
            # eso no se deshace. Se espera a que la guía quede resuelta.
            problemas.append(motivo_guia_desp)
        elif (desp.numero_guia or "").strip():
            # N° de guía EN PAPEL: sólo citable si consta que este despacho no tiene una
            # 52 electrónica EMITIDA en Wasabil (ver _problema_52_de_papel).
            problema_papel, auditoria_52 = _problema_52_de_papel(
                db, desp.id, desp.numero_guia, declaracion_52_papel)
            if problema_papel:
                problemas.append(problema_papel)
            else:
                # Gemelo de _guia_referencia_de_factura. Los dos están en el camino de la
                # emisión y hacen cosas distintas: éste es el GUARD previo (lo corren
                # /facturas/preview y la puerta de /facturas/emitir), aquél ARMA el
                # documento que efectivamente viaja al SII (_armar_payload_factura, en el
                # emitir y en el reintento). La fecha sale del MISMO helper a propósito: si
                # cada copia la calculara por su lado, el guard podía decir "puede emitir"
                # y el documento salir con otra fecha.
                fecha_papel, problema_fecha = _fecha_guia_papel(desp)
                if problema_fecha:
                    problemas.append(problema_fecha)
                else:
                    guia_folio = desp.numero_guia.strip()
                    guia_fecha = fecha_papel
        else:
            problemas.append("La guía del despacho no tiene folio registrado: emite la guía "
                             "SII (o registra el N° manual) antes de facturarla")
    anticipos_ref = []
    for dsc in datos.get("descuentos", []):
        fa = db.query(ContFacturaCliente).filter(
            ContFacturaCliente.id == dsc["anticipo_factura_id"]).first()
        anticipos_ref.append({"folio": (fa.numero_factura if fa else None) or f"#{dsc['anticipo_factura_id']}",
                              "fecha": fa.fecha_emision if fa else None})
    referencias, problemas_ref = armar_referencias_factura(
        numero_oc=(oc.numero_oc or "").strip(),
        fecha_oc=parse_fecha_oc(oc.fecha_oc),
        guia_folio=guia_folio, guia_fecha=guia_fecha, anticipos=anticipos_ref,
        # Fecha que va a llevar el documento: MISMA fórmula que _persistir_factura
        # (routers.contabilidad), para que el guard mida contra lo que se va a emitir.
        fecha_documento=_parse_date(payload.fecha_emision) or hoy_chile())
    problemas.extend(problemas_ref)

    return {
        "oc": oc, "cot": cot, "datos": datos, "receptor": receptor,
        "client_id": client_id, "referencias": referencias,
        "problemas": problemas, "advertencias": advertencias,
        "auditoria_52": auditoria_52, "despacho_52_id": getattr(datos.get("desp"), "id", None),
    }


def _armar_payload_factura(db: Session, factura, client_id: Optional[int], issue: bool,
                           declaracion_52_papel: Optional[str] = None
                           ) -> Tuple[dict, List[str], Optional[str]]:
    """Payload del DTE 33 DESDE la factura local persistida (líneas congeladas):
    lo emitido es EXACTAMENTE lo registrado — y el reintento re-arma lo mismo.

    Tercera componente: la línea de auditoría de la verificación humana del N° de guía en
    papel, que el llamador persiste ANTES de emitir (None en el camino normal)."""
    lineas, problemas = armar_lineas_factura(list(factura.items))
    oc = factura.oc_cliente
    # Cinturón del camino del REINTENTO (arma desde la factura ya congelada): ninguna línea
    # puede venir de otra guía —ni callarse de qué ítem de la guía salió—, o la 52 citaría
    # una guía que no trasladó esa mercadería. Solo las líneas de MERCADERÍA: las de
    # descuento de anticipo (anticipo_factura_id) y la línea única del anticipo no amparan
    # traslado y por construcción no tienen ítem de despacho.
    problema_lineas = _problema_lineas_de_la_guia(
        db, factura.despacho_id,
        [it.despacho_item_id for it in factura.items
         if it.item_cotizacion_id is not None and not it.anticipo_factura_id])
    if problema_lineas:
        problemas.append(problema_lineas)
    guia_folio, guia_fecha, problema_guia, auditoria_52 = _guia_referencia_de_factura(
        db, factura, declaracion_52_papel)
    if problema_guia:
        # Sin la 52 no se emite: antes este camino omitía la referencia en silencio.
        problemas.append(problema_guia)
    referencias, problemas_ref = armar_referencias_factura(
        numero_oc=(oc.numero_oc or "").strip() if oc else "",
        fecha_oc=parse_fecha_oc(oc.fecha_oc) if oc else None,
        guia_folio=guia_folio, guia_fecha=guia_fecha,
        anticipos=_anticipos_referenciados(db, factura),
        # MISMO valor que armar_factura pone en documentDate (abajo): si difirieran, el
        # control cruzaría la fecha de la guía contra una fecha que el DTE no lleva.
        fecha_documento=factura.fecha_emision or hoy_chile())
    problemas.extend(problemas_ref)
    doc = armar_factura(
        referencia_interna=_referencia_interna_factura(factura.id),
        lineas=lineas, referencias=referencias, client_id=client_id,
        fecha_emision=factura.fecha_emision, issue=issue,
        payment_method=_payment_method(factura.plazo_dias, factura.condicion_pago),
    )
    return doc, problemas, auditoria_52


def _emision_33_en_vuelo_de_oc(db: Session, oc_id: int) -> Optional[WasabilDte]:
    """DTE 33 con claim VIGENTE de cualquier factura de esta OC, si lo hay.

    Candado de INTENCIÓN para el flujo "emitir factura NUEVA": el índice único
    `uq_wasabil_dte_factura` protege una factura ya creada, pero aquí cada request
    crearía una factura distinta (id distinto), así que no aplica. Sin este candado,
    dos clics simultáneos en Emitir producen DOS documentos reales ante el SII —
    especialmente en anticipos, donde no hay tope de mercadería que los frene."""
    candidatos = (db.query(WasabilDte)
                  .join(ContFacturaCliente, ContFacturaCliente.id == WasabilDte.factura_id)
                  .filter(ContFacturaCliente.oc_cliente_id == oc_id,
                          WasabilDte.tipo_dte == TIPO_DOC_FACTURA,
                          WasabilDte.en_vuelo_desde.isnot(None))
                  .populate_existing().all())
    return next((d for d in candidatos if claim_vigente(d)), None)


def _reclamar_emision_factura(db: Session, factura_id: int, para_reintento: bool,
                              usuario_id: Optional[int], empresa: str) -> WasabilDte:
    """Claim anti doble emisión de la factura (espejo de _reclamar_emision):
    transacción CORTA bajo lock, commit antes de cualquier HTTP."""
    factura = (db.query(ContFacturaCliente)
               .filter(ContFacturaCliente.id == factura_id)
               .populate_existing().with_for_update().first())
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    dte = _dte_de_factura(db, factura_id, lock=True)
    problema = _estado_dte_bloquea(dte, para_reintento, _SUST_FACTURA, _DOC_FACTURA)
    if problema:
        db.rollback()
        raise HTTPException(409, problema)
    ahora = datetime.utcnow()
    if dte:
        dte.en_vuelo_desde = ahora
        dte.status_id = STATUS_PENDIENTE
        dte.uuid = None
        dte.error = None
        dte.usuario_id = usuario_id or dte.usuario_id
    else:
        dte = WasabilDte(
            empresa=empresa, tipo_dte=TIPO_DOC_FACTURA, factura_id=factura_id,
            status_id=STATUS_PENDIENTE, en_vuelo_desde=ahora, usuario_id=usuario_id,
        )
        db.add(dte)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Ya existe una emisión para esta factura (refresca la página)")
    db.refresh(dte)
    return dte


def _finalizar_factura_emitida(db: Session, dte: WasabilDte) -> None:
    """Al confirmarse EMITIDO (status 3): folio del SII → numero_factura de la
    factura local, y RECIÉN AHÍ se aplican los adelantos aprobados como cobranza
    (diferidos desde la persistencia: una factura rechazada no movió plata).
    Idempotente: si el folio ya está escrito, no repite nada."""
    if dte.status_id != STATUS_EMITIDO or not dte.folio or not dte.factura_id:
        return
    factura = db.query(ContFacturaCliente).filter(
        ContFacturaCliente.id == dte.factura_id).first()
    if not factura or (factura.numero_factura or "").strip():
        return  # ya finalizada (idempotencia del sondeo/reintento)
    # Orden GLOBAL de locks de la casa: OC → factura (igual que eliminar/cobranzas)
    oc = (db.query(OcCliente).filter(OcCliente.id == factura.oc_cliente_id)
          .with_for_update().first())
    factura = (db.query(ContFacturaCliente)
               .filter(ContFacturaCliente.id == dte.factura_id)
               .populate_existing().with_for_update().first())
    if not factura or (factura.numero_factura or "").strip():
        db.rollback()
        return
    try:
        factura.numero_factura = str(dte.folio)
        db.flush()
    except IntegrityError:
        # Colisión de folio (folio ya registrado a mano para otra factura): el DTE
        # queda emitido igual; se anota para resolverlo a mano sin perder el folio.
        db.rollback()
        dte.error = (f"Folio {dte.folio} ya registrado en otra factura local: "
                     "resolver duplicado a mano")[:2000]
        db.commit()
        return
    if oc:
        _aplicar_adelantos_pendientes(db, oc, factura, usuario_id=dte.usuario_id)
    db.commit()


@router.post("/facturas/preview")
def preview_factura_sii(
    payload: FacturaCreate,
    verificado_sin_guia_electronica: Optional[str] = Query(
        None, description="Verificación humana AUDITADA para citar el N° de guía EN PAPEL "
                          "cuando el despacho tiene un documento en Wasabil y el listado no "
                          "permite concluir si hay una 52 emitida: el N° de despacho EXACTO "
                          "(ver _problema_52_de_papel)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Previsualización de la factura 33: documento + referencias + receptor real
    de Wasabil + validaciones. NO persiste, NO toca el SII (tampoco registra la
    verificación humana del N° de papel: eso lo hace sólo el camino que emite)."""
    ctx = _preparar_emision_factura(db, payload, verificado_sin_guia_electronica)
    datos = ctx["datos"]
    return {
        "puede_emitir": not ctx["problemas"],
        "problemas": ctx["problemas"],
        "advertencias": ctx["advertencias"],
        "receptor": ctx["receptor"],
        "lineas": datos["lineas"],
        "totales": {"neto": datos["neto"], "iva": datos["iva"], "bruto": datos["bruto"]},
        "referencias": [{"tipo": r["documentType"], "folio": r["folio"],
                         "fecha": r.get("date"), "descripcion": r.get("reason")}
                        for r in ctx["referencias"]],
        "precio_de_guia": datos.get("precio_de_guia", False),
        "descuentos": datos.get("descuentos", []),
    }


@router.post("/facturas/emitir")
def emitir_factura_sii(
    payload: FacturaCreate,
    verificado_sin_guia_electronica: Optional[str] = Query(
        None, description="Verificación humana AUDITADA para citar el N° de guía EN PAPEL "
                          "cuando el despacho tiene un documento en Wasabil y el listado no "
                          "permite concluir si hay una 52 emitida: el N° de despacho EXACTO "
                          "(ver _problema_52_de_papel)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE la factura al SII vía Wasabil (IRREVERSIBLE — el frontend solo habilita
    este botón tras la previsualización). Crea la factura LOCAL sin folio + el claim
    en la MISMA transacción; el folio del SII llega al confirmarse la emisión."""
    ctx = _preparar_emision_factura(db, payload, verificado_sin_guia_electronica)
    if ctx["problemas"]:
        raise HTTPException(409, " · ".join(ctx["problemas"]))
    empresa = getattr(current_user, "empresa", None) or "mineria"
    usuario_id = getattr(current_user, "id", None)

    # ── TX corta: lock OC → re-validar → persistir SIN folio → claim → COMMIT ──
    # rollback ANTES del lock: _preparar_emision_factura ya abrió la transacción
    # (SELECTs + HTTP a Wasabil), y bajo REPEATABLE READ todas las lecturas NO
    # bloqueantes de más abajo servirían ese snapshot VIEJO — la re-validación no
    # vería la factura que un request gemelo acaba de commitear y se emitirían DOS
    # documentos reales al SII. Con el rollback, el snapshot nace con el FOR UPDATE.
    db.rollback()
    oc = (db.query(OcCliente).filter(OcCliente.id == payload.oc_cliente_id)
          .with_for_update().first())
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "OC Cliente no encontrada")
    cot = oc.cotizacion
    # Candado de intención (ver _emision_33_en_vuelo_de_oc): el índice único es por
    # factura_id y aquí cada request crearía una factura nueva, así que el tope de
    # mercadería es la única defensa — y en anticipos no existe.
    en_vuelo = _emision_33_en_vuelo_de_oc(db, oc.id)
    if en_vuelo is not None:
        db.rollback()
        raise HTTPException(409, "Ya hay una emisión de factura EN CURSO para esta venta "
                                 "(otra pestaña u otro usuario). Espera su resultado antes "
                                 "de emitir otra.")
    if payload.es_anticipo:
        datos = _construir_factura_anticipo(db, payload, oc, cot, empresa)
    else:
        datos = _construir_factura(db, payload, oc, cot, empresa)
    if datos["problemas"]:
        db.rollback()
        raise HTTPException(409, " · ".join(datos["problemas"]))
    try:
        factura = _persistir_factura(
            db, payload, oc, cot, datos, folio=None, tipo_doc="factura",
            empresa=empresa, usuario_id=usuario_id, aplicar_adelantos=False)
        db.flush()
        dte = WasabilDte(
            empresa=empresa, tipo_dte=TIPO_DOC_FACTURA, factura_id=factura.id,
            status_id=STATUS_PENDIENTE, en_vuelo_desde=datetime.utcnow(),
            usuario_id=usuario_id,
        )
        db.add(dte)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "No se pudo registrar la factura (conflicto de integridad)")
    db.refresh(dte)
    factura_id = dte.factura_id

    # ── Payload DESDE la factura persistida + HTTP (sin locks) ──
    factura = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    # La verificación humana del N° de papel queda escrita en la GUÍA (el documento sobre
    # el que se declaró) ANTES de que salga la 33: es el rastro de quién autorizó citarlo.
    _anotar_auditoria(db, _dte_de_despacho(db, ctx["despacho_52_id"]) if
                      ctx.get("despacho_52_id") else None, ctx.get("auditoria_52"))
    doc, problemas_doc, _aud = _armar_payload_factura(
        db, factura, ctx["client_id"], issue=True,
        declaracion_52_papel=verificado_sin_guia_electronica)
    if problemas_doc:
        # No debería ocurrir (el preview ya validó). Nada salió aún hacia Wasabil, así
        # que se DESHACE la factura recién creada en vez de dejarla sin folio: si no,
        # quedaba una factura zombi consumiendo el tope facturable del despacho.
        try:
            # Desvincular los adelantos ANTES de borrar (cont_adelanto.factura_anticipo_id
            # es una FK sin ON DELETE: sin esto el DELETE choca con IntegrityError, la TX
            # se revierte entera y queda algo peor — factura zombi + claim vivo bloqueando
            # la OC 180 s). Mismo paso previo que hace eliminar_factura.
            db.query(ContAdelanto).filter(
                ContAdelanto.factura_anticipo_id == factura.id
            ).update({ContAdelanto.factura_anticipo_id: None}, synchronize_session=False)
            db.delete(dte)
            db.delete(factura)
            db.commit()
        except IntegrityError:
            # Red de seguridad: si algo más quedó referenciando la factura, no se puede
            # deshacer — se libera al menos el claim para no bloquear la OC.
            db.rollback()
            dte_vivo = _dte_de_factura(db, factura_id)
            if dte_vivo is not None:
                dte_vivo.en_vuelo_desde = None
                dte_vivo.error = " · ".join(problemas_doc)[:2000]
                db.commit()
        raise HTTPException(409, " · ".join(problemas_doc))
    dte.payload_json = json.dumps(doc, ensure_ascii=False)[:60000]
    dte.monto_neto, dte.iva, dte.monto_total = datos["neto"], datos["iva"], datos["bruto"]
    db.commit()
    try:
        data = wasabil.crear_documento(payload_a_rest(doc))
    except wasabil.WasabilError as e:
        dte.error = (str(e) + (f" · {e.detalle[:500]}" if e.detalle else ""))[:2000]
        if not e.ambiguo:
            # Creación confirmadamente fallida: se libera el claim y el estado deja de
            # decir PENDIENTE ("borrador en Wasabil"), que no es cierto — ver la nota
            # extensa en _emitir_en_wasabil.
            dte.en_vuelo_desde = None
            dte.status_id = STATUS_FALLIDO
        db.commit()
        raise HTTPException(502, f"No se pudo emitir en Wasabil: {e}")
    # La 33 tiene el MISMO hueco que la 52: respuesta EMITIDA sin folio → se trae el
    # documento completo, si no la factura local se queda sin numero_factura y los
    # adelantos diferidos nunca se aplican. Sin uuid, por la referencia interna FACT-<id>.
    data = _completar_documento_emitido(
        data, referencia_interna=_referencia_interna_factura(factura_id))
    _actualizar_desde_wasabil(db, dte, data)
    db.commit()
    _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**serialize_dte(dte), "factura_id": factura_id}


@router.post("/facturas/{factura_id}/registrar-folio")
def registrar_folio_factura(
    factura_id: int,
    folio: str = Query(..., description="Folio REAL del SII, leído en app.wasabil.com"),
    confirmo_folio: str = Query(..., description="Repite el folio (confirmación explícita)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SALIDA del callejón "factura EMITIDA sin folio" (MEDIO-6), gemela de la de guías.
    NO emite nada. Al registrar el folio se ejecuta el cierre normal de la factura
    (numero_factura + aplicación de los adelantos diferidos), que es justo lo que quedaba
    sin ocurrir mientras el folio no llegaba."""
    dte = _dte_de_factura(db, factura_id)
    if not dte:
        raise HTTPException(404, "Esta factura no tiene emisión electrónica")
    if claim_vigente(dte):
        raise HTTPException(409, "Hay una emisión EN CURSO para esta factura: espera su "
                                 "resultado antes de registrar un folio")
    resultado = _registrar_folio_a_mano(
        db, dte, folio, confirmo_folio, "factura",
        _referencia_interna_factura(factura_id), False,
        getattr(current_user, "id", None))
    _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**resultado, **serialize_dte(dte), "factura_id": factura_id,
            "registro_manual": resultado.get("registro_manual")}


@router.get("/facturas/{factura_id}/estado")
def estado_factura_sii(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Estado del DTE de la factura (sondeo del frontend). Al quedar Emitido,
    escribe el folio en la factura local y aplica los adelantos diferidos."""
    dte = _dte_de_factura(db, factura_id)
    if not dte:
        raise HTTPException(404, "Esta factura no tiene emisión electrónica")
    # Espejo del sondeo de guías: 'emitida SIN folio' se re-consulta; de lo contrario la
    # factura local se queda para siempre sin numero_factura (con el adelanto diferido
    # sin aplicar) y /reintentar responde 409 'ya está emitida'.
    emitida_sin_folio = dte.status_id == STATUS_EMITIDO and _vacio(dte.folio)
    if dte.uuid and (dte.status_id not in (STATUS_EMITIDO, STATUS_FALLIDO)
                     or emitida_sin_folio):
        try:
            data = wasabil.estado_documento(dte.uuid)
            if int(data.get("status_id") or 0) == STATUS_EMITIDO:
                data = wasabil.obtener_documento(dte.uuid)
            _actualizar_desde_wasabil(db, dte, data)
            db.commit()
            db.refresh(dte)
        except wasabil.WasabilError as e:
            return {**serialize_dte(dte), "factura_id": factura_id, "error_consulta": str(e)}
    elif emitida_sin_folio:
        # Sin uuid no hay documento que consultar por id: el sondeo se autocura por la
        # referencia interna FACT-<id> (solo lectura). Sin esto la factura local se queda
        # sin folio para siempre y los adelantos diferidos nunca se aplican.
        try:
            doc = _rescatar_por_referencia(_referencia_interna_factura(factura_id))
        except wasabil.WasabilError as e:
            return {**serialize_dte(dte), "factura_id": factura_id, "error_consulta": str(e)}
        if doc:
            _actualizar_desde_wasabil(db, dte, doc)
            db.commit()
            db.refresh(dte)
    _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**serialize_dte(dte), "factura_id": factura_id}


@router.post("/facturas/{factura_id}/reintentar")
def reintentar_factura_sii(
    factura_id: int,
    verificado_sin_emitido: Optional[str] = Query(
        None, description="Salida humana AUDITADA del cinturón por referencia cuando "
                          "Wasabil no permite concluir si ya existe un documento emitido: "
                          "la referencia interna EXACTA (FACT-<id>), tecleada por el "
                          "operador después de revisarla en app.wasabil.com"),
    verificado_sin_guia_electronica: Optional[str] = Query(
        None, description="Verificación humana AUDITADA para citar el N° de guía EN PAPEL "
                          "cuando el despacho tiene un documento en Wasabil y el listado no "
                          "permite concluir si hay una 52 emitida: el N° de despacho EXACTO "
                          "(ver _problema_52_de_papel)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reintento SEGURO de una emisión de factura fallida (espejo del de guías):
    verifica el estado real en Wasabil (por uuid o por la referencia interna
    FACT-<id>) ANTES de re-crear; si no puede verificar, ABORTA.

    Caso 'EMITIDA SIN folio': igual que en las guías, el documento EXISTE ante el SII y el
    reintento se vuelve un RESCATE de solo lectura del folio — jamás una re-emisión."""
    dte = _dte_de_factura(db, factura_id)
    if not dte:
        raise HTTPException(404, "Esta factura no tiene emisión que reintentar")
    rescate_de_folio = dte.status_id == STATUS_EMITIDO and _vacio(dte.folio)
    if dte.status_id == STATUS_EMITIDO and not rescate_de_folio:
        raise HTTPException(409, f"La factura ya está emitida (folio {dte.folio})")
    if claim_vigente(dte):
        raise HTTPException(409, "Hay una emisión EN CURSO para esta factura: "
                                 "espera unos minutos y consulta el estado")

    if dte.uuid:
        try:
            data = wasabil.estado_documento(dte.uuid)
            if int(data.get("status_id") or 0) == STATUS_EMITIDO:
                data = wasabil.obtener_documento(dte.uuid)
            _actualizar_desde_wasabil(db, dte, data)
            db.commit()
            db.refresh(dte)
        except wasabil.WasabilError:
            raise HTTPException(502, "No se pudo verificar el estado real del documento en "
                                     "Wasabil; reintenta en unos minutos (no se re-crea a ciegas)")
        if dte.status_id in (STATUS_EMITIDO, STATUS_PROCESANDO, STATUS_PENDIENTE):
            _finalizar_factura_emitida(db, dte)
            db.refresh(dte)
            if rescate_de_folio and _vacio(dte.folio):
                raise HTTPException(409, _msg_rescate_sin_folio(dte, _SUST_FACTURA))
            return {**serialize_dte(dte), "factura_id": factura_id}
    else:
        ref = _referencia_interna_factura(factura_id)
        try:
            # Misma fuente única que las guías: gana el EMITIDO, aborta si hay dos.
            doc_w = _rescatar_por_referencia(ref)
        except wasabil.WasabilError as e:
            raise HTTPException(502, "No se pudo verificar en Wasabil si el documento ya existe; "
                                     f"reintenta en unos minutos (no se re-crea a ciegas). {e}")
        if doc_w:
            _actualizar_desde_wasabil(db, dte, doc_w)
            db.commit()
            _finalizar_factura_emitida(db, dte)
            db.refresh(dte)
            if rescate_de_folio and _vacio(dte.folio):
                raise HTTPException(409, _msg_rescate_sin_folio(dte, _SUST_FACTURA))
            return {**serialize_dte(dte), "factura_id": factura_id}

    # 'EMITIDA sin folio' que el rescate no resolvió: el DTE 33 ya existe ante el SII, así
    # que este camino JAMÁS puede seguir a re-emitir (sería una segunda factura REAL).
    if rescate_de_folio:
        raise HTTPException(409, _msg_rescate_sin_folio(dte, _SUST_FACTURA))

    # CINTURÓN ANTI DOBLE EMISIÓN por REFERENCIA (ver el gemelo en reintentar_guia): con
    # el uuid del intento rechazado, `estado_documento` confirma "fallido" y se re-emitía
    # aunque Wasabil ya tuviera una 33 EMITIDA con la misma referencia FACT-<id>. Duplicar
    # un DTE 33 es peor que duplicar una guía: son dos ventas ante el SII. FALLA CERRADO.
    auditoria = _abortar_si_puede_haber_documento_emitido(
        _referencia_interna_factura(factura_id), "factura",
        declaracion=verificado_sin_emitido,
        usuario_id=getattr(current_user, "id", None))

    # Documento confirmado fallido/inexistente → re-emitir DESDE la factura local
    factura = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    problemas: List[str] = []
    advertencias: List[str] = []
    # La cotización se alcanza por la OC (ContFacturaCliente NO tiene relación
    # `cotizacion`; solo la columna cotizacion_id y la relación `oc_cliente`).
    cot_fac = factura.oc_cliente.cotizacion if factura.oc_cliente else None
    rut = (cot_fac.rut_cliente if cot_fac else None) or ""
    _receptor, client_id = _receptor_factura(
        db, rut, cot_fac.cliente if cot_fac else None, problemas, advertencias)
    doc, problemas_doc, auditoria_52 = _armar_payload_factura(
        db, factura, client_id, issue=True,
        declaracion_52_papel=verificado_sin_guia_electronica)
    problemas.extend(problemas_doc)
    if problemas:
        raise HTTPException(409, " · ".join(problemas))
    if auditoria_52 and factura.despacho_id:
        _anotar_auditoria(db, _dte_de_despacho(db, factura.despacho_id), auditoria_52)

    dte = _reclamar_emision_factura(
        db, factura_id, para_reintento=True,
        usuario_id=getattr(current_user, "id", None),
        empresa=getattr(current_user, "empresa", None) or "mineria")
    dte.payload_json = json.dumps(doc, ensure_ascii=False)[:60000]
    db.commit()
    # Autorización humana persistida ANTES del HTTP (ver el gemelo en reintentar_guia)
    _anotar_auditoria(db, dte, auditoria)
    try:
        data = wasabil.crear_documento(payload_a_rest(doc))
    except wasabil.WasabilError as e:
        dte.error = (str(e) + (f" · {e.detalle[:500]}" if e.detalle else ""))[:2000]
        if not e.ambiguo:
            # Creación confirmadamente fallida: se libera el claim y el estado deja de
            # decir PENDIENTE ("borrador en Wasabil"), que no es cierto — ver la nota
            # extensa en _emitir_en_wasabil.
            dte.en_vuelo_desde = None
            dte.status_id = STATUS_FALLIDO
        db.commit()
        _anotar_auditoria(db, dte, auditoria, tolerante=True)
        raise HTTPException(502, f"No se pudo emitir en Wasabil: {e}")
    # Mismo rescate del folio en la RE-emisión del reintento (por uuid o por FACT-<id>).
    data = _completar_documento_emitido(
        data, referencia_interna=_referencia_interna_factura(factura_id))
    _actualizar_desde_wasabil(db, dte, data)
    db.commit()
    _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    _anotar_auditoria(db, dte, auditoria, tolerante=True)
    return {**serialize_dte(dte), "factura_id": factura_id}
