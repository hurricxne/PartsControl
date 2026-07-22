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
from datetime import datetime
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import (
    User, Despacho, OcCliente, Cotizacion, ConfiguracionCotizador,
)
from routers.contabilidad import _cfg_to_dict, _precios_de_cotizacion

from . import client as wasabil
from .models import (
    WasabilDte, STATUS_EMITIDO, STATUS_FALLIDO, STATUS_PROCESANDO, STATUS_PENDIENTE,
)
from .service import (
    TIPO_DOC_GUIA, FOLIO_REF_MAX, TIPOS_TRASLADO, TIPO_TRASLADO_DEFAULT,
    armar_lineas, armar_guia, payload_a_rest, parse_fecha_oc, cuadratura,
    total_neto_lineas, serialize_dte, claim_vigente, _f,
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


def _estado_dte_bloquea(dte: Optional[WasabilDte], para_reintento: bool) -> Optional[str]:
    """Máquina de estados EXPLÍCITA del DTE previo: devuelve el problema que
    bloquea la emisión, o None si se puede proceder. Cubre TODOS los estados:

      sin fila            → emitir OK · reintentar no aplica (404 antes)
      emitido (3)         → bloquea siempre
      claim en vuelo      → bloquea siempre (hay una emisión HTTP en curso)
      uuid + procesando/sin status → bloquea siempre (consultar estado)
      uuid + pendiente (6)→ bloquea siempre (borrador en Wasabil: resolver allá)
      fallido (4)         → bloquea emitir (usar Reintentar) · reintentar OK
      sin uuid + error    → bloquea emitir (usar Reintentar) · reintentar OK
    """
    if not dte:
        return None
    if dte.status_id == STATUS_EMITIDO:
        return f"Este despacho YA tiene guía electrónica emitida (folio {dte.folio})"
    if claim_vigente(dte):
        return ("Hay una emisión EN CURSO para este despacho (otro usuario o pestaña). "
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


def _actualizar_desde_wasabil(db: Session, dte: WasabilDte, data: dict) -> None:
    """Vuelca la respuesta de Wasabil en la fila (uuid/estado/folio/PDF).

    El folio SOLO se registra cuando el documento queda EMITIDO (status 3) — y en
    ese momento se copia a despacho.numero_guia (única escritura de este módulo
    sobre una tabla existente). Con uuid conocido, el claim en_vuelo se libera:
    el uuid pasa a ser el candado."""
    if data.get("uuid"):
        dte.uuid = data["uuid"]
        dte.en_vuelo_desde = None
    if data.get("status_id") is not None:
        dte.status_id = int(data["status_id"])
    if data.get("display_error") or data.get("error"):
        dte.error = str(data.get("display_error") or data.get("error"))[:2000]
    dte.respuesta_json = json.dumps(data, ensure_ascii=False, default=str)[:60000]

    if dte.status_id == STATUS_EMITIDO:
        if data.get("folio"):
            dte.folio = str(data["folio"])
        if data.get("document_pdf_url"):
            dte.pdf_url = data["document_pdf_url"]
        if data.get("document_xml_url"):
            dte.xml_url = data["document_xml_url"]
        dte.error = None
        dte.en_vuelo_desde = None
        if dte.folio and dte.despacho_id:
            despacho = db.query(Despacho).filter(Despacho.id == dte.despacho_id).first()
            if despacho:
                despacho.numero_guia = dte.folio


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
            # liberar el claim para que el reintento quede disponible de inmediato
            dte.en_vuelo_desde = None
        # Ambiguo (timeout/5xx): el claim queda puesto y expira solo — mientras
        # tanto nadie puede duplicar, y el reintento posterior verifica en Wasabil
        db.commit()
        raise HTTPException(502, f"No se pudo emitir en Wasabil: {e}")

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
    if ctx["fecha_oc"] and ctx["lineas"] and (ctx["oc"].numero_oc or "").strip():
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
    if dte.uuid and dte.status_id not in (STATUS_EMITIDO, STATUS_FALLIDO):
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
    return serialize_dte(dte)


@router.post("/despachos/{despacho_id}/reintentar")
def reintentar_guia(
    despacho_id: int,
    tipo_traslado: int = Query(TIPO_TRASLADO_DEFAULT,
                               description="dispatchTypeCode del SII (ver TIPOS_TRASLADO)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reintento SEGURO de una emisión fallida (o que no llegó a Wasabil).

    Anti doble emisión: (1) si hay uuid, consulta el estado real; (2) si no hay
    uuid, busca en Wasabil por la referencia interna (OC + N° despacho) por si el
    documento SÍ se creó y la respuesta se perdió. Si la verificación FALLA se
    ABORTA (nunca se re-crea a ciegas). Solo si el documento está confirmado
    fallido/inexistente se re-emite — reclamando el claim bajo lock.
    """
    _validar_tipo_traslado(tipo_traslado)
    dte = _dte_de_despacho(db, despacho_id)
    if not dte:
        raise HTTPException(404, "Este despacho no tiene emisión que reintentar")
    if dte.status_id == STATUS_EMITIDO:
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
            return serialize_dte(dte)
    else:
        despacho_prev, _oc_prev, _cot_prev = _cargar_contexto(db, despacho_id)
        try:
            ref = despacho_prev.numero_despacho
            documentos, busqueda_completa = wasabil.buscar_documentos(ref)
            for doc in documentos:
                # Match EXACTO contra el formato vigente (invoice_reference == N° de
                # despacho) y compat con el formato v1 "OC <n> · <N° despacho>" de los
                # documentos ya emitidos (p.ej. folio 136). Sufijo con "· " y no
                # substring: DSP-0001 no debe confundirse con DSP-00012.
                inv = str(doc.get("invoice_reference") or "")
                if inv == ref or inv.endswith(f"· {ref}"):
                    if int(doc.get("status_id") or 0) == STATUS_EMITIDO and doc.get("uuid"):
                        # Traer el documento COMPLETO (folio + PDF/XML garantizados)
                        doc = wasabil.obtener_documento(doc["uuid"])
                    _actualizar_desde_wasabil(db, dte, doc)
                    db.commit()
                    db.refresh(dte)
                    return serialize_dte(dte)
        except wasabil.WasabilError:
            raise HTTPException(502, "No se pudo verificar en Wasabil si el documento ya existe; "
                                     "reintenta en unos minutos (no se re-crea a ciegas)")
        if not busqueda_completa:
            # La lista quedó truncada por paginación: "no lo encontré" NO prueba
            # que no exista — re-emitir aquí arriesga una segunda guía legal.
            raise HTTPException(502, "La búsqueda en Wasabil quedó incompleta (lista paginada): "
                                     "no se puede confirmar que la guía no exista — no se re-crea a ciegas")

    # 2) Documento confirmado fallido (o inexistente): revalidar y re-emitir.
    ctx = _preparar_emision(db, despacho_id, para_reintento=True)
    if ctx["problemas"]:
        raise HTTPException(409, " · ".join(ctx["problemas"]))
    dte = _reclamar_emision(
        db, despacho_id, para_reintento=True,
        usuario_id=getattr(current_user, "id", None),
        empresa=getattr(current_user, "empresa", None) or "mineria",
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
    armar_referencias_factura,
)


def _dte_de_factura(db: Session, factura_id: int, lock: bool = False) -> Optional[WasabilDte]:
    q = db.query(WasabilDte).filter(WasabilDte.factura_id == factura_id)
    if lock:
        q = q.populate_existing().with_for_update()
    return q.first()


def _referencia_interna_factura(factura_id: int) -> str:
    """Ancla anti doble emisión de la factura (formato v2: Wasabil la imprime,
    así que NO lleva la OC — única por factura local)."""
    return f"FACT-{factura_id}"


def _guia_electronica_en_proceso(db: Session, despacho_id: int) -> bool:
    """¿El despacho tiene una guía electrónica cuyo folio del SII TODAVÍA no llega?

    (status 2 procesando / 6 pendiente, o un claim en vuelo sin respuesta.) En esa
    ventana `despacho.numero_guia` aún conserva el N° manual viejo — el módulo lo pisa
    con el folio real recién al confirmarse la emisión —, así que caer al fallback
    manual haría que la factura saliera al SII referenciando un folio que el SII NO
    conoce. Y eso es irreversible. Mejor bloquear y esperar el folio."""
    dte = (db.query(WasabilDte)
           .filter(WasabilDte.despacho_id == despacho_id,
                   WasabilDte.tipo_dte == TIPO_DOC_GUIA)
           .first())
    if dte is None or dte.status_id == STATUS_EMITIDO:
        return False
    return bool(dte.uuid) or claim_vigente(dte)


def _guia_referencia_de_factura(db: Session, factura) -> Tuple[Optional[str], Optional[object]]:
    """(folio, fecha) de la guía a referenciar (tipo 52) en la factura.

    Preferencia: la guía ELECTRÓNICA emitida del despacho (folio SII + fecha
    tributaria del payload). Fallback: guía manual (despacho.numero_guia +
    fecha_despacho). Sin despacho (anticipo) → (None, None).

    OJO: si hay una guía electrónica EN PROCESO se devuelve (None, None) — nunca el
    N° manual viejo (ver _guia_electronica_en_proceso). Quien arma el documento debe
    tratar eso como bloqueo."""
    if not factura.despacho_id:
        return None, None
    if _guia_electronica_en_proceso(db, factura.despacho_id):
        return None, None
    dte_guia = (db.query(WasabilDte)
                .filter(WasabilDte.despacho_id == factura.despacho_id,
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
        return dte_guia.folio, fecha
    desp = factura.despacho
    if desp and (desp.numero_guia or "").strip():
        fecha = desp.fecha_despacho.date() if desp.fecha_despacho else None
        return desp.numero_guia.strip(), fecha
    return None, None


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


def _preparar_emision_factura(db: Session, payload: FacturaCreate) -> dict:
    """Arma y valida TODO para emitir una factura NUEVA (SIN persistir ni locks;
    puede llamar a Wasabil para la ficha del cliente). Única fuente de verdad de
    la validación del preview — la emisión re-valida bajo lock con las mismas
    funciones de Contabilidad."""
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
    guia_folio, guia_fecha = None, None
    if not payload.es_anticipo and payload.despacho_id and datos.get("desp"):
        desp = datos["desp"]
        dte_guia = (db.query(WasabilDte)
                    .filter(WasabilDte.despacho_id == desp.id,
                            WasabilDte.status_id == STATUS_EMITIDO).first())
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
        elif _guia_electronica_en_proceso(db, desp.id):
            # El folio del SII viene en camino y `desp.numero_guia` todavía tiene el N°
            # manual viejo: referenciarlo mandaría la factura al SII apuntando a un folio
            # inexistente, y eso no se deshace. Se espera a que la guía quede emitida.
            problemas.append(
                "La guía electrónica de este despacho está EN PROCESO en el SII: espera a "
                "que quede emitida (con su folio) antes de facturarla — si no, la factura "
                "referenciaría un N° de guía que el SII no reconoce")
        elif (desp.numero_guia or "").strip():
            guia_folio = desp.numero_guia.strip()
            guia_fecha = desp.fecha_despacho.date() if desp.fecha_despacho else None
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
        guia_folio=guia_folio, guia_fecha=guia_fecha, anticipos=anticipos_ref)
    problemas.extend(problemas_ref)

    return {
        "oc": oc, "cot": cot, "datos": datos, "receptor": receptor,
        "client_id": client_id, "referencias": referencias,
        "problemas": problemas, "advertencias": advertencias,
    }


def _armar_payload_factura(db: Session, factura, client_id: Optional[int],
                           issue: bool) -> Tuple[dict, List[str]]:
    """Payload del DTE 33 DESDE la factura local persistida (líneas congeladas):
    lo emitido es EXACTAMENTE lo registrado — y el reintento re-arma lo mismo."""
    lineas, problemas = armar_lineas_factura(list(factura.items))
    oc = factura.oc_cliente
    guia_folio, guia_fecha = _guia_referencia_de_factura(db, factura)
    referencias, problemas_ref = armar_referencias_factura(
        numero_oc=(oc.numero_oc or "").strip() if oc else "",
        fecha_oc=parse_fecha_oc(oc.fecha_oc) if oc else None,
        guia_folio=guia_folio, guia_fecha=guia_fecha,
        anticipos=_anticipos_referenciados(db, factura))
    problemas.extend(problemas_ref)
    doc = armar_factura(
        referencia_interna=_referencia_interna_factura(factura.id),
        lineas=lineas, referencias=referencias, client_id=client_id,
        fecha_emision=factura.fecha_emision, issue=issue,
        payment_method=_payment_method(factura.plazo_dias, factura.condicion_pago),
    )
    return doc, problemas


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
    problema = _estado_dte_bloquea(dte, para_reintento)
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Previsualización de la factura 33: documento + referencias + receptor real
    de Wasabil + validaciones. NO persiste, NO toca el SII."""
    ctx = _preparar_emision_factura(db, payload)
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE la factura al SII vía Wasabil (IRREVERSIBLE — el frontend solo habilita
    este botón tras la previsualización). Crea la factura LOCAL sin folio + el claim
    en la MISMA transacción; el folio del SII llega al confirmarse la emisión."""
    ctx = _preparar_emision_factura(db, payload)
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
    doc, problemas_doc = _armar_payload_factura(db, factura, ctx["client_id"], issue=True)
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
            dte.en_vuelo_desde = None
        db.commit()
        raise HTTPException(502, f"No se pudo emitir en Wasabil: {e}")
    _actualizar_desde_wasabil(db, dte, data)
    db.commit()
    _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**serialize_dte(dte), "factura_id": factura_id}


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
    if dte.uuid and dte.status_id not in (STATUS_EMITIDO, STATUS_FALLIDO):
        try:
            data = wasabil.estado_documento(dte.uuid)
            if int(data.get("status_id") or 0) == STATUS_EMITIDO:
                data = wasabil.obtener_documento(dte.uuid)
            _actualizar_desde_wasabil(db, dte, data)
            db.commit()
            db.refresh(dte)
        except wasabil.WasabilError as e:
            return {**serialize_dte(dte), "factura_id": factura_id, "error_consulta": str(e)}
    _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**serialize_dte(dte), "factura_id": factura_id}


@router.post("/facturas/{factura_id}/reintentar")
def reintentar_factura_sii(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reintento SEGURO de una emisión de factura fallida (espejo del de guías):
    verifica el estado real en Wasabil (por uuid o por la referencia interna
    FACT-<id>) ANTES de re-crear; si no puede verificar, ABORTA."""
    dte = _dte_de_factura(db, factura_id)
    if not dte:
        raise HTTPException(404, "Esta factura no tiene emisión que reintentar")
    if dte.status_id == STATUS_EMITIDO:
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
            return {**serialize_dte(dte), "factura_id": factura_id}
    else:
        ref = _referencia_interna_factura(factura_id)
        try:
            documentos, busqueda_completa = wasabil.buscar_documentos(ref)
            for doc_w in documentos:
                # Match EXACTO v2 (invoice_reference == FACT-<id>): sin formatos legados
                if str(doc_w.get("invoice_reference") or "") == ref:
                    if int(doc_w.get("status_id") or 0) == STATUS_EMITIDO and doc_w.get("uuid"):
                        doc_w = wasabil.obtener_documento(doc_w["uuid"])
                    _actualizar_desde_wasabil(db, dte, doc_w)
                    db.commit()
                    _finalizar_factura_emitida(db, dte)
                    db.refresh(dte)
                    return {**serialize_dte(dte), "factura_id": factura_id}
        except wasabil.WasabilError:
            raise HTTPException(502, "No se pudo verificar en Wasabil si el documento ya existe; "
                                     "reintenta en unos minutos (no se re-crea a ciegas)")
        if not busqueda_completa:
            raise HTTPException(502, "La búsqueda en Wasabil quedó incompleta; reintenta en "
                                     "unos minutos (no se re-crea a ciegas)")

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
    doc, problemas_doc = _armar_payload_factura(db, factura, client_id, issue=True)
    problemas.extend(problemas_doc)
    if problemas:
        raise HTTPException(409, " · ".join(problemas))

    dte = _reclamar_emision_factura(
        db, factura_id, para_reintento=True,
        usuario_id=getattr(current_user, "id", None),
        empresa=getattr(current_user, "empresa", None) or "mineria")
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
    _actualizar_desde_wasabil(db, dte, data)
    db.commit()
    _finalizar_factura_emitida(db, dte)
    db.refresh(dte)
    return {**serialize_dte(dte), "factura_id": factura_id}
