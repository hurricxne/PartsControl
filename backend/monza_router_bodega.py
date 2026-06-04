"""
Bodega MonzaParts - Recepcion fisica de items que llegan a Chile.
Confirma items por_recibir -> en_bodega (OK) o -> reclamo (danado/faltante).
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_notif import crear_notif
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor, MonzaReclamo,
)

router = APIRouter(prefix="/api/monza/bodega", tags=["monza-bodega"])


def _log(db, user_email, accion, entidad, entidad_id=None, entidad_ref=None, detalle=None):
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=user_email, accion=accion, entidad=entidad,
                    entidad_id=entidad_id, entidad_ref=entidad_ref, detalle=detalle))
    db.commit()


def _item_dict(it: MonzaCotizacionItem, cot: MonzaCotizacion, ocp: Optional[MonzaOcProveedor]) -> dict:
    return {
        "id": it.id,
        "cot_numero": cot.numero,
        "cliente": cot.cliente.nombre if cot.cliente else None,
        "vehiculo": cot.vehiculo,
        "descripcion": it.descripcion,
        "numero_parte": it.numero_parte,
        "marca": it.marca,
        "cantidad": it.cantidad,
        "estado_linea": it.estado_linea,
        "ocp_numero": ocp.numero if ocp else None,
        "ocp_proveedor": ocp.proveedor_nombre if ocp else None,
        "ocp_awb": ocp.awb if ocp else None,
    }


def _ocp_for(db, ocp_id, cache):
    if not ocp_id:
        return None
    if ocp_id not in cache:
        cache[ocp_id] = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == ocp_id).first()
    return cache[ocp_id]


# KPIs
@router.get("/kpis")
def kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    def cnt(estado):
        return db.query(func.count(MonzaCotizacionItem.id)).filter(
            MonzaCotizacionItem.estado_linea == estado
        ).scalar() or 0
    reclamos_pend = db.query(func.count(MonzaReclamo.id)).filter(
        MonzaReclamo.estado.in_(["pendiente", "reclamado"])
    ).scalar() or 0
    return {
        "por_recibir": cnt("por_recibir"),
        "en_bodega": cnt("en_bodega"),
        "despachado": cnt("despachado"),
        "reclamos_pendientes": reclamos_pend,
    }


# Items pendientes de recibir (llegaron a Chile)
@router.get("/por-recibir")
def por_recibir(q: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "por_recibir")
    )
    if q:
        query = query.filter(
            (MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) |
            (MonzaCotizacion.numero.ilike(f"%{q}%"))
        )
    items = query.order_by(MonzaCotizacionItem.id).all()
    cache = {}
    return [_item_dict(it, it.cotizacion, _ocp_for(db, it.oc_proveedor_id, cache)) for it in items]


# Items confirmados en bodega (listos para despacho)
@router.get("/en-bodega")
def en_bodega(q: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "en_bodega")
    )
    if q:
        query = query.filter(
            (MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) |
            (MonzaCotizacion.numero.ilike(f"%{q}%"))
        )
    items = query.order_by(MonzaCotizacionItem.id.desc()).all()
    cache = {}
    return [_item_dict(it, it.cotizacion, _ocp_for(db, it.oc_proveedor_id, cache)) for it in items]


# Confirmar recepcion de un item
class ConfirmarBody(BaseModel):
    item_id: int
    resultado: str  # ok | danado | faltante | no_llego
    qty_afectada: Optional[int] = 0
    observacion: Optional[str] = None


@router.post("/confirmar")
def confirmar(body: ConfirmarBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == body.item_id).first()
    if not it:
        raise HTTPException(status_code=404, detail="Item no encontrado")
    cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == it.cotizacion_id).first()

    if body.resultado == "ok":
        it.estado_linea = "en_bodega"
        _log(db, current_user.email, "UPDATE", "item", it.id, cot.numero if cot else None,
             f"Recepcion OK: {it.descripcion}")
    else:
        # Crear reclamo y marcar item en reclamo
        it.estado_linea = "reclamo"
        rec = MonzaReclamo(
            item_id=it.id,
            oc_proveedor_id=it.oc_proveedor_id,
            cot_numero=cot.numero if cot else None,
            descripcion=it.descripcion,
            motivo=body.resultado,
            qty_afectada=body.qty_afectada or it.cantidad,
            estado="pendiente",
            observacion=body.observacion,
            user_email=current_user.email,
        )
        db.add(rec)
        _log(db, current_user.email, "CREATE", "reclamo", None, cot.numero if cot else None,
             f"Reclamo {body.resultado}: {it.descripcion}")
        crear_notif(db, f"Reclamo · {it.descripcion}", f"{body.resultado} — {cot.numero if cot else ''}", "danger", "/monzaparts/bodega", "reclamo", it.id)
    db.commit()
    return {"ok": True, "estado_linea": it.estado_linea}


# Reclamos
@router.get("/reclamos")
def list_reclamos(estado: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    query = db.query(MonzaReclamo)
    if estado:
        query = query.filter(MonzaReclamo.estado == estado)
    recs = query.order_by(MonzaReclamo.id.desc()).all()
    cache = {}
    out = []
    for r in recs:
        ocp = _ocp_for(db, r.oc_proveedor_id, cache)
        out.append({
            "id": r.id,
            "cot_numero": r.cot_numero,
            "descripcion": r.descripcion,
            "motivo": r.motivo,
            "qty_afectada": r.qty_afectada,
            "estado": r.estado,
            "observacion": r.observacion,
            "user_email": r.user_email,
            "ocp_numero": ocp.numero if ocp else None,
            "ocp_proveedor": ocp.proveedor_nombre if ocp else None,
            "fecha_creacion": r.fecha_creacion.isoformat() if r.fecha_creacion else None,
            "fecha_resolucion": r.fecha_resolucion.isoformat() if r.fecha_resolucion else None,
        })
    return out


class ReclamoUpdate(BaseModel):
    estado: Optional[str] = None
    observacion: Optional[str] = None


@router.patch("/reclamos/{rec_id}")
def update_reclamo(rec_id: int, body: ReclamoUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    r = db.query(MonzaReclamo).filter(MonzaReclamo.id == rec_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Reclamo no encontrado")
    if body.estado:
        r.estado = body.estado
        if body.estado in ("resuelto", "anulado"):
            r.fecha_resolucion = datetime.utcnow()
    if body.observacion is not None:
        r.observacion = body.observacion
    db.commit()
    _log(db, current_user.email, "UPDATE", "reclamo", r.id, r.cot_numero, f"Reclamo -> {r.estado}")
    return {"ok": True}


# Resumen "listo para despachar": cotizaciones con todos los items en_bodega
@router.get("/listo-despacho")
def listo_despacho(db: Session = Depends(get_db), _=Depends(get_current_user)):
    # cotizaciones vendidas (no despachadas) con items
    cots = (
        db.query(MonzaCotizacion)
        .options(joinedload(MonzaCotizacion.cliente), joinedload(MonzaCotizacion.items))
        .filter(MonzaCotizacion.estado == "vendida")
        .all()
    )
    out = []
    for c in cots:
        items = c.items
        if not items:
            continue
        en_bod = sum(1 for i in items if i.estado_linea == "en_bodega")
        total = len(items)
        out.append({
            "id": c.id,
            "numero": c.numero,
            "cliente": c.cliente.nombre if c.cliente else None,
            "vehiculo": c.vehiculo,
            "total_items": total,
            "en_bodega": en_bod,
            "listo": en_bod == total,
            "total_bruto": c.total_bruto,
        })
    # Solo los que tienen al menos 1 item en bodega
    return [o for o in out if o["en_bodega"] > 0]
