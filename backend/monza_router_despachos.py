from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_models import MonzaCotizacion, MonzaCliente, MonzaCotizacionItem, MonzaDespacho, MonzaDespachoItem
from pydantic import BaseModel
from typing import List
from monza_notif import crear_notif

router = APIRouter(prefix="/api/monza/despachos", tags=["monza-despachos"])


def _despacho_dict(c: MonzaCotizacion) -> dict:
    asesor_nombre = c.asesor.email.split("@")[0].title() if c.asesor else None
    return {
        "id": c.id,
        "numero": c.numero,
        "estado": c.estado,
        "linea": c.linea,
        "vehiculo": c.vehiculo,
        "vin": c.vin,
        "anio": c.anio,
        "oc_cliente": c.oc_cliente,
        "numero_factura": c.numero_factura,
        "tipo_documento": c.tipo_documento,
        "tiene_documento": bool(c.documento_path),
        "fecha_venta": c.fecha_venta.isoformat() if c.fecha_venta else None,
        "fecha_despacho": c.fecha_despacho.isoformat() if c.fecha_despacho else None,
        "total_bruto": c.total_bruto,
        "items_count": len(c.items),
        "asesor": asesor_nombre,
        "fecha_creacion": c.fecha_creacion.isoformat(),
        "cliente": {
            "id": c.cliente.id,
            "nombre": c.cliente.nombre,
            "rut": c.cliente.rut,
        } if c.cliente else None,
        "lead_numero": c.lead.numero if c.lead else None,
    }


@router.get("")
def list_despachos(
    q: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = (
        db.query(MonzaCotizacion)
        .options(
            joinedload(MonzaCotizacion.cliente),
            joinedload(MonzaCotizacion.lead),
            joinedload(MonzaCotizacion.asesor),
            joinedload(MonzaCotizacion.items),
        )
        .filter(MonzaCotizacion.estado == "despachado")
    )

    if q:
        query = query.join(MonzaCotizacion.cliente, isouter=True).filter(
            or_(
                MonzaCotizacion.numero.ilike(f"%{q}%"),
                MonzaCliente.nombre.ilike(f"%{q}%"),
                MonzaCotizacion.oc_cliente.ilike(f"%{q}%"),
                MonzaCotizacion.vehiculo.ilike(f"%{q}%"),
                MonzaCotizacion.numero_factura.ilike(f"%{q}%"),
            )
        )

    if desde:
        try:
            query = query.filter(MonzaCotizacion.fecha_despacho >= datetime.fromisoformat(desde).date())
        except Exception:
            pass
    if hasta:
        try:
            query = query.filter(MonzaCotizacion.fecha_despacho <= datetime.fromisoformat(hasta).date())
        except Exception:
            pass

    total = query.count()
    items = (
        query.order_by(MonzaCotizacion.fecha_despacho.desc(), MonzaCotizacion.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_despacho_dict(c) for c in items],
    }


@router.get("/kpis")
def despachos_kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    anio = datetime.utcnow().year
    mes = datetime.utcnow().month
    from datetime import date
    inicio_mes = date(anio, mes, 1)

    total_count = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "despachado"
    ).scalar() or 0

    mes_count = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "despachado",
        MonzaCotizacion.fecha_despacho >= inicio_mes,
    ).scalar() or 0

    total_monto = db.query(func.sum(MonzaCotizacion.total_bruto)).filter(
        MonzaCotizacion.estado == "despachado",
        MonzaCotizacion.fecha_despacho >= inicio_mes,
    ).scalar() or 0

    sin_doc = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "despachado",
        MonzaCotizacion.documento_path.is_(None),
    ).scalar() or 0

    return {
        "total_despachados": total_count,
        "despachados_mes": mes_count,
        "monto_mes": float(total_monto),
        "sin_documento": sin_doc,
    }


# ── Despacho como entidad (alineacion MachParts) ──────────────────────────────

def _gen_num_desp(db):
    anio = datetime.utcnow().year
    last = db.query(MonzaDespacho).filter(MonzaDespacho.numero.like(f"DSP-{anio}-%")).order_by(MonzaDespacho.id.desc()).first()
    n = int(last.numero.split("-")[-1]) + 1 if last and last.numero else 1
    return f"DSP-{anio}-{n:04d}"


@router.get("/listos")
def listos_despacho(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Cotizaciones con >=1 item en_bodega (listas para despachar)."""
    cots = (
        db.query(MonzaCotizacion)
        .options(joinedload(MonzaCotizacion.cliente), joinedload(MonzaCotizacion.items))
        .filter(MonzaCotizacion.estado == "vendida")
        .all()
    )
    out = []
    for c in cots:
        en_bod = [i for i in c.items if i.estado_linea == "en_bodega"]
        if not en_bod:
            continue
        total = len(c.items)
        out.append({
            "id": c.id, "numero": c.numero,
            "cliente": c.cliente.nombre if c.cliente else None,
            "vehiculo": c.vehiculo, "total_items": total, "en_bodega": len(en_bod),
            "listo_completo": len(en_bod) == total, "total_bruto": c.total_bruto,
            "items": [{"id": i.id, "descripcion": i.descripcion, "numero_parte": i.numero_parte, "cantidad": i.cantidad} for i in en_bod],
        })
    return out


class CrearDespachoBody(BaseModel):
    cotizacion_id: int
    item_ids: List[int]
    numero_guia: str = ""
    transportista: str = ""
    destinatario: str = ""
    direccion_entrega: str = ""
    observaciones: str = ""


@router.post("/crear")
def crear_despacho(body: CrearDespachoBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    cot = db.query(MonzaCotizacion).options(joinedload(MonzaCotizacion.cliente), joinedload(MonzaCotizacion.items)).filter(MonzaCotizacion.id == body.cotizacion_id).first()
    if not cot:
        raise HTTPException(404, "Cotización no encontrada")
    items = [i for i in cot.items if i.id in body.item_ids and i.estado_linea == "en_bodega"]
    if not items:
        raise HTTPException(400, "Sin ítems en bodega para despachar")
    dsp = MonzaDespacho(
        numero=_gen_num_desp(db), cotizacion_id=cot.id,
        cliente_nombre=cot.cliente.nombre if cot.cliente else None,
        numero_guia=body.numero_guia or None, transportista=body.transportista or None,
        destinatario=body.destinatario or None, direccion_entrega=body.direccion_entrega or None,
        observaciones=body.observaciones or None, estado="despachado", asesor_email=current_user.email,
    )
    db.add(dsp); db.flush()
    for it in items:
        it.estado_linea = "despachado"
        db.add(MonzaDespachoItem(despacho_id=dsp.id, item_id=it.id, qty_despachada=it.cantidad or 1))
    # Si todos los items quedaron despachados, marcar cotizacion despachada
    db.flush()
    if all((i.estado_linea == "despachado") for i in cot.items):
        cot.estado = "despachado"
        if not cot.fecha_despacho:
            cot.fecha_despacho = datetime.utcnow().date()
    db.commit(); db.refresh(dsp)
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=current_user.email, accion="DESPACHADO", entidad="despacho", entidad_id=dsp.id, entidad_ref=dsp.numero, detalle=f"Despacho {dsp.numero} · {len(items)} ítem(s) · {cot.numero}"))
    db.commit()
    crear_notif(db, f"Despacho realizado · {dsp.numero}", f"{cot.numero} — {len(items)} ítem(s) despachado(s)", "success", "/monzaparts/despachos", "despacho", dsp.id)
    return {"ok": True, "id": dsp.id, "numero": dsp.numero, "items": len(items)}


@router.get("/entidades")
def list_despachos(db: Session = Depends(get_db), _=Depends(get_current_user)):
    out = []
    for d in db.query(MonzaDespacho).order_by(MonzaDespacho.id.desc()).limit(100).all():
        n = db.query(func.count(MonzaDespachoItem.id)).filter(MonzaDespachoItem.despacho_id == d.id).scalar() or 0
        out.append({
            "id": d.id, "numero": d.numero, "cotizacion_id": d.cotizacion_id,
            "cliente_nombre": d.cliente_nombre, "numero_guia": d.numero_guia,
            "transportista": d.transportista, "destinatario": d.destinatario,
            "items_count": n, "fecha": d.fecha.isoformat() if d.fecha else None,
        })
    return out
