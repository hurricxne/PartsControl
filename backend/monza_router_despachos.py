from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_models import MonzaCotizacion, MonzaCliente

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
