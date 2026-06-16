"""
Logistica MonzaParts (alineacion MachParts): Embarque como entidad.
Agrupa items 'preparado' en un Embarque -> 'embarcado'.
Estados embarque: en_bodega_proveedor -> en_transito -> en_aduana -> en_bodega
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaEmbarque, MonzaEmbarqueItem, MonzaOcProveedor,
)
from monza_notif import crear_notif

router = APIRouter(prefix="/api/monza/logistica", tags=["monza-logistica"])

ESTADOS_EMB = ["en_bodega_proveedor", "en_transito", "en_aduana", "en_bodega"]


def _log(db, email, accion, entidad, eid=None, ref=None, det=None):
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=email, accion=accion, entidad=entidad, entidad_id=eid, entidad_ref=ref, detalle=det))
    db.commit()


def _gen_numero_emb(db):
    anio = datetime.utcnow().year
    last = db.query(MonzaEmbarque).filter(MonzaEmbarque.numero.like(f"EMB-{anio}-%")).order_by(MonzaEmbarque.id.desc()).first()
    n = int(last.numero.split("-")[-1]) + 1 if last and last.numero else 1
    return f"EMB-{anio}-{n:04d}"


def _item_dict(it, cot):
    ocp = None
    return {
        "id": it.id, "cot_numero": cot.numero if cot else None,
        "cliente": cot.cliente.nombre if cot and cot.cliente else None,
        "descripcion": it.descripcion, "numero_parte": it.numero_parte,
        "marca": it.marca, "calidad": it.calidad, "cantidad": it.cantidad,
        "estado_linea": it.estado_linea, "oc_proveedor_id": it.oc_proveedor_id,
    }


def _emb_dict(db, e: MonzaEmbarque, with_items=False):
    n = db.query(func.count(MonzaEmbarqueItem.id)).filter(MonzaEmbarqueItem.embarque_id == e.id).scalar() or 0
    d = {
        "id": e.id, "numero": e.numero, "estado": e.estado,
        "awb": e.awb, "forwarder": e.forwarder, "tracking": e.tracking,
        "fecha_despacho": e.fecha_despacho, "fecha_llegada_est": e.fecha_llegada_est,
        "notas": e.notas, "items_count": n,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }
    if with_items:
        eis = db.query(MonzaEmbarqueItem).filter(MonzaEmbarqueItem.embarque_id == e.id).all()
        items = []
        for ei in eis:
            it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == ei.item_id).first()
            if it:
                cot = db.query(MonzaCotizacion).options(joinedload(MonzaCotizacion.cliente)).filter(MonzaCotizacion.id == it.cotizacion_id).first()
                items.append(_item_dict(it, cot))
        d["items"] = items
    return d


# ── Items preparados (listos para embarcar) ───────────────────────────────────
@router.get("/preparados")
def preparados(q: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "preparado")
    )
    if q:
        query = query.filter((MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) | (MonzaCotizacion.numero.ilike(f"%{q}%")))
    return [_item_dict(it, it.cotizacion) for it in query.order_by(MonzaCotizacionItem.id).all()]


# ── KPIs ──────────────────────────────────────────────────────────────────────
@router.get("/kpis")
def kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    prep = db.query(func.count(MonzaCotizacionItem.id)).filter(MonzaCotizacionItem.estado_linea == "preparado").scalar() or 0
    def cnt(estado):
        return db.query(func.count(MonzaEmbarque.id)).filter(MonzaEmbarque.estado == estado).scalar() or 0
    return {
        "preparados": prep,
        "en_bodega_proveedor": cnt("en_bodega_proveedor"),
        "en_transito": cnt("en_transito"),
        "en_aduana": cnt("en_aduana"),
        "en_bodega": cnt("en_bodega"),
    }


# ── Crear embarque con items preparados ───────────────────────────────────────
class EmbarqueBody(BaseModel):
    item_ids: List[int]
    awb: Optional[str] = None
    forwarder: Optional[str] = None
    tracking: Optional[str] = None
    fecha_despacho: Optional[str] = None
    fecha_llegada_est: Optional[str] = None
    notas: Optional[str] = None


@router.post("/embarques")
def crear_embarque(body: EmbarqueBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if not body.item_ids:
        raise HTTPException(400, "Sin items")
    emb = MonzaEmbarque(
        numero=_gen_numero_emb(db), estado="en_bodega_proveedor",
        awb=body.awb, forwarder=body.forwarder, tracking=body.tracking,
        fecha_despacho=body.fecha_despacho, fecha_llegada_est=body.fecha_llegada_est,
        notas=body.notas, asesor_email=current_user.email,
    )
    db.add(emb); db.flush()
    items = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(body.item_ids), MonzaCotizacionItem.estado_linea == "preparado").all()
    for it in items:
        it.estado_linea = "embarcado"
        db.add(MonzaEmbarqueItem(embarque_id=emb.id, item_id=it.id))
    db.commit(); db.refresh(emb)
    _log(db, current_user.email, "CREATE", "embarque", emb.id, emb.numero, f"Embarque {emb.numero} · {len(items)} item(s)")
    return {"ok": True, "id": emb.id, "numero": emb.numero, "items": len(items)}


@router.get("/embarques")
def list_embarques(estado: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(MonzaEmbarque)
    if estado:
        q = q.filter(MonzaEmbarque.estado == estado)
    return [_emb_dict(db, e) for e in q.order_by(MonzaEmbarque.id.desc()).all()]


@router.get("/embarques/{emb_id}")
def get_embarque(emb_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    e = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == emb_id).first()
    if not e:
        raise HTTPException(404, "Embarque no encontrado")
    return _emb_dict(db, e, with_items=True)


class EmbUpdate(BaseModel):
    estado: Optional[str] = None
    awb: Optional[str] = None
    forwarder: Optional[str] = None
    tracking: Optional[str] = None
    fecha_despacho: Optional[str] = None
    fecha_llegada_est: Optional[str] = None
    notas: Optional[str] = None


@router.patch("/embarques/{emb_id}")
def update_embarque(emb_id: int, body: EmbUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    e = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == emb_id).first()
    if not e:
        raise HTTPException(404, "Embarque no encontrado")
    for f, v in body.model_dump(exclude_none=True).items():
        setattr(e, f, v)
    e.updated_at = datetime.utcnow()
    db.commit()
    if body.estado:
        _log(db, current_user.email, "UPDATE", "embarque", e.id, e.numero, f"Embarque -> {e.estado}")
        if body.estado == "en_transito":
            crear_notif(db, f"Embarque en tránsito · {e.numero}", f"{e.awb or 'Sin AWB'} — viaja a Chile", "info", "/monzaparts/logistica", "embarque", e.id)
        elif body.estado == "en_bodega":
            crear_notif(db, f"Embarque llegó · {e.numero}", "Listo para recepción en Bodega", "info", "/monzaparts/bodega", "embarque", e.id)
    return {"ok": True}


@router.delete("/embarques/{emb_id}/items/{item_id}")
def quitar_item(emb_id: int, item_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    ei = db.query(MonzaEmbarqueItem).filter(MonzaEmbarqueItem.embarque_id == emb_id, MonzaEmbarqueItem.item_id == item_id).first()
    if not ei:
        raise HTTPException(404, "No está en el embarque")
    it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == item_id).first()
    if it and it.estado_linea == "embarcado":
        it.estado_linea = "preparado"
    db.delete(ei); db.commit()
    return {"ok": True}
