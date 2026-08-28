"""
Integraciones MonzaParts. Webhook receptor de leads (Nexor).
Captura el payload crudo (auto-documentado) + crea el lead.
Auth: header X-API-Key == settings.NEXOR_WEBHOOK_KEY
"""
import json
import re
from fastapi import APIRouter, Depends, Request, HTTPException, Header
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_correlativos import agregar_lead_con_numero, reintentar_carrera
from monza_rut import rut_identidad
from monza_telefono import buscar_ficha_por_telefono
from empresa_guard import require_empresa
from config import settings
from monza_models import MonzaLead, MonzaCliente, MonzaWebhookLog, MonzaLeadItem


def _parse_repuestos(raw):
    """Convierte el campo repuestos (lista o texto) en una lista de descripciones."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip(" -•\t") for x in raw if str(x).strip(" -•\t")]
    if isinstance(raw, dict):
        return [str(v).strip(" -•\t") for v in raw.values() if str(v).strip(" -•\t")]
    parts = re.split(r"[\n;,•]+|\s/\s|\s\|\s", str(raw))
    return [x.strip(" -•\t") for x in parts if x.strip(" -•\t")]

router = APIRouter(prefix="/api/monza/integraciones", tags=["monza-integraciones"])


def _pick(d: dict, *keys):
    """Devuelve el primer valor no vacío entre varias claves posibles."""
    for k in keys:
        if isinstance(d, dict) and d.get(k):
            return d.get(k)
    return None


def _pick_fuzzy(d: dict, *subs):
    """Devuelve el primer valor cuya CLAVE contiene alguno de los substrings (case-insensitive).
    Tolera nombres de variable de Nexor tipo etiqueta larga o slug."""
    if not isinstance(d, dict):
        return None
    for k, v in d.items():
        kl = str(k).lower()
        if v and any(s in kl for s in subs):
            return v
    return None


def _flatten(d: dict) -> dict:
    """Une el payload + sus subobjetos comunes (lead, contact, data, metadata) para mapear plano."""
    out = dict(d) if isinstance(d, dict) else {}
    for sub in ("lead", "contact", "data", "metadata", "custom_fields", "fields"):
        v = d.get(sub) if isinstance(d, dict) else None
        if isinstance(v, dict):
            for k, val in v.items():
                out.setdefault(k, val)
    return out


@router.post("/nexor/leads")
async def recibir_lead_nexor(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    # ── Auth ──────────────────────────────────────────────────────────────────
    key = settings.NEXOR_WEBHOOK_KEY
    # también aceptar ?key= por si el panel de Nexor no permite headers
    qkey = request.query_params.get("key")
    if not key or (x_api_key != key and qkey != key):
        raise HTTPException(status_code=401, detail="API key inválida")

    # ── Capturar payload crudo (auto-documentación) ───────────────────────────
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except Exception:
        payload = {}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("x-api-key", "authorization")}

    log = MonzaWebhookLog(
        fuente="nexor",
        headers=json.dumps(headers, ensure_ascii=False)[:4000],
        payload=raw.decode("utf-8", "replace")[:8000] if raw else "",
    )
    db.add(log)
    # COMMIT inmediato del payload capturado, ANTES de procesar. Con un simple `flush`, el
    # `db.rollback()` del manejador de errores de más abajo se llevaba por delante esta
    # misma fila: en el ÚNICO caso donde el payload sirve —cuando el procesamiento falla—
    # el log quedaba vacío, y el módulo existe justamente para capturarlo. Nexor da el
    # lead por entregado y no queda ni rastro de qué llegó.
    db.commit()

    # ── Mapeo (estructura Nexor: data.lead) con fallbacks ─────────────────────
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    lead_obj = data.get("lead") if isinstance(data.get("lead"), dict) else None
    if lead_obj is None:
        lead_obj = payload.get("lead") if isinstance(payload.get("lead"), dict) else payload
    if not isinstance(lead_obj, dict):
        lead_obj = {}
    meta = lead_obj.get("metadata") if isinstance(lead_obj.get("metadata"), dict) else {}
    p = {**meta, **lead_obj}  # campos del lead + metadata accesibles plano

    sc = data.get("status_change") if isinstance(data.get("status_change"), dict) else {}
    to_status = sc.get("to_status") if isinstance(sc.get("to_status"), dict) else {}
    estado_nexor = to_status.get("label") or to_status.get("key")

    nombre = _pick(p, "name", "full_name", "nombre")
    if not nombre:
        fn = _pick(p, "first_name", "firstName") or ""
        ln = _pick(p, "last_name", "lastName") or ""
        nombre = f"{fn} {ln}".strip()
    telefono = _pick(p, "phone", "telefono", "phone_number", "phoneNumber", "celular", "whatsapp")
    email = _pick(p, "email", "correo", "email_address")
    marca = _pick(p, "marca", "brand", "vehicle_brand")
    modelo = _pick(p, "modelo", "model", "vehicle_model")
    anio = _pick(p, "anio", "ano", "year", "vehicle_year")
    vin = _pick(p, "vin", "chasis", "chassis")
    # Variable Nexor combinada "Marca, modelo y año del vehículo"
    vehiculo_combo = _pick_fuzzy(p, "marca", "modelo", "vehiculo del", "vehículo del")
    # Variable Nexor "Nivel de urgencia"
    urgencia = _pick(p, "urgencia", "urgency", "prioridad") or _pick_fuzzy(p, "urgencia", "urgency", "prioridad")
    # external_id: el id estable del lead en Nexor (external_id o ld_...)
    external_id = _pick(lead_obj, "external_id", "id", "lead_id", "leadId", "uuid", "_id")
    canal = "Nexor"

    # Patente y repuestos (campos personalizados de Nexor, vienen en metadata)
    patente = _pick(p, "patente", "plate", "license_plate", "licenseplate", "ppu", "matricula", "placa") \
        or _pick_fuzzy(p, "patente", "plate", "matricula", "matrícula", "placa", "ppu")
    repuestos_raw = _pick(
        p, "repuestos", "repuesto", "partes", "parts", "piezas", "productos",
        "repuestos_solicitados", "parts_needed", "items_solicitados", "necesita", "requerimiento",
    ) or _pick_fuzzy(p, "repuesto", "pieza")
    repuestos_list = _parse_repuestos(repuestos_raw)

    # comentario: mensaje libre, o resumen (estado Nexor + empresa + campaña + origen)
    comentario = _pick(p, "message", "comentario", "notes", "note", "comment", "mensaje", "description")
    if not comentario:
        bits = []
        if estado_nexor: bits.append(f"Estado Nexor: {estado_nexor}")
        if lead_obj.get("company"): bits.append(f"Empresa: {lead_obj.get('company')}")
        camp = "/".join([x for x in [meta.get("utm_source"), meta.get("utm_campaign")] if x])
        if camp: bits.append(f"Campaña: {camp}")
        if lead_obj.get("source"): bits.append(f"Origen: {lead_obj.get('source')}")
        comentario = " · ".join(bits) or None
    prefix = []
    if patente: prefix.append(f"Patente: {patente}")
    if urgencia: prefix.append(f"Urgencia: {urgencia}")
    if prefix:
        comentario = " · ".join(prefix) + (f" · {comentario}" if comentario else "")

    log.external_id = str(external_id) if external_id else None
    # Se GUARDA acá, antes de cualquier reintento: cada vuelta hace `db.rollback()`, y
    # con la asignación sin confirmar la fila quedaba con external_id NULL — o sea, la
    # llave que sostiene la idempotencia contra una reentrega de Nexor se perdía justo
    # cuando hubo problemas, que es cuando Nexor reintenta.
    db.commit()

    # ── Idempotencia: si ese external_id ya se procesó, no duplicar ───────────
    if external_id:
        prev = (
            db.query(MonzaWebhookLog)
            .filter(MonzaWebhookLog.external_id == str(external_id),
                    MonzaWebhookLog.procesado == 1, MonzaWebhookLog.id != log.id)
            .first()
        )
        if prev:
            log.procesado = 1
            log.lead_numero = prev.lead_numero
            log.error = "duplicado (external_id ya procesado)"
            db.commit()
            return {"ok": True, "duplicado": True, "lead_numero": prev.lead_numero}

    if not nombre and not telefono and not email:
        log.error = "payload sin nombre/telefono/email — revisar mapeo en monza_webhook_log"
        db.commit()
        return {"ok": False, "error": "Sin datos de contacto reconocibles. Payload capturado para revisión.", "log_id": log.id}

    # El reintento va acá, envolviendo TODO el procesamiento, igual que en POST /leads.
    # Esta puerta es la que MÁS lo necesita: corre sola, sin operador que reintente a
    # mano, y su carrera natural es contra el vendedor que está registrando al mismo
    # cliente en ese momento. Cuando el reintento vivía escrito dentro del endpoint del
    # vendedor, ESTE camino se quedó sin ninguna protección y perdía 4 de cada 6 leads
    # simultáneos — el mismo error de «arreglar una sola de las dos copias» que causó el
    # bug original. Ahora las dos puertas llaman al MISMO helper (monza_correlativos).
    # La función es anidada a propósito: el procesamiento usa una docena de variables
    # locales ya calculadas, y pasarlas por parámetro sería un diff grande y frágil.
    def _procesar():
        # Cliente: dedup por teléfono o email. TERCERA puerta que crea fichas, y la única
        # que corre sin un operador mirando — razón de más para que decida igual que las
        # otras dos (POST /clientes y el «cliente al vuelo» del lead). Comparaba el
        # teléfono LITERAL, así que un lead de Nexor con el número en otro formato
        # ('+56 9 6229 3336' contra '962293336') no encontraba la ficha que ya existía y
        # creaba una duplicada — justo lo que las otras dos puertas dejaron de hacer.
        # `buscar_ficha_por_telefono` normaliza los dos lados y descarta los números que
        # no identifican a nadie (menos de 8 dígitos: '-', '0', '2342').
        cliente = None
        if telefono:
            cliente = buscar_ficha_por_telefono(db, MonzaCliente, telefono)
        if not cliente and email:
            cliente = db.query(MonzaCliente).filter(MonzaCliente.email == email).first()

        # Un número compartido (la recepción de un taller, el celular de un gestor) puede
        # pegar el lead a la ficha de OTRO contribuyente, y acá no hay RUT en el payload
        # con el que desempatar ni operador que lo note. No se bloquea —cortar la entrada
        # automática de leads es peor— pero queda anotado en el log del webhook para que
        # alguien lo revise: es el único rastro posible de una fusión dudosa.
        aviso_revisar = None
        if (cliente and nombre and cliente.nombre
                and rut_identidad(getattr(cliente, "rut", None))
                and nombre.strip().lower() != cliente.nombre.strip().lower()):
            aviso_revisar = (f"REVISAR: lead de «{nombre}» vinculado por teléfono a la ficha "
                             f"«{cliente.nombre}» (RUT {cliente.rut}) — confirmar si es el mismo cliente")
            log.error = aviso_revisar[:400]

        # Dedup de LEAD: si ya existe un lead de Nexor para este cliente
        # (mismo contacto), actualizar su estado en vez de crear un duplicado.
        if cliente:
            existente = (
                db.query(MonzaLead)
                .filter(MonzaLead.cliente_id == cliente.id, MonzaLead.canal_origen == "Nexor")
                .order_by(MonzaLead.id.desc())
                .first()
            )
            if existente:
                if comentario:
                    existente.comentario = comentario
                existente.fecha_actualizacion = datetime.utcnow()
                # Si llegan repuestos y el lead aún no tiene ítems, agregarlos
                if repuestos_list and db.query(MonzaLeadItem).filter(MonzaLeadItem.lead_id == existente.id).count() == 0:
                    for desc_r in repuestos_list:
                        db.add(MonzaLeadItem(lead_id=existente.id, descripcion=desc_r[:255], cantidad=1, calidad="sin_calificar"))
                log.procesado = 1
                log.lead_id = existente.id
                log.lead_numero = existente.numero
                # Se CONCATENA, no se pisa: el aviso de fusión dudosa es justo el que
                # importa en el caso repetido —la ficha equivocada ya viene arrastrada de
                # la entrega anterior— y era el que se perdía.
                _nota = "lead Nexor existente actualizado (mismo contacto)"
                log.error = (f"{aviso_revisar} · {_nota}" if aviso_revisar else _nota)[:400]
                db.commit()
                return {"ok": True, "actualizado": True, "lead_numero": existente.numero, "lead_id": existente.id}

        if not cliente:
            cliente = MonzaCliente(nombre=nombre or telefono or email, telefono=telefono, email=email)
            db.add(cliente)
            db.flush()

        vehiculo = vehiculo_combo or (" ".join([x for x in [marca, modelo] if x]).strip() or None)
        lead = MonzaLead(
            cliente_id=cliente.id,
            vehiculo=vehiculo, vehicle_label=vehiculo, marca=marca, modelo=modelo,
            anio=str(anio) if anio else None, vin=vin,
            canal_origen=canal, estado="pendiente", comentario=comentario, linea="autos",
        )
        # MISMA puerta que POST /leads (monza_correlativos): la carrera del correlativo
        # es justo entre este camino autónomo y el del vendedor, así que arreglar solo
        # uno de los dos no habría servido de nada.
        agregar_lead_con_numero(db, lead)

        # Repuestos solicitados → ítems del lead
        for desc_r in repuestos_list:
            db.add(MonzaLeadItem(lead_id=lead.id, descripcion=desc_r[:255], cantidad=1, calidad="sin_calificar"))

        log.procesado = 1
        log.lead_id = lead.id
        log.lead_numero = lead.numero
        db.commit()

        # Notificación in-app
        try:
            from monza_notif import crear_notif
            crear_notif(db, f"Nuevo lead desde Nexor · {lead.numero}", f"{nombre or telefono or email}", "info", "/monzaparts/leads", "lead", lead.id)
        except Exception:
            pass

        return {"ok": True, "lead_numero": lead.numero, "lead_id": lead.id}

    try:
        return reintentar_carrera(db, _procesar, que="leads")
    except HTTPException as e:
        # Para un llamador AUTOMÁTICO, un 4xx significa «no insistas, el pedido está mal»
        # — y este 409 dice justo lo contrario: «estaba ocupado, vuelve a intentar». Se
        # traduce a 503, que es el código que le pide a Nexor reintentar. El payload ya
        # quedó guardado, así que el lead se puede recuperar a mano igual.
        if e.status_code == 409:
            log2 = db.query(MonzaWebhookLog).filter(MonzaWebhookLog.id == log.id).first()
            if log2:
                log2.error = "creación simultánea: no se pudo asignar correlativo tras varios intentos"
                db.commit()
            raise HTTPException(
                status_code=503,
                detail="Sistema ocupado procesando otros leads. Reintenta el envío.",
            )
        raise
    except Exception as e:
        db.rollback()
        # La fila del log YA está commiteada, así que el rollback no se la lleva y esta
        # relectura siempre la encuentra: se le anota el motivo del fallo y queda
        # disponible en el visor para reprocesar el lead a mano.
        log2 = db.query(MonzaWebhookLog).filter(MonzaWebhookLog.id == log.id).first()
        if log2:
            log2.error = str(e)[:400]
            db.commit()
        # El detalle del error NO viaja al llamador: un error de base de datos arrastra el
        # INSERT completo con los nombres de las columnas y los datos del cliente, y eso
        # es el esquema servido en bandeja a quien tenga la API key. El motivo entero
        # queda en el log, que es donde el operador lo necesita.
        raise HTTPException(
            status_code=500,
            detail="Error procesando el lead. El payload quedó guardado para revisión.",
        )


@router.get("/nexor/log")
def ver_log(limit: int = 30, db: Session = Depends(get_db),
            _=Depends(require_empresa("automotriz"))):
    """Inspeccionar los últimos webhooks recibidos (para finalizar el mapeo).

    El candado va EN ESTE ENDPOINT y no en el router: el webhook de arriba lo llama
    Nexor, que se autentica con `X-API-Key` y no tiene sesión de usuario — candar el
    router entero dejaría de recibir leads. Acá el dato sí es sensible: el log guarda el
    payload y las cabeceras crudas de cada lead (nombre, teléfono y vehículo del cliente).
    """
    rows = db.query(MonzaWebhookLog).order_by(desc(MonzaWebhookLog.id)).limit(limit).all()
    return [
        {
            "id": r.id, "fuente": r.fuente, "procesado": bool(r.procesado),
            "lead_numero": r.lead_numero, "external_id": r.external_id, "error": r.error,
            "payload": r.payload, "headers": r.headers,
            "fecha": r.fecha.isoformat() if r.fecha else None,
        }
        for r in rows
    ]
