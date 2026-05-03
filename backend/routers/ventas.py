"""
Router de Ventas — vista enriquecida de cotizaciones aceptadas con datos de OC.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models.models import (
    Cotizacion, ItemCotizacion, OcCliente, OcProveedor, OcProveedorItem,
    User, Embarque, EmbarqueItem, PreEmbarque, ConfiguracionCotizador,
)
from services.pricing_service import calcular_cotizacion

router = APIRouter(prefix="/ventas", tags=["ventas"])


def _cfg_to_dict(cfg) -> dict:
    if cfg is None:
        return {}
    return {
        "tipo_cambio_usd": cfg.tipo_cambio_usd,
        "costo_shipping_usd_kg": cfg.costo_shipping_usd_kg,
        "adicionales_shipping_usd": cfg.adicionales_shipping_usd,
        "costo_agencia_pct": cfg.costo_agencia_pct,
        "costo_agencia_minimo_clp": cfg.costo_agencia_minimo_clp,
        "desconsolidado_clp": cfg.desconsolidado_clp,
        "bodegaje_clp": cfg.bodegaje_clp,
        "margen_venta_pct": cfg.margen_venta_pct,
    }


def _calc_items(items_db, cfg_dict):
    """Returns mapping item_id -> total_venta_clp."""
    item_dicts = [
        {
            "id": i.id,
            "cantidad": i.cantidad or 0,
            "precio_unit_cotizacion": i.precio_unit_cotizacion or 0,
            "peso_unit_lbs": i.peso_unit_lbs or 0,
            "margen_pct": i.margen_pct,
        }
        for i in items_db
    ]
    result = calcular_cotizacion(item_dicts, cfg_dict)
    return {ci["id"]: ci for ci in result.get("items", [])}, result.get("totales", {})


def _build_venta(cot, items_db, cfg_dict, db):
    """Build full venta dict from cotizacion + its items."""
    calc_map, totales = _calc_items(items_db, cfg_dict)

    oc = cot.oc_cliente

    items_out = []
    items_vendidos = 0
    for item in items_db:
        ci = calc_map.get(item.id, {})
        total_venta = ci.get("total_venta_clp", 0)
        precio_unit_venta = ci.get("precio_venta_clp", 0)

        if item.estado_item not in ("ingresado", None):
            items_vendidos += 1

        asig = (
            db.query(OcProveedorItem)
            .filter(OcProveedorItem.item_cotizacion_id == item.id)
            .first()
        )
        ocp = None
        if asig and asig.oc_proveedor_id:
            ocp = db.query(OcProveedor).filter(OcProveedor.id == asig.oc_proveedor_id).first()

        # embarque info
        emb_item = (
            db.query(EmbarqueItem)
            .filter(EmbarqueItem.item_cotizacion_id == item.id)
            .first()
        )
        emb = None
        if emb_item:
            emb = db.query(Embarque).filter(Embarque.id == emb_item.embarque_id).first()

        items_out.append({
            "id": item.id,
            "item_num": item.item_num,
            "numero_parte": item.numero_parte or "",
            "descripcion": item.descripcion or "",
            "marca": item.marca or "",
            "cantidad": item.cantidad or 1,
            "precio_unit_venta_clp": round(precio_unit_venta, 0),
            "total_venta_clp": round(total_venta, 0),
            "estado_item": item.estado_item or "ingresado",
            "plazo_entrega_max": item.plazo_entrega_max,
            "ocp_numero": ocp.numero if ocp else None,
            "ocp_numero_oc": ocp.numero_oc if ocp else None,
            "ocp_proveedor": ocp.proveedor if ocp else None,
            "ocp_pais": ocp.pais if ocp else None,
            "embarque_numero": emb.numero if emb else None,
            "embarque_estado": emb.estado if emb else None,
            "embarque_awb": emb.awb if emb else None,
        })

    total_neto = totales.get("subtotal_neto_clp", 0)
    iva = totales.get("iva_clp", 0)
    total_con_iva = totales.get("total_con_iva_clp", 0)

    asesor_nombre = None
    if cot.user_id:
        user = db.query(User).filter(User.id == cot.user_id).first()
        asesor_nombre = user.nombre if user else None

    return {
        "id": cot.id,
        "numero": cot.numero,
        "cliente": cot.cliente or "",
        "rut_cliente": cot.rut_cliente or "",
        "referencia": cot.referencia or "",
        "asesor": asesor_nombre,
        "fase_comercial": cot.fase_comercial or "ingresada",
        "total_items": len(items_out),
        "items_vendidos": items_vendidos,
        "pct_vendido": round(items_vendidos / len(items_out) * 100) if items_out else 0,
        "total_neto_clp": round(total_neto, 0),
        "iva_clp": round(iva, 0),
        "total_con_iva_clp": round(total_con_iva, 0),
        "numero_oc_cliente": oc.numero_oc if oc else None,
        "fecha_oc": oc.fecha_oc if oc else None,
        "cond_pago": oc.cond_pago if oc else None,
        "fecha_entrega": oc.fecha_entrega if oc else None,
        "created_at": cot.created_at.isoformat() if cot.created_at else None,
        "items": items_out,
    }


@router.get("/")
def listar_ventas(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cfg = db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    cfg_dict = _cfg_to_dict(cfg)

    # Cotizaciones with fase_comercial='aceptada' OR that have an OcCliente
    cots_aceptadas = (
        db.query(Cotizacion)
        .filter(Cotizacion.fase_comercial == "aceptada")
        .all()
    )
    cot_ids_aceptadas = {c.id for c in cots_aceptadas}

    ocs = db.query(OcCliente).all()
    cot_ids_con_oc = {oc.cotizacion_id for oc in ocs}

    all_cot_ids = cot_ids_aceptadas | cot_ids_con_oc
    if not all_cot_ids:
        return []

    cots = (
        db.query(Cotizacion)
        .filter(Cotizacion.id.in_(all_cot_ids))
        .order_by(Cotizacion.created_at.desc())
        .all()
    )

    result = []
    for cot in cots:
        items_db = (
            db.query(ItemCotizacion)
            .filter(ItemCotizacion.cotizacion_id == cot.id)
            .all()
        )
        if not items_db:
            continue
        result.append(_build_venta(cot, items_db, cfg_dict, db))

    return result


@router.get("/kpis")
def get_kpis(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cfg = db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    cfg_dict = _cfg_to_dict(cfg)

    ocs = db.query(OcCliente).all()
    cot_ids = {oc.cotizacion_id for oc in ocs}

    total_ventas = len(cot_ids)
    total_facturado = 0.0

    for cot_id in cot_ids:
        items_db = (
            db.query(ItemCotizacion)
            .filter(ItemCotizacion.cotizacion_id == cot_id)
            .all()
        )
        item_dicts = [
            {
                "id": i.id,
                "cantidad": i.cantidad or 0,
                "precio_unit_cotizacion": i.precio_unit_cotizacion or 0,
                "peso_unit_lbs": i.peso_unit_lbs or 0,
                "margen_pct": i.margen_pct,
            }
            for i in items_db
        ]
        calc = calcular_cotizacion(item_dicts, cfg_dict)
        total_facturado += calc.get("totales", {}).get("total_con_iva_clp", 0)

    return {
        "total_ventas": total_ventas,
        "total_facturado_clp": round(total_facturado, 0),
        "total_x_cobrar_clp": 0,
        "total_cobrado_clp": 0,
    }


@router.get("/mensual")
def get_mensual(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from collections import defaultdict
    cfg = db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    cfg_dict = _cfg_to_dict(cfg)

    ocs = db.query(OcCliente).order_by(OcCliente.created_at).all()
    monthly = defaultdict(lambda: {"ventas": 0, "total_clp": 0})

    for oc in ocs:
        cot = db.query(Cotizacion).filter(Cotizacion.id == oc.cotizacion_id).first()
        if not cot:
            continue
        ts = cot.created_at
        key = f"{ts.year}-{ts.month:02d}" if ts else "unknown"
        items_db = (
            db.query(ItemCotizacion)
            .filter(ItemCotizacion.cotizacion_id == cot.id)
            .all()
        )
        item_dicts = [
            {
                "id": i.id,
                "cantidad": i.cantidad or 0,
                "precio_unit_cotizacion": i.precio_unit_cotizacion or 0,
                "peso_unit_lbs": i.peso_unit_lbs or 0,
                "margen_pct": i.margen_pct,
            }
            for i in items_db
        ]
        calc = calcular_cotizacion(item_dicts, cfg_dict)
        monthly[key]["ventas"] += 1
        monthly[key]["total_clp"] += calc.get("totales", {}).get("subtotal_neto_clp", 0)

    return [
        {"mes": k, "ventas": v["ventas"], "total_neto_clp": round(v["total_clp"], 0)}
        for k, v in sorted(monthly.items())
    ]


@router.get("/export")
def export_ventas(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns a simple JSON export (Excel generation can be added later)."""
    from fastapi.responses import JSONResponse
    cfg = db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    cfg_dict = _cfg_to_dict(cfg)

    ocs = db.query(OcCliente).all()
    cot_ids = {oc.cotizacion_id for oc in ocs}

    rows = []
    for cot_id in cot_ids:
        cot = db.query(Cotizacion).filter(Cotizacion.id == cot_id).first()
        if not cot:
            continue
        items_db = (
            db.query(ItemCotizacion)
            .filter(ItemCotizacion.cotizacion_id == cot.id)
            .all()
        )
        v = _build_venta(cot, items_db, cfg_dict, db)
        rows.append(v)

    return rows
