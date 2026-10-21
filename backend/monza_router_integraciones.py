"""
Integraciones MonzaParts. Receptores de leads:
  · Nexor     POST /nexor/leads      — mapeo flexible del payload de Nexor (auto-documentado).
                                       Auth: X-API-Key (o ?key=) == settings.NEXOR_WEBHOOK_KEY
  · PartsFlow POST /partsflow/leads  — contrato ESTRICTO, versión 1 (documento «Integración
                                       PartsFlow → MonzaParts: creación de leads», 2026-10-21).
                                       Auth: SOLO X-API-Key == settings.PARTSFLOW_API_KEY
Los dos guardan el payload crudo en monza_webhook_log antes de procesar.
"""
import hmac
import json
import re
import threading
import time
from collections import deque
from fastapi import APIRouter, Depends, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List, Literal, Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_correlativos import agregar_lead_con_numero, reintentar_carrera
from monza_rut import rut_identidad, buscar_ficha_por_rut
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


# ══ PartsFlow (2026-10-21) ════════════════════════════════════════════════════════════
# WhatsApp IA de repuestos (Meta Business Partner): el agente pulsa «Crear lead» y PartsFlow
# llama a este endpoint. A DIFERENCIA de Nexor, el contrato lo escribimos nosotros (el
# documento entregado al proveedor), así que acá se VALIDA en vez de adivinar: un dato
# mal formado vuelve 422 con el campo exacto, y PartsFlow lo corrige de su lado.
# Lo que se reutiliza de Nexor, por ser lo ya probado en producción: el payload crudo se
# guarda ANTES de procesar, el cliente se dedupea con las puertas únicas de la casa y el
# correlativo del lead pasa por agregar_lead_con_numero + reintentar_carrera.

PF_FUENTE = "partsflow"
PF_CANAL = "PartsFlow"
PF_MAX_BYTES = 64 * 1024
PF_MAX_POR_MINUTO = 60

_E164 = re.compile(r"\+[1-9]\d{7,14}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_ID_EXTERNO = re.compile(r"[A-Za-z0-9._:\-]{1,120}")


def _dv_rut(cuerpo: str) -> str:
    s, m = 0, 2
    for d in reversed(cuerpo):
        s += int(d) * m
        m = 2 if m == 7 else m + 1
    dv = 11 - s % 11
    return "0" if dv == 11 else "K" if dv == 10 else str(dv)


class _PFContacto(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    nombre: str = Field(min_length=1, max_length=200)
    telefono: str = Field(max_length=20)
    email: Optional[str] = Field(None, max_length=150)
    rut: Optional[str] = Field(None, max_length=12)
    empresa: Optional[str] = Field(None, max_length=200)

    @field_validator("telefono")
    @classmethod
    def _telefono(cls, v):
        if not _E164.fullmatch(v):
            raise ValueError("Debe venir en formato E.164, por ejemplo +56912345678")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        if v and not _EMAIL.fullmatch(v):
            raise ValueError("No es un correo válido")
        return v or None

    @field_validator("rut")
    @classmethod
    def _rut(cls, v):
        if not v:
            return None
        ident = rut_identidad(v)
        if not ident or _dv_rut(ident[:-1]) != ident[-1]:
            raise ValueError("RUT inválido (revisa el dígito verificador), por ejemplo 76.123.456-7")
        return v


class _PFVehiculo(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    patente: Optional[str] = Field(None, max_length=10)
    vin: Optional[str] = Field(None, max_length=17)
    marca: Optional[str] = Field(None, max_length=100)
    modelo: Optional[str] = Field(None, max_length=100)
    anio: Optional[int] = Field(None, ge=1950, le=datetime.utcnow().year + 1)
    motor: Optional[str] = Field(None, max_length=100)

    @field_validator("patente", "vin")
    @classmethod
    def _alfanumerico(cls, v):
        if v and not v.isalnum():
            raise ValueError("Solo letras y números, sin guiones ni espacios")
        return v.upper() if v else None


class _PFRepuesto(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    descripcion: str = Field(min_length=1, max_length=300)
    cantidad: int = Field(1, ge=1, le=999)
    numero_parte: Optional[str] = Field(None, max_length=100)
    marca: Optional[str] = Field(None, max_length=100)


class _PFLead(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    external_id: str
    creado_en: Optional[datetime] = None
    contacto: _PFContacto
    vehiculo: Optional[_PFVehiculo] = None
    repuestos: List[_PFRepuesto] = Field(default_factory=list, max_length=50)
    comentario: Optional[str] = Field(None, max_length=2000)
    urgencia: Literal["normal", "alta"] = "normal"
    url_conversacion: Optional[str] = Field(None, max_length=500)

    @field_validator("external_id")
    @classmethod
    def _external_id(cls, v):
        if not _ID_EXTERNO.fullmatch(v):
            raise ValueError("Entre 1 y 120 caracteres: letras, números y . _ : -")
        return v

    @field_validator("url_conversacion")
    @classmethod
    def _url(cls, v):
        if v and not v.lower().startswith("https://"):
            raise ValueError("Debe ser una dirección https://")
        return v or None


def _pf_campo(loc) -> str:
    """('repuestos', 0, 'descripcion') -> 'repuestos[0].descripcion', como lo nombra el documento."""
    texto = ""
    for parte in loc:
        texto += f"[{parte}]" if isinstance(parte, int) else (f".{parte}" if texto else str(parte))
    return texto or "cuerpo"


def _pf_mensaje(err: dict) -> str:
    tipo, ctx = err.get("type", ""), err.get("ctx") or {}
    if tipo == "missing":
        return "Es obligatorio"
    if tipo == "string_too_short":
        return "No puede venir vacío"
    if tipo == "string_too_long":
        return f"Supera el máximo de {ctx.get('max_length')} caracteres"
    if tipo == "too_long":
        return f"Máximo {ctx.get('max_length')} ítems"
    if tipo in ("int_parsing", "int_type", "int_from_float"):
        return "Debe ser un número entero"
    if tipo == "greater_than_equal":
        return f"Debe ser mayor o igual a {ctx.get('ge')}"
    if tipo == "less_than_equal":
        return f"Debe ser menor o igual a {ctx.get('le')}"
    if tipo == "literal_error":
        return f"Valor no permitido; use {ctx.get('expected')}"
    if tipo.startswith("datetime"):
        return "Fecha inválida; use ISO 8601, por ejemplo 2026-10-21T15:42:00-03:00"
    if tipo in ("model_type", "dict_type", "model_attributes_type"):
        return "Debe ser un objeto JSON"
    if tipo == "list_type":
        return "Debe ser una lista"
    if tipo == "string_type":
        return "Debe ser texto"
    if tipo == "value_error":
        return str(err.get("msg", "")).removeprefix("Value error, ")
    return str(err.get("msg", "Valor inválido"))


# Límite de uso: 60 solicitudes por minuto. En MEMORIA del proceso, igual que el
# limitador del login: supone UN proceso uvicorn (--workers 1), y un restart lo vacía.
_PF_MARCAS: deque = deque()
_PF_LOCK = threading.Lock()


def _pf_cupo() -> int:
    """0 si hay cupo (y lo toma), o los segundos a esperar."""
    ahora = time.time()
    with _PF_LOCK:
        while _PF_MARCAS and _PF_MARCAS[0] <= ahora - 60:
            _PF_MARCAS.popleft()
        if len(_PF_MARCAS) >= PF_MAX_POR_MINUTO:
            return max(1, int(_PF_MARCAS[0] + 60 - ahora) + 1)
        _PF_MARCAS.append(ahora)
        return 0


# Un candado por external_id: el doble clic llega en dos peticiones casi simultáneas, y sin
# esto las dos verían «no procesado» y crearían dos leads. Por proceso (ver arriba).
_PF_CANDADOS: dict = {}
_PF_CANDADOS_LOCK = threading.Lock()


def _pf_candado(external_id: str) -> threading.Lock:
    with _PF_CANDADOS_LOCK:
        if len(_PF_CANDADOS) > 1000:  # techo: los candados libres se descartan
            for k in [k for k, c in _PF_CANDADOS.items() if not c.locked()]:
                del _PF_CANDADOS[k]
        return _PF_CANDADOS.setdefault(external_id, threading.Lock())


def _pf_error(status: int, detalle: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "detail": detalle, **extra})


@router.post("/partsflow/leads")
async def recibir_lead_partsflow(request: Request, db: Session = Depends(get_db),
                                 x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    # Clave SOLO por cabecera (a diferencia de Nexor, nunca ?key=: una URL queda en logs).
    # compare_digest: el tiempo de la comparación no dice cuántos caracteres acertó.
    clave = settings.PARTSFLOW_API_KEY or ""
    if not clave or not x_api_key or not hmac.compare_digest(x_api_key.encode(), clave.encode()):
        return _pf_error(401, "Clave ausente o incorrecta")

    espera = _pf_cupo()
    if espera:
        return JSONResponse(status_code=429, headers={"Retry-After": str(espera)},
                            content={"ok": False, "detail": f"Demasiadas solicitudes. Reintenta en {espera} segundos."})

    raw = await request.body()
    if len(raw) > PF_MAX_BYTES:
        return _pf_error(413, "El cuerpo supera 64 KB")
    try:
        cuerpo = json.loads(raw or b"")
    except (ValueError, UnicodeDecodeError):
        cuerpo = None
    if not isinstance(cuerpo, dict):
        return _pf_error(400, "El cuerpo debe ser un objeto JSON válido")

    # Se GUARDA antes de validar y de procesar: un 422 o un 500 quedan con su payload para
    # revisar con el proveedor (mismo criterio que Nexor; la clave no se guarda).
    cabeceras = {k: v for k, v in request.headers.items() if k.lower() not in ("x-api-key", "authorization", "cookie")}
    log = MonzaWebhookLog(fuente=PF_FUENTE, headers=json.dumps(cabeceras, ensure_ascii=False)[:4000],
                          payload=raw.decode("utf-8", "replace")[:8000])
    log.external_id = str(cuerpo.get("external_id"))[:120] if cuerpo.get("external_id") else None
    db.add(log)
    db.commit()

    try:
        datos = _PFLead.model_validate(cuerpo)
    except ValidationError as e:
        errores = [{"campo": _pf_campo(err.get("loc", ())), "mensaje": _pf_mensaje(err)} for err in e.errors()]
        log.error = ("422: " + "; ".join(f"{x['campo']}: {x['mensaje']}" for x in errores))[:400]
        db.commit()
        return JSONResponse(status_code=422, content={"ok": False, "errores": errores})

    c, v = datos.contacto, datos.vehiculo or _PFVehiculo()
    with _pf_candado(datos.external_id):
        previo = (db.query(MonzaWebhookLog)
                  .filter(MonzaWebhookLog.fuente == PF_FUENTE, MonzaWebhookLog.external_id == datos.external_id,
                          MonzaWebhookLog.procesado == 1, MonzaWebhookLog.id != log.id)
                  .order_by(MonzaWebhookLog.id).first())
        if previo:
            log.procesado, log.lead_id, log.lead_numero = 1, previo.lead_id, previo.lead_numero
            log.error = "duplicado (external_id ya procesado)"
            db.commit()
            return JSONResponse(status_code=200, content={"ok": True, "lead_numero": previo.lead_numero,
                                                          "duplicado": True})

        def _procesar():
            # Cliente: RUT (evidencia fuerte) → teléfono → email, con las puertas únicas de
            # la casa. buscar_ficha_por_telefono no fusiona fichas con RUT contradictorio.
            cliente = buscar_ficha_por_rut(db, MonzaCliente, c.rut) if c.rut else None
            if not cliente:
                cliente = buscar_ficha_por_telefono(db, MonzaCliente, c.telefono, rut_tecleado=c.rut)
            if not cliente and c.email:
                cliente = db.query(MonzaCliente).filter(MonzaCliente.email == c.email).first()
            existente = cliente is not None
            if cliente:
                if c.rut and not rut_identidad(cliente.rut):
                    cliente.rut = c.rut          # el RUT recibido COMPLETA la ficha sin RUT
                if c.email and not cliente.email:
                    cliente.email = c.email
                if (cliente.nombre and c.nombre.strip().lower() != cliente.nombre.strip().lower()):
                    log.error = (f"REVISAR: lead de «{c.nombre}» vinculado a la ficha «{cliente.nombre}» "
                                 "por RUT/teléfono/email — confirmar si es el mismo cliente")[:400]
            else:
                cliente = MonzaCliente(nombre=c.nombre, telefono=c.telefono, email=c.email, rut=c.rut)
                db.add(cliente)
                db.flush()

            notas = []
            if v.patente:
                notas.append(f"Patente: {v.patente}")
            if v.motor:
                notas.append(f"Motor: {v.motor}")
            if datos.urgencia == "alta":
                notas.append("Urgencia: ALTA")
            if c.empresa:
                notas.append(f"Empresa: {c.empresa}")
            if datos.url_conversacion:
                notas.append(f"Conversación PartsFlow: {datos.url_conversacion}")
            comentario = " · ".join(notas)
            if datos.comentario:
                comentario = f"{comentario}\n{datos.comentario}" if comentario else datos.comentario

            vehiculo = " ".join(x for x in (v.marca, v.modelo) if x) or None
            lead = MonzaLead(cliente_id=cliente.id, vehiculo=vehiculo, vehicle_label=vehiculo,
                             marca=v.marca, modelo=v.modelo, anio=str(v.anio) if v.anio else None,
                             vin=v.vin, canal_origen=PF_CANAL, estado="pendiente",
                             comentario=comentario or None, linea="autos")
            agregar_lead_con_numero(db, lead)
            for r in datos.repuestos:
                db.add(MonzaLeadItem(lead_id=lead.id, descripcion=r.descripcion, cantidad=r.cantidad,
                                     numero_parte=r.numero_parte, marca=r.marca, calidad="sin_calificar"))
            log.procesado, log.lead_id, log.lead_numero = 1, lead.id, lead.numero
            db.commit()
            try:
                from monza_notif import crear_notif
                crear_notif(db, f"Nuevo lead desde PartsFlow · {lead.numero}", c.nombre, "info",
                            "/monzaparts/leads", "lead", lead.id)
            except Exception:
                pass
            return JSONResponse(status_code=201, content={"ok": True, "lead_numero": lead.numero,
                                                          "duplicado": False, "cliente_existente": existente})

        try:
            return reintentar_carrera(db, _procesar, que="leads")
        except HTTPException as e:
            if e.status_code == 409:   # correlativo ocupado tras varios intentos: «reintenta»
                db.rollback()
                fila = db.query(MonzaWebhookLog).filter(MonzaWebhookLog.id == log.id).first()
                if fila:
                    fila.error = "creación simultánea: no se pudo asignar correlativo"
                    db.commit()
                return _pf_error(503, "Sistema ocupado. Reintenta el envío.")
            raise
        except Exception as e:
            db.rollback()
            fila = db.query(MonzaWebhookLog).filter(MonzaWebhookLog.id == log.id).first()
            if fila:
                fila.error = str(e)[:400]
                db.commit()
            # Sin el detalle: arrastraría el INSERT con datos del cliente (igual que Nexor).
            return _pf_error(500, "Error procesando el lead. La solicitud quedó guardada para revisión.")


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
