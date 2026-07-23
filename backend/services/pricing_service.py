"""
Servicio de cálculo de precios del Cotizador.
Replica la lógica del Excel 'Cotizador MACHPARTS'.

Cálculo en dos pasadas (los costos locales y shipping adicional son proporcionales al total):
  Pasada 1: CIF por ítem (necesita totales de peso y CIF)
  Pasada 2: Gastos locales proporcionales → costo total → precio venta
"""
import json
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("pricing_service")

# Parámetros GLOBALES que determinan el precio de venta: si cualquiera cambia, TODA la
# cotización se re-precia. Congelarlos como "foto" al cerrar la venta evita que una venta
# ya cerrada se mueva sola cuando después se actualiza el dólar (u otro parámetro).
# El IVA no está aquí: en Grupo AM es fijo 19% en el propio cálculo, no un parámetro global.
CLAVES_PRICING = (
    "tipo_cambio_usd",
    "costo_shipping_usd_kg",
    "adicionales_shipping_usd",
    "costo_agencia_pct",
    "costo_agencia_minimo_clp",
    "desconsolidado_clp",
    "bodegaje_clp",
    "margen_venta_pct",
)


def snapshot_desde_config(config: Dict) -> Dict:
    """Extrae de un config global SOLO las claves que congelan el precio (CLAVES_PRICING).
    Esto es lo que se guarda como `Cotizacion.pricing_snapshot` (JSON) al cerrar la venta.
    Ignora valores None: una clave ausente cae al global vía `config_efectivo`."""
    return {k: config[k] for k in CLAVES_PRICING
            if config.get(k) is not None}


def config_efectivo(snapshot_json: Optional[str], config_global: Dict) -> Dict:
    """Config de precio a USAR: la foto congelada de la cotización si existe; si no, el
    config global vivo.

    - `snapshot_json` None/'' (borrador o venta anterior a esta función) → global tal cual.
    - JSON corrupto o no-dict → global (nunca revienta: un dato malo no debe tumbar precios).
    - El snapshot se mergea SOBRE el global, así una clave nueva que aún no estaba congelada
      cae al global (agregar un parámetro futuro no rompe cotizaciones viejas). Solo se
      respetan claves de CLAVES_PRICING con valor no-None.
    """
    if not snapshot_json:
        return config_global
    try:
        snap = json.loads(snapshot_json)
    except (ValueError, TypeError):
        logger.warning("pricing_snapshot corrupto (no es JSON); usando config global.")
        return config_global
    if not isinstance(snap, dict) or not snap:
        return config_global
    congelado = {k: v for k, v in snap.items()
                 if k in CLAVES_PRICING and v is not None}
    return {**config_global, **congelado}


def calcular_cotizacion(items: List[Dict], config: Dict) -> Dict:
    """
    Recibe:
      items: lista de dicts con campos editables del ítem
      config: dict con parámetros globales

    Retorna: dict con items_calculados y totales
    """
    tc = config.get("tipo_cambio_usd", 940.0)
    shipping_rate = config.get("costo_shipping_usd_kg", 3.8)
    awb_fijo_usd = config.get("adicionales_shipping_usd", 440.0)
    agencia_pct = config.get("costo_agencia_pct", 0.01)
    agencia_min = config.get("costo_agencia_minimo_clp", 160000.0)
    desconsolidado = config.get("desconsolidado_clp", 90000.0)
    bodegaje = config.get("bodegaje_clp", 90000.0)
    margen_default = config.get("margen_venta_pct", 0.19)

    if not items:
        return {"items": [], "totales": _totales_vacios()}

    # RAMA_VENTA_CLP: precios de venta en CLP (margen ya incluido); sin USD/CIF/IVA.
    if str(config.get("origen") or "") == "venta_clp":
        _res = []
        for _it in items:
            _qty = float(_it.get("cantidad") or 0)
            _costo = float(_it.get("precio_unit_cotizacion") or 0)
            _margen = float(_it.get("margen_pct") or 0)
            _pv = round(_costo * (1 + _margen))  # CLP: peso entero (exactitud del round-trip precio<->margen)
            _tv = _pv * _qty
            _res.append({**_it, "peso_total_kg": 0.0, "cif_clp": 0.0, "gastos_locales_clp": 0.0,
                         "costo_total_clp": _tv, "costo_unitario_clp": _costo,
                         "margen_efectivo": _margen,
                         "precio_venta_clp": _pv, "total_venta_clp": _tv, "total_exwork_usd": 0.0})
        _sub = sum(_r["total_venta_clp"] for _r in _res)
        return {"items": _res, "totales": {"total_peso_kg": 0.0, "total_cif_clp": 0.0,
                "gastos_locales_total_clp": 0.0, "subtotal_neto_clp": _sub, "iva_clp": _sub * 0.19,
                "total_con_iva_clp": _sub * 1.19, "total_exwork_usd": 0.0}}

    # --- Pasada 1: cálculos por ítem independientes ---
    pass1 = []
    for item in items:
        qty = float(item.get("cantidad") or 0)
        precio_usd = float(item.get("precio_unit_cotizacion") or 0)
        peso_lbs = float(item.get("peso_unit_lbs") or 0)

        total_exwork_usd = qty * precio_usd
        peso_total_lbs = qty * peso_lbs
        peso_total_kg = peso_total_lbs * 0.45359
        shipping_item_clp = peso_total_kg * shipping_rate * tc

        pass1.append({
            **item,
            "total_exwork_usd": total_exwork_usd,
            "peso_total_lbs": peso_total_lbs,
            "peso_total_kg": peso_total_kg,
            "shipping_item_clp": shipping_item_clp,
        })

    total_peso_kg = sum(r["peso_total_kg"] for r in pass1)

    # CIF previo por ítem (sin gastos locales)
    for r in pass1:
        adic = (r["peso_total_kg"] / total_peso_kg * awb_fijo_usd * tc) if total_peso_kg > 0 else 0
        r["adic_shipping_clp"] = adic
        r["cif_clp"] = (r["total_exwork_usd"] * tc) + r["shipping_item_clp"] + adic

    total_cif = sum(r["cif_clp"] for r in pass1)

    # Gastos locales totales
    gastos_locales_total = max(agencia_min, agencia_pct * total_cif) + desconsolidado + bodegaje

    # --- Pasada 2: gastos locales proporcionales → costo total → precio ---
    result_items = []
    for r in pass1:
        gastos_loc = (r["cif_clp"] / total_cif * gastos_locales_total) if total_cif > 0 else 0
        costo_total = r["cif_clp"] + gastos_loc
        qty = float(r.get("cantidad") or 1)
        costo_unit = costo_total / qty if qty else 0
        margen = float(r.get("margen_pct") or margen_default)
        precio_venta = round(costo_unit * (1 + margen))  # CLP: peso entero
        total_venta = precio_venta * qty

        result_items.append({
            **r,
            "gastos_locales_clp": gastos_loc,
            "costo_total_clp": costo_total,
            "costo_unitario_clp": costo_unit,
            "margen_efectivo": margen,
            "precio_venta_clp": precio_venta,
            "total_venta_clp": total_venta,
        })

    # Totales generales
    subtotal_neto = sum(r["total_venta_clp"] for r in result_items)
    iva = subtotal_neto * 0.19
    totales = {
        "total_peso_kg": total_peso_kg,
        "total_cif_clp": total_cif,
        "gastos_locales_total_clp": gastos_locales_total,
        "subtotal_neto_clp": subtotal_neto,
        "iva_clp": iva,
        "total_con_iva_clp": subtotal_neto + iva,
        "total_exwork_usd": sum(r["total_exwork_usd"] for r in result_items),
    }

    return {"items": result_items, "totales": totales}


def _totales_vacios() -> Dict:
    return {
        "total_peso_kg": 0,
        "total_cif_clp": 0,
        "gastos_locales_total_clp": 0,
        "subtotal_neto_clp": 0,
        "iva_clp": 0,
        "total_con_iva_clp": 0,
        "total_exwork_usd": 0,
    }
