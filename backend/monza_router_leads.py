from fastapi import Request, APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_, and_
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta

from database import get_db
from auth import get_current_user
from monza_models import (
    MonzaLead, MonzaLeadItem, MonzaLeadActividad,
    MonzaProximoPaso, MonzaCliente, MonzaCotizacion, MonzaAsesor
)
from models.models import User

router = APIRouter(prefix="/api/monza/leads", tags=["monza-leads"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class ClienteQuick(BaseModel):
    nombre: str
    telefono: Optional[str] = None
    email: Optional[str] = None
    rut: Optional[str] = None

class ItemUpdate(BaseModel):
    descripcion: Optional[str] = None
    numero_parte: Optional[str] = None
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    calidad: Optional[str] = None
    cantidad: Optional[int] = None
    plazo_entrega: Optional[str] = None

class ItemIn(BaseModel):
    descripcion: str
    numero_parte: Optional[str] = None
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    calidad: str = "sin_calificar"
    cantidad: int = 1
    plazo_entrega: Optional[str] = None

class LeadCreate(BaseModel):
    canal_origen: str = "WhatsApp"
    cliente_id: Optional[int] = None
    cliente: Optional[ClienteQuick] = None  # crear cliente al vuelo
    vehiculo: Optional[str] = None
    marca: Optional[str] = None
    modelo: Optional[str] = None
    anio: Optional[str] = None
    vin: Optional[str] = None
    linea: Optional[str] = None
    comentario: Optional[str] = None
    items: List[ItemIn] = []

class LeadUpdate(BaseModel):
    estado: Optional[str] = None
    vehiculo: Optional[str] = None
    vin: Optional[str] = None
    canal_origen: Optional[str] = None
    asesor_id: Optional[int] = None
    linea: Optional[str] = None
    comentario: Optional[str] = None

class ProximoPasoIn(BaseModel):
    tipo: str  # llamada/whatsapp/email/visita
    cuando: Optional[datetime] = None
    asesor_id: Optional[int] = None

class ActividadIn(BaseModel):
    tipo: str  # nota/llamada/whatsapp/email/visita
    descripcion: str

class ClienteUpdate(BaseModel):
    nombre: Optional[str] = None
    rut: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    vehiculos: Optional[List[dict]] = None
    etiquetas: Optional[List[str]] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gen_numero_lead(db: Session) -> str:
    anio = datetime.utcnow().year
    last = (
        db.query(MonzaLead)
        .filter(MonzaLead.numero.like(f"L-{anio}-%"))
        .order_by(MonzaLead.id.desc())
        .first()
    )
    if last:
        n = int(last.numero.split("-")[-1]) + 1
    else:
        n = 1
    return f"L-{anio}-{n:04d}"


def _get_asesor_id(db: Session, user_id: int) -> int | None:
    """
    Busca el MonzaAsesor asociado al usuario logueado.
    Intenta match por user_id, luego por email, luego devuelve el primero activo.
    """
    from models.models import User as _User
    asesor = db.query(MonzaAsesor).filter(MonzaAsesor.user_id == user_id).first()
    if asesor:
        return asesor.id
    # Intentar match por email del user
    user = db.query(_User).filter(_User.id == user_id).first()
    if user:
        asesor = db.query(MonzaAsesor).filter(
            MonzaAsesor.email == user.email
        ).first()
        if asesor:
            return asesor.id
    # Fallback: primer asesor activo
    asesor = db.query(MonzaAsesor).filter(MonzaAsesor.activo == True).first()  # noqa: E712
    return asesor.id if asesor else None


def _lead_dict(lead: MonzaLead) -> dict:
    asesor_nombre = None
    if lead.asesor:
        asesor_nombre = lead.asesor.nombre  # MonzaAsesor.nombre es el nombre real

    items_count = len(lead.items)
    # Total estimado = suma de precios con precio_clp
    total = sum(
        (it.precio_clp or 0) * it.cantidad for it in lead.items if it.precio_clp
    )

    # Próximo paso próximo
    proximos = [p for p in lead.proximos_pasos if not p.completado]
    proximo = None
    if proximos:
        prox = sorted(proximos, key=lambda p: p.cuando or datetime.max)[0]
        proximo = {
            "tipo": prox.tipo,
            "cuando": prox.cuando.isoformat() if prox.cuando else None,
        }

    # Días sin contactar
    delta = (datetime.utcnow() - lead.fecha_actualizacion).days

    return {
        "id": lead.id,
        "numero": lead.numero,
        "estado": lead.estado,
        "canal_origen": lead.canal_origen,
        "vehiculo": lead.vehiculo,
        "marca": lead.marca,
        "modelo": lead.modelo,
        "anio": lead.anio,
        "vin": lead.vin,
        "linea": lead.linea,
        "comentario": lead.comentario,
        "total_estimado": total,
        "items_count": items_count,
        "asesor_id": lead.asesor_id,
        "asesor": asesor_nombre,
        "proximo_paso": proximo,
        "sin_contactar_dias": delta,
        # Flete aéreo elegido para ESTA cotización (uno solo para todos sus ítems).
        # NULL = nunca se eligió y manda la configuración global, que es como se
        # comportaba antes de que la moneda del flete fuera seleccionable.
        "moneda_tarifa": lead.moneda_tarifa,
        "tarifa_aerea": lead.tarifa_aerea,
        "fecha_creacion": lead.fecha_creacion.isoformat(),
        "fecha_actualizacion": lead.fecha_actualizacion.isoformat(),
        "cliente": _cliente_dict(lead.cliente) if lead.cliente else None,
    }


def _cliente_dict(c: MonzaCliente) -> dict:
    return {
        "id": c.id,
        "nombre": c.nombre,
        "rut": c.rut,
        "telefono": c.telefono,
        "email": c.email,
        "vehiculos": c.vehiculos or [],
        "etiquetas": c.etiquetas or [],
        "ltv": c.ltv or 0,
        "leads_total": c.leads_total or 0,
        "vendidos_total": c.vendidos_total or 0,
        "fecha_creacion": c.fecha_creacion.isoformat() if c.fecha_creacion else None,
    }


def _item_dict(it: MonzaLeadItem) -> dict:
    return {
        "id": it.id,
        "descripcion": it.descripcion,
        "numero_parte": it.numero_parte,
        "marca": it.marca,
        "procedencia": it.procedencia,
        "calidad": it.calidad,
        "cantidad": it.cantidad,
        "precio_clp": it.precio_clp,
        "plazo_entrega": it.plazo_entrega,
        # Parámetros con que la calculadora obtuvo el precio. Viajan para que al
        # REABRIRLA se restaure lo que el operador puso (costo en su moneda, peso,
        # margen) en vez del camino de emergencia que mostraba el precio final como si
        # fuera el costo, en CLP. NULL en los ítems anteriores a la migración
        # monza_cotizador_parametros: ahí la pantalla cae al comportamiento viejo.
        "costo": it.costo,
        "moneda": it.moneda,
        "peso_kg": it.peso_kg,
        "markup_pct": it.markup_pct,
        "tc_aplicado": it.tc_aplicado,
    }


# ── KPIs ──────────────────────────────────────────────────────────────────────


# ── Log helper ────────────────────────────────────────────────────────────────
def _log(db, user_email, accion, entidad, entidad_id=None, entidad_ref=None, detalle=None, request=None):
    from monza_models import MonzaLog
    ip = None
    if request:
        forwarded = request.headers.get("X-Forwarded-For")
        ip = forwarded.split(",")[0].strip() if forwarded else getattr(request.client, "host", None)
    lg = MonzaLog(user_email=user_email, accion=accion, entidad=entidad,
                  entidad_id=entidad_id, entidad_ref=entidad_ref, detalle=detalle, ip=ip)
    db.add(lg)
    db.commit()

@router.get("/kpis")
def get_kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    anio = datetime.utcnow().year
    mes = datetime.utcnow().month
    inicio_mes = datetime(anio, mes, 1)

    nuevos_mes = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.fecha_creacion >= inicio_mes
    ).scalar() or 0

    en_proceso = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.estado.in_(["pendiente", "en_proceso"])
    ).scalar() or 0

    vendidos_mes = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "vendida",
        MonzaCotizacion.fecha_venta >= inicio_mes,
    ).scalar() or 0

    total_mes = nuevos_mes if nuevos_mes else 1
    tasa = round((vendidos_mes / total_mes) * 100, 1)

    sin_contactar = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.estado.in_(["pendiente", "en_proceso"]),
        MonzaLead.fecha_actualizacion <= datetime.utcnow() - timedelta(days=3),
    ).scalar() or 0

    # Total cotizado del mes
    desde = inicio_mes
    total_cotizado = db.query(func.sum(MonzaCotizacion.total_bruto)).filter(
        MonzaCotizacion.fecha_creacion >= desde,
        MonzaCotizacion.estado == "vendida",
    ).scalar() or 0

    pendientes = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.estado == "pendiente"
    ).scalar() or 0
    en_trabajo = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.estado == "en_proceso"
    ).scalar() or 0

    return {
        "nuevos_mes": nuevos_mes,
        "en_proceso": en_proceso,
        "vendidos_mes": vendidos_mes,
        "tasa_cierre_pct": tasa,
        "total_cotizado_mes": total_cotizado,
        "sin_contactar_3d": sin_contactar,
        "pendientes": pendientes,
        "en_trabajo": en_trabajo,
    }


# ── List leads ────────────────────────────────────────────────────────────────

@router.get("")
def list_leads(
    q: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
    asesor_id: Optional[int] = Query(None),
    etiqueta: Optional[str] = Query(None),
    solo_mios: bool = Query(False),
    sin_contactar: bool = Query(False),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = (
        db.query(MonzaLead)
        .options(
            joinedload(MonzaLead.cliente),
            joinedload(MonzaLead.asesor),
            joinedload(MonzaLead.items),
            joinedload(MonzaLead.proximos_pasos),
        )
    )

    if q:
        query = query.join(MonzaLead.cliente, isouter=True).filter(
            or_(
                MonzaLead.numero.ilike(f"%{q}%"),
                MonzaCliente.nombre.ilike(f"%{q}%"),
                MonzaCliente.telefono.ilike(f"%{q}%"),
                MonzaLead.vehiculo.ilike(f"%{q}%"),
            )
        )

    if estado and estado != "todos":
        query = query.filter(MonzaLead.estado == estado)

    if asesor_id:
        query = query.filter(MonzaLead.asesor_id == asesor_id)

    if solo_mios:
        query = query.filter(MonzaLead.asesor_id == current_user.id)

    if sin_contactar:
        cutoff = datetime.utcnow() - timedelta(days=3)
        query = query.filter(
            MonzaLead.estado.in_(["pendiente", "en_proceso"]),
            MonzaLead.fecha_actualizacion <= cutoff,
        )

    if desde:
        query = query.filter(MonzaLead.fecha_creacion >= datetime.fromisoformat(desde))
    if hasta:
        query = query.filter(MonzaLead.fecha_creacion <= datetime.fromisoformat(hasta))

    total = query.count()
    leads = query.order_by(MonzaLead.fecha_creacion.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_lead_dict(l) for l in leads],
    }


# ── Create lead ───────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_lead(body: LeadCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # Resolver cliente
    cliente_id = body.cliente_id
    if not cliente_id and body.cliente:
        # Buscar por teléfono o RUT
        cliente = None
        if body.cliente.telefono:
            cliente = db.query(MonzaCliente).filter(
                MonzaCliente.telefono == body.cliente.telefono
            ).first()
        if not cliente and body.cliente.rut:
            cliente = db.query(MonzaCliente).filter(
                MonzaCliente.rut == body.cliente.rut
            ).first()
        if not cliente:
            cliente = MonzaCliente(
                nombre=body.cliente.nombre,
                telefono=body.cliente.telefono,
                email=body.cliente.email,
                rut=body.cliente.rut,
            )
            db.add(cliente)
            db.flush()
        cliente_id = cliente.id

    lead = MonzaLead(
        numero=_gen_numero_lead(db),
        cliente_id=cliente_id,
        vehiculo=(f"{body.marca} {body.modelo}".strip() if (body.marca or body.modelo) else body.vehiculo),
        marca=body.marca,
        modelo=body.modelo,
        anio=body.anio,
        vin=body.vin,
        canal_origen=body.canal_origen,
        asesor_id=_get_asesor_id(db, current_user.id),
        estado="pendiente",
        comentario=body.comentario,
        linea=body.linea,
    )
    db.add(lead)
    db.flush()

    for it in body.items:
        if it.descripcion.strip():
            db.add(MonzaLeadItem(lead_id=lead.id, **it.model_dump()))

    # Actividad inicial
    asesor_nombre = current_user.email.split("@")[0].title()
    db.add(MonzaLeadActividad(
        lead_id=lead.id,
        tipo="lead_creado",
        descripcion=f"Lead creado por {asesor_nombre}",
        usuario=asesor_nombre,
        usuario_id=current_user.id,
    ))

    # Actualizar stats cliente
    if cliente_id:
        c = db.query(MonzaCliente).filter(MonzaCliente.id == cliente_id).first()
        if c:
            c.leads_total = (c.leads_total or 0) + 1

    db.commit()
    db.refresh(lead)
    _log(db, current_user.email, "CREATE", "lead",
         lead.id, lead.numero, f"Lead {lead.numero} creado")
    return _lead_dict(lead)


# ── Get lead detail ───────────────────────────────────────────────────────────

@router.get("/{lead_id}")
def get_lead(lead_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    lead = (
        db.query(MonzaLead)
        .options(
            joinedload(MonzaLead.cliente),
            joinedload(MonzaLead.asesor),
            joinedload(MonzaLead.items),
            joinedload(MonzaLead.actividades),
            joinedload(MonzaLead.proximos_pasos).joinedload(MonzaProximoPaso.asesor),
            joinedload(MonzaLead.cotizaciones),
        )
        .filter(MonzaLead.id == lead_id)
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    data = _lead_dict(lead)
    data["items"] = [_item_dict(it) for it in lead.items]
    data["actividades"] = [
        {
            "id": a.id,
            "tipo": a.tipo,
            "descripcion": a.descripcion,
            "usuario": a.usuario,
            "fecha": a.fecha.isoformat(),
        }
        for a in sorted(lead.actividades, key=lambda x: x.fecha, reverse=True)
    ]
    data["proximos_pasos"] = [
        {
            "id": p.id,
            "tipo": p.tipo,
            "cuando": p.cuando.isoformat() if p.cuando else None,
            "asesor": p.asesor.nombre if p.asesor else None,
            "completado": p.completado,
        }
        for p in lead.proximos_pasos
    ]
    data["cotizaciones"] = [
        {"id": c.id, "numero": c.numero, "estado": c.estado, "total_bruto": c.total_bruto}
        for c in lead.cotizaciones
    ]
    return data


# ── Update lead ───────────────────────────────────────────────────────────────

@router.patch("/{lead_id}")
def update_lead(lead_id: int, body: LeadUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    old_estado = lead.estado
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(lead, field, value)
    lead.fecha_actualizacion = datetime.utcnow()

    asesor_nombre = current_user.email.split("@")[0].title()
    if body.estado and body.estado != old_estado:
        db.add(MonzaLeadActividad(
            lead_id=lead.id,
            tipo="cambio_estado",
            descripcion=f"Estado cambiado a '{body.estado}' por {asesor_nombre}",
            usuario=asesor_nombre,
            usuario_id=current_user.id,
        ))
        if body.estado == "vendido":
            c = db.query(MonzaCliente).filter(MonzaCliente.id == lead.cliente_id).first()
            if c:
                c.vendidos_total = (c.vendidos_total or 0) + 1

    db.commit()
    db.refresh(lead)
    _log(db, current_user.email, "UPDATE", "lead",
         lead.id, lead.numero, f"Estado: {lead.estado}")
    return _lead_dict(lead)


# ── Items ─────────────────────────────────────────────────────────────────────

@router.post("/{lead_id}/items", status_code=201)
def add_item(lead_id: int, body: ItemIn, db: Session = Depends(get_db), _=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    item = MonzaLeadItem(lead_id=lead_id, **body.model_dump())
    db.add(item)
    lead.fecha_actualizacion = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _item_dict(item)


@router.put("/{lead_id}/items/{item_id}")
def update_item(lead_id: int, item_id: int, body: ItemUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    item = db.query(MonzaLeadItem).filter(
        MonzaLeadItem.id == item_id, MonzaLeadItem.lead_id == lead_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item no encontrado")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return _item_dict(item)


@router.delete("/{lead_id}/items/{item_id}", status_code=204)
def delete_item(lead_id: int, item_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    item = db.query(MonzaLeadItem).filter(
        MonzaLeadItem.id == item_id, MonzaLeadItem.lead_id == lead_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item no encontrado")
    db.delete(item)
    db.commit()


# ── Próximos pasos ────────────────────────────────────────────────────────────

@router.post("/{lead_id}/proximos-pasos", status_code=201)
def agendar_paso(lead_id: int, body: ProximoPasoIn, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    paso = MonzaProximoPaso(
        lead_id=lead_id,
        tipo=body.tipo,
        cuando=body.cuando,
        asesor_id=body.asesor_id or _get_asesor_id(db, current_user.id),
    )
    db.add(paso)
    asesor_nombre = current_user.email.split("@")[0].title()
    cuando_str = body.cuando.strftime("%d/%m/%Y %H:%M") if body.cuando else "sin fecha"
    db.add(MonzaLeadActividad(
        lead_id=lead_id,
        tipo=body.tipo,
        descripcion=f"Próximo paso agendado: {body.tipo} para {cuando_str}",
        usuario=asesor_nombre,
        usuario_id=current_user.id,
    ))
    lead.fecha_actualizacion = datetime.utcnow()
    db.commit()
    db.refresh(paso)
    return {"id": paso.id, "tipo": paso.tipo, "cuando": paso.cuando.isoformat() if paso.cuando else None}


@router.patch("/{lead_id}/proximos-pasos/{paso_id}/completar", status_code=200)
def completar_paso(lead_id: int, paso_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    paso = db.query(MonzaProximoPaso).filter(
        MonzaProximoPaso.id == paso_id, MonzaProximoPaso.lead_id == lead_id
    ).first()
    if not paso:
        raise HTTPException(status_code=404, detail="Próximo paso no encontrado")
    paso.completado = True
    db.commit()
    return {"ok": True}


# ── Actividades / Notas ───────────────────────────────────────────────────────

@router.post("/{lead_id}/actividades", status_code=201)
def add_actividad(lead_id: int, body: ActividadIn, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    asesor_nombre = current_user.email.split("@")[0].title()
    act = MonzaLeadActividad(
        lead_id=lead_id,
        tipo=body.tipo,
        descripcion=body.descripcion,
        usuario=asesor_nombre,
        usuario_id=current_user.id,
    )
    db.add(act)
    lead.fecha_actualizacion = datetime.utcnow()
    db.commit()
    db.refresh(act)
    return {"id": act.id, "tipo": act.tipo, "descripcion": act.descripcion, "usuario": act.usuario, "fecha": act.fecha.isoformat()}


# ── Clientes ──────────────────────────────────────────────────────────────────

@router.get("/clientes/search")
def search_clientes(q: str = Query(""), db: Session = Depends(get_db), _=Depends(get_current_user)):
    clientes = (
        db.query(MonzaCliente)
        .filter(or_(
            MonzaCliente.nombre.ilike(f"%{q}%"),
            MonzaCliente.telefono.ilike(f"%{q}%"),
            MonzaCliente.rut.ilike(f"%{q}%"),
            MonzaCliente.email.ilike(f"%{q}%"),
        ))
        .limit(20)
        .all()
    )
    return [_cliente_dict(c) for c in clientes]


@router.patch("/{lead_id}/cliente")
def update_cliente(lead_id: int, body: ClienteUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead or not lead.cliente_id:
        raise HTTPException(status_code=404, detail="Lead o cliente no encontrado")
    c = db.query(MonzaCliente).filter(MonzaCliente.id == lead.cliente_id).first()
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(c, field, value)
    c.fecha_actualizacion = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return _cliente_dict(c)


# ── Asesores ──────────────────────────────────────────────────────────────────

@router.get("/asesores/list")
def list_asesores(db: Session = Depends(get_db), _=Depends(get_current_user)):
    asesores = db.query(MonzaAsesor).filter(MonzaAsesor.activo == True).all()  # noqa: E712
    return [{"id": a.id, "nombre": a.nombre, "email": a.email, "slug": a.slug} for a in asesores]


@router.delete("/{lead_id}", status_code=204)
def delete_lead(lead_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    # Desasociar cotizaciones (conservarlas, solo romper el vínculo)
    from monza_models import MonzaCotizacion
    db.query(MonzaCotizacion).filter(MonzaCotizacion.lead_id == lead_id).update({"lead_id": None})
    db.commit()
    _log(db, _.email if hasattr(_, "email") else str(_), "DELETE", "lead",
         lead.id, lead.numero, f"Lead {lead.numero} eliminado")
    db.delete(lead)
    db.commit()
