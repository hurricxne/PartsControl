"""
Bodega MonzaParts (alineacion MachParts): recepcion fisica por embarque.
Recibir embarque -> abrir recepcion -> marcar item x item -> cerrar.
Estados recepcion: completo faltante sobrante danado_utilizable danado_no_utilizable no_llego
Cierre: completo/danado_util/sobrante -> en_bodega ; resto -> reclamo
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor, MonzaReclamo,
    MonzaEmbarque, MonzaEmbarqueItem, MonzaRecepcion, MonzaRecepcionItem, MonzaDocumento,
)
from monza_notif import crear_notif

router = APIRouter(prefix="/api/monza/bodega", tags=["monza-bodega"])

ESTADOS_RECEP = {"completo", "faltante", "sobrante", "danado_utilizable", "danado_no_utilizable", "no_llego"}
A_BODEGA = {"completo", "danado_utilizable", "sobrante"}


def _log(db, email, accion, entidad, eid=None, ref=None, det=None):
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=email, accion=accion, entidad=entidad, entidad_id=eid, entidad_ref=ref, detalle=det))
    db.commit()


def _item_dict(it, cot, ocp=None):
    return {
        "id": it.id, "cot_numero": cot.numero if cot else None,
        "cliente": cot.cliente.nombre if cot and cot.cliente else None, "vehiculo": cot.vehiculo if cot else None,
        "descripcion": it.descripcion, "numero_parte": it.numero_parte, "marca": it.marca,
        "calidad": it.calidad, "cantidad": it.cantidad, "estado_linea": it.estado_linea,
        "ocp_numero": (ocp.numero_oc or ocp.numero) if ocp else None,
        "ocp_proveedor": ocp.proveedor_nombre if ocp else None,
    }


def _ocp(db, oid, cache):
    if not oid: return None
    if oid not in cache:
        cache[oid] = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == oid).first()
    return cache[oid]


# ── KPIs ──────────────────────────────────────────────────────────────────────
@router.get("/kpis")
def kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    # embarques a recibir = estado en_transito/en_aduana/en_bodega sin recepcion cerrada
    recv = db.query(MonzaEmbarque).filter(MonzaEmbarque.estado.in_(["en_transito", "en_aduana", "en_bodega"])).all()
    a_recibir = 0
    for e in recv:
        r = db.query(MonzaRecepcion).filter(MonzaRecepcion.embarque_id == e.id, MonzaRecepcion.estado == "cerrada").first()
        if not r:
            a_recibir += 1
    en_bodega = db.query(func.count(MonzaCotizacionItem.id)).filter(MonzaCotizacionItem.estado_linea == "en_bodega").scalar() or 0
    despachado = db.query(func.count(MonzaCotizacionItem.id)).filter(MonzaCotizacionItem.estado_linea == "despachado").scalar() or 0
    reclamos = db.query(func.count(MonzaReclamo.id)).filter(MonzaReclamo.estado.in_(["pendiente", "reclamado"])).scalar() or 0
    return {"a_recibir": a_recibir, "en_bodega": en_bodega, "despachado": despachado, "reclamos_pendientes": reclamos}


# ── Embarques a recibir ───────────────────────────────────────────────────────
@router.get("/embarques")
def embarques_a_recibir(db: Session = Depends(get_db), _=Depends(get_current_user)):
    embs = db.query(MonzaEmbarque).filter(MonzaEmbarque.estado.in_(["en_transito", "en_aduana", "en_bodega"])).order_by(MonzaEmbarque.id.desc()).all()
    out = []
    for e in embs:
        rec = db.query(MonzaRecepcion).filter(MonzaRecepcion.embarque_id == e.id).order_by(MonzaRecepcion.id.desc()).first()
        if rec and rec.estado == "cerrada":
            continue
        n = db.query(func.count(MonzaEmbarqueItem.id)).filter(MonzaEmbarqueItem.embarque_id == e.id).scalar() or 0
        out.append({
            "id": e.id, "numero": e.numero, "estado": e.estado, "awb": e.awb,
            "forwarder": e.forwarder, "tracking": e.tracking, "fecha_llegada_est": e.fecha_llegada_est,
            "items_count": n, "recepcion_id": rec.id if rec else None,
            "recepcion_abierta": bool(rec and rec.estado == "abierta"),
        })
    return out


# ── Iniciar recepcion ─────────────────────────────────────────────────────────
@router.post("/embarques/{emb_id}/recibir")
def recibir(emb_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    e = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == emb_id).first()
    if not e:
        raise HTTPException(404, "Embarque no encontrado")
    rec = db.query(MonzaRecepcion).filter(MonzaRecepcion.embarque_id == emb_id, MonzaRecepcion.estado == "abierta").first()
    if rec:
        return {"ok": True, "recepcion_id": rec.id}
    rec = MonzaRecepcion(embarque_id=emb_id, estado="abierta", usuario_email=current_user.email)
    db.add(rec); db.commit(); db.refresh(rec)
    _log(db, current_user.email, "CREATE", "recepcion", rec.id, e.numero, f"Recepción abierta · {e.numero}")
    return {"ok": True, "recepcion_id": rec.id}


# ── Detalle recepcion (items con su estado_recepcion) ─────────────────────────
@router.get("/recepciones/{rec_id}")
def get_recepcion(rec_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    rec = db.query(MonzaRecepcion).filter(MonzaRecepcion.id == rec_id).first()
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    emb = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == rec.embarque_id).first()
    eis = db.query(MonzaEmbarqueItem).filter(MonzaEmbarqueItem.embarque_id == rec.embarque_id).all()
    cache = {}
    items = []
    marcados = 0
    for ei in eis:
        it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == ei.item_id).first()
        if not it:
            continue
        cot = db.query(MonzaCotizacion).options(joinedload(MonzaCotizacion.cliente)).filter(MonzaCotizacion.id == it.cotizacion_id).first()
        ri = db.query(MonzaRecepcionItem).filter(MonzaRecepcionItem.recepcion_id == rec_id, MonzaRecepcionItem.item_id == it.id).first()
        nfotos = db.query(func.count(MonzaDocumento.id)).filter(MonzaDocumento.entidad == "recepcion_item", MonzaDocumento.entidad_id == it.id).scalar() or 0
        d = _item_dict(it, cot, _ocp(db, it.oc_proveedor_id, cache))
        d["estado_recepcion"] = ri.estado_recepcion if ri else None
        d["qty_recibida"] = ri.qty_recibida if ri else None
        d["qty_danada"] = ri.qty_danada if ri else 0
        d["observacion"] = ri.observacion if ri else None
        d["fotos"] = nfotos
        if ri and ri.estado_recepcion:
            marcados += 1
        items.append(d)
    return {
        "id": rec.id, "embarque_id": rec.embarque_id, "embarque_numero": emb.numero if emb else None,
        "estado": rec.estado, "total": len(items), "marcados": marcados, "items": items,
    }


# ── Marcar item ───────────────────────────────────────────────────────────────
class MarcarBody(BaseModel):
    estado_recepcion: str
    qty_recibida: Optional[int] = None
    qty_danada: Optional[int] = 0
    observacion: Optional[str] = None


@router.patch("/recepciones/{rec_id}/items/{item_id}")
def marcar_item(rec_id: int, item_id: int, body: MarcarBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if body.estado_recepcion not in ESTADOS_RECEP:
        raise HTTPException(400, f"Estado inválido: {body.estado_recepcion}")
    # foto obligatoria si dañado
    if "danado" in body.estado_recepcion:
        nfotos = db.query(func.count(MonzaDocumento.id)).filter(MonzaDocumento.entidad == "recepcion_item", MonzaDocumento.entidad_id == item_id).scalar() or 0
        if nfotos == 0:
            raise HTTPException(400, "Debe adjuntar al menos una foto para ítems dañados")
    ri = db.query(MonzaRecepcionItem).filter(MonzaRecepcionItem.recepcion_id == rec_id, MonzaRecepcionItem.item_id == item_id).first()
    if not ri:
        ri = MonzaRecepcionItem(recepcion_id=rec_id, item_id=item_id)
        db.add(ri)
    ri.estado_recepcion = body.estado_recepcion
    ri.qty_recibida = body.qty_recibida
    ri.qty_danada = body.qty_danada or 0
    ri.observacion = body.observacion
    ri.fecha = datetime.utcnow()
    db.commit()
    return {"ok": True}


# ── Cerrar recepcion ──────────────────────────────────────────────────────────
@router.post("/recepciones/{rec_id}/cerrar")
def cerrar_recepcion(rec_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    rec = db.query(MonzaRecepcion).filter(MonzaRecepcion.id == rec_id).first()
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    eis = db.query(MonzaEmbarqueItem).filter(MonzaEmbarqueItem.embarque_id == rec.embarque_id).all()
    pendientes = []
    a_bodega = 0
    reclamos = 0
    for ei in eis:
        it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == ei.item_id).first()
        if not it:
            continue
        ri = db.query(MonzaRecepcionItem).filter(MonzaRecepcionItem.recepcion_id == rec_id, MonzaRecepcionItem.item_id == it.id).first()
        if not ri or not ri.estado_recepcion:
            pendientes.append(it.descripcion)
            continue
        cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == it.cotizacion_id).first()
        if ri.estado_recepcion in A_BODEGA:
            it.estado_linea = "en_bodega"
            a_bodega += 1
        else:
            it.estado_linea = "reclamo"
            db.add(MonzaReclamo(
                item_id=it.id, oc_proveedor_id=it.oc_proveedor_id,
                cot_numero=cot.numero if cot else None, descripcion=it.descripcion,
                motivo=ri.estado_recepcion, qty_afectada=ri.qty_danada or it.cantidad,
                estado="pendiente", observacion=ri.observacion, user_email=current_user.email,
            ))
            reclamos += 1
    if pendientes:
        raise HTTPException(400, f"Faltan {len(pendientes)} ítem(s) por marcar")
    rec.estado = "cerrada"
    rec.fecha_cierre = datetime.utcnow()
    emb = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == rec.embarque_id).first()
    if emb:
        emb.estado = "en_bodega"
    db.commit()
    _log(db, current_user.email, "UPDATE", "recepcion", rec.id, emb.numero if emb else None, f"Recepción cerrada · {a_bodega} a bodega, {reclamos} reclamo(s)")
    if reclamos:
        crear_notif(db, f"Reclamos en recepción · {emb.numero if emb else ''}", f"{reclamos} ítem(s) con problema", "danger", "/monzaparts/bodega", "recepcion", rec.id)
    return {"ok": True, "en_bodega": a_bodega, "reclamos": reclamos}


# ── Items en bodega (listos para despacho) ────────────────────────────────────
@router.get("/en-bodega")
def en_bodega(q: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "en_bodega")
    )
    if q:
        query = query.filter((MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) | (MonzaCotizacion.numero.ilike(f"%{q}%")))
    cache = {}
    return [_item_dict(it, it.cotizacion, _ocp(db, it.oc_proveedor_id, cache)) for it in query.order_by(MonzaCotizacionItem.id.desc()).all()]


# ── Reclamos ──────────────────────────────────────────────────────────────────
@router.get("/reclamos")
def list_reclamos(estado: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(MonzaReclamo)
    if estado:
        q = q.filter(MonzaReclamo.estado == estado)
    cache = {}
    out = []
    for r in q.order_by(MonzaReclamo.id.desc()).all():
        ocp = _ocp(db, r.oc_proveedor_id, cache)
        out.append({
            "id": r.id, "cot_numero": r.cot_numero, "descripcion": r.descripcion,
            "motivo": r.motivo, "qty_afectada": r.qty_afectada, "estado": r.estado,
            "observacion": r.observacion, "ocp_proveedor": ocp.proveedor_nombre if ocp else None,
            "fecha_creacion": r.fecha_creacion.isoformat() if r.fecha_creacion else None,
        })
    return out


class ReclamoUpd(BaseModel):
    estado: Optional[str] = None
    observacion: Optional[str] = None


@router.patch("/reclamos/{rid}")
def update_reclamo(rid: int, body: ReclamoUpd, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    r = db.query(MonzaReclamo).filter(MonzaReclamo.id == rid).first()
    if not r:
        raise HTTPException(404, "No encontrado")
    if body.estado:
        r.estado = body.estado
        if body.estado in ("resuelto", "anulado"):
            r.fecha_resolucion = datetime.utcnow()
    if body.observacion is not None:
        r.observacion = body.observacion
    db.commit()
    return {"ok": True}
