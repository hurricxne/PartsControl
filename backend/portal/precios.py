"""Los montos que ve el cliente: SOLO el valor de venta de su pedido, en CLP.

Este archivo es la ÚNICA puerta por la que un monto sale hacia el portal, y existe para
que por esa puerta no pueda salir un costo:

  · `items_cotizacion.precio_unit_cotizacion` NO es un precio de venta. En una cotización
    de origen 'costo' es el precio del PROVEEDOR en USD; el precio de venta se calcula
    (flete, internación, gastos locales, margen). Mostrarlo sería mostrarle al cliente
    cuánto se le paga al proveedor.
  · `pricing_service.calcular_cotizacion` devuelve, junto al precio de venta, el costo
    unitario, el CIF, los gastos locales y el margen efectivo de cada ítem. De todo eso
    acá se toma UN número por ítem (`precio_venta_clp`) y nada más: lo que no se copia no
    se puede filtrar.

CUÁNDO HAY MONTO Y CUÁNDO NO (`precios_unitarios` devuelve {} = "sin montos")
    Solo se calcula un precio cuyos PARÁMETROS ya no se mueven:
      · cotización de origen 'venta_clp' (el precio se pactó en pesos), o
      · cotización con la foto de precios congelada al Cierre (`pricing_snapshot`) y
        COMPLETA: con todas las CLAVES_PRICING. A una foto parcial, lo que le falta se
        le completa con la configuración de hoy, o sea que no está congelada.
    Una venta de origen 'costo' cerrada ANTES de que existiera esa foto se recalcula con
    el dólar de hoy: su "precio" cambiaría en la pantalla del cliente cada vez que se
    actualiza el tipo de cambio, y no coincidiría con sus facturas. Para esas órdenes el
    portal no muestra montos (la pantalla pone un guion), tampoco en los ítems que ya
    tienen factura: lo que falta por facturar se valorizaría con el dólar del día y el
    total se movería. Sus facturas, que sí están congeladas, se siguen viendo como
    documentos.
    También queda sin montos una orden cuyo cálculo falla (config incompleta): mejor un
    guion que un 500 o un precio inventado con valores por defecto.

LA FOTO CONGELA PARÁMETROS, NO FILAS
    En origen 'costo' el precio de cada ítem depende de TODAS las filas de la cotización
    (el flete adicional y los gastos locales se prorratean), y el ERP deja editar o
    borrar después del cierre una línea que todavía no se compra. Cuando eso pasa, el
    cálculo de los demás ítems cambia en pesos, pero lo que ya viajó en una guía o se
    facturó, no. Por eso lo que sale de acá es solo el precio CALCULADO: el valor que ve
    el cliente lo arma portal/valor.py, que le da prioridad, unidad por unidad, al precio
    que quedó en la factura o en la guía.

El cálculo necesita TODAS las filas de la cotización: calcular sobre una parte da otros
precios.

EL REDONDEO es el de la factura y la guía (routers/contabilidad.py: _precio2,
_total_linea, _iva_clp): precio unitario a 2 decimales, total de línea e IVA a peso con
half-up, multiplicando en float y redondeando después. No "mejorarlo" con Decimal de
punta a punta: da $1 de diferencia contra la factura en algunos casos. El total de una
línea se calcula sobre su cantidad COMPLETA (`total_de_linea`), no sumando los totales de
las filas en que esté partida: la suma de redondeos también difiere en $1.
"""
import json
import logging
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable

from sqlalchemy.orm import Session

from models.models import ConfiguracionCotizador, Cotizacion, ItemCotizacion
from services.pricing_service import CLAVES_PRICING, calcular_cotizacion, config_efectivo

logger = logging.getLogger("portal")

TASA_IVA = 0.19
ORIGEN_VENTA_CLP = "venta_clp"


@dataclass(frozen=True)
class TotalesOrden:
    neto_clp: int
    iva_clp: int
    total_clp: int


def a_peso(valor: float) -> int:
    """Redondeo a peso, half-up (el de la factura)."""
    return int(Decimal(str(valor)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def config_global(db: Session) -> dict:
    """Los parámetros de precio vigentes hoy. Solo completan una foto a la que le falte
    alguna clave; una venta sin foto no se calcula con ellos (ver el encabezado)."""
    cfg = db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    if cfg is None:
        return {}
    return {clave: getattr(cfg, clave, None) for clave in CLAVES_PRICING}


def _es_numero(valor) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


def _precio_congelado(cot: Cotizacion) -> bool:
    if (cot.origen or "") == ORIGEN_VENTA_CLP:
        return True
    try:
        foto = json.loads(cot.pricing_snapshot or "")
    except (ValueError, TypeError):
        return False
    if not isinstance(foto, dict):
        return False
    completa = all(_es_numero(foto.get(clave)) for clave in CLAVES_PRICING)
    if foto and not completa:
        logger.warning("portal: la foto de precios de la cotización %s está incompleta; "
                       "la orden se muestra sin montos", cot.id)
    return completa


def precios_unitarios(
    cot: Cotizacion, items: Iterable[ItemCotizacion], cfg_global: dict,
) -> Dict[int, float]:
    """{item_id: precio de VENTA unitario neto, 2 decimales} de la cotización.

    `items` debe traer TODAS las filas de `cot`. Devuelve {} cuando la orden no tiene un
    precio congelado que mostrar o el cálculo falla. Una fila cuyo precio da 0 no entra
    al dict: para el ERP un precio $0 es un dato faltante, no un regalo.
    """
    items = list(items)
    if not items or not _precio_congelado(cot):
        return {}
    entrada = [
        {
            "id": it.id,
            "cantidad": it.cantidad or 0,
            "precio_unit_cotizacion": it.precio_unit_cotizacion or 0,
            "peso_unit_lbs": it.peso_unit_lbs or 0,
            "margen_pct": it.margen_pct,
        }
        for it in items
    ]
    try:
        cfg = config_efectivo(cot.pricing_snapshot, cfg_global)
        calculo = calcular_cotizacion(entrada, {**cfg, "origen": (cot.origen or "costo")})
    except Exception:  # noqa: BLE001 — una config incompleta no puede tumbar la orden
        logger.exception("portal: no se pudo calcular el precio de la cotización %s", cot.id)
        return {}
    unitarios: Dict[int, float] = {}
    for fila in calculo.get("items", []):
        precio = round(float(fila.get("precio_venta_clp") or 0), 2)
        if precio > 0:
            unitarios[fila["id"]] = precio
    return unitarios


def total_de_linea(precio_unitario: float, cantidad: float) -> int:
    """Total neto de una línea: precio × cantidad COMPLETA de la línea, a peso."""
    return a_peso(precio_unitario * cantidad)


def totales(totales_de_linea: Iterable[int]) -> TotalesOrden:
    """Neto = suma de los totales de línea ya redondeados (lo que suma la factura), IVA a
    peso sobre ese neto."""
    neto = sum(totales_de_linea)
    iva = a_peso(neto * TASA_IVA)
    return TotalesOrden(neto_clp=neto, iva_clp=iva, total_clp=neto + iva)
