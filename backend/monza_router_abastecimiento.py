"""
Abastecimiento MonzaParts - Panel de Compras + Seguimiento.
Mueve lineas de items vendidos por el pipeline:
  cotizado -> por_comprar -> comprado -> en_transito -> en_bodega -> despachado
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
    MonzaCotizacion, MonzaCotizacionItem, MonzaCliente,
    MonzaProvAbast, MonzaOcProveedor,
)

router = APIRouter(prefix="/api/monza/abastecimiento", tags=["monza-abastecimiento"])


def _log(db, user_email, accion, entidad, entidad_id=None, entidad_ref=None, detalle=None):
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=user_email, accion=accion, entidad=entidad,
                    entidad_id=entidad_id, entidad_ref=entidad_ref, detalle=detalle))
    db.commit()


def _gen_numero_ocp(db: Session) -> str:
    anio = datetime.utcnow().year
    last = (
        db.query(MonzaOcProveedor)
        .filter(MonzaOcProveedor.numero.like(f"OCP-{anio}-%"))
        .order_by(MonzaOcProveedor.id.desc())
        .first()
    )
    n = int(last.numero.split("-")[-1]) + 1 if last and last.numero else 1
    return f"OCP-{anio}-{n:04d}"


def _item_dict(it: MonzaCotizacionItem, cot: MonzaCotizacion) -> dict:
    return {
        "id": it.id,
        "cotizacion_id": cot.id,
        "cot_numero": cot.numero,
        "cliente": cot.cliente.nombre if cot.cliente else None,
        "vehiculo": cot.vehiculo,
        "descripcion": it.descripcion,
        "numero_parte": it.numero_parte,
        "marca": it.marca,
        "procedencia": it.procedencia,
        "calidad": it.calidad,
        "cantidad": it.cantidad,
        "costo": it.costo,
        "moneda": it.moneda,
        "precio_unitario_clp": it.precio_unitario_clp,
        "subtotal_clp": it.subtotal_clp,
        "plazo_entrega": it.plazo_entrega,
        "estado_linea": it.estado_linea or "cotizado",
        "oc_proveedor_id": it.oc_proveedor_id,
        "fecha_venta": cot.fecha_venta.isoformat() if cot.fecha_venta else None,
    }


# KPIs
@router.get("/kpis")
def kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    def count_estado(estado):
        return db.query(func.count(MonzaCotizacionItem.id)).filter(
            MonzaCotizacionItem.estado_linea == estado
        ).scalar() or 0

    return {
        "por_comprar": count_estado("por_comprar"),
        "comprado": count_estado("comprado"),
        "en_transito": count_estado("en_transito"),
        "en_bodega": count_estado("en_bodega"),
        "despachado": count_estado("despachado"),
        "reclamo": count_estado("reclamo"),
        "ocs_abiertas": db.query(func.count(MonzaOcProveedor.id)).filter(
            MonzaOcProveedor.estado.in_(["emitida", "en_transito"])
        ).scalar() or 0,
    }


# Panel Compras: items por comprar
@router.get("/por-comprar")
def por_comprar(
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "por_comprar")
    )
    if q:
        query = query.filter(
            (MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) |
            (MonzaCotizacionItem.numero_parte.ilike(f"%{q}%")) |
            (MonzaCotizacion.numero.ilike(f"%{q}%"))
        )
    items = query.order_by(MonzaCotizacion.fecha_venta.desc(), MonzaCotizacionItem.id).all()
    return [_item_dict(it, it.cotizacion) for it in items]


# Seguimiento: items comprados / en transito / en bodega
@router.get("/seguimiento")
def seguimiento(
    estado: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    estados = [estado] if estado else ["comprado", "preparado", "embarcado", "en_bodega"]
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea.in_(estados))
    )
    if q:
        query = query.filter(
            (MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) |
            (MonzaCotizacion.numero.ilike(f"%{q}%"))
        )
    items = query.order_by(MonzaCotizacionItem.id.desc()).all()

    ocp_cache = {}
    out = []
    for it in items:
        d = _item_dict(it, it.cotizacion)
        if it.oc_proveedor_id:
            if it.oc_proveedor_id not in ocp_cache:
                ocp_cache[it.oc_proveedor_id] = db.query(MonzaOcProveedor).filter(
                    MonzaOcProveedor.id == it.oc_proveedor_id
                ).first()
            ocp = ocp_cache[it.oc_proveedor_id]
            if ocp:
                d["ocp_numero"] = ocp.numero
                d["ocp_proveedor"] = ocp.proveedor_nombre
                d["ocp_estado"] = ocp.estado
                d["ocp_awb"] = ocp.awb
                d["ocp_tracking"] = ocp.tracking
                d["ocp_numero_oc"] = ocp.numero_oc
                d["ocp_plazo_dias"] = ocp.plazo_dias
        out.append(d)
    return out


# Crear OC de proveedor (comprar items)
class ComprarBody(BaseModel):
    item_ids: List[int]
    proveedor_id: Optional[int] = None
    proveedor_nombre: Optional[str] = None
    pais: Optional[str] = None
    moneda: Optional[str] = "EUR"
    plazo_dias: Optional[int] = None
    numero_oc: Optional[str] = None
    awb: Optional[str] = None
    tracking: Optional[str] = None
    notas: Optional[str] = None


@router.post("/comprar")
def comprar(body: ComprarBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    if not body.item_ids:
        raise HTTPException(status_code=400, detail="Sin items")

    nombre = body.proveedor_nombre
    pais = body.pais
    moneda = body.moneda or "EUR"
    if body.proveedor_id:
        prov = db.query(MonzaProvAbast).filter(MonzaProvAbast.id == body.proveedor_id).first()
        if prov:
            nombre = prov.nombre
            pais = pais or prov.pais
            moneda = prov.moneda or moneda

    ocp = MonzaOcProveedor(
        numero=_gen_numero_ocp(db),
        proveedor_id=body.proveedor_id,
        proveedor_nombre=nombre,
        pais=pais,
        moneda=moneda,
        estado="emitida",
        plazo_dias=body.plazo_dias,
        numero_oc=body.numero_oc,
        awb=body.awb,
        tracking=body.tracking,
        notas=body.notas,
        asesor_email=current_user.email,
    )
    db.add(ocp)
    db.flush()

    items = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(body.item_ids)).all()
    for it in items:
        it.estado_linea = "comprado"
        it.oc_proveedor_id = ocp.id
    db.commit()
    db.refresh(ocp)

    _log(db, current_user.email, "CREATE", "oc_proveedor", ocp.id, ocp.numero,
         f"OC {ocp.numero} a {nombre or 'proveedor'} - {len(items)} item(s)")
    return {"ok": True, "ocp_id": ocp.id, "numero": ocp.numero, "items": len(items)}


# Items comprados (OC emitida, esperando preparar)
@router.get("/comprados")
def comprados(q: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "comprado")
    )
    if q:
        query = query.filter((MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) | (MonzaCotizacion.numero.ilike(f"%{q}%")))
    items = query.order_by(MonzaCotizacionItem.id).all()
    cache = {}
    out = []
    for it in items:
        d = _item_dict(it, it.cotizacion)
        if it.oc_proveedor_id:
            if it.oc_proveedor_id not in cache:
                cache[it.oc_proveedor_id] = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == it.oc_proveedor_id).first()
            ocp = cache[it.oc_proveedor_id]
            if ocp:
                d["ocp_numero"] = ocp.numero_oc or ocp.numero
                d["ocp_proveedor"] = ocp.proveedor_nombre
                d["ocp_plazo_dias"] = ocp.plazo_dias
        out.append(d)
    return out


# Preparar items: comprado -> preparado (handoff a Logistica)
class PrepararBody(BaseModel):
    item_ids: List[int]


@router.post("/preparar")
def preparar(body: PrepararBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    items = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(body.item_ids), MonzaCotizacionItem.estado_linea == "comprado").all()
    for it in items:
        it.estado_linea = "preparado"
    db.commit()
    _log(db, current_user.email, "UPDATE", "item", None, None, f"{len(items)} ítem(s) preparado(s) para Logística")
    return {"ok": True, "preparados": len(items)}


# Listar OCs de proveedor
@router.get("/ocs")
def list_ocs(
    estado: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = db.query(MonzaOcProveedor)
    if estado:
        query = query.filter(MonzaOcProveedor.estado == estado)
    ocs = query.order_by(MonzaOcProveedor.id.desc()).all()
    out = []
    for ocp in ocs:
        n_items = db.query(func.count(MonzaCotizacionItem.id)).filter(
            MonzaCotizacionItem.oc_proveedor_id == ocp.id
        ).scalar() or 0
        out.append({
            "id": ocp.id,
            "numero": ocp.numero,
            "proveedor_nombre": ocp.proveedor_nombre,
            "pais": ocp.pais,
            "moneda": ocp.moneda,
            "estado": ocp.estado,
            "plazo_dias": ocp.plazo_dias,
            "numero_oc": ocp.numero_oc,
            "awb": ocp.awb,
            "tracking": ocp.tracking,
            "notas": ocp.notas,
            "asesor_email": ocp.asesor_email,
            "items_count": n_items,
            "created_at": ocp.created_at.isoformat() if ocp.created_at else None,
        })
    return out


class OcUpdateBody(BaseModel):
    estado: Optional[str] = None
    numero_oc: Optional[str] = None
    awb: Optional[str] = None
    tracking: Optional[str] = None
    plazo_dias: Optional[int] = None
    notas: Optional[str] = None


@router.patch("/ocs/{ocp_id}")
def update_oc(ocp_id: int, body: OcUpdateBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    ocp = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == ocp_id).first()
    if not ocp:
        raise HTTPException(status_code=404, detail="OC no encontrada")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(ocp, field, value)
    ocp.updated_at = datetime.utcnow()

    # El estado de la OC es informativo; el pipeline de items lo manejan
    # Abastecimiento (preparar), Logistica (embarque) y Bodega (recepcion).
    db.commit()
    db.refresh(ocp)
    _log(db, current_user.email, "UPDATE", "oc_proveedor", ocp.id, ocp.numero,
         f"OC {ocp.numero} -> {ocp.estado}")
    if body.estado == "llegada":
        crear_notif(db, f"Mercadería en recepción · {ocp.numero}", f"{ocp.proveedor_nombre or 'Proveedor'} — lista para recibir en bodega", "info", "/monzaparts/bodega", "oc_proveedor", ocp.id)
    return {"ok": True}


# Items de una OC (detalle completo para Logistica)
@router.get("/ocs/{ocp_id}/items")
def oc_items(ocp_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    items = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.oc_proveedor_id == ocp_id)
        .all()
    )
    return [_item_dict(it, it.cotizacion) for it in items]


# Proveedores de abastecimiento CRUD
class ProveedorBody(BaseModel):
    nombre: str
    pais: Optional[str] = None
    moneda: Optional[str] = "EUR"
    contacto: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    notas: Optional[str] = None


@router.get("/proveedores")
def list_proveedores(db: Session = Depends(get_db), _=Depends(get_current_user)):
    provs = db.query(MonzaProvAbast).filter(MonzaProvAbast.activo == 1).order_by(MonzaProvAbast.nombre).all()
    return [
        {
            "id": p.id, "nombre": p.nombre, "pais": p.pais, "moneda": p.moneda,
            "contacto": p.contacto, "email": p.email, "telefono": p.telefono, "notas": p.notas,
        }
        for p in provs
    ]


@router.post("/proveedores")
def create_proveedor(body: ProveedorBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    p = MonzaProvAbast(**body.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    _log(db, current_user.email, "CREATE", "proveedor", p.id, p.nombre, f"Proveedor {p.nombre} creado")
    return {"ok": True, "id": p.id}


@router.patch("/proveedores/{prov_id}")
def update_proveedor(prov_id: int, body: ProveedorBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    p = db.query(MonzaProvAbast).filter(MonzaProvAbast.id == prov_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(p, field, value)
    db.commit()
    return {"ok": True}


@router.delete("/proveedores/{prov_id}")
def delete_proveedor(prov_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    p = db.query(MonzaProvAbast).filter(MonzaProvAbast.id == prov_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    p.activo = 0
    db.commit()
    return {"ok": True}
