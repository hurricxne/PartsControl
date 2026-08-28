from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from monza_fechas import inicio_mes_utc, rango_utc
from monza_models import MonzaCotizacion, MonzaCliente

# CANDADO DE EMPRESA a nivel de ROUTER (2026-08-22): el CRM de MonzaParts estaba
# abierto a cualquier usuario autenticado —incluidos los de minería— mientras
# Despachos, Bodega y el PATCH de Cotizaciones ya lo tenían desde la auditoría F6.
# Router COMPLETO (lecturas incluidas): candar solo las escrituras deja la lectura
# de los datos del cliente como puerta del costado. Ver monza_router_leads.py.
router = APIRouter(
    prefix="/api/monza/ventas",
    tags=["monza-ventas"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

# Qué es una VENTA para esta pestaña (arreglos del equipo 2026-08-21): una cotización
# CERRADA — vendida o ya despachada. Antes el filtro metía propuestas y enviadas (que
# viven en Cotizaciones) y EXCLUÍA las despachadas, así que una venta desaparecía de la
# pestaña el día que salía a reparto. Espejo del par de monza_contabilidad/router.py
# (ESTADOS_VENTA), replicado acá con comentario — patrón de la casa, los routers no se
# importan entre sí para esto.
ESTADOS_VENTA = ("vendida", "despachado")


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
    # Ventas = SOLO cotizaciones cerradas (vendida/despachado); las propuestas y
    # enviadas se siguen desde la pestaña Cotizaciones. `estado` filtra DENTRO del par.
    query = (
        db.query(MonzaCotizacion)
        .options(
            joinedload(MonzaCotizacion.cliente),
            joinedload(MonzaCotizacion.lead),
            joinedload(MonzaCotizacion.asesor),
            joinedload(MonzaCotizacion.items),
        )
        .filter(MonzaCotizacion.estado.in_(ESTADOS_VENTA))
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

    # El operador digita DÍAS DE CHILE y la columna guarda UTC (ver monza_fechas):
    # rango SEMIABIERTO para que el día `hasta` entre COMPLETO. Antes, «hasta hoy»
    # comparaba contra la medianoche y escondía todas las ventas del propio día.
    # Se filtra por `fecha_venta`, NO por `fecha_creacion`: esta pantalla se llama
    # Ventas, la columna que muestra dice «Vendida» y las tarjetas de arriba cuentan con
    # fecha_venta. Filtrando por la fecha de la COTIZACIÓN, una venta cotizada en junio y
    # cerrada en agosto no aparecía al pedir agosto —aparecía al pedir junio, con la
    # columna diciendo agosto— y el número de la tarjeta no cuadraba con las filas de
    # abajo. (La lista de Cotizaciones sí debe seguir cortando por fecha_creacion: ahí la
    # fecha del negocio es cuándo se cotizó.)
    desde_utc, hasta_utc = rango_utc(desde, hasta)
    if desde_utc:
        query = query.filter(MonzaCotizacion.fecha_venta >= desde_utc)
    if hasta_utc:
        query = query.filter(MonzaCotizacion.fecha_venta < hasta_utc)

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
    # Mes en curso DE CHILE (monza_fechas): con el corte en UTC, las ventas cerradas
    # entre las 21:00 y la medianoche del último día caían en el mes siguiente.
    inicio_mes = inicio_mes_utc()

    # Los DOS KPIs de plata usan el MISMO par que la lista: sin 'despachado', una venta
    # cerrada y despachada dentro del mes aparecía en la tabla pero no sumaba en las
    # tarjetas de arriba (ni en el Dashboard, que consume este endpoint). El corte
    # mensual sigue siendo fecha_venta — despachar no cambia el mes en que se vendió.
    vendidas_mes = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado.in_(ESTADOS_VENTA),
        MonzaCotizacion.fecha_venta >= inicio_mes,
    ).scalar() or 0

    total_mes = db.query(func.sum(MonzaCotizacion.total_bruto)).filter(
        MonzaCotizacion.estado.in_(ESTADOS_VENTA),
        MonzaCotizacion.fecha_venta >= inicio_mes,
    ).scalar() or 0

    # Solo 'vendida' A PROPÓSITO: el flip a 'despachado' es exactamente lo que saca a
    # una venta de "pendiente de entrega".
    pendientes_entrega = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "vendida",
        MonzaCotizacion.fecha_entrega_est.isnot(None),
    ).scalar() or 0

    return {
        "vendidas_mes": vendidas_mes,
        "total_mes": total_mes,
        "pendientes_entrega": pendientes_entrega,
    }
