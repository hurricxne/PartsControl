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


# Vocabulario de SALIDA del badge de la lista de Ventas (las claves de PIPELINE_CFG
# en MonzaVentasPage.tsx). NO es el vocabulario de estado_linea: el pipeline REAL
# escribe 'preparado' y 'embarcado', y NUNCA escribe 'en_transito' ni 'por_recibir'
# (0 filas con esos valores; ver el comentario del KPI en monza_router_abastecimiento).
ORDER_LINEA = ["cotizado", "por_comprar", "comprado", "en_transito", "por_recibir",
               "en_bodega", "despachado", "reclamo"]

# BUG VIVO que arregla la Fase 9b: 'preparado' y 'embarcado' NO estaban en la lista,
# así que caían al `else 0` = "cotizado" y una venta con TODO preparado se mostraba
# como "Sin abastecer" en Ventas. Con la partición de líneas el síntoma pasaría de
# esporádico a permanente: el remanente en 'comprado'/'preparado' sería el mínimo de
# casi todas las ventas partidas. El mapeo espeja _STATE_BUCKETS de
# monza_router_despachos.py (preparado/embarcado = tramo "volando"), que es la fuente
# de verdad de los buckets, y traduce al vocabulario que el badge ya conoce.
_LINEA_A_PIPELINE = {
    "cotizado": "cotizado",
    "por_comprar": "por_comprar",
    "comprado": "comprado",
    "preparado": "en_transito",     # listo en el proveedor: ya "volando" para Ventas
    "embarcado": "en_transito",
    "en_transito": "en_transito",    # valores legados: se toleran, no se escriben
    "por_recibir": "por_recibir",
    "en_bodega": "en_bodega",
    "despachado": "despachado",
    # 'reclamo' va ÚLTIMO en ORDER_LINEA (igual que en _BUCKET_ORDER de despachos):
    # solo gana el min cuando TODAS las líneas están en reclamo, así una venta mixta
    # sigue mostrando la etapa que realmente la está frenando.
    "reclamo": "reclamo",
}


def _pipeline_estado(items):
    """Etapa de la venta = la línea MENOS avanzada (lo que la está frenando).

    Un estado desconocido se IGNORA en vez de contarse como "cotizado": ese `else 0`
    era justamente el bug — un solo valor nuevo arrastraba la venta entera al primer
    casillero. Si ninguna línea tiene estado reconocible se devuelve None (el front
    simplemente no pinta el badge)."""
    if not items:
        return None
    idxs = [
        ORDER_LINEA.index(_LINEA_A_PIPELINE[i.estado_linea])
        for i in items if i.estado_linea in _LINEA_A_PIPELINE
    ]
    if not idxs:
        return None
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
        # Venta a cliente particular: la "OC" es el N° de esta cotización (ver
        # monza_models.MonzaCotizacion.cliente_sin_oc). Viaja para que la pantalla lo
        # marque como tal en vez de mostrarlo como una OC del cliente.
        "cliente_sin_oc": bool(c.cliente_sin_oc),
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
