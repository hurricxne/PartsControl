from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_models import MonzaCotizacion, MonzaCliente

router = APIRouter(prefix="/api/monza/ventas", tags=["monza-ventas"])


@router.get("")
def list_ventas(
    q: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    # Ventas = cotizaciones vendidas + otras (para seguimiento completo)
    query = (
        db.query(MonzaCotizacion)
        .options(
            joinedload(MonzaCotizacion.cliente),
            joinedload(MonzaCotizacion.lead),
            joinedload(MonzaCotizacion.asesor),
            joinedload(MonzaCotizacion.items),
        )
        .filter(MonzaCotizacion.estado.in_(["vendida", "propuesta", "enviada"]))
    )

    if q:
        query = query.join(MonzaCotizacion.cliente, isouter=True).filter(
            or_(
                MonzaCotizacion.numero.ilike(f"%{q}%"),
                MonzaCliente.nombre.ilike(f"%{q}%"),
                MonzaCotizacion.oc_cliente.ilike(f"%{q}%"),
                MonzaCotizacion.vehiculo.ilike(f"%{q}%"),
            )
        )

    if estado and estado != "todas":
        query = query.filter(MonzaCotizacion.estado == estado)

    if desde:
        query = query.filter(MonzaCotizacion.fecha_creacion >= datetime.fromisoformat(desde))
    if hasta:
        query = query.filter(MonzaCotizacion.fecha_creacion <= datetime.fromisoformat(hasta))

    total = query.count()
    items = (
        query.order_by(MonzaCotizacion.fecha_creacion.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_venta_dict(c) for c in items],
    }


ORDER_LINEA = ["cotizado","por_comprar","comprado","en_transito","por_recibir","en_bodega","despachado"]
def _pipeline_estado(items):
    if not items: return None
    idxs=[ORDER_LINEA.index(i.estado_linea) if (i.estado_linea in ORDER_LINEA) else 0 for i in items]
    return ORDER_LINEA[min(idxs)]

def _venta_dict(c: MonzaCotizacion) -> dict:
    asesor_nombre = c.asesor.email.split("@")[0].title() if c.asesor else None
    return {
        "id": c.id,
        "numero": c.numero,
        "estado": c.estado,
        "linea": c.linea,
        "vehiculo": c.vehiculo,
        "oc_cliente": c.oc_cliente,
        "fecha_venta": c.fecha_venta.isoformat() if c.fecha_venta else None,
        "fecha_entrega_est": c.fecha_entrega_est.isoformat() if c.fecha_entrega_est else None,
        "total_bruto": c.total_bruto,
        "items_count": len(c.items),
        "pipeline": _pipeline_estado(c.items),
        "asesor": asesor_nombre,
        "fecha_creacion": c.fecha_creacion.isoformat(),
        "cliente": {
            "id": c.cliente.id,
            "nombre": c.cliente.nombre,
            "rut": c.cliente.rut,
        } if c.cliente else None,
        "lead_numero": c.lead.numero if c.lead else None,
    }


@router.get("/kpis")
def ventas_kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    anio = datetime.utcnow().year
    mes = datetime.utcnow().month
    inicio_mes = datetime(anio, mes, 1)

    vendidas_mes = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "vendida",
        MonzaCotizacion.fecha_venta >= inicio_mes,
    ).scalar() or 0

    total_mes = db.query(func.sum(MonzaCotizacion.total_bruto)).filter(
        MonzaCotizacion.estado == "vendida",
        MonzaCotizacion.fecha_venta >= inicio_mes,
    ).scalar() or 0

    pendientes_entrega = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "vendida",
        MonzaCotizacion.fecha_entrega_est.isnot(None),
    ).scalar() or 0

    return {
        "vendidas_mes": vendidas_mes,
        "total_mes": total_mes,
        "pendientes_entrega": pendientes_entrega,
    }
