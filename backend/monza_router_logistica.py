"""
Logistica MonzaParts (alineacion MachParts): Embarque como entidad.
Agrupa items 'preparado' en un Embarque -> 'embarcado'.
Estados embarque: en_bodega_proveedor -> en_transito -> en_aduana -> en_bodega
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError
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
class EmbarqueItemQty(BaseModel):
    item_id: int
    # int (la columna `cantidad` es Integer) y None = toda la línea. Ver el porqué
    # completo en monza_router_abastecimiento.PrepararParcialItem.
    cantidad: Optional[int] = None


class EmbarqueBody(BaseModel):
    # Vía LEGADA intacta: item_ids solos = la línea completa de cada ítem. Se le
    # quitó la obligatoriedad (default []) para que una llamada que solo trae
    # `items` valide; cualquier llamada actual sigue funcionando igual.
    item_ids: List[int] = []
    # Vía NUEVA (Fase 9b): cantidades por ítem. Campo en el MISMO body en vez de un
    # endpoint aparte — el criterio es no romper ninguna llamada actual, y con un
    # campo opcional el front elige la vía sin cambiar de URL (mismo patrón que
    # CrearDespachoBody.items en monza_router_despachos.py, ya probado en Monza).
    items: Optional[List[EmbarqueItemQty]] = None
    awb: Optional[str] = None
    forwarder: Optional[str] = None
    tracking: Optional[str] = None
    fecha_despacho: Optional[str] = None
    fecha_llegada_est: Optional[str] = None
    notas: Optional[str] = None


@router.post("/embarques")
def crear_embarque(body: EmbarqueBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Agrupa ítems 'preparado' en un Embarque → 'embarcado', con cantidad PARCIAL
    opcional por ítem (Fase 9b: el proveedor mandó 6 de 10 → viajan 6 y las 4 quedan
    en 'preparado' esperando el próximo AWB, sin reclamo fantasma en Bodega).

    Retry 1213/1205 (regla de la casa): ahora toma lock sobre MonzaCotizacionItem,
    las mismas filas que lockean despachos y el costeo de compras."""
    for _ in range(3):
        try:
            return _crear_embarque_tx(db, body, current_user)
        except OperationalError as e:
            db.rollback()
            code = getattr(getattr(e, "orig", None), "args", [None])[0]
            if code not in (1213, 1205):
                raise
    raise HTTPException(409, "Conflicto de concurrencia al crear el embarque: reintenta")


def _crear_embarque_tx(db: Session, body: EmbarqueBody, current_user):
    # Import LOCAL para no crear ciclo logistica↔abastecimiento a nivel de módulo
    # (el guard anti-nacional ya usaba esta puerta; el split y el lock viven al lado
    # de él para que la REGLA DE ORO exista en un solo lugar).
    from monza_router_abastecimiento import (
        _rechazar_items_nacionales, _validar_pedidos_parciales,
        _lockear_items_para_split, _guard_duro_del_split, _partir_linea,
    )
    pedidos = list(body.items) if body.items else [
        EmbarqueItemQty(item_id=i) for i in (body.item_ids or [])
    ]
    if not pedidos:
        raise HTTPException(400, "Sin items")
    ids = _validar_pedidos_parciales(pedidos)

    # Guard anti-embarque: segunda de las 2 entradas al pipeline físico Monza
    # (la primera es /preparar en Abastecimiento). Un ítem de OC NACIONAL jamás
    # entra a un embarque — su camino es 'Registrar entrega nacional'. Sigue PRIMERO
    # para que su mensaje gane sobre el guard de estado.
    _rechazar_items_nacionales(db, ids)

    # Guard de ESTADO + lock id ASC. Endurecimiento deliberado de la vía legada: antes
    # el `filter(estado_linea == 'preparado')` DESCARTABA en silencio los ítems en
    # otro estado y el embarque nacía con menos ítems (o vacío) sin avisar. Ahora es
    # un 400 con el estado de cada ítem — el estándar que Monza fijó en la Fase 2.
    items_by_id = _lockear_items_para_split(db, ids, "preparado")
    # Guard DURO del split (409): no partir una línea que ya tocó un documento legal.
    # Solo se aplica a las líneas que de verdad se parten (ver _guard_duro_del_split).
    items_by_id = _guard_duro_del_split(db, pedidos, items_by_id, ids, "preparado")

    # Todas las validaciones ANTES de crear el embarque: un 400 no deja ni un
    # correlativo consumido ni una cabecera a medio flushear.
    emb = MonzaEmbarque(
        numero=_gen_numero_emb(db), estado="en_bodega_proveedor",
        awb=body.awb, forwarder=body.forwarder, tracking=body.tracking,
        fecha_despacho=body.fecha_despacho, fecha_llegada_est=body.fecha_llegada_est,
        notas=body.notas, asesor_email=current_user.email,
    )
    db.add(emb); db.flush()

    partidos = []
    for p in pedidos:
        it = items_by_id[p.item_id]
        # El remanente queda en 'preparado': ya está listo, solo espera otro AWB.
        clon = _partir_linea(db, it, p.cantidad, "preparado")
        it.estado_linea = "embarcado"
        # SOLO la línea que viaja entra al embarque. El clon NO se agrega: si entrara,
        # Bodega lo vería en el panel de recepción de ESTE embarque y volvería a
        # aparecer el faltante fantasma que esta fase viene a matar.
        db.add(MonzaEmbarqueItem(embarque_id=emb.id, item_id=it.id))
        if clon is not None:
            partidos.append({"item_id": it.id, "remanente_item_id": clon.id,
                             "embarcado": it.cantidad, "pendiente": clon.cantidad})

    # UN solo commit con el log adentro (patrón GA de despachos): sin trabajo después
    # del commit, porque esta función se reintenta ante deadlock.
    from monza_models import MonzaLog
    det = f"Embarque {emb.numero} · {len(pedidos)} item(s)"
    if partidos:
        det += " · " + ", ".join(
            f"parcial {p['embarcado']} (quedan {p['pendiente']})" for p in partidos)
    db.add(MonzaLog(user_email=current_user.email, accion="CREATE", entidad="embarque",
                    entidad_id=emb.id, entidad_ref=emb.numero, detalle=det))
    # Valores capturados ANTES del commit: expire_on_commit los expiraría y un
    # db.refresh() posterior sería trabajo DESPUÉS del commit dentro de una función
    # reintentada — un deadlock ahí crearía un SEGUNDO embarque por el mismo pedido.
    emb_id, emb_num = emb.id, emb.numero
    db.commit()
    return {"ok": True, "id": emb_id, "numero": emb_num, "items": len(pedidos),
            "partidos": len(partidos), "remanentes": partidos}


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
