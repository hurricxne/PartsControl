"""API del módulo Wasabil DTE de MonzaParts — guías de despacho electrónicas (SII 52).

Prefijo: /monza/wasabil (se monta con prefix=/api → /api/monza/wasabil, calzando
con el baseURL '/api/monza' del frontend). Solo MonzaParts (candado 'automotriz')
y bajo el gate MONZA_CONTAB_ENABLED en main.py: Wasabil emite con el RUT de
LOPEZ HERNANDEZ INVERSIONES SPA (78.121.316-0), jamás con el de Grupo AM.

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

Disciplina anti doble emisión (espejo EXACTO del módulo batalla-probado de GA,
wasabil_dte/router.py — folios reales 136/137):
  - La fila `monza_wasabil_dte` es el ancla: única por despacho (índice) y con un
    claim `en_vuelo_desde` que se marca BAJO LOCK antes de la llamada HTTP y
    bloquea a cualquier otro request mientras esté fresco (CLAIM_TTL_SEGUNDOS).
  - Los locks (SELECT ... FOR UPDATE) son SIEMPRE cortos y sin red adentro: las
    llamadas a Wasabil ocurren fuera de toda transacción con locks.
  - La máquina de estados es explícita (_estado_dte_bloquea): nada de filtrar
    mensajes por texto.

Adaptaciones Monza vs GA (decisión de arquitectura de la Fase 5):
  - No existe OcCliente: la COTIZACIÓN es la venta (N°/fecha de OC viven en
    MonzaCotizacion.oc_cliente / oc_fecha — columna Date desde F3, sin parseo).
  - Los precios de las líneas son el CONGELADO MonzaCotizacionItem.precio_unitario_clp
    (la foto al vender) — el MISMO que honra la factura Monza: guía y factura
    cuadran por construcción. Jamás un recálculo vivo.
  - La tasa de IVA es POR VENTA: iva_rate_de(cotización, MonzaConfig) — no la
    constante 0.19 de GA (una venta con iva_pct congelado distinto descuadraría).
  - El reintento por referencia usa match EXACTO puro (Monza no tiene documentos
    legados formato v1: nace en v2/v3).
"""
import json
import logging
from datetime import date, datetime
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User
from monza_models import (
    MonzaDespacho, MonzaDespachoItem, MonzaCotizacion, MonzaCotizacionItem,
    MonzaConfig,
)
# Lógica pura compartida con la contabilidad Monza: la MISMA resolución de tasa
# de IVA que usa la factura (iva_pct congelado en la venta → config → 0.19).
from monza_contabilidad.service import iva_rate_de

from . import client as wasabil
from .models import (
    MonzaWasabilDte, STATUS_EMITIDO, STATUS_FALLIDO, STATUS_PROCESANDO,
    STATUS_PENDIENTE,
)
from .service import (
    TIPO_DOC_GUIA, FOLIO_REF_MAX, TIPOS_TRASLADO, TIPO_TRASLADO_DEFAULT,
    armar_lineas, armar_guia, payload_a_rest, cuadratura, total_neto_lineas,
    serialize_dte, claim_vigente,
)

# Módulo SOLO MonzaParts ('automotriz'): Wasabil emite con el RUT de LOPEZ
# HERNANDEZ INVERSIONES SPA, por lo que usuarios de otra empresa quedan
# denegados (403) — mismo patrón que monza_contabilidad/router.py.
router = APIRouter(
    prefix="/monza/wasabil",
    tags=["monza-wasabil-dte"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

# Log del servidor: rastro PERMANENTE de las decisiones que un humano se hace cargo de
# tomar (ver _abortar_si_ya_hay_documento_emitido). `dte.error` no sirve para esto: se
# limpia cuando el documento queda emitido, que es justo el caso a auditar.
logger = logging.getLogger("monza_wasabil_dte")


# ─── Helpers ────────────────────────────────────────────────────────────────────
def _cargar_contexto(db: Session, despacho_id: int) -> Tuple[MonzaDespacho, list, MonzaCotizacion]:
    """Despacho + sus ítems + su cotización, con 404 claros. SIN locks (las
    validaciones con red van fuera de transacciones con locks).

    En Monza NO hay relación ORM despacho→items ni despacho→cotización: se
    cargan a mano (mismo patrón que monza_router_despachos)."""
    despacho = db.query(MonzaDespacho).filter(MonzaDespacho.id == despacho_id).first()
    if not despacho:
        raise HTTPException(404, "Despacho no encontrado")
    if not despacho.cotizacion_id:
        raise HTTPException(404, "El despacho no tiene venta (cotización) asociada")
    cot = (
        db.query(MonzaCotizacion)
        .filter(MonzaCotizacion.id == despacho.cotizacion_id)
        .first()
    )
    if not cot:
        raise HTTPException(404, "La venta (cotización) de este despacho no existe")
    items_despacho = (
        db.query(MonzaDespachoItem)
        .filter(MonzaDespachoItem.despacho_id == despacho.id)
        .all()
    )
    return despacho, items_despacho, cot


def _items_cotizacion_por_id(db: Session, items_despacho: list) -> dict:
    """{MonzaCotizacionItem.id: item} para el JOIN manual por item_id (en Monza no
    hay relación ORM despacho_item→cotizacion_item)."""
    ids = [di.item_id for di in items_despacho] or [0]
    return {
        it.id: it
        for it in db.query(MonzaCotizacionItem)
        .filter(MonzaCotizacionItem.id.in_(ids))
        .all()
    }


def _config(db: Session) -> Optional[MonzaConfig]:
    return db.query(MonzaConfig).order_by(MonzaConfig.id.asc()).first()


def _dte_de_despacho(db: Session, despacho_id: int, lock: bool = False) -> Optional[MonzaWasabilDte]:
    q = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.despacho_id == despacho_id,
                                         MonzaWasabilDte.tipo_dte == TIPO_DOC_GUIA)
    if lock:
        # populate_existing es OBLIGATORIO: sin él, si la fila ya está en el identity
        # map de la sesión, SQLAlchemy DESCARTA los valores frescos que devuelve el
        # SELECT ... FOR UPDATE y la re-validación vería datos viejos (p.ej. el claim
        # que otro request acaba de commitear) → doble emisión.
        q = q.populate_existing().with_for_update()
    return q.first()


def _estado_dte_bloquea(dte: Optional[MonzaWasabilDte], para_reintento: bool,
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
    Fase 6 para facturas 33, y en GA el usuario que reintentaba una FACTURA leía un
    mensaje que hablaba de despachos. La lógica es idéntica; cambia el sustantivo.
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
    despacho, items_despacho, cot = _cargar_contexto(db, despacho_id)
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
    numero_despacho = (despacho.numero or "").strip()
    if not numero_despacho:
        problemas.append("El despacho no tiene N° interno (DSP-AAAA-####): sin él no hay "
                         "ancla de referencia para la guía electrónica")

    # ── Guía electrónica previa (máquina de estados explícita) ──
    dte = _dte_de_despacho(db, despacho.id)
    problema_dte = _estado_dte_bloquea(dte, para_reintento)
    if problema_dte:
        problemas.append(problema_dte)
    if dte and dte.status_id == STATUS_FALLIDO and para_reintento:
        advertencias.append(f"Emisión anterior rechazada por el SII: {dte.error or 'sin detalle'}")

    # ── Datos del cliente / OC (lo que exige el SII) ──
    cliente = cot.cliente  # relación MonzaCotizacion.cliente (FK NOT NULL)
    rut = ((cliente.rut if cliente else None) or "").strip()
    if not rut:
        problemas.append("El cliente de la venta no tiene RUT: complétalo en la ficha del "
                         "cliente antes de emitir (el SII exige el RUT del receptor)")
    numero_oc = (cot.oc_cliente or "").strip()
    if not numero_oc:
        problemas.append("La venta no tiene N° de OC del cliente: la guía debe referenciarla "
                         "(tipo 801) — complétalo en el Cierre de Venta")
    elif len(numero_oc) > FOLIO_REF_MAX:
        # El SII limita el folio de una referencia a 18 caracteres; truncarlo cambiaría
        # la referencia legal a la OC del cliente, así que se BLOQUEA para que el
        # operador acorte el N° real (mejor detectarlo aquí que en el rechazo al emitir).
        problemas.append(
            f"El N° de OC del cliente ('{numero_oc}') tiene {len(numero_oc)} caracteres; "
            f"el SII permite máximo {FOLIO_REF_MAX} en la referencia. Acorta el N° de OC en la venta."
        )
    # oc_fecha es columna Date (F3): se usa directa, sin parseo — solo puede faltar.
    fecha_oc = cot.oc_fecha
    if not fecha_oc:
        problemas.append("La venta no tiene FECHA de OC del cliente: la referencia 801 "
                         "lleva la fecha real de la OC — complétala en el Cierre de Venta")

    # ── Líneas (cantidades del despacho × precio CONGELADO de la venta) ──
    lineas, problemas_lineas = armar_lineas(
        items_despacho, _items_cotizacion_por_id(db, items_despacho))
    problemas.extend(problemas_lineas)

    # ── Tasa de IVA POR VENTA (iva_pct congelado → config → 0.19) ──
    iva_rate = iva_rate_de(cot, _config(db))

    # ── Receptor: ficha del cliente en Wasabil (autocompleta datos ante el SII) ──
    receptor = {
        "rut": rut or None,
        "razon_social": (cliente.nombre if cliente else None) or None,
        "giro": None, "direccion": None, "comuna": None, "ciudad": None,
        "fuente": "cotizacion",
    }
    client_id = None
    if not wasabil.esta_configurado():
        problemas.append("Wasabil no está configurado (falta WASABIL_API_TOKEN_MONZA en "
                         "backend/.env): puedes previsualizar, pero no emitir")
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
                    "razon_social": cli.get("name") or cli.get("razon_social")
                    or (cliente.nombre if cliente else None),
                    "giro": cli.get("giro") or cli.get("activity"),
                    "direccion": cli.get("address") or cli.get("direccion"),
                    "comuna": cli.get("comuna") or cli.get("commune"),
                    "ciudad": cli.get("city") or cli.get("ciudad"),
                    "fuente": "wasabil",
                }
                # Asimetría deliberada guías vs facturas: la ficha incompleta en la
                # guía 52 es solo ADVERTENCIA (el SII puede rechazarla, se avisa);
                # la factura 33 (Fase B) sí bloqueará con receptor incompleto.
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

    neto, iva, total = cuadratura(total_neto_lineas(lineas), iva_rate) if lineas else (0, 0, 0)
    return {
        "despacho": despacho, "cot": cot, "dte": dte,
        "lineas": lineas, "fecha_oc": fecha_oc, "client_id": client_id,
        "receptor": receptor, "problemas": problemas, "advertencias": advertencias,
        "neto": neto, "iva": iva, "total": total, "iva_rate": iva_rate,
    }


def _reclamar_emision(db: Session, despacho_id: int, para_reintento: bool,
                      usuario_id: Optional[int], empresa: str) -> MonzaWasabilDte:
    """Transacción CORTA y bajo lock (sin red) que deja el claim anti doble emisión:

    0. rollback() ANTES del lock: _preparar_emision ya abrió la transacción
       (SELECTs + HTTP a Wasabil) y su snapshot es VIEJO — la re-validación debe
       nacer con el FOR UPDATE, no servir datos congelados de antes del HTTP.
    1. FOR UPDATE sobre el despacho (serializa claims del mismo despacho).
    2. FOR UPDATE sobre la fila monza_wasabil_dte (si existe) y RE-VALIDACIÓN del
       estado con datos frescos — lo que otro request haya hecho ya es visible aquí.
    3. Marca `en_vuelo_desde` (o crea la fila) y COMMITEA: el claim queda visible
       y los locks se liberan ANTES de cualquier llamada HTTP.
    """
    db.rollback()
    despacho = (
        db.query(MonzaDespacho)
        .filter(MonzaDespacho.id == despacho_id)
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
        dte = MonzaWasabilDte(
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
    curaba solo, porque el sondeo necesita uuid). Acá un valor vacío del enriquecido nunca
    pisa uno útil del original."""
    fusion = dict(base or {})
    for clave, valor in (extra or {}).items():
        if _vacio(valor) and not _vacio(fusion.get(clave)):
            continue  # el documento completo no sabe: se conserva lo que ya teníamos
        fusion[clave] = valor
    return fusion


# ─── La referencia interna: UNA sola definición para el rescate y para el cinturón ──
# El rescate (adopta un documento) y el cinturón (decide si se puede re-emitir) tienen que
# preguntar EXACTAMENTE lo mismo a la MISMA fuente: si una viera un documento que la otra
# no, el cinturón dejaría pasar justo lo que el rescate iba a adoptar.
def _coincide_referencia(doc: dict, referencia_interna: str) -> bool:
    """¿Este documento de Wasabil lleva ESTA referencia interna? Match EXACTO puro: Monza
    nace en formato v2/v3 y no tiene documentos legados que reencontrar con sufijos
    tolerantes (GA sí, por sus folios 136/137) — un `endswith` acá solo agregaría falsos
    positivos (DSP-0001 vs DSP-00012)."""
    return str(doc.get("invoice_reference") or "") == referencia_interna


def _status_de(doc: dict) -> int:
    """status_id del documento como int (0 = desconocido). Un dato ilegible NUNCA debe
    reventar la verificación: 0 no es EMITIDO, así que cae del lado conservador."""
    try:
        return int(doc.get("status_id") or 0)
    except (TypeError, ValueError):
        return 0


def _buscar_por_referencia(referencia_interna: str) -> Tuple[List[dict], bool]:
    """(documentos con esta referencia, búsqueda_completa) — SOLO LECTURA, jamás crea nada.

    `busqueda_completa=False` = la lista PUEDE estar truncada (paginación sin agotar): no
    haber visto un documento NO prueba que no exista. Deliberadamente NO se interpreta
    acá: el rescate y el cinturón necesitan decisiones distintas ante la MISMA respuesta
    (el rescate puede quedarse con el EMITIDO que sí vio; el cinturón, que necesita probar
    una AUSENCIA, no puede concluir nada de una lista truncada)."""
    documentos, busqueda_completa = wasabil.buscar_documentos(referencia_interna)
    coincidencias = [d for d in (documentos or [])
                     if isinstance(d, dict) and _coincide_referencia(d, referencia_interna)]
    return coincidencias, bool(busqueda_completa)


def _es_rechazo_confirmado(doc: dict) -> bool:
    """¿CONSTA que este documento no va a tener folio nunca?

    Es la única lectura que autoriza a re-emitir, así que se exige que el dato sea
    LEGIBLE. Ojo con reusar `_status_de`, que devuelve 0 ante un dato ilegible: 0 no es
    FALLIDO, pero tampoco es «consta que fue rechazado», y confundirlos es el agujero
    por el que se reprodujo doble emisión REAL con el listado sano.
    """
    try:
        return int(doc.get("status_id")) == STATUS_FALLIDO
    except (TypeError, ValueError):
        return False


def _clasificar_referencia(referencia_interna: str) -> Tuple[List[dict], List[dict], bool]:
    """(emitidos, los que TODAVÍA pueden quedarse con un folio, búsqueda_completa).

    La pregunta antes de re-emitir no es "¿hay un status 3?" sino "¿puedo PROBAR que no
    existe ningún documento capaz de quedarse con un folio?". Uno `procesando`, uno en
    cola o uno con un `status_id` ilegible todavía pueden convertirse en un DTE real:
    son «no se puede concluir», no «no hay nada». Es la misma lectura que el rescate ya
    hace (trata como vivo todo lo que no es EMITIDO ni FALLIDO): ambos preguntan a la
    misma fuente y tienen que leerla igual, o uno bloquea mientras el otro deja pasar.
    """
    coincidencias, completa = _buscar_por_referencia(referencia_interna)
    emitidos = [d for d in coincidencias if _status_de(d) == STATUS_EMITIDO]
    pueden = [d for d in coincidencias
              if _status_de(d) != STATUS_EMITIDO and not _es_rechazo_confirmado(d)]
    return emitidos, pueden, completa


def _folios_de(documentos: List[dict]) -> str:
    return ", ".join(str(d.get("folio") or "sin folio") for d in documentos)


# Clave interna (NO del API) con la que el rescate deja dicho que hay algo que un HUMANO
# tiene que mirar: `_actualizar_desde_wasabil` la conserva en `dte.error` incluso cuando el
# documento quedó EMITIDO (donde el error se limpia), porque es el único canal por el que
# el operador y el sondeo ven la ambigüedad. Bloquear sin dejar rastro es media defensa:
# la otra mitad del principio rector es PEDIR INTERVENCIÓN HUMANA, y para pedirla hay que
# decir qué pasó.
CLAVE_AVISO = "_aviso_rescate"


def _msg_rescate_ambiguo(referencia_interna: str, folios: List[str]) -> str:
    return (f"Wasabil tiene {len(folios)} documentos EMITIDOS con la MISMA referencia "
            f"interna '{referencia_interna}' (folios: {', '.join(folios)}). No se elige "
            "ninguno automáticamente: cualquiera de esos folios es un documento tributario "
            "REAL y quedarse con el otro perdería un folio para siempre. NO se emite nada "
            "nuevo. Revísalos en app.wasabil.com y pide soporte para dejar registrado el "
            "folio correcto (probablemente haya que anular uno con nota de crédito).")


def _rescatar_por_referencia(referencia_interna: str, *,
                             solo_emitido: bool) -> Optional[dict]:
    """Busca en Wasabil el documento cuyo `invoice_reference` sea la referencia interna.
    SOLO LECTURA: jamás crea nada.

    Es la ÚNICA vía de rescate cuando la respuesta no trajo uuid (sin uuid no hay a quién
    consultar por id).

    REGLA RECTORA: ante un documento tributario IRREVERSIBLE, un estado remoto ambiguo o
    contradictorio se BLOQUEA; no se "recupera con astucia". Por eso:

      · El estado NORMAL después de un reintento son DOS documentos con la MISMA
        referencia (el rechazado viejo + el nuevo): `_reclamar_emision` reusa la fila y
        Wasabil crea un documento nuevo cada vez. Quedarse con "el primero de la lista"
        adoptaba el VIEJO y DEGRADABA a 'fallida' la emisión que el SII sí aceptó —
        perdiendo su folio para siempre. Acá el EMITIDO manda siempre.
      · DOS documentos EMITIDOS con la misma referencia = doble emisión real ya ocurrida.
        Elegir uno sería inventar una verdad: se ABORTA con `ambiguo=True` para que el
        llamador falle CERRADO y un humano mire (una nota de crédito cuesta muchísimo más
        que un 409).
      · `solo_emitido=True` lo usa el rescate del folio (ya sabemos que NUESTRO documento
        salió emitido: un rechazado viejo no es una respuesta, es ruido). `False` lo usa
        el reintento, que además necesita saber si hay uno EN PROCESO para no re-crear.

    Una lista truncada por paginación se convierte en WasabilError: "no lo encontré" NO
    prueba que no exista, y el llamador tiene que abortar (nunca re-emitir a ciegas)."""
    coincidencias, busqueda_completa = _buscar_por_referencia(referencia_interna)
    emitidos = [d for d in coincidencias if _status_de(d) == STATUS_EMITIDO]
    if len(emitidos) > 1:
        # Los FOLIOS van en el mensaje a propósito: es el dato con el que un humano puede
        # ir a app.wasabil.com y resolverlo. Este texto termina en `dte.error` vía
        # CLAVE_AVISO — bloquear sin decir cuáles son los dos documentos deja al operador
        # sin nada que hacer (MEDIO-4 de la re-refutación).
        raise wasabil.WasabilError(
            _msg_rescate_ambiguo(referencia_interna,
                                 [str(d.get("folio") or "sin folio") for d in emitidos]),
            ambiguo=True)
    if emitidos:
        doc = emitidos[0]
        if doc.get("uuid"):
            # El documento COMPLETO garantiza folio + PDF/XML; la fusión no degrada.
            return _fusionar_respuesta(doc, wasabil.obtener_documento(doc["uuid"]) or {})
        return doc
    if not busqueda_completa:
        # Se comprueba ANTES de concluir cualquier "no existe": aplica a los dos modos.
        raise wasabil.WasabilError(
            "La búsqueda en Wasabil quedó incompleta (lista paginada): no se puede "
            "concluir que el documento no exista", ambiguo=True)
    if solo_emitido:
        return None
    en_proceso = [d for d in coincidencias
                  if _status_de(d) in (STATUS_PROCESANDO, STATUS_PENDIENTE, 0)]
    if len(en_proceso) > 1:
        raise wasabil.WasabilError(
            f"En Wasabil hay {len(en_proceso)} documentos EN PROCESO con la misma "
            f"referencia '{referencia_interna}': hay que resolverlos a mano antes de "
            "reintentar (re-emitir ahora podría triplicar el documento)", ambiguo=True)
    if en_proceso:
        return en_proceso[0]
    # Solo quedan RECHAZADOS (status 4). Varios es lo normal tras varios reintentos y
    # ninguno está vivo, así que se devuelve el primero para dejar constancia del
    # rechazo: no hay nada que degradar (nuestra fila está en PENDIENTE, sin folio).
    return coincidencias[0] if coincidencias else None


# ═══ CINTURÓN ANTI DOBLE EMISIÓN POR REFERENCIA ═════════════════════════════════════
# Nombre del parámetro con el que un humano se hace cargo cuando Wasabil NO permite
# concluir. Se declara acá (no en cada endpoint) porque el mensaje del 409 lo nombra: el
# texto y el parámetro tienen que ser el MISMO dato en un solo lugar.
PARAM_CONFIRMACION = "confirmo_sin_documento_emitido"

def _msg_sin_ancla(sustantivo: str) -> str:
    # `sustantivo` mantiene el texto en el idioma del documento (quien reintenta una
    # FACTURA no debe leer un mensaje sobre despachos), igual que el resto del módulo.
    return (f"No se puede verificar si ya existe un documento emitido por esta "
            f"{sustantivo} porque falta su referencia interna, que es el ancla anti doble "
            "emisión (para la guía, el N° de despacho DSP-AAAA-####): NO se re-emite. "
            "Complétala antes de reintentar.")


def _msg_ya_emitido(referencia_interna: str, sustantivo: str,
                    emitidos: List[dict]) -> str:
    return (f"Wasabil ya tiene {len(emitidos)} documento(s) EMITIDO(S) con la referencia "
            f"'{referencia_interna}' (folio(s): {_folios_de(emitidos)}): re-emitir la "
            f"{sustantivo} crearía un SEGUNDO documento tributario REAL por lo mismo, y eso "
            "no se deshace. NO se re-emite. Revísalo en app.wasabil.com y pide soporte para "
            "dejar registrado el folio que corresponde.")


def _msg_no_concluyente(referencia_interna: str, sustantivo: str, detalle: str) -> str:
    return (f"NO se pudo verificar en Wasabil si ya existe un documento EMITIDO con la "
            f"referencia '{referencia_interna}', así que NO se re-emite la {sustantivo}: "
            "re-emitir sin poder descartarlo arriesga un SEGUNDO documento tributario REAL "
            f"ante el SII, y eso no se deshace. Detalle: {detalle}. Qué hacer: busca "
            f"'{referencia_interna}' en app.wasabil.com; si CONFIRMAS que no existe ningún "
            f"documento emitido con esa referencia, el reintento se puede autorizar a mano "
            f"(parámetro {PARAM_CONFIRMACION}=true), y esa autorización queda registrada.")


def _referencia_interna_guia(db: Session, despacho_id: int) -> str:
    """Ancla anti doble emisión de la guía 52: el N° de despacho (formato v2, es el
    `invoiceReference` con el que sale el documento). Cadena vacía si el despacho no tiene
    N° — jamás se busca en Wasabil con `search=""`, que traería documentos ajenos."""
    despacho = db.query(MonzaDespacho).filter(MonzaDespacho.id == despacho_id).first()
    return (despacho.numero or "").strip() if despacho else ""


def _abortar_si_ya_hay_documento_emitido(referencia_interna: str, sustantivo: str, *,
                                         confirmado_por_humano: bool = False,
                                         usuario_id: Optional[int] = None) -> None:
    """Cinturón que se cruza JUSTO ANTES de re-emitir: la pregunta es por REFERENCIA, no
    por uuid — el uuid de la fila puede ser el del intento RECHAZADO mientras el documento
    bueno vive con OTRO uuid y la misma referencia (el estado normal tras un reintento son
    DOS documentos con la misma referencia). `estado_documento(uuid)` responde por ESE
    documento, no por la referencia: confirmaba "fallido" y se re-emitía → dos guías 52 /
    dos facturas 33 REALES por lo mismo (CRÍTICO-2 de la re-refutación, reproducido en
    MonzaParts con el listado SANO).

    FALLA CERRADO — los TRES desenlaces, que son tres, no dos:

      1. Consta que NO hay ningún documento emitido (búsqueda COMPLETA y sin emitidos)
         → se sigue: es el único caso en que re-emitir es seguro.
      2. Consta que SÍ hay → 409 nombrando el/los folio(s). ABSOLUTO: ninguna
         confirmación humana lo levanta (el daño ya está hecho y re-emitir lo triplica).
      3. NO SE PUEDE CONCLUIR (la consulta falló, o la lista vino truncada) → 409 pidiendo
         verificación humana. Un "no lo vi" NO es un "no existe".

    Por qué el 3 NO puede ser «best effort» (el agujero que esta ronda cerró en GA): el
    rescate y el cinturón preguntan a la MISMA fuente; si el rescate falla CERRADO (502) y
    el cinturón falla ABIERTO, el cinturón desaparece exactamente cuando más se necesita.
    Y `GET /documents` responde 405 en el API real (ver client.buscar_documentos), así que
    un cinturón best effort no bloquearía NUNCA en producción: un guard inerte es peor que
    ninguno, porque da confianza falsa.

    Y por qué existe la autorización manual: con el listado caído, el desenlace 3 dejaría
    el Reintentar de MonzaParts BLOQUEADO PARA SIEMPRE (y con él la factura del despacho,
    ver `_guia_no_referenciable`). La salida no puede ser que el guard se apague solo: es
    que una PERSONA mire app.wasabil.com y se haga cargo, que es precisamente lo que el
    principio rector pide. La confirmación:
      · nunca levanta el desenlace 2 (documento emitido PROBADO),
      · es explícita y por request (no hay estado que quede "autorizado"),
      · queda registrada en el log del servidor con el usuario y la referencia.

    NO repara la fila a propósito: el estado local (rechazado) y el remoto (emitido) se
    contradicen, y elegir uno automáticamente es exactamente lo que hay que evitar."""
    referencia = (referencia_interna or "").strip()
    if not referencia:
        # Sin ancla no hay pregunta posible: el lado seguro es no re-emitir. (Y jamás se
        # busca con search="": esa consulta devuelve documentos AJENOS.)
        raise HTTPException(409, _msg_sin_ancla(sustantivo))
    try:
        emitidos, pueden_tener_folio, busqueda_completa = _clasificar_referencia(referencia)
    except wasabil.WasabilError as e:
        if confirmado_por_humano:
            logger.warning(
                "REINTENTO AUTORIZADO A MANO (%s=true) por el usuario %s: no se pudo "
                "verificar en Wasabil si ya existe un documento emitido con la referencia "
                "%r (%s). La persona declara haberlo revisado en app.wasabil.com.",
                PARAM_CONFIRMACION, usuario_id, referencia, e)
            return
        raise HTTPException(409, _msg_no_concluyente(referencia, sustantivo, str(e)))
    if emitidos:
        # Desenlace 2: PROBADO. La confirmación humana no aplica ni se consulta.
        raise HTTPException(409, _msg_ya_emitido(referencia, sustantivo, emitidos))
    if not busqueda_completa:
        # Lista truncada por paginación: no vimos un emitido, pero tampoco pudimos
        # recorrer todo — el que falta puede ser justo el que tiene folio.
        if confirmado_por_humano:
            logger.warning(
                "REINTENTO AUTORIZADO A MANO (%s=true) por el usuario %s: la búsqueda de "
                "la referencia %r en Wasabil quedó INCOMPLETA (lista paginada). La persona "
                "declara haberlo revisado en app.wasabil.com.",
                PARAM_CONFIRMACION, usuario_id, referencia)
            return
        raise HTTPException(409, _msg_no_concluyente(
            referencia, sustantivo,
            "la búsqueda quedó incompleta (lista paginada), así que no se puede concluir "
            "que no exista"))
    if pueden_tener_folio:
        # Ni emitidos ni rechazos CONFIRMADOS: procesando, en cola, o con un status que no
        # se pudo leer. Todavía pueden quedarse con un folio REAL, así que re-emitir ahora
        # arriesga dos documentos tributarios por lo mismo. También es «no se puede
        # concluir», con la misma salida humana que la lista truncada.
        estados = sorted({str(d.get("status_id")) for d in pueden_tener_folio})
        detalle = (f"hay {len(pueden_tener_folio)} documento(s) con esta referencia que NO "
                   f"son un rechazo confirmado (estado: {', '.join(estados)}): todavía "
                   "pueden quedarse con un folio REAL")
        if confirmado_por_humano:
            logger.warning(
                "REINTENTO AUTORIZADO A MANO (%s=true) por el usuario %s: %s (referencia "
                "%r). La persona declara haberlo revisado en app.wasabil.com.",
                PARAM_CONFIRMACION, usuario_id, detalle, referencia)
            return
        raise HTTPException(409, _msg_no_concluyente(referencia, sustantivo, detalle))
    # Desenlace 1: consta que no hay nada emitido con esta referencia.


def _completar_documento_emitido(data: dict,
                                 referencia_interna: Optional[str] = None) -> dict:
    """Si la respuesta de Wasabil dice EMITIDO (3) pero NO trae folio, re-consulta el
    documento completo y devuelve la respuesta enriquecida.

    POR QUÉ (auditoría F1-F6, hallazgos #4 y #12): el POST /documents puede volver
    con {"uuid": ..., "status_id": 3} SIN la clave `folio` (forma que el README del
    módulo declara PENDIENTE de confirmar contra el API real). Sin esto, el DTE queda
    'emitido' con folio NULL y el estado es PERMANENTE: el sondeo no volvía a
    consultar, /reintentar responde 409 'ya está emitida' y el N° manual no se puede
    editar (guard anti-pisado). Consecuencias medidas: el despacho nunca recibe su N°
    de guía y la factura 33 salía al SII referenciando el N° TECLEADO a mano.

    La variante SIN uuid es el MISMO callejón y llega igual de fácil (una respuesta
    `{"status_id": 3}` pelada): ahí no hay documento que consultar por id, así que el
    rescate va por la REFERENCIA INTERNA — el ancla anti doble emisión (N° de despacho
    para la 52, FACT-<id> para la 33) pasa a ser también la llave de rescate. Sigue
    siendo SOLO LECTURA y solo acepta el documento EMITIDO (ver
    `_rescatar_por_referencia`: con dos emitidos ABORTA en vez de adivinar).

    Falla ABIERTO a propósito: un error de CONSULTA jamás debe convertirse en el
    fracaso de una emisión que SÍ salió (el documento ya existe ante el SII). Si la
    consulta falla se devuelve la respuesta original: la fila queda 'emitida sin folio',
    el sondeo la rescata después y — mientras tanto — `_guia_no_referenciable` BLOQUEA la
    factura 33 para que nunca cite el N° tecleado a mano.

    Fail open SÍ, pero NUNCA en silencio (MEDIO-4): la ambigüedad —DOS documentos EMITIDOS
    con la misma referencia, o una lista que no permite concluir— viaja como aviso en
    CLAVE_AVISO hasta `dte.error`, que es el único canal por el que el operador y el
    sondeo la ven. Bloquear/no-resolver sin decir qué pasó es media defensa: la otra mitad
    del principio rector es pedir intervención humana, y para pedirla hay que dejar rastro
    con los folios en cuestión."""
    try:
        if not (int(data.get("status_id") or 0) == STATUS_EMITIDO
                and _vacio(data.get("folio"))):
            return data
        if data.get("uuid"):
            return _fusionar_respuesta(data, wasabil.obtener_documento(data["uuid"]) or {})
        if referencia_interna and referencia_interna.strip():
            doc = _rescatar_por_referencia(referencia_interna.strip(), solo_emitido=True)
            if doc:
                return _fusionar_respuesta(data, doc)
    except wasabil.WasabilError as e:
        if e.ambiguo:
            return {**data, CLAVE_AVISO: str(e)}
    except (TypeError, ValueError):
        pass
    return data


# Mensaje ÚNICO del rescate que no pudo resolverse: el documento EXISTE ante el SII
# (Wasabil lo dio por emitido) y su folio no llegó. Fail closed: se bloquea y se pide
# intervención humana — re-emitir sería un SEGUNDO documento tributario REAL.
# `sustantivo` mantiene el texto en el idioma del documento (el operador de una FACTURA
# no debe leer un mensaje sobre despachos).
def _msg_rescate_sin_folio(dte: MonzaWasabilDte, sustantivo: str) -> str:
    return (f"El documento de {sustantivo} quedó EMITIDO en el SII pero su folio todavía "
            f"no llega desde Wasabil (uuid {dte.uuid or 'no registrado'}): NO se re-emite, "
            "porque sería un segundo documento tributario REAL. Consulta el estado en unos "
            "minutos; si el folio no aparece, búscalo en app.wasabil.com y pide soporte "
            "para registrarlo.")


def _actualizar_desde_wasabil(db: Session, dte: MonzaWasabilDte, data: dict) -> None:
    """Vuelca la respuesta de Wasabil en la fila (uuid/estado/folio/PDF).

    El folio SOLO se registra cuando el documento queda EMITIDO (status 3) — y en
    ese momento se copia a MonzaDespacho.numero_guia (única escritura de este
    módulo sobre una tabla existente). Con uuid conocido, el claim en_vuelo se
    libera: el uuid pasa a ser el candado.

    TRES PISOS, todos por la MISMA razón (regla rectora): un dato que ya CONFIRMA un
    documento tributario vivo no se degrada nunca, porque degradarlo habilita re-emitir.
    Un estado remoto que contradiga lo confirmado se anota en `error` (para que un humano
    lo mire) y se conserva lo confirmado:

      1. STATUS: un 3 ya confirmado no baja a 4/2/6. Camino real medido: la fila queda
         'emitida sin folio', el sondeo re-consulta y una respuesta 4 la marcaba FALLIDA
         → el botón Reintentar se habilitaba → SEGUNDA guía/factura REAL ante el SII.
      2. FOLIO: un folio ya registrado no se pisa con otro distinto (sería adoptar el
         documento de otra venta y despachar/facturar contra un folio ajeno).
      3. UUID: un uuid ya registrado no se pisa con otro distinto. En el flujo normal no
         puede pasar (`_reclamar_emision` limpia el uuid antes de re-emitir), así que si
         pasa es que estamos mirando OTRO documento."""
    # Los conflictos se juntan y se escriben AL FINAL: un `display_error` cualquiera de
    # la respuesta no debe tapar el aviso de que hay dos verdades en pugna.
    #
    # El aviso del rescate (CLAVE_AVISO) entra PRIMERO y por el mismo canal: así sobrevive
    # al `error = None` del documento emitido, que es exactamente el caso en que un humano
    # tiene que mirar (dos documentos EMITIDOS con la misma referencia). Ver
    # `_completar_documento_emitido`.
    aviso_rescate = str(data.get(CLAVE_AVISO)) if data.get(CLAVE_AVISO) else None
    conflictos: List[str] = [aviso_rescate] if aviso_rescate else []
    nuevo_uuid = data.get("uuid")
    if nuevo_uuid:
        if dte.uuid and str(dte.uuid) != str(nuevo_uuid):
            conflictos.append(
                f"CONFLICTO: esta fila ya tenía el uuid {dte.uuid} y Wasabil respondió "
                f"con {nuevo_uuid}. Se conserva el original: NO se re-emite y hay que "
                "revisarlo en app.wasabil.com.")
        else:
            dte.uuid = nuevo_uuid
            dte.en_vuelo_desde = None
    if data.get("status_id") is not None:
        nuevo_status = int(data["status_id"])
        if dte.status_id == STATUS_EMITIDO and nuevo_status != STATUS_EMITIDO:
            # PISO 1: el documento ya está EMITIDO ante el SII. Bajarlo abriría el
            # camino a re-emitirlo (segundo documento tributario REAL).
            conflictos.append(
                f"CONFLICTO: esta fila ya estaba EMITIDA (status 3) y Wasabil respondió "
                f"status {nuevo_status}. Se conserva el 3: NO se re-emite. Revísalo en "
                "app.wasabil.com y pide soporte.")
        else:
            dte.status_id = nuevo_status
    error_respuesta = data.get("display_error") or data.get("error")
    dte.respuesta_json = json.dumps(data, ensure_ascii=False, default=str)[:60000]

    if dte.status_id == STATUS_EMITIDO:
        folio_nuevo = str(data["folio"]) if data.get("folio") else None
        if folio_nuevo and dte.folio and dte.folio != folio_nuevo:
            # PISO 2: dos folios distintos para la misma fila = estamos mirando otro
            # documento. Se conserva el registrado y se pide intervención humana.
            conflictos.append(
                f"CONFLICTO: esta fila ya tenía el folio {dte.folio} y Wasabil respondió "
                f"con {folio_nuevo}. Se conserva el original: revísalo en app.wasabil.com "
                "antes de facturar.")
        elif folio_nuevo:
            dte.folio = folio_nuevo
        if data.get("document_pdf_url"):
            dte.pdf_url = data["document_pdf_url"]
        if data.get("document_xml_url"):
            dte.xml_url = data["document_xml_url"]
        # Emitido = sin error… salvo que haya un conflicto que un humano deba mirar.
        dte.error = " · ".join(conflictos)[:2000] if conflictos else None
        dte.en_vuelo_desde = None
        if dte.folio and dte.despacho_id:
            despacho = db.query(MonzaDespacho).filter(
                MonzaDespacho.id == dte.despacho_id).first()
            if despacho:
                despacho.numero_guia = dte.folio
    elif conflictos or error_respuesta:
        dte.error = " · ".join(conflictos + ([str(error_respuesta)] if error_respuesta
                                             else []))[:2000]


# ─────────────────────── SALIDA del callejón "emitido sin folio" ────────────────────────
# `_completar_documento_emitido` falla ABIERTO a propósito (un error de CONSULTA no debe
# convertirse en el fracaso de una emisión que SÍ salió), y su precio es esta fila:
# status 3 y folio NULL. Ese estado es PERMANENTE por diseño — el sondeo no lo repara solo,
# «Reintentar» responde 409 «ya está emitida» (correcto: re-emitir sería un SEGUNDO
# documento tributario REAL) y el N° manual no se puede editar (guard anti-pisado). Sin lo
# que sigue, la única salida era un UPDATE a mano en la base de datos.
#
# Nada de esto emite. Todo es SOLO LECTURA contra Wasabil + una escritura local.

def _folio_dte_valido(folio: str) -> bool:
    """¿Sirve como folio de un documento tributario del SII?

    MISMA regla que ya aplica `service.armar_referencias_*` al FolioRef de la 52: la
    reutiliza en vez de re-derivarla para que no puedan divergir. `isascii()` además de
    `isdigit()` no es paranoia: '٣'.isdigit() es True y no es un folio del SII.
    """
    f = (folio or "").strip()
    if not f or len(f) > FOLIO_REF_MAX:
        return False
    if not (f.isascii() and f.isdigit()):
        return False
    return int(f) > 0


def _folio_confirmado_por_wasabil(
        referencia_interna: str) -> Tuple[Optional[str], str, bool]:
    """(folio_maquina, motivo, contradice) — qué sabe Wasabil del folio de este documento.

    LA MÁQUINA MANDA CUANDO PUEDE CONCLUIR. El operador teclea lo que leyó en
    app.wasabil.com, pero si el sistema puede averiguarlo solo, su lectura gana: teclear un
    folio es exactamente donde se cuela un dedazo, y este dato termina dentro de un DTE.

    Los tres desenlaces son TRES, no dos — la misma disciplina de `_verificar_no_emitido`:

      · folio_maquina con valor  → CONSTA cuál es. Si no coincide con lo tecleado, el
        llamador rechaza: no se elige por cuenta propia entre dos folios.
      · contradice=True          → el estado remoto CONTRADICE lo que dice la fila local
        (aquí consta EMITIDO y allá no hay ningún emitido con esta referencia, o hay DOS).
        Registrar un folio sobre una contradicción puede tapar un documento duplicado, así
        que el llamador bloquea y pide que lo mire una persona.
      · folio_maquina=None y contradice=False → NO SE PUDO CONCLUIR (la consulta falló, la
        lista vino truncada, o el emitido no traía folio). Acá —y sólo acá— vale la
        declaración del operador, que para eso repitió el folio y firma con su usuario.

    Por qué el «no se pudo concluir» NO bloquea, al revés que en el cinturón: son preguntas
    distintas. El cinturón autoriza CREAR un documento nuevo ante el SII (irreversible), así
    que ante la duda no se emite. Esto sólo escribe un número en una fila local de un
    documento que YA existe, y bloquear aquí deja el callejón cerrado para siempre — que es
    el problema que se está resolviendo. El riesgo real (que el folio tecleado sea de otro
    documento) lo acotan las otras cuatro reglas de `_registrar_folio_a_mano`.
    """
    try:
        emitidos, _pueden, completa = _clasificar_referencia(referencia_interna)
    except wasabil.WasabilError as e:
        return None, f"no se pudo consultar Wasabil ({e})", False
    if len(emitidos) > 1:
        return None, (
            f"Wasabil tiene {len(emitidos)} documentos EMITIDOS con la referencia "
            f"'{referencia_interna}' (folios: {_folios_de(emitidos)}): son documentos "
            "tributarios REALES distintos y elegir uno sería inventar una verdad"), True
    if emitidos:
        folio_remoto = str(emitidos[0].get("folio") or "").strip()
        if folio_remoto:
            return folio_remoto, "confirmado por Wasabil", False
        return None, "Wasabil confirma el documento emitido pero tampoco trae su folio", False
    if not completa:
        return None, ("la búsqueda en Wasabil quedó incompleta (lista paginada), así que "
                      "no se pudo confirmar el folio"), False
    # Búsqueda COMPLETA y ni un emitido: aquí la fila dice EMITIDO. Dos verdades en pugna.
    return None, (
        f"aquí este documento consta EMITIDO, pero Wasabil no tiene NINGÚN documento "
        f"emitido con la referencia '{referencia_interna}'"), True


def _registrar_folio_a_mano(db: Session, dte: MonzaWasabilDte, folio: str,
                            confirmo_folio: str, sustantivo: str,
                            referencia_interna: str,
                            usuario_id: Optional[int]) -> dict:
    """Registra a mano el folio de un documento que YA está EMITIDO y llegó sin folio.

    Cinco reglas, todas fail-closed, y NINGUNA capaz de emitir:

      1. Sólo sobre el callejón EXACTO: status 3 y folio vacío. Cualquier otro estado es
         409 — nunca se pisa un folio ya registrado. Folio idéntico = idempotente (200),
         para que el doble clic no sea un error.
      2. El operador repite el folio en `confirmo_folio`: es la constancia de que lo leyó
         del documento y no lo dedujo.
      3. El folio tiene que ser un correlativo del SII (`_folio_dte_valido`).
      4. La máquina manda cuando puede concluir (`_folio_confirmado_por_wasabil`): folio
         remoto distinto del tecleado → 409; estado contradictorio → 409.
      5. Se escribe por el MISMO camino que la emisión (`_actualizar_desde_wasabil`), así el
         folio llega a `MonzaDespacho.numero_guia` igual que siempre y con los tres pisos
         puestos. El rastro (quién, cuándo, con qué origen) queda en `respuesta_json`.

    Devuelve `{"dte", "origen", "ya_estaba"}` y **NO commitea**: el llamador cierra la
    transacción. La factura necesita además `_finalizar_factura_emitida` (folio →
    `numero_factura`) DENTRO de la misma transacción, y partir el commit en dos dejaría la
    fila con folio y la factura sin número si el proceso muere entremedio.
    """
    if dte.status_id != STATUS_EMITIDO:
        raise HTTPException(
            409, f"Esta {sustantivo} no está EMITIDA (estado {dte.status_id}): registrar un "
                 "folio a mano sólo corresponde cuando el SII YA aceptó el documento y el "
                 "folio no llegó. Consulta el estado o usa Reintentar.")
    folio = (folio or "").strip()
    if not _vacio(dte.folio):
        if str(dte.folio).strip() == folio:
            # Idempotente: el doble clic (o el reintento del navegador) no es un error.
            return {"dte": dte, "origen": "ya estaba registrado", "ya_estaba": True}
        raise HTTPException(
            409, f"Esta {sustantivo} ya tiene registrado el folio {dte.folio}: no se pisa "
                 f"con {folio or '(vacío)'}. Son dos folios reales distintos — revísalo en "
                 "app.wasabil.com y pide soporte.")
    if (confirmo_folio or "").strip() != folio:
        raise HTTPException(
            400, "Repite EXACTAMENTE el mismo folio en el campo de confirmación: es la "
                 "constancia de que lo leíste del documento en app.wasabil.com.")
    if not _folio_dte_valido(folio):
        raise HTTPException(
            400, f"'{folio or '(vacío)'}' no sirve como folio de un documento tributario: "
                 f"el folio del SII es un correlativo numérico de hasta {FOLIO_REF_MAX} "
                 "dígitos.")
    # La consulta a Wasabil va SIN LOCKS (tiene red): regla de la casa. La transacción que
    # escribe viene después, es corta y no tiene red adentro.
    folio_maquina, motivo, contradice = _folio_confirmado_por_wasabil(referencia_interna)
    if contradice:
        raise HTTPException(
            409, f"No se registra el folio {folio}: {motivo}. Resuélvelo en "
                 "app.wasabil.com antes de tocar nada acá — registrar un folio sobre un "
                 "estado contradictorio puede tapar un documento duplicado.")
    if folio_maquina and folio_maquina != folio:
        raise HTTPException(
            409, f"Wasabil dice que el folio de esta {sustantivo} es {folio_maquina} y tú "
                 f"escribiste {folio}: no se elige por cuenta propia. Verifica cuál "
                 "corresponde en app.wasabil.com.")
    # Re-lectura BAJO LOCK con datos frescos: entre la consulta a Wasabil y esta escritura,
    # el sondeo o el registro de otra pestaña pueden haber puesto el folio. Sin esto, el
    # segundo en llegar pisaría lo que el primero acaba de escribir.
    db.rollback()
    fresca = (db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == dte.id)
              .populate_existing().with_for_update().first())
    # `claim_vigente` se re-evalúa AQUÍ además de en el endpoint: el llamador lo miró antes
    # de la consulta a Wasabil (que tiene red y tarda), y en esa ventana puede haber nacido
    # una emisión. Escribir un folio bajo los pies de un documento que está naciendo lo
    # dejaría con el folio de otro. En el flujo normal no debería ocurrir —emitir sobre un
    # DTE ya emitido lo frena `_estado_dte_bloquea`—, pero éste es el último punto en que
    # se puede comprobar con datos frescos y bajo lock, y cuesta una condición.
    if (fresca is None or fresca.status_id != STATUS_EMITIDO
            or not _vacio(fresca.folio) or claim_vigente(fresca)):
        db.rollback()
        raise HTTPException(
            409, "El estado del documento cambió mientras se verificaba en Wasabil (el "
                 "sondeo u otro usuario ya registró el folio, o empezó una emisión): "
                 "refresca la página y revísalo antes de volver a intentarlo.")
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
    logger.warning(
        "REGISTRO MANUAL DE FOLIO (%s): folio %s en la fila DTE %s (referencia %r) por el "
        "usuario %s — origen: %s", sustantivo, folio, dte.id, referencia_interna,
        usuario_id, origen)
    return {"dte": dte, "origen": origen, "ya_estaba": False}


def _emitir_en_wasabil(db: Session, ctx: dict, dte: MonzaWasabilDte,
                       tipo_traslado: int = TIPO_TRASLADO_DEFAULT) -> MonzaWasabilDte:
    """Llama a Wasabil con issue=true. El claim YA está commiteado y no hay locks:
    si la red muere después de crear el documento, el claim sigue bloqueando a
    otros mientras esté fresco, y el reintento verificará en Wasabil antes de
    re-crear (por uuid o por la referencia interna del despacho)."""
    doc = armar_guia(
        numero_oc=(ctx["cot"].oc_cliente or "").strip(),
        fecha_oc=ctx["fecha_oc"],
        numero_despacho=ctx["despacho"].numero,
        lineas=ctx["lineas"],
        client_id=ctx["client_id"],
        contacto=ctx["despacho"].destinatario,
        issue=True,  # ÚNICO punto del módulo con issue=True (tras el OK explícito del usuario)
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
            # liberar el claim para que el reintento quede disponible de inmediato
            dte.en_vuelo_desde = None
        # Ambiguo (timeout/5xx): el claim queda puesto y expira solo — mientras
        # tanto nadie puede duplicar, y el reintento posterior verifica en Wasabil
        db.commit()
        raise HTTPException(502, f"No se pudo emitir en Wasabil: {e}")

    # Hallazgos #4/#12: si la respuesta viene EMITIDA pero sin folio, se trae el
    # documento completo ANTES de dar la emisión por cerrada (si no, el despacho se
    # quedaría sin N° de guía y la factura referenciaría el N° tecleado a mano).
    # Sin uuid el rescate va por el N° de despacho, que es el invoiceReference de la guía.
    data = _completar_documento_emitido(
        data, referencia_interna=ctx["despacho"].numero)
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
    """Si Wasabil está configurado (token Monza presente). El frontend lo usa para
    avisar ANTES de que el usuario arme el despacho (no expone el token)."""
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
    if (ctx["fecha_oc"] and ctx["lineas"] and (ctx["cot"].oc_cliente or "").strip()
            and (ctx["despacho"].numero or "").strip()
            and len((ctx["cot"].oc_cliente or "").strip()) <= FOLIO_REF_MAX):
        doc_preview = armar_guia(
            numero_oc=(ctx["cot"].oc_cliente or "").strip(),
            fecha_oc=ctx["fecha_oc"],
            numero_despacho=ctx["despacho"].numero,
            lineas=ctx["lineas"],
            client_id=ctx["client_id"],
            contacto=ctx["despacho"].destinatario,
            issue=False,  # el preview JAMÁS emite
            tipo_traslado=tipo_traslado,
        )
    return {
        "puede_emitir": not ctx["problemas"],
        "problemas": ctx["problemas"],
        "advertencias": ctx["advertencias"],
        "receptor": ctx["receptor"],
        "lineas": ctx["lineas"],
        # iva_rate viaja al frontend: el modal pinta el % REAL de la venta
        # (iva_pct congelado), jamás un 19% hardcodeado.
        "totales": {"neto": ctx["neto"], "iva": ctx["iva"], "total": ctx["total"],
                    "iva_rate": ctx["iva_rate"]},
        "referencias": ([{"tipo": "801", "folio": ctx["cot"].oc_cliente,
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
        empresa=getattr(current_user, "empresa", None) or "automotriz",
    )
    dte = _emitir_en_wasabil(db, ctx, dte, tipo_traslado=tipo_traslado)
    return serialize_dte(dte)


@router.get("/despachos/estado-batch")
def estado_batch(
    ids: str = Query(..., description="IDs de despachos separados por coma"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Estado (solo BD, sin llamar a Wasabil) de los DTE de varios despachos —
    para pintar los badges de folio/PDF en las tarjetas sin N llamadas.

    OJO: declarado ANTES de /despachos/{despacho_id}/estado para que FastAPI no
    intente parsear 'estado-batch' como un despacho_id."""
    try:
        despacho_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids debe ser una lista de enteros separados por coma")
    if not despacho_ids:
        return {}
    if len(despacho_ids) > 200:
        raise HTTPException(400, "Máximo 200 despachos por consulta")
    dtes = (
        db.query(MonzaWasabilDte)
        .filter(MonzaWasabilDte.despacho_id.in_(despacho_ids),
                MonzaWasabilDte.tipo_dte == TIPO_DOC_GUIA)
        .all()
    )
    return {d.despacho_id: serialize_dte(d) for d in dtes}


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
    # Hallazgo #12: 'emitido SIN folio' TAMBIÉN se re-consulta. Antes la condición
    # frenaba en cuanto el status era 3, y una emisión que volvió sin folio quedaba en
    # callejón sin salida (el despacho nunca recibía su N° de guía y /reintentar
    # responde 409 'ya está emitida'). Con esto el sondeo se autocura solo.
    sin_folio_emitido = (dte.status_id == STATUS_EMITIDO and _vacio(dte.folio))
    if dte.uuid and (dte.status_id not in (STATUS_EMITIDO, STATUS_FALLIDO)
                     or sin_folio_emitido):
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
            # (200 con error_consulta, nunca 500 — el frontend sigue sondeando)
            return {**serialize_dte(dte), "error_consulta": str(e)}
    elif sin_folio_emitido and not dte.uuid:
        # Callejón SIN uuid: no hay a quién consultar por id, así que el sondeo se cura
        # por la REFERENCIA INTERNA (SOLO LECTURA, jamás re-emite). Si no se puede
        # resolver, la fila queda igual y `_guia_no_referenciable` sigue bloqueando la 33.
        try:
            desp_ref = db.query(MonzaDespacho).filter(
                MonzaDespacho.id == despacho_id).first()
            doc = _rescatar_por_referencia((desp_ref.numero or "").strip(),
                                           solo_emitido=True) if desp_ref else None
            if doc:
                _actualizar_desde_wasabil(db, dte, doc)
                db.commit()
                db.refresh(dte)
        except wasabil.WasabilError as e:
            return {**serialize_dte(dte), "error_consulta": str(e)}
    return serialize_dte(dte)


@router.post("/despachos/{despacho_id}/reintentar")
def reintentar_guia(
    despacho_id: int,
    tipo_traslado: int = Query(TIPO_TRASLADO_DEFAULT,
                               description="dispatchTypeCode del SII (ver TIPOS_TRASLADO)"),
    confirmo_sin_documento_emitido: bool = Query(
        False,
        description="SOLO para el caso en que Wasabil no permite verificar si ya existe "
                    "una guía emitida con la referencia de este despacho: declara que una "
                    "PERSONA lo revisó en app.wasabil.com y no existe. Nunca levanta el "
                    "bloqueo cuando el documento emitido está PROBADO. Queda en el log."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reintento SEGURO de una emisión fallida (o que no llegó a Wasabil).

    Anti doble emisión: (1) si hay uuid, consulta el estado real; (2) si no hay
    uuid, busca en Wasabil por la referencia interna (N° de despacho) por si el
    documento SÍ se creó y la respuesta se perdió. (3) CINTURÓN por REFERENCIA justo
    antes de lo irreversible: aunque el estado del uuid diga "fallido", Wasabil puede
    tener OTRO documento EMITIDO con la misma referencia. Si cualquiera de las tres
    verificaciones no puede CONCLUIR se ABORTA (nunca se re-crea a ciegas). Solo si el
    documento está confirmado fallido/inexistente se re-emite — reclamando el claim
    bajo lock.

    `tipo_traslado` se re-recibe: el reintento puede CORREGIR el tipo elegido
    (p.ej. se emitió como venta y era traslado interno, y el SII la rechazó).
    """
    _validar_tipo_traslado(tipo_traslado)
    dte = _dte_de_despacho(db, despacho_id)
    if not dte:
        raise HTTPException(404, "Este despacho no tiene emisión que reintentar")
    if claim_vigente(dte):
        raise HTTPException(409, "Hay una emisión EN CURSO para este despacho: "
                                 "espera unos minutos y consulta el estado")
    if dte.status_id == STATUS_EMITIDO:
        # El documento EXISTE ante el SII: este camino NUNCA re-emite. Con folio, el 409
        # de siempre. Sin folio era un callejón ("La guía ya está emitida (folio None)"):
        # ahora se intenta un rescate de SOLO LECTURA y, si no resuelve, se bloquea con el
        # remedio humano. Bloquear y pedir intervención es la salida correcta; re-emitir
        # sería una SEGUNDA guía tributaria real.
        if not _vacio(dte.folio):
            raise HTTPException(409, f"La guía ya está emitida (folio {dte.folio})")
        try:
            if dte.uuid:
                doc = wasabil.obtener_documento(dte.uuid)
            else:
                desp_ref = db.query(MonzaDespacho).filter(
                    MonzaDespacho.id == despacho_id).first()
                doc = _rescatar_por_referencia((desp_ref.numero or "").strip(),
                                               solo_emitido=True) if desp_ref else None
            if doc:
                _actualizar_desde_wasabil(db, dte, doc)
                db.commit()
                db.refresh(dte)
        except wasabil.WasabilError as e:
            raise HTTPException(409, f"{_msg_rescate_sin_folio(dte, 'este despacho')} "
                                     f"(detalle: {e})")
        if _vacio(dte.folio):
            # FAIL CLOSED: no se pudo confirmar el folio → se bloquea, no se re-emite.
            raise HTTPException(409, _msg_rescate_sin_folio(dte, "este despacho"))
        return serialize_dte(dte)

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
            if dte.status_id == STATUS_EMITIDO and _vacio(dte.folio):
                raise HTTPException(409, _msg_rescate_sin_folio(dte, "este despacho"))
            return serialize_dte(dte)
    else:
        despacho_prev, _items_prev, _cot_prev = _cargar_contexto(db, despacho_id)
        try:
            # Criterio ÚNICO del rescate (ver _rescatar_por_referencia): prefiere el
            # EMITIDO, ABORTA si hay más de uno con la misma referencia y nunca degrada.
            doc = _rescatar_por_referencia(despacho_prev.numero, solo_emitido=False)
        except wasabil.WasabilError as e:
            raise HTTPException(502, f"No se pudo verificar en Wasabil si el documento ya "
                                     f"existe; reintenta en unos minutos (no se re-crea a "
                                     f"ciegas). Detalle: {e}")
        if doc:
            _actualizar_desde_wasabil(db, dte, doc)
            db.commit()
            db.refresh(dte)
            if dte.status_id == STATUS_EMITIDO and _vacio(dte.folio):
                raise HTTPException(409, _msg_rescate_sin_folio(dte, "este despacho"))
            return serialize_dte(dte)

    # 3) CINTURÓN ANTI DOBLE EMISIÓN POR REFERENCIA, justo antes de lo irreversible.
    # El uuid que tenemos puede ser el del intento RECHAZADO mientras Wasabil conserva OTRO
    # documento EMITIDO con la MISMA referencia (el estado normal tras un reintento son dos
    # documentos con la misma referencia). `estado_documento(uuid)` responde por ESE
    # documento, no por la referencia: confirmaba "fallido" y se re-emitía → dos guías 52
    # REALES por la misma mercadería. Falla CERRADO (ver la función): «no pude verificar»
    # nunca significa «sigamos».
    _abortar_si_ya_hay_documento_emitido(
        _referencia_interna_guia(db, despacho_id), "guía",
        confirmado_por_humano=confirmo_sin_documento_emitido,
        usuario_id=getattr(current_user, "id", None))

    # 4) Documento confirmado fallido (o inexistente): revalidar y re-emitir.
    ctx = _preparar_emision(db, despacho_id, para_reintento=True)
    if ctx["problemas"]:
        raise HTTPException(409, " · ".join(ctx["problemas"]))
    dte = _reclamar_emision(
        db, despacho_id, para_reintento=True,
        usuario_id=getattr(current_user, "id", None),
        empresa=getattr(current_user, "empresa", None) or "automotriz",
    )
    dte = _emitir_en_wasabil(db, ctx, dte, tipo_traslado=tipo_traslado)
    return serialize_dte(dte)


# ═══════════════════════════════════════════════════════════════════════════════
# FASE 6 — FACTURAS ELECTRÓNICAS (DTE 33)
#
# Mismo protocolo que las guías (preview → emitir con OK explícito → sondeo →
# reintento seguro) y la MISMA ancla anti doble emisión (fila monza_wasabil_dte,
# ahora única por factura_id).
#
# La diferencia ESTRUCTURAL con la guía: la factura LOCAL se crea PRIMERO (sin
# folio — numero_factura queda NULL) reutilizando la maquinaria de Contabilidad
# Monza (_construir_factura / _persistir_factura, las MISMAS que usa el registro
# manual), y el folio del SII se escribe recién al confirmarse la emisión. La
# aplicación del adelanto como cobranza se DIFIERE hasta ese momento: una factura
# que el SII rechaza no debe haber movido plata.
#
# Adaptaciones vs la Fase B de Grupo AM:
#   · No existe OcCliente: el punto de serialización de la venta es la COTIZACIÓN,
#     así que el candado de intención es por cotización (_emision_33_en_vuelo_de_cot)
#     y el orden global de locks es cotización → factura → adelanto.
#   · Existe 'Retiro en oficina' (sin_guia), que NO tiene guía: la factura 33 es
#     el documento que ampara el traslado y va con la sola referencia 801.
#
# FASE 7 — FACTURA DE ANTICIPO (vía B). El cliente paga un adelanto antes de que
# llegue la mercadería y se le emite un DTE 33 SIN guía por ese monto:
#   · `payload.es_anticipo` rutea a _construir_factura_anticipo (Contabilidad) en
#     el preview Y en el emitir; el resto del protocolo es idéntico.
#   · La factura de anticipo NUNCA lleva referencia 52 (no ampara traslado) ni 33;
#     sí lleva la 801 de la venta.
#   · La factura del despacho real referencia con tipo 33 cada anticipo que
#     descuenta, y su línea negativa viaja como `discount` porcentual (el API
#     rechaza price<0) — ver aplicar_descuento_lineas en service.py.
#   · Un anticipo SIN folio del SII BLOQUEA la factura del despacho: se referencia
#     con el placeholder "#<id>" y armar_referencias_factura lo rechaza.
# ═══════════════════════════════════════════════════════════════════════════════
# Los imports cruzados van AL FINAL del archivo (patrón heredado de GA): puestos
# arriba crearían un ciclo contabilidad ↔ wasabil que reventaría el arranque de
# main.py. Simétricamente, monza_contabilidad importa este paquete SIEMPRE dentro
# de funciones, nunca a nivel de módulo.
from monza_contabilidad.schemas import FacturaCreate  # noqa: E402
from monza_contabilidad.models import MonzaContFacturaCliente  # noqa: E402
from monza_contabilidad.router import (  # noqa: E402
    _cargar_venta, _construir_factura, _construir_factura_anticipo, _persistir_factura,
    _aplicar_adelantos_pendientes, _cobranzas_bloqueadas,
)
# _parse_date: MISMO parseo tolerante que usa Contabilidad Monza para `fecha_emision`,
# para que el guard mida contra la fecha que realmente se va a persistir.
from monza_contabilidad.service import _recompute_factura, _parse_date  # noqa: E402
from .service import (  # noqa: E402
    TIPO_DOC_FACTURA, NETO_MINIMO_DTE, armar_factura, armar_lineas_factura,
    armar_referencias_factura, hoy_chile,
)

# Sustantivos para los mensajes de _estado_dte_bloquea en el camino de facturas
# (la misma máquina de estados, pero el usuario que reintenta una FACTURA no debe
# leer un mensaje que habla de despachos).
_SUST_FACTURA = "esta factura"
_DOC_FACTURA = "factura electrónica emitida"


def _dte_de_factura(db: Session, factura_id: int, lock: bool = False) -> Optional[MonzaWasabilDte]:
    """Fila DTE de la factura. Se filtra por tipo_dte 33 (a diferencia de GA, que
    solo filtra por factura_id): el día que una factura tenga además una nota de
    crédito electrónica (61), esta consulta seguiría devolviendo la factura correcta.
    Falla CERRADO: si por alguna razón hubiera otra fila con ese factura_id, el
    INSERT del claim choca con el UNIQUE y responde 409 en vez de duplicar."""
    q = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.factura_id == factura_id,
                                         MonzaWasabilDte.tipo_dte == TIPO_DOC_FACTURA)
    if lock:
        # populate_existing: ver la nota en _dte_de_despacho (sin él se re-validaría
        # con los datos viejos del identity map y no con los del SELECT ... FOR UPDATE).
        q = q.populate_existing().with_for_update()
    return q.first()


def _referencia_interna_factura(factura_id: int) -> str:
    """Ancla anti doble emisión y de RECUPERACIÓN de la factura: única por factura
    local. El reintento sin uuid busca esta cadena en Wasabil con match EXACTO, así
    que NO puede colisionar con el ancla de las guías (que es el N° de despacho
    DSP-AAAA-####). Wasabil imprime este campo: por eso no lleva la OC adentro (la
    referencia legal a la OC es la 801)."""
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


# MEDIO-5: el ÚNICO motivo que NO habla de "EN PROCESO", porque el estado es otro — el SII
# rechazó, pero el documento EXISTE en Wasabil. Va aparte a propósito: decirle "en proceso"
# a un rechazo sería mentirle al operador y el remedio es distinto.
def _msg_guia_rechazada_con_documento(dte: MonzaWasabilDte) -> str:
    return (f"El SII RECHAZÓ la guía electrónica de este despacho, pero el documento existe "
            f"en Wasabil (uuid {dte.uuid}): el N° de guía tecleado a mano ya NO se acepta "
            "como referencia 52. Este despacho pasó por guía electrónica, así que el único "
            "N° válido es el folio del SII, y un rechazo de ESE documento no prueba que "
            "Wasabil no tenga otro EMITIDO con la misma referencia. Usa Reintentar (antes "
            "de re-emitir, el sistema verifica en Wasabil si ya hay una guía emitida por "
            "este despacho); si la mercadería salió con guía en PAPEL, pide soporte para "
            "registrar su folio.")


def _guia_no_referenciable(db: Session, despacho_id: int) -> Optional[str]:
    """Motivo por el que el N° de guía de este despacho NO se puede usar como referencia
    52 de una factura, o None si sí se puede. Fuente ÚNICA del criterio Y del mensaje.

    En la ventana en que el folio del SII no ha llegado, `despacho.numero_guia` conserva
    el N° manual VIEJO — este módulo lo pisa recién al confirmarse la emisión —, así que
    caer al fallback manual haría salir una factura 33 REAL referenciando un folio 52 que
    el SII no reconoce. Eso no se deshace (solo con nota de crédito): se BLOQUEA.

    Los TRES estados que bloquean (regla rectora: ante un estado remoto ambiguo o
    contradictorio se bloquea y se pide intervención humana):

      · EN PROCESO / BORRADOR: claim de emisión vigente, o uuid conocido con el
        documento todavía sin resolver (status 2/6/desconocido).
      · EMITIDA pero SIN folio: Wasabil puede responder al POST con status 3 y sin folio,
        CON o SIN uuid. El criterio viejo (`status == 3 and folio` → False, y después
        `bool(uuid) or claim_vigente`) dejaba pasar la variante SIN uuid: la 33 salía al
        SII citando el N° TECLEADO a mano. Ahora se exige **estado emitido Y folio no
        vacío**, sin mirar el uuid: ese estado nunca es uno en que el N° manual sea
        legítimo. Queda alineado con `_guia_sii_en_proceso` (monza_router_despachos.py),
        que ya exigía folio no vacío: antes se contradecían.
      · AMBIGUA: `en_vuelo_desde` puesto y uuid NULL — el POST salió y NADIE confirmó el
        resultado (timeout/5xx: `_emitir_en_wasabil` conserva el timestamp a propósito, y
        una respuesta status 4 sin uuid tampoco lo libera). El documento PUDO nacer con
        folio real. Se evalúa ANTES del corte por FALLIDO: con el orden viejo de GA
        (…→ FALLIDO → en_vuelo) el `status == 4` disparaba primero y la guía ambigua NO
        bloqueaba, que es justo el estado que dejan los caminos de rescate.

      · RECHAZADA pero con el documento EXISTIENDO en Wasabil (status 4 CON uuid) —
        MEDIO-5. Es el estado que el CRÍTICO-2 explota: el rechazo es de ESE documento y no
        prueba que Wasabil no conserve OTRO EMITIDO con la MISMA referencia (después de un
        reintento hay dos documentos por referencia). Además, en el flujo guía-primero de
        Monza el `numero_guia` que sobrevive a un intento electrónico es el VIEJO tecleado a
        mano — el que la emisión iba a pisar. Antes esto NO bloqueaba y la 33 salía citando
        ese N°; el remedio es Reintentar (que ahora cruza el cinturón por referencia) o
        registrar el folio de la guía de papel con soporte.

    Direccionalidad deliberada (control anti sobre-bloqueo): el intento que NUNCA llegó a
    Wasabil (sin uuid y con el claim ya liberado) NO bloquea — ahí consta que no existe
    documento electrónico alguno y el N° manual es la referencia legítima. Igual que un
    despacho sin fila DTE: la guía en papel de toda la vida sigue funcionando."""
    dte = (db.query(MonzaWasabilDte)
           .filter(MonzaWasabilDte.despacho_id == despacho_id,
                   MonzaWasabilDte.tipo_dte == TIPO_DOC_GUIA)
           .first())
    if dte is None:
        return None
    if dte.status_id == STATUS_EMITIDO:
        return None if not _vacio(dte.folio) else _MSG_GUIA_SIN_FOLIO
    if claim_vigente(dte):
        return _MSG_GUIA_EN_PROCESO
    if dte.en_vuelo_desde is not None and dte.uuid is None:
        return _MSG_GUIA_AMBIGUA
    if dte.status_id == STATUS_FALLIDO:
        # MEDIO-5: el rechazo CON uuid no habilita el N° tecleado a mano (ver arriba). Sin
        # uuid nunca nació documento: ahí sí, el N° manual es lo único que hay y es válido.
        return _msg_guia_rechazada_con_documento(dte) if dte.uuid else None
    if dte.uuid:
        return _MSG_GUIA_EN_PROCESO
    return None  # intento que nunca llegó a Wasabil: no hay guía electrónica


def _guia_electronica_en_proceso(db: Session, despacho_id: int) -> bool:
    """¿El folio del SII de la guía de este despacho TODAVÍA no está disponible? (bool
    sobre `_guia_no_referenciable`, que es donde vive el criterio y el mensaje).

    Lo consume además `monza_contabilidad._guia_sii_en_proceso` (aviso del selector de
    guías): esta firma es contrato con ese módulo."""
    return _guia_no_referenciable(db, despacho_id) is not None


def _fecha_guia_papel(desp) -> Tuple[Optional[date], Optional[str]]:
    """(fecha, problema) de EMISIÓN de una guía EN PAPEL, para el FchRef de la ref 52.

    Fuente ÚNICA: `monza_despachos.fecha_guia`, que teclea el operador en Despachos →
    Editar. Espejo de wasabil_dte._fecha_guia_papel de MachParts: MISMA regla, código
    SEPARADO — los dos módulos SII son independientes por marca a propósito (MachParts ya
    emite documentos reales; parametrizar uno solo pondría en riesgo al otro).

    POR QUÉ BLOQUEA SI FALTA
    Antes la fecha se sacaba de `fecha_despacho`, que no es la fecha de la guía sino el
    instante en que se cerró el despacho en el sistema. Cuando la guía se emite en el
    portal del SII un día y el despacho se cierra otro (lo habitual), el DTE 33 salía REAL
    citando la guía con una fecha que esa guía no tiene, y un documento emitido no se
    corrige. Sin el dato real se BLOQUEA en vez de sustituirlo por uno parecido.

    Sólo aplica a la guía en PAPEL: con guía ELECTRÓNICA la fecha sale del `documentDate`
    del propio DTE 52 y este helper no se llama.
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


def _referencia_guia_de_despacho(
        db: Session, despacho_id: Optional[int]
) -> Tuple[Optional[str], Optional[date], Optional[str]]:
    """(folio, fecha, problema) de la guía a referenciar (tipo 52) en la factura.

    Preferencia: guía ELECTRÓNICA emitida del despacho (folio del SII + fecha
    tributaria del payload, con created_at de respaldo) → guía EN PAPEL
    (despacho.numero_guia + despacho.fecha_guia, ver _fecha_guia_papel) → problema
    bloqueante.

    El folio SALE DE LA FILA DTE, jamás del snapshot `factura.numero_guia`: ese
    snapshot se congela al crear la factura y puede tener el N° tecleado viejo.

    En Monza no hay relación ORM factura→despacho (despacho_id es un snapshot sin
    FK), así que el despacho se consulta a mano."""
    if not despacho_id:
        return None, None, None
    motivo_guia = _guia_no_referenciable(db, despacho_id)
    if motivo_guia:
        # Fuente ÚNICA del texto: preview, emisión y reintento leen el MISMO mensaje, y
        # cada estado bloqueante nombra su propio remedio (ver _guia_no_referenciable).
        return None, None, motivo_guia
    dte_guia = (db.query(MonzaWasabilDte)
                .filter(MonzaWasabilDte.despacho_id == despacho_id,
                        MonzaWasabilDte.tipo_dte == TIPO_DOC_GUIA,
                        MonzaWasabilDte.status_id == STATUS_EMITIDO)
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
        return dte_guia.folio, fecha, None
    desp = db.query(MonzaDespacho).filter(MonzaDespacho.id == despacho_id).first()
    if desp and (desp.numero_guia or "").strip():
        fecha, problema_fecha = _fecha_guia_papel(desp)
        if problema_fecha:
            return None, None, problema_fecha
        return desp.numero_guia.strip(), fecha, None
    return None, None, (
        "La guía de este despacho no tiene folio registrado: emite la guía electrónica "
        "(o registra el N° manual) antes de facturarla")


def _lineas_fuera_de_la_guia(db: Session, despacho_id: Optional[int],
                             lineas) -> Tuple[List[int], int]:
    """¿Hay líneas de mercadería que NO estén amparadas por la guía que se va a citar
    como referencia 52? Devuelve (despacho_item_id ajenos ordenados, N° de líneas de
    mercadería sin declarar su despacho_item).

    POR QUÉ: `_construir_factura` (monza_contabilidad) valida el `despacho_item_id` de la
    línea contra TODOS los despacho_items de la VENTA — `di_by_id` se arma con
    `_despacho_items_de_cot` — y solo comprueba que el ítem calce
    (`di.item_id != ln.item_cotizacion_id`), NUNCA que el despacho_item pertenezca al
    despacho cuya guía se referencia. El tope físico por ÍTEM también suma TODOS los
    despachos de la venta. Resultado reproducido: mercadería que salió en la guía B,
    facturada con la 52 de la guía A. El invariante «una 33 nunca sale sin su 52 válida»
    se cumplía en la forma y se violaba en el fondo.

    DOS agujeros, no uno (la versión que solo mira los ids DECLARADOS es vacua, porque
    `despacho_item_id` es opcional y basta omitirlo para vaciar el guard):
      1. ids declarados que pertenecen a OTRO despacho.
      2. líneas de mercadería SIN declarar despacho_item: no hay forma de probar que
         salieron en esta guía, y el tope por ítem las deja pasar con la cantidad de
         cualquier otro despacho. En la vía electrónica se EXIGE la declaración.

    Fail closed. Quedan fuera, a propósito: las líneas de DESCUENTO por anticipo
    (`anticipo_factura_id`, no son mercadería) y las líneas sin `item_cotizacion_id`
    (la línea única "ANTICIPO"). Sin `despacho_id` no hay 52 que citar — 'Retiro en
    oficina' y la factura de anticipo — y el guard no aplica."""
    if not despacho_id:
        return [], 0
    ids: set = set()
    sin_declarar = 0
    for ln in (lineas or []):
        if getattr(ln, "anticipo_factura_id", None):
            continue  # línea de descuento: no ampara traslado de mercadería
        if getattr(ln, "item_cotizacion_id", None) is None:
            continue  # línea "ANTICIPO" / glosa: no es mercadería
        di = getattr(ln, "despacho_item_id", None)
        if di is None:
            sin_declarar += 1
        else:
            ids.add(int(di))
    if not ids:
        return [], sin_declarar
    propios = {fila[0] for fila in db.query(MonzaDespachoItem.id).filter(
        MonzaDespachoItem.despacho_id == despacho_id,
        MonzaDespachoItem.id.in_(ids)).all()}
    return sorted(ids - propios), sin_declarar


def _problema_lineas_fuera_de_la_guia(db: Session, despacho_id: Optional[int],
                                      lineas) -> Optional[str]:
    """Mensaje bloqueante único de `_lineas_fuera_de_la_guia` (o None si todo cuadra).
    Fuente ÚNICA del texto: lo usan el preview/emisión y el armado del payload del
    reintento."""
    ajenos, sin_declarar = _lineas_fuera_de_la_guia(db, despacho_id, lineas)
    if not ajenos and not sin_declarar:
        return None
    partes = []
    if ajenos:
        partes.append(f"hay líneas que salieron en OTRA guía (despacho_item "
                      f"{', '.join(str(i) for i in ajenos)})")
    if sin_declarar:
        partes.append(f"{sin_declarar} línea(s) no declaran de qué guía salieron")
    return ("La factura referenciaría la guía de este despacho (tipo 52) pero "
            + " y ".join(partes)
            + ". Emitir así ampararía mercadería con una guía que no la trasladó, y eso "
              "no se deshace: factura cada guía por separado desde su propio despacho.")


def _receptor_factura(db: Session, rut: str, razon_social_local: Optional[str],
                      problemas: List[str], advertencias: List[str]):
    """Resuelve la ficha del cliente en Wasabil (client_id). La factura 33 es MÁS
    ESTRICTA que la guía 52: ficha inexistente o SIN giro/dirección/comuna BLOQUEA
    (en la guía era solo advertencia). El SII exige receptor completo en el 33 y
    emitirlo incompleto termina en rechazo — con el documento ya consumido."""
    receptor = {"rut": rut or None, "razon_social": razon_social_local,
                "giro": None, "direccion": None, "comuna": None, "ciudad": None,
                "fuente": "local"}
    client_id = None
    if not wasabil.esta_configurado():
        problemas.append("Wasabil no está configurado (falta WASABIL_API_TOKEN_MONZA en "
                         "backend/.env): puedes previsualizar, pero no emitir")
        return receptor, client_id
    if not rut:
        return receptor, client_id  # el RUT faltante/ inválido ya lo reportó _construir_factura
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


def _anticipos_referenciados(db: Session, factura) -> List[dict]:
    """[{folio, fecha}] de las facturas de ANTICIPO descontadas en ESTA factura
    (sus líneas negativas con anticipo_factura_id) — para las referencias tipo 33.

    Camino de la factura YA PERSISTIDA (emisión desde la factura congelada y
    reintento). El folio se lee EN VIVO de la factura de anticipo: si todavía no lo
    tiene (su emisión electrónica está en curso o falló), viaja el placeholder
    "#<id>" y armar_referencias_factura BLOQUEA — nunca se emite un 33 que descuenta
    un anticipo sin respaldo referenciable."""
    ids = {it.anticipo_factura_id for it in factura.items if it.anticipo_factura_id}
    if not ids:
        return []
    anticipos = (db.query(MonzaContFacturaCliente)
                 .filter(MonzaContFacturaCliente.id.in_(ids))
                 .order_by(MonzaContFacturaCliente.id.asc()).all())
    return [{"folio": (fa.numero_factura or "").strip() or f"#{fa.id}",
             "fecha": fa.fecha_emision} for fa in anticipos]


def _anticipos_de_descuentos(db: Session, descuentos: List[dict]) -> List[dict]:
    """Igual que _anticipos_referenciados pero para una factura que TODAVÍA NO existe
    (preview y emisión de una factura nueva): la fuente son los `descuentos` que
    calculó _construir_factura. Misma forma de salida y mismo placeholder "#<id>",
    para que el preview bloquee EXACTAMENTE lo mismo que bloqueará la emisión."""
    ids = [d["anticipo_factura_id"] for d in (descuentos or [])
           if d.get("anticipo_factura_id")]
    if not ids:
        return []
    por_id = {fa.id: fa for fa in db.query(MonzaContFacturaCliente)
              .filter(MonzaContFacturaCliente.id.in_(ids)).all()}
    salida = []
    for anticipo_id in ids:
        fa = por_id.get(anticipo_id)
        salida.append({"folio": ((fa.numero_factura or "").strip() if fa else "") or f"#{anticipo_id}",
                       "fecha": fa.fecha_emision if fa else None})
    return salida


def _referencias_de_venta(db: Session, cot, *, sin_guia: bool, despacho_id: Optional[int],
                          problemas: List[str], advertencias: List[str],
                          anticipos: Optional[List[dict]] = None,
                          es_anticipo: bool = False,
                          fecha_documento: Optional[date] = None):
    """Referencias 801 (+ 52 + 33) de una factura de esta venta. Única fuente de verdad
    de la matriz de referencias: la usan el preview/emitir (desde el payload) y el
    armado del documento (desde la factura persistida), para que el reintento arme
    EXACTAMENTE lo mismo que se validó."""
    guia_folio, guia_fecha = None, None
    if es_anticipo:
        # FACTURA DE ANTICIPO: rama EXPLÍCITA y primera (gana sobre sin_guia, que
        # también es True aquí porque no hay despacho). No lleva 52 —no ampara ningún
        # traslado, respalda plata recibida por adelantado— ni 33 —no descuenta nada—:
        # sale con la sola referencia 801 de la venta.
        advertencias.append("Factura de anticipo: no lleva referencia a guía de despacho "
                            "(tipo 52) porque no ampara traslado de mercadería")
        anticipos = None
    elif sin_guia:
        # RETIRO EN OFICINA: rama EXPLÍCITA (no un efecto colateral de "no hay
        # despacho"). Sin guía no hay referencia 52 y la factura misma ampara el
        # traslado — es un modo legítimo de Monza que GA no tiene.
        advertencias.append("Retiro en oficina: la factura no lleva referencia a guía de "
                            "despacho (tipo 52); la factura ampara el traslado")
    elif despacho_id:
        guia_folio, guia_fecha, problema_guia = _referencia_guia_de_despacho(db, despacho_id)
        if problema_guia:
            problemas.append(problema_guia)
    referencias, problemas_ref = armar_referencias_factura(
        numero_oc=(getattr(cot, "oc_cliente", None) or "").strip() if cot else "",
        # oc_fecha es columna Date desde la Fase 3: se usa DIRECTA (sin parseo tolerante).
        fecha_oc=getattr(cot, "oc_fecha", None) if cot else None,
        guia_folio=guia_folio, guia_fecha=guia_fecha, anticipos=anticipos,
        fecha_documento=fecha_documento)
    problemas.extend(problemas_ref)
    return referencias


def _preparar_emision_factura(db: Session, payload: FacturaCreate) -> dict:
    """Arma y valida TODO para emitir una factura NUEVA (SIN persistir y SIN locks;
    puede llamar a Wasabil para la ficha del cliente). Es la única fuente de verdad
    de la validación del preview — la emisión RE-VALIDA bajo lock con las mismas
    funciones de Contabilidad."""
    problemas: List[str] = []
    advertencias: List[str] = []
    if (payload.numero_factura or "").strip():
        problemas.append("El folio lo asigna el SII al emitir: deja el N° de factura vacío "
                         "(para registrar una factura ya emitida a mano usa Contabilidad → Facturas)")
    if (payload.tipo_doc or "factura") != "factura":
        problemas.append("La emisión electrónica es para tipo 'factura' (DTE 33); "
                         "las boletas se registran por la vía manual")

    # Sin lock: esta fase valida y hace HTTP. El lock lo toma el emitir, después del
    # rollback que renueva el snapshot (ver emitir_factura_sii).
    cot = _cargar_venta(db, payload.cotizacion_id, lock=False)
    # acumular=True: la vía SII necesita la LISTA COMPLETA de problemas para mostrarla
    # antes de una emisión irreversible (la manual sale con el primero).
    # Fase 7: la factura de ANTICIPO no deriva líneas de una guía (es la única que no
    # nace de una), así que la construye su propia función — misma forma de salida.
    if payload.es_anticipo:
        datos = _construir_factura_anticipo(db, payload, cot)
    else:
        datos = _construir_factura(db, payload, cot, acumular=True)
    problemas.extend(datos["problemas"])
    advertencias.extend(datos["advertencias"])
    # Segunda capa del piso de $1 (la primera está en armar_lineas_factura /
    # aplicar_descuento_lineas): sin ésta, el preview diría "puede emitir" y el emitir
    # moriría en 409 DESPUÉS de haber creado la factura local — una zombi consumiendo
    # el cupo facturable.
    neto_prev = float(datos["neto"]) if datos.get("neto") is not None else None
    if neto_prev is not None and datos.get("descuentos") and neto_prev < NETO_MINIMO_DTE:
        # Con descuento de anticipo, un neto de EXACTAMENTE 0 es un caso legítimo del
        # negocio (el anticipo cubría todo lo despachado) que la vía manual permite con
        # una advertencia — pero el SII no acepta un DTE en cero. Por eso aquí el borde
        # 0 SÍ bloquea, a diferencia del caso sin descuento de más abajo.
        problemas.append(
            "El descuento del anticipo deja la factura en $0: el SII no acepta un "
            "DTE en cero — ajusta el descuento o registra la factura por la vía manual")
    elif neto_prev is not None and 0 < neto_prev < NETO_MINIMO_DTE:
        # `> 0`: con neto EXACTAMENTE 0 el problema real es otro (sin líneas facturables)
        # y ya lo reporta su propio mensaje — este aviso encima solo ensuciaba el preview.
        problemas.append(
            "El neto de la factura queda bajo $1: el SII no acepta un DTE en cero — "
            "revisa cantidades y precios o regístrala por la vía manual")

    # Receptor: ficha REAL en Wasabil (client_id). Bloquea si está incompleta.
    receptor, client_id = _receptor_factura(
        db, (datos["receptor"].get("rut") or "").strip(),
        datos["receptor"].get("razon_social"), problemas, advertencias)

    # Referencias 801 + 52 + 33. El despacho se resuelve como en la factura persistida:
    # el elegido en el payload o el derivado de las líneas (snap_desp_id).
    desp_id = datos["desp"].id if datos.get("desp") else datos.get("snap_desp_id")
    if not payload.es_anticipo and not payload.sin_guia and not desp_id and datos["validadas"]:
        # Líneas facturables pero sin guía identificable (modo `items` con líneas de
        # más de un despacho, o sin despacho_item_id): no se puede armar la referencia
        # 52 y emitir sin ella dejaría mercadería trasladada sin documento referenciado.
        problemas.append(
            "No se pudo determinar la guía de despacho a referenciar (tipo 52): emite la "
            "factura desde una guía despachada, o marca 'Retiro en oficina' si la "
            "mercadería no salió con guía")
    if not payload.es_anticipo and not payload.sin_guia and desp_id:
        # La 52 que se va a citar tiene que amparar TODAS las líneas de mercadería.
        problema_ajenas = _problema_lineas_fuera_de_la_guia(
            db, desp_id, [ln for _it, ln, _c, _p in datos["validadas"]])
        if problema_ajenas:
            problemas.append(problema_ajenas)
        # CONTRADICCIÓN entre la guía que el operador ELIGIÓ y la que sale de las líneas:
        # en modo `items` el snapshot se DERIVA de las líneas (monza_contabilidad:
        # snap_desp_id), así que elegir la guía A y mandar líneas de la B emitía una 33
        # citando la B en silencio — el operador aprobó otra cosa en el preview. Ante dos
        # verdades en pugna se BLOQUEA (regla rectora), no se elige una.
        if payload.despacho_id is not None and int(payload.despacho_id) != int(desp_id):
            problemas.append(
                f"La guía elegida (despacho {payload.despacho_id}) NO es la que sale de "
                f"las líneas (despacho {desp_id}): no se emite una factura que referencie "
                "una guía distinta a la que trasladó la mercadería. Revisa las líneas o "
                "factura desde la guía correcta.")
    referencias = _referencias_de_venta(
        db, cot, sin_guia=payload.sin_guia, despacho_id=desp_id,
        problemas=problemas, advertencias=advertencias,
        # Los anticipos salen de los `descuentos` recién calculados (la factura todavía
        # no existe). Un anticipo sin folio del SII llega como "#<id>" y BLOQUEA aquí,
        # antes de emitir — no después, con la factura local ya creada.
        anticipos=_anticipos_de_descuentos(db, datos.get("descuentos", [])),
        es_anticipo=bool(payload.es_anticipo),
        # Fecha que va a llevar el documento: MISMA fórmula que usa Contabilidad Monza al
        # persistir la factura, para que el guard mida contra lo que se va a emitir.
        fecha_documento=_parse_date(payload.fecha_emision) or hoy_chile())

    return {
        "cot": cot, "datos": datos, "receptor": receptor, "client_id": client_id,
        "referencias": referencias, "problemas": problemas, "advertencias": advertencias,
    }


def _armar_payload_factura(db: Session, factura, client_id: Optional[int],
                           issue: bool) -> Tuple[dict, List[str]]:
    """Payload del DTE 33 DESDE la factura local persistida (líneas congeladas): lo
    emitido es EXACTAMENTE lo registrado, y el reintento re-arma lo mismo."""
    problemas: List[str] = []
    advertencias: List[str] = []
    lineas, problemas_lineas = armar_lineas_factura(list(factura.items))
    problemas.extend(problemas_lineas)
    # CINTURÓN del reintento: las líneas ya están congeladas en la factura local, así que
    # el guard se re-evalúa sobre ellas (a diferencia de GA, donde _persistir_factura
    # graba despacho_item_id=NULL y este cinturón queda ciego, Monza sí lo persiste).
    # Una factura creada antes de este guard —o por una vía que no lo aplicó— no puede
    # re-emitirse citando una guía que no la ampara.
    problema_ajenas = _problema_lineas_fuera_de_la_guia(
        db, factura.despacho_id if not factura.es_anticipo else None, list(factura.items))
    if problema_ajenas:
        problemas.append(problema_ajenas)
    cot = (db.query(MonzaCotizacion)
           .filter(MonzaCotizacion.id == factura.cotizacion_id).first()
           if factura.cotizacion_id else None)
    # 'Retiro en oficina' quedó registrado en la factura como ausencia de despacho
    # (despacho_id NULL). Se re-deriva de ahí para no depender del payload original;
    # lo mismo con `es_anticipo` y con los anticipos descontados, que se leen de las
    # líneas persistidas — así el REINTENTO arma exactamente el mismo documento.
    referencias = _referencias_de_venta(
        db, cot, sin_guia=not factura.despacho_id, despacho_id=factura.despacho_id,
        problemas=problemas, advertencias=advertencias,
        anticipos=_anticipos_referenciados(db, factura),
        es_anticipo=bool(factura.es_anticipo),
        # MISMO valor que armar_factura pone en documentDate (abajo): si difirieran, el
        # control cruzaría la fecha de la guía contra una fecha que el DTE no lleva.
        fecha_documento=factura.fecha_emision or hoy_chile())
    doc = armar_factura(
        referencia_interna=_referencia_interna_factura(factura.id),
        lineas=lineas, referencias=referencias, client_id=client_id,
        fecha_emision=factura.fecha_emision, issue=issue,
        payment_method=_payment_method(factura.plazo_dias, factura.condicion_pago),
    )
    return doc, problemas


def _emision_33_en_vuelo_de_cot(db: Session, cot_id: int) -> Optional[MonzaWasabilDte]:
    """DTE 33 con claim VIGENTE de cualquier factura de esta VENTA, si lo hay.

    Candado de INTENCIÓN para el flujo "emitir factura NUEVA": el índice único
    `uq_monza_wasabil_dte_factura` protege una factura YA creada, pero aquí cada
    request crearía una factura con id DISTINTO, así que no aplica. Sin este candado,
    dos clics simultáneos en Emitir producen DOS documentos reales ante el SII — y en
    'Retiro en oficina' no hay siquiera un tope por guía que frene al segundo."""
    candidatos = (db.query(MonzaWasabilDte)
                  .join(MonzaContFacturaCliente,
                        MonzaContFacturaCliente.id == MonzaWasabilDte.factura_id)
                  .filter(MonzaContFacturaCliente.cotizacion_id == cot_id,
                          MonzaWasabilDte.tipo_dte == TIPO_DOC_FACTURA,
                          MonzaWasabilDte.en_vuelo_desde.isnot(None))
                  .populate_existing().all())
    return next((d for d in candidatos if claim_vigente(d)), None)


def _reclamar_emision_factura(db: Session, factura_id: int, para_reintento: bool,
                              usuario_id: Optional[int], empresa: str) -> MonzaWasabilDte:
    """Claim anti doble emisión de la factura (espejo de _reclamar_emision de guías):
    transacción CORTA bajo lock y sin red, commiteada ANTES de cualquier HTTP.

    Solo la usa el REINTENTO: el emitir crea la factura y su claim en la MISMA
    transacción (ver emitir_factura_sii). Un arreglo en uno de los dos caminos hay
    que aplicarlo en ambos."""
    db.rollback()  # el snapshot debe nacer con el FOR UPDATE (ver _reclamar_emision)
    factura = (db.query(MonzaContFacturaCliente)
               .filter(MonzaContFacturaCliente.id == factura_id)
               .populate_existing().with_for_update().first())
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    dte = _dte_de_factura(db, factura_id, lock=True)
    problema = _estado_dte_bloquea(dte, para_reintento, _SUST_FACTURA, _DOC_FACTURA)
    if problema:
        db.rollback()
        raise HTTPException(409, problema)
    ahora = datetime.utcnow()  # UTC naive, igual que claim_vigente
    if dte:
        dte.en_vuelo_desde = ahora
        dte.status_id = STATUS_PENDIENTE
        dte.uuid = None
        dte.error = None
        dte.usuario_id = usuario_id or dte.usuario_id
    else:
        dte = MonzaWasabilDte(
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


def _finalizar_factura_emitida(db: Session, dte: MonzaWasabilDte) -> List[str]:
    """Al confirmarse EMITIDO (status 3): folio del SII → `numero_factura` de la
    factura local y RECIÉN AHÍ se aplica el adelanto verificado como cobranza (que la
    emisión había diferido: una factura rechazada no debe haber movido plata).

    IDEMPOTENTE: la invocan emitir, estado y reintentar, y si el folio ya está
    escrito no repite nada (los DOS chequeos de numero_factura, antes y después del
    lock, cierran la carrera entre el sondeo y el reintento).

    DEVUELVE las advertencias de la aplicación del adelanto (Fase 7): cuando la factura
    es de ANTICIPO y el adelanto ya estaba aplicado en otra factura, Contabilidad intenta
    RE-ENCAUZARLO hacia ella, y si no puede (factoring vigente, cobranza conciliada con
    el banco) devuelve el aviso con el remedio. Sin propagarlo, el operador veía
    "Factura emitida — Folio SII X" y la factura de anticipo quedaba impaga EN SILENCIO.
    Como la función es idempotente, el aviso nace UNA sola vez: en el request que
    finalice el folio — por eso lo devuelven los CINCO llamadores, no solo `emitir`.

    Hace su propio commit: se llama SIEMPRE al final, nunca dentro de una transacción
    del llamador que aún tenga trabajo pendiente."""
    if dte.status_id != STATUS_EMITIDO or not dte.folio or not dte.factura_id:
        return []
    factura = (db.query(MonzaContFacturaCliente)
               .filter(MonzaContFacturaCliente.id == dte.factura_id).first())
    if not factura or (factura.numero_factura or "").strip():
        return []  # ya finalizada (idempotencia del sondeo/reintento)
    # Orden GLOBAL de locks de la casa Monza: cotización → factura → adelanto (el
    # adelanto lo bloquea _aplicar_adelanto). Bloquear la factura primero deadlockearía
    # contra eliminar_cobranza y verificar_adelanto.
    cot = None
    if factura.cotizacion_id:
        cot = (db.query(MonzaCotizacion)
               .filter(MonzaCotizacion.id == factura.cotizacion_id)
               .populate_existing().with_for_update(of=MonzaCotizacion).first())
    factura = (db.query(MonzaContFacturaCliente)
               .filter(MonzaContFacturaCliente.id == dte.factura_id)
               .populate_existing().with_for_update().first())
    if not factura or (factura.numero_factura or "").strip():
        db.rollback()
        return []
    try:
        factura.numero_factura = str(dte.folio)
        db.flush()
    except IntegrityError:
        # Colisión con un folio ya registrado A MANO en otra factura local (el UNIQUE
        # de Monza es global). El DTE queda EMITIDO igual: perder el folio de un
        # documento ya vivo ante el SII sería peor que el duplicado.
        #
        # El mensaje tiene que traer el REMEDIO COMPLETO, porque desde aquí la venta se
        # traba: esta factura queda sin N° y, si es un anticipo, la factura del despacho
        # de la misma venta se bloquea (la referencia 33 exige el folio) y el anticipo
        # tampoco se puede borrar. La salida existe y es idempotente: corregir el N° de
        # la OTRA factura y volver a consultar el estado de ésta —el sondeo llama a esta
        # misma función en cada pasada y el folio se graba solo—, así que se NOMBRA a la
        # culpable en vez de dejar al operador buscándola con SQL.
        db.rollback()
        otra = (db.query(MonzaContFacturaCliente)
                .filter(MonzaContFacturaCliente.numero_factura == str(dte.folio))
                .first())
        if otra:
            venta = f" de la venta {otra.numero_cotizacion}" if otra.numero_cotizacion else ""
            quien = f"la factura local #{otra.id}{venta}"
        else:
            quien = "otra factura local"
        dte.error = (
            f"El SII emitió el folio {dte.folio}, pero ese N° ya estaba registrado a mano "
            f"en {quien}. Esta factura quedó SIN N°: corrige (o elimina) el N° de esa otra "
            f"factura en Contabilidad → Facturas y vuelve a consultar el estado de ésta — "
            f"el folio se graba solo. No re-emitas: el documento {dte.folio} ya existe ante "
            f"el SII.")[:2000]
        db.commit()
        return []
    avisos: List[str] = []
    if cot is not None:
        avisos = _aplicar_adelantos_pendientes(db, cot, factura, usuario_id=dte.usuario_id)
        db.flush()
        # Los totales de cobranza se recalculan con las cobranzas leídas BAJO LOCK
        # (misma disciplina que verificar_adelanto: la relación perezosa es una lectura
        # plana y no sirve para decidir plata).
        _recompute_factura(factura, cobranzas=_cobranzas_bloqueadas(db, factura.id))
    db.commit()
    return avisos


@router.post("/despachos/{despacho_id}/registrar-folio")
def registrar_folio_guia(
    despacho_id: int,
    folio: str = Query(..., description="Folio REAL del SII, leído en app.wasabil.com"),
    confirmo_folio: str = Query(..., description="Repite el folio (confirmación explícita)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SALIDA del callejón «guía EMITIDA sin folio». NO emite nada.

    El documento ya existe ante el SII y su folio no llegó: «Reintentar» responde 409 (bien:
    re-emitir sería una SEGUNDA guía 52 REAL) y el N° de guía no se puede editar a mano
    (guard anti-pisado de monza_router_despachos). Sin esto, el despacho se quedaba sin N°
    de guía para siempre y su factura quedaba bloqueada detrás. Reglas y rastro: ver
    `_registrar_folio_a_mano`."""
    dte = _dte_de_despacho(db, despacho_id)
    if not dte:
        raise HTTPException(404, "Este despacho no tiene emisión electrónica")
    if claim_vigente(dte):
        # Hay un request emitiendo AHORA. Escribir el folio bajo sus pies dejaría la fila
        # con un folio que quizá no es el del documento que está naciendo.
        raise HTTPException(
            409, "Hay una emisión en curso para este despacho: espera a que termine y "
                 "consulta el estado antes de registrar un folio a mano.")
    resultado = _registrar_folio_a_mano(
        db, dte, folio, confirmo_folio, "guía",
        _referencia_interna_guia(db, despacho_id),
        getattr(current_user, "id", None))
    db.commit()
    db.refresh(resultado["dte"])
    return {**serialize_dte(resultado["dte"]), "registro_manual": resultado["origen"]}


@router.post("/facturas/preview")
def preview_factura_sii(
    payload: FacturaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Previsualización de la factura 33: líneas + referencias + receptor real de
    Wasabil + validaciones. NO persiste NADA y NO toca el SII (issue=False).
    `puede_emitir` es True solo si no hay ningún problema bloqueante."""
    ctx = _preparar_emision_factura(db, payload)
    datos = ctx["datos"]
    return {
        "puede_emitir": not ctx["problemas"],
        "problemas": ctx["problemas"],
        "advertencias": ctx["advertencias"],
        "receptor": ctx["receptor"],
        "lineas": datos["lineas"],
        # iva_rate viaja al frontend: el modal pinta el % REAL de la venta (iva_pct
        # congelado), jamás un 19% hardcodeado.
        "totales": {"neto": datos["neto"], "iva": datos["iva"], "bruto": datos["bruto"],
                    "iva_rate": datos.get("iva_rate")},
        "referencias": [{"tipo": r["documentType"], "folio": r["folio"],
                         "fecha": r.get("date"), "descripcion": r.get("reason")}
                        for r in ctx["referencias"]],
        "sin_guia": bool(payload.sin_guia),
        # Fase 7: el modal pinta el bloque "descuento por anticipo" (folio + monto)
        # cuando esta lista viene con datos, y el badge cuando es_anticipo.
        "es_anticipo": bool(payload.es_anticipo),
        "descuentos": datos.get("descuentos", []),
    }


@router.post("/facturas/emitir")
def emitir_factura_sii(
    payload: FacturaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE la factura al SII vía Wasabil (IRREVERSIBLE — el frontend solo habilita
    este botón tras la previsualización). Crea la factura LOCAL sin folio + el claim
    anti doble emisión en la MISMA transacción, commiteados ANTES de cualquier HTTP;
    el folio del SII se escribe al confirmarse la emisión."""
    ctx = _preparar_emision_factura(db, payload)
    if ctx["problemas"]:
        raise HTTPException(409, " · ".join(ctx["problemas"]))
    empresa = getattr(current_user, "empresa", None) or "automotriz"
    usuario_id = getattr(current_user, "id", None)

    # ── TX corta: lock cotización → re-validar → persistir SIN folio → claim → COMMIT ──
    # rollback ANTES del lock: _preparar_emision_factura ya abrió la transacción
    # (SELECTs + HTTP a Wasabil) y bajo REPEATABLE READ todas las lecturas NO
    # bloqueantes de más abajo servirían ese snapshot VIEJO — la re-validación no
    # vería la factura que un request gemelo acaba de commitear y saldrían DOS
    # documentos reales al SII. Con el rollback, el snapshot nace con el FOR UPDATE.
    db.rollback()
    cot = _cargar_venta(db, payload.cotizacion_id, lock=True)
    # Candado de INTENCIÓN por venta (ver _emision_33_en_vuelo_de_cot): el índice único
    # es por factura_id y aquí cada request crearía una factura nueva.
    if _emision_33_en_vuelo_de_cot(db, cot.id) is not None:
        db.rollback()
        raise HTTPException(409, "Ya hay una emisión de factura EN CURSO para esta venta "
                                 "(otra pestaña u otro usuario). Espera su resultado antes "
                                 "de emitir otra.")
    # Se RE-CONSTRUYE bajo el lock (no se reusa ctx["datos"]: ese venía del snapshot
    # viejo). Son ESTOS montos los que se congelan en el DTE. El ruteo por es_anticipo
    # es el MISMO que hizo el preview: si divergieran, se emitiría algo distinto a lo
    # que el usuario aprobó.
    if payload.es_anticipo:
        datos = _construir_factura_anticipo(db, payload, cot)
    else:
        datos = _construir_factura(db, payload, cot, acumular=True)
    if datos["problemas"]:
        db.rollback()
        raise HTTPException(datos.get("problemas_status") or 409, " · ".join(datos["problemas"]))
    try:
        factura = _persistir_factura(
            db, payload, cot, datos, folio=None, tipo_doc="factura",
            usuario_id=usuario_id, aplicar_adelantos=False)
        db.flush()
        dte = MonzaWasabilDte(
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

    # ── Payload DESDE la factura persistida + HTTP (ya sin locks) ──
    factura = (db.query(MonzaContFacturaCliente)
               .filter(MonzaContFacturaCliente.id == factura_id).first())
    doc, problemas_doc = _armar_payload_factura(db, factura, ctx["client_id"], issue=True)
    if problemas_doc:
        # No debería ocurrir (el preview ya validó). Nada salió aún hacia Wasabil, así
        # que se DESHACE la factura recién creada: si no, quedaría una zombi
        # consumiendo el cupo facturable de la mercadería (los topes cuentan TODAS las
        # facturas de la venta, con folio o sin él) y con el claim vivo 180 s.
        # En Monza no hay que desvincular adelantos antes de borrar (MonzaContAdelanto
        # no referencia facturas); sí importa el ORDEN: primero el DTE, que tiene la FK.
        try:
            db.delete(dte)
            db.delete(factura)
            db.commit()
        except IntegrityError:
            # Red de seguridad: si algo más quedó referenciando la factura, no se puede
            # deshacer — se libera al menos el claim para no bloquear la venta.
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
            # Seguro que NO se creó documento: liberar el claim para que el reintento
            # quede disponible de inmediato. Ambiguo (timeout/5xx): el claim se queda
            # puesto y expira solo; el reintento verificará en Wasabil antes de re-crear.
            dte.en_vuelo_desde = None
        db.commit()
        raise HTTPException(502, f"No se pudo emitir en Wasabil: {e}")
    # Hallazgo #12 (la 33 tiene el MISMO hueco que la 52): respuesta EMITIDA sin folio
    # → se trae el documento completo, si no la factura local se queda sin
    # numero_factura y el adelanto diferido nunca se aplica. Sin uuid el rescate va por
    # la referencia interna FACT-<id>.
    data = _completar_documento_emitido(
        data, referencia_interna=_referencia_interna_factura(factura_id))
    _actualizar_desde_wasabil(db, dte, data)
    db.commit()
    avisos = _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**serialize_dte(dte), "factura_id": factura_id, "advertencias": avisos}


@router.get("/facturas/estado-batch")
def estado_batch_facturas(
    ids: str = Query(..., description="IDs de facturas separados por coma"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Estado (solo BD, sin llamar a Wasabil) de los DTE de varias facturas — para
    pintar los badges de folio/PDF en el listado sin N llamadas.

    OJO: declarado ANTES de /facturas/{factura_id}/estado para que FastAPI no intente
    parsear 'estado-batch' como un factura_id."""
    try:
        factura_ids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids debe ser una lista de enteros separados por coma")
    if not factura_ids:
        return {}
    if len(factura_ids) > 200:
        raise HTTPException(400, "Máximo 200 facturas por consulta")
    dtes = (db.query(MonzaWasabilDte)
            .filter(MonzaWasabilDte.factura_id.in_(factura_ids),
                    MonzaWasabilDte.tipo_dte == TIPO_DOC_FACTURA)
            .all())
    return {d.factura_id: serialize_dte(d) for d in dtes}


@router.get("/facturas/{factura_id}/estado")
def estado_factura_sii(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Estado del DTE de la factura (sondeo del frontend). Al quedar Emitido escribe
    el folio en la factura local y aplica el adelanto diferido."""
    dte = _dte_de_factura(db, factura_id)
    if not dte:
        raise HTTPException(404, "Esta factura no tiene emisión electrónica")
    # Hallazgo #12 (espejo del sondeo de guías): 'emitida SIN folio' se re-consulta;
    # de lo contrario la factura local se queda para siempre sin numero_factura y
    # /reintentar responde 409 'ya está emitida'.
    sin_folio_emitido = (dte.status_id == STATUS_EMITIDO and _vacio(dte.folio))
    if dte.uuid and (dte.status_id not in (STATUS_EMITIDO, STATUS_FALLIDO)
                     or sin_folio_emitido):
        try:
            data = wasabil.estado_documento(dte.uuid)
            if int(data.get("status_id") or 0) == STATUS_EMITIDO:
                # El status trae lo esencial; el documento completo trae folio + PDF/XML
                data = wasabil.obtener_documento(dte.uuid)
            _actualizar_desde_wasabil(db, dte, data)
            db.commit()
            db.refresh(dte)
        except wasabil.WasabilError as e:
            # No romper el sondeo por un error transitorio (200 con error_consulta)
            return {**serialize_dte(dte), "factura_id": factura_id, "error_consulta": str(e)}
    elif sin_folio_emitido and not dte.uuid:
        # Callejón SIN uuid (espejo del sondeo de guías): rescate SOLO LECTURA por la
        # referencia interna. Si no resuelve, la factura sigue sin folio y bloqueada.
        try:
            doc = _rescatar_por_referencia(_referencia_interna_factura(factura_id),
                                           solo_emitido=True)
            if doc:
                _actualizar_desde_wasabil(db, dte, doc)
                db.commit()
                db.refresh(dte)
        except wasabil.WasabilError as e:
            return {**serialize_dte(dte), "factura_id": factura_id, "error_consulta": str(e)}
    avisos = _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**serialize_dte(dte), "factura_id": factura_id, "advertencias": avisos}


@router.post("/facturas/{factura_id}/reintentar")
def reintentar_factura_sii(
    factura_id: int,
    confirmo_sin_documento_emitido: bool = Query(
        False,
        description="SOLO para el caso en que Wasabil no permite verificar si ya existe "
                    "una factura emitida con la referencia FACT-<id>: declara que una "
                    "PERSONA lo revisó en app.wasabil.com y no existe. Nunca levanta el "
                    "bloqueo cuando el documento emitido está PROBADO. Queda en el log."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reintento SEGURO de una emisión de factura fallida (espejo del de guías):
    verifica el estado REAL en Wasabil (por uuid, o por la referencia interna
    FACT-<id>) y cruza el CINTURÓN por referencia ANTES de re-crear. Si no puede
    verificar, ABORTA: nunca re-crea a ciegas — un segundo DTE 33 por la misma
    mercadería es irreversible, y son DOS ventas ante el SII."""
    dte = _dte_de_factura(db, factura_id)
    if not dte:
        raise HTTPException(404, "Esta factura no tiene emisión que reintentar")
    if claim_vigente(dte):
        raise HTTPException(409, "Hay una emisión EN CURSO para esta factura: "
                                 "espera unos minutos y consulta el estado")
    if dte.status_id == STATUS_EMITIDO:
        # Espejo del reintento de guías: el documento EXISTE ante el SII y este camino
        # NUNCA re-emite. Sin folio se intenta un rescate de SOLO LECTURA y, si no
        # resuelve, se bloquea con el remedio humano (era un callejón: "La factura ya
        # está emitida (folio None)" y la factura local sin numero_factura para siempre).
        if not _vacio(dte.folio):
            raise HTTPException(409, f"La factura ya está emitida (folio {dte.folio})")
        try:
            doc = (wasabil.obtener_documento(dte.uuid) if dte.uuid
                   else _rescatar_por_referencia(_referencia_interna_factura(factura_id),
                                                 solo_emitido=True))
            if doc:
                _actualizar_desde_wasabil(db, dte, doc)
                db.commit()
                db.refresh(dte)
        except wasabil.WasabilError as e:
            raise HTTPException(409, f"{_msg_rescate_sin_folio(dte, _SUST_FACTURA)} "
                                     f"(detalle: {e})")
        if _vacio(dte.folio):
            raise HTTPException(409, _msg_rescate_sin_folio(dte, _SUST_FACTURA))
        avisos = _finalizar_factura_emitida(db, dte)
        db.refresh(dte)
        return {**serialize_dte(dte), "factura_id": factura_id, "advertencias": avisos}

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
            if dte.status_id == STATUS_EMITIDO and _vacio(dte.folio):
                raise HTTPException(409, _msg_rescate_sin_folio(dte, _SUST_FACTURA))
            avisos = _finalizar_factura_emitida(db, dte)
            db.refresh(dte)
            return {**serialize_dte(dte), "factura_id": factura_id, "advertencias": avisos}
    else:
        try:
            # Criterio ÚNICO del rescate: prefiere el EMITIDO, ABORTA con dos emitidos y
            # nunca degrada un status 3 confirmado (ver _rescatar_por_referencia).
            doc_w = _rescatar_por_referencia(_referencia_interna_factura(factura_id),
                                             solo_emitido=False)
        except wasabil.WasabilError as e:
            raise HTTPException(502, f"No se pudo verificar en Wasabil si el documento ya "
                                     f"existe; reintenta en unos minutos (no se re-crea a "
                                     f"ciegas). Detalle: {e}")
        if doc_w:
            _actualizar_desde_wasabil(db, dte, doc_w)
            db.commit()
            if dte.status_id == STATUS_EMITIDO and _vacio(dte.folio):
                raise HTTPException(409, _msg_rescate_sin_folio(dte, _SUST_FACTURA))
            avisos = _finalizar_factura_emitida(db, dte)
            db.refresh(dte)
            return {**serialize_dte(dte), "factura_id": factura_id,
                    "advertencias": avisos}

    # 3) CINTURÓN ANTI DOBLE EMISIÓN POR REFERENCIA (gemelo del de reintentar_guia): con
    # el uuid del intento rechazado, `estado_documento` confirma "fallido" y se re-emitía
    # aunque Wasabil ya tuviera una 33 EMITIDA con la misma referencia FACT-<id>. Duplicar
    # un DTE 33 es peor que duplicar una guía: son DOS VENTAS ante el SII. Falla CERRADO.
    _abortar_si_ya_hay_documento_emitido(
        _referencia_interna_factura(factura_id), "factura",
        confirmado_por_humano=confirmo_sin_documento_emitido,
        usuario_id=getattr(current_user, "id", None))

    # 4) Documento confirmado fallido (o inexistente) → re-emitir DESDE la factura local
    factura = (db.query(MonzaContFacturaCliente)
               .filter(MonzaContFacturaCliente.id == factura_id).first())
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    problemas: List[str] = []
    advertencias: List[str] = []
    cot_fac = (db.query(MonzaCotizacion)
               .filter(MonzaCotizacion.id == factura.cotizacion_id).first()
               if factura.cotizacion_id else None)
    cli = cot_fac.cliente if cot_fac else None
    _receptor, client_id = _receptor_factura(
        db, ((cli.rut if cli else None) or "").strip(),
        (cli.nombre if cli else None) or factura.cliente_nombre,
        problemas, advertencias)
    doc, problemas_doc = _armar_payload_factura(db, factura, client_id, issue=True)
    problemas.extend(problemas_doc)
    if problemas:
        raise HTTPException(409, " · ".join(problemas))

    dte = _reclamar_emision_factura(
        db, factura_id, para_reintento=True,
        usuario_id=getattr(current_user, "id", None),
        empresa=getattr(current_user, "empresa", None) or "automotriz")
    dte.payload_json = json.dumps(doc, ensure_ascii=False)[:60000]
    db.commit()
    try:
        data = wasabil.crear_documento(payload_a_rest(doc))
    except wasabil.WasabilError as e:
        dte.error = (str(e) + (f" · {e.detalle[:500]}" if e.detalle else ""))[:2000]
        if not e.ambiguo:
            dte.en_vuelo_desde = None
        db.commit()
        raise HTTPException(502, f"No se pudo emitir en Wasabil: {e}")
    # Hallazgo #12: mismo rescate del folio en la RE-emisión del reintento.
    data = _completar_documento_emitido(
        data, referencia_interna=_referencia_interna_factura(factura_id))
    _actualizar_desde_wasabil(db, dte, data)
    db.commit()
    avisos = _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**serialize_dte(dte), "factura_id": factura_id, "advertencias": avisos}


@router.post("/facturas/{factura_id}/registrar-folio")
def registrar_folio_factura(
    factura_id: int,
    folio: str = Query(..., description="Folio REAL del SII, leído en app.wasabil.com"),
    confirmo_folio: str = Query(..., description="Repite el folio (confirmación explícita)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SALIDA del callejón «factura EMITIDA sin folio». NO emite nada. Gemela de
    `registrar_folio_guia`, con un paso más: la factura local necesita su `numero_factura`.

    Sin esto, la factura quedaba EMITIDA ante el SII pero sin N° en el ERP: no entraba en
    cartera con su folio, `_finalizar_factura_emitida` nunca corría —así que el adelanto
    verificado que la emisión había DIFERIDO no se aplicaba jamás— y «Reintentar» respondía
    409 (correcto: el DTE 33 ya existe).

    Por qué `_finalizar_factura_emitida` va DESPUÉS y en la MISMA transacción: es el mismo
    camino que recorre el sondeo cuando el folio llega solo, así que el resultado es
    idéntico se haya registrado el folio a mano o automáticamente. Es idempotente (no repite
    nada si la factura ya tiene N°) y devuelve las advertencias de la aplicación de
    adelantos, que el operador tiene que ver."""
    dte = _dte_de_factura(db, factura_id)
    if not dte:
        raise HTTPException(404, "Esta factura no tiene emisión electrónica")
    if claim_vigente(dte):
        raise HTTPException(
            409, "Hay una emisión en curso para esta factura: espera a que termine y "
                 "consulta el estado antes de registrar un folio a mano.")
    resultado = _registrar_folio_a_mano(
        db, dte, folio, confirmo_folio, "factura",
        _referencia_interna_factura(factura_id),
        getattr(current_user, "id", None))
    # Cierra la transacción del registro Y hace el trabajo pendiente de la factura (folio →
    # numero_factura + adelanto diferido). Commitea adentro; el commit de abajo cubre los
    # caminos en que retorna temprano (factura ya finalizada) para no dejar el folio sin
    # persistir.
    avisos = _finalizar_factura_emitida(db, resultado["dte"])
    db.commit()
    db.refresh(resultado["dte"])
    return {**serialize_dte(resultado["dte"]), "factura_id": factura_id,
            "registro_manual": resultado["origen"], "advertencias": avisos}
