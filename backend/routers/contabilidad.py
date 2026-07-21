"""Módulo de Contabilidad — Ventas + Facturas/Cobranzas (con Factoring).

Router montado en /api/contabilidad. Es el lado de CUENTAS POR COBRAR del ERP.
Documentación completa del módulo (flujo, endpoints, mapeo con el frontend):
ver docs/ventas-contabilidad.md.

Reutiliza lo que ya existe en la app (no lo reescribe):
  - Identidad y montos de la venta: Cotizacion + OcCliente + ItemCotizacion, con el
    precio de venta calculado por `pricing_service.calcular_cotizacion` (igual que ventas.py).
  - Despachos/guías: Despacho + DespachoItem (los ítems se despachan por partes).

Agrega las tablas cont_*: factura a cliente, sus líneas, cobranzas (pagos) y factoring.

Flujo extremo a extremo:
  venta (Cotizacion + OcCliente) → despacho creado en Despachos → guía CERRADA y luego
  FIRMADA (en despachos.py: Despacho.estado=='despachado' AND guia_firmada==1) →
  recién ahí se EMITE la factura de esos ítems → cobranzas / factoring → KPIs.

REGLA RECTORA: SOLO se factura una guía de despacho FIRMADA (entregada y firmada por el
cliente), nunca por encima de la cantidad despachada ni dos veces (doble tope por ítem y
por guía, con control de cantidad ya facturada). Ver `_despacho_items_de_oc`.
ÚNICA EXCEPCIÓN: la factura de ANTICIPO (es_anticipo=1) respalda un adelanto del cliente
ante el SII y se emite SIN guía; su neto se descuenta automáticamente (línea negativa)
de las facturas del despacho real, para que Σ facturas de la OC == total de la venta.

ADELANTOS (cont_adelanto): Comercial los INFORMA (Cierre de Venta / Ventas), Tesorería
los APRUEBA (tesoreria/router.py, confirma la plata sin exigir cartola) y aquí se
APLICAN como cobranza medio='adelanto' al emitir facturas (vía A: facturas del despacho;
vía B: la factura de anticipo que los respalda). Esa cobranza NO es un depósito nuevo:
se excluye de la conciliación bancaria de ingresos (la plata se concilia abono↔adelanto).

Endpoints (todos requieren autenticación):
  GET    /ventas                              listado de ventas por OC + resumen de cobranza
  GET    /ventas/{oc}                         detalle de una venta (ítems, guías, facturas)
  GET    /ventas/{oc}/despachos-facturables   guías firmadas aún facturables (selector "Emitir factura")
  GET    /facturas                            listado de facturas + antigüedad de cartera
  POST   /facturas                            EMITIR una factura (desde una guía firmada o ítems)
  DELETE /facturas/{id}                       borrado seguro (no si hay pagos/factoring)
  POST   /facturas/{id}/cobranzas             registrar un pago del cliente
  DELETE /facturas/{id}/cobranzas/{id}        revertir un pago
  POST   /facturas/{id}/factoring             ceder la factura a un factor (adelanto/retención)
  POST   /facturas/{id}/factoring/liquidar    liquidar el factoring (cierra el saldo a 0)
  GET    /kpis                                indicadores de cobranza (ver get_kpis)
  POST   /ventas/adelantos                    informar un adelanto (Comercial; acepta cotizacion_id)
  GET    /ventas/{oc}/adelantos               adelantos de la venta (con estados derivados)
  PATCH  /adelantos/{id}                      editar lo informado (aprobados: re-aprobar en Tesorería)
  POST   /adelantos/{id}/anular               anular (no aplicado ni conciliado)
"""
import json
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload, joinedload
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from database import get_db
from models.models import (
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ConfiguracionCotizador, User, Cliente,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContFactoring,
    ContAdelanto,
)
from auth import get_current_user
from empresa_guard import require_empresa
from services.pricing_service import calcular_cotizacion
# Solo lectura del enlace de conciliación de Tesorería (abono ↔ cobranza): una cobranza
# conciliada con el banco no se puede borrar sin desconciliarla primero allá.
from tesoreria.models import ConciliacionIngreso

# Módulo SOLO MachParts (Grupo AM = 'mineria'): el guard a nivel de router deniega (403)
# a usuarios de otra empresa que intenten llegar por la API por fuera de la app.
router = APIRouter(
    prefix="/contabilidad",
    tags=["contabilidad"],
    dependencies=[Depends(require_empresa("mineria"))],
)

IVA_RATE = 0.19      # IVA Chile (19%); mantener sincronizado con pricing_service
TOL = 0.5            # tolerancia en CLP para clasificar saldos (pagada / al_día)
TOL_QTY = 0.001      # tolerancia para comparaciones de cantidades (unidades)
TOL_PAGO = 1.0       # holgura de 1 CLP en topes de pago/adelanto (redondeo de IVA/factoring)
DIAS_POR_VENCER = 7  # días para marcar una factura como 'por_vencer' en el semáforo
MEDIO_FACT_ADELANTO = "factoring_adelanto"
MEDIO_FACT_RETENCION = "factoring_retencion"
# Aplicación AUTOMÁTICA de un adelanto de cliente aprobado por Tesorería (cont_adelanto).
# NO es un depósito nuevo: su plata se concilia por la vía abono↔adelanto en Tesorería,
# por eso estas cobranzas se EXCLUYEN de la conciliación bancaria de ingresos.
MEDIO_ADELANTO = "adelanto"
ESTADOS_ADELANTO = ("informado", "aprobado", "anulado")

# Carga ansiosa (evita N+1): los serializadores/resúmenes recorren estas relaciones.
_FACTURA_EAGER = (
    selectinload(ContFacturaCliente.items),
    selectinload(ContFacturaCliente.cobranzas),
    selectinload(ContFacturaCliente.factoring),
    joinedload(ContFacturaCliente.despacho),
    joinedload(ContFacturaCliente.oc_cliente).joinedload(OcCliente.cotizacion),
)


def _es_medio_factoring(medio: Optional[str]) -> bool:
    return bool(medio and medio.startswith("factoring"))


# ─── Helpers básicos ──────────────────────────────────────────────────────────
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


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ─── Dinero / fecha / RUT — criterio ÚNICO compartido con la guía electrónica ──
# (wasabil_dte). Que la factura 33 y la guía 52 del mismo despacho CUADREN exige
# redondear IGUAL: precio unitario a 2 decimales ANTES de multiplicar, y half-up
# (no el round() banker's de Python) a peso en la línea y en el IVA.
def _precio2(precio) -> float:
    """Precio unitario neto redondeado a 2 decimales (base común guía/factura)."""
    return round(_f(precio), 2)


def _total_linea(precio, cantidad) -> float:
    """Total neto de una línea: precio(2 dec) × cantidad, half-up a peso (== guía/SII)."""
    p2 = _precio2(precio)
    return float(Decimal(str(p2 * cantidad)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _iva_clp(neto) -> float:
    """IVA 19% a peso con half-up (== SII / guía electrónica)."""
    return float(Decimal(str(_f(neto) * IVA_RATE)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _hoy_chile() -> date:
    """Fecha de hoy en Chile (America/Santiago) — la fecha tributaria del documento.
    El server en producción corre en UTC: pasadas las ~20-21h en Chile, date.today()
    ya sería 'mañana'. Mismo criterio que wasabil_dte.service.hoy_chile()."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Santiago")).date()


def _rut_saneado(rut: Optional[str]) -> str:
    return (rut or "").replace(".", "").replace(" ", "").strip().upper()


def _rut_canonico(rut: Optional[str]) -> str:
    """Forma canónica para COMPARAR RUTs (cuerpo+DV, sin puntos ni guión): así
    '78.279.030-7' y '782790307' son el mismo RUT. Para mostrar se usa el original."""
    return _rut_saneado(rut).replace("-", "")


def _rut_valido(rut: Optional[str]) -> bool:
    """Valida RUT chileno (cuerpo + dígito verificador, módulo 11). Acepta con o sin
    puntos/guión. No bloquea por puntos: el SII/Wasabil exige un RUT bien formado."""
    r = _rut_saneado(rut)
    if "-" in r:
        cuerpo, _, dv = r.partition("-")
    else:
        cuerpo, dv = r[:-1], r[-1:]
    if not cuerpo.isdigit() or len(cuerpo) < 7 or not dv:
        return False
    suma, factor = 0, 2
    for d in reversed(cuerpo):
        suma += int(d) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    dv_calc = "0" if resto == 11 else "K" if resto == 10 else str(resto)
    return dv == dv_calc


def _precios_de_cotizacion(db: Session, cot_id: int, cfg_dict: dict, items_db=None):
    """(items_db, {item_id: dict_calc}, totales) reutilizando pricing_service.
    SOLO precio de venta (sin costo: alcance de fase posterior).
    `items_db` permite pasar los ítems ya precargados en lote (anti N+1)."""
    if items_db is None:
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
    calc = calcular_cotizacion(item_dicts, {**cfg_dict, "origen": (items_db[0].cotizacion.origen if items_db else None) or "costo"})
    pmap = {ci["id"]: ci for ci in calc.get("items", [])}
    return items_db, pmap, calc.get("totales", {})


# ─── Helpers de integración con despachos (solo despachos 'despachado') ────────
def _despacho_items_de_oc(db: Session, oc_id: int):
    """DespachoItems facturables de la OC: despachos cerrados Y con la guía FIRMADA
    (entregada y firmada por el cliente). Solo eso es facturable."""
    return (
        db.query(DespachoItem, Despacho)
        .join(Despacho, Despacho.id == DespachoItem.despacho_id)
        .filter(
            Despacho.oc_cliente_id == oc_id,
            Despacho.estado == "despachado",
            Despacho.guia_firmada == 1,
        )
        .all()
    )


def _qty_despachada_por_item(db: Session, oc_id: int) -> dict:
    out = {}
    for di, _d in _despacho_items_de_oc(db, oc_id):
        out[di.item_cotizacion_id] = out.get(di.item_cotizacion_id, 0.0) + _f(di.qty_despachada)
    return out


def _qty_facturada_por_item(db: Session, oc_id: int) -> dict:
    rows = (
        db.query(ContFacturaClienteItem.item_cotizacion_id, ContFacturaClienteItem.cantidad)
        .join(ContFacturaCliente, ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
        .filter(ContFacturaCliente.oc_cliente_id == oc_id)
        .all()
    )
    out = {}
    for iid, qty in rows:
        out[iid] = out.get(iid, 0.0) + _f(qty)
    return out


def _qty_facturada_por_despacho_item(db: Session, oc_id: int) -> dict:
    rows = (
        db.query(ContFacturaClienteItem.despacho_item_id, ContFacturaClienteItem.cantidad)
        .join(ContFacturaCliente, ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
        .filter(
            ContFacturaCliente.oc_cliente_id == oc_id,
            ContFacturaClienteItem.despacho_item_id.isnot(None),
        )
        .all()
    )
    out = {}
    for did, qty in rows:
        out[did] = out.get(did, 0.0) + _f(qty)
    return out


# ─── Estado / saldo de la factura ─────────────────────────────────────────────
def _semaforo(fecha_venc: Optional[date], saldo: float) -> str:
    """Semáforo de vencimiento de una factura según su saldo y fecha de vencimiento.
    Devuelve: al_dia (saldada) | sin_fecha | vencida | por_vencer (<= DIAS_POR_VENCER) | vigente."""
    if saldo <= TOL:
        return "al_dia"
    if not fecha_venc:
        return "sin_fecha"
    dias = (fecha_venc - date.today()).days
    if dias < 0:
        return "vencida"
    if dias <= DIAS_POR_VENCER:
        return "por_vencer"
    return "vigente"


def _estado_pago(factura: ContFacturaCliente, pagado: float, saldo: float) -> str:
    """El saldo manda: si está saldada → 'pagada'. Solo con saldo pendiente y
    factoring vigente → 'factorizada'."""
    if saldo <= TOL:
        return "pagada"
    fac = factura.factoring
    if fac and fac.estado == "vigente":
        return "factorizada"
    venc = factura.fecha_vencimiento
    vencida = bool(venc and venc < date.today())
    if pagado > TOL:
        return "vencida" if vencida else "parcial"
    return "vencida" if vencida else "por_cobrar"


def _recompute_factura(factura: ContFacturaCliente) -> None:
    bruto = _f(factura.monto_bruto)
    pagado = sum(_f(c.monto) for c in factura.cobranzas)
    saldo = round(max(bruto - pagado, 0.0), 2)  # nunca negativo persistido
    factura.monto_pagado = round(pagado, 2)
    factura.saldo = saldo
    factura.estado_pago = _estado_pago(factura, pagado, saldo)


def _serialize_factura(factura: ContFacturaCliente) -> dict:
    bruto = _f(factura.monto_bruto)
    pagado = _f(factura.monto_pagado)
    saldo = _f(factura.saldo)
    fac = factura.factoring
    return {
        "id": factura.id,
        "numero_factura": factura.numero_factura,
        "tipo_doc": factura.tipo_doc,
        "es_anticipo": bool(factura.es_anticipo),
        "oc_cliente_id": factura.oc_cliente_id,
        "cotizacion_id": factura.cotizacion_id,
        "despacho_id": factura.despacho_id,
        "numero_guia": factura.despacho.numero_guia if factura.despacho else None,
        "numero_expedicion": factura.despacho.numero_expedicion if factura.despacho else None,
        "guia_firmada_archivo": factura.despacho.guia_firmada_archivo if factura.despacho else None,
        "fecha_emision": factura.fecha_emision.isoformat() if factura.fecha_emision else None,
        "condicion_pago": factura.condicion_pago,
        "plazo_dias": factura.plazo_dias,
        "fecha_vencimiento": factura.fecha_vencimiento.isoformat() if factura.fecha_vencimiento else None,
        "monto_neto": _f(factura.monto_neto),
        "iva": _f(factura.iva),
        "monto_bruto": bruto,
        "monto_pagado": pagado,
        "saldo": saldo,
        # SIEMPRE recalculado al servir: el estado persistido no transiciona a 'vencida'
        # con el paso del tiempo (solo se actualiza al escribir la factura/cobranzas).
        "estado_pago": _estado_pago(factura, pagado, saldo),
        "semaforo": _semaforo(factura.fecha_vencimiento, saldo),
        "dias_vencimiento": (factura.fecha_vencimiento - date.today()).days if factura.fecha_vencimiento else None,
        "observaciones": factura.observaciones,
        "items": [
            {
                "id": it.id,
                "item_cotizacion_id": it.item_cotizacion_id,
                "despacho_item_id": it.despacho_item_id,
                "anticipo_factura_id": it.anticipo_factura_id,
                "numero_parte": it.numero_parte,
                "descripcion": it.descripcion,
                "cantidad": _f(it.cantidad),
                "precio_unit_neto": _f(it.precio_unit_neto),
                "total_neto": _f(it.total_neto),
            }
            for it in factura.items
        ],
        "cobranzas": [
            {
                "id": c.id,
                "fecha": c.fecha.isoformat() if c.fecha else None,
                "monto": _f(c.monto),
                "medio": c.medio,
                "es_factoring": _es_medio_factoring(c.medio),
                "es_adelanto": c.medio == MEDIO_ADELANTO,
                "adelanto_id": c.adelanto_id,
                "banco": c.banco,
                "numero_operacion": c.numero_operacion,
                "observaciones": c.observaciones,
            }
            for c in factura.cobranzas
        ],
        "factoring": None if not fac else {
            "id": fac.id,
            "empresa_factoring": fac.empresa_factoring,
            "id_operacion": fac.id_operacion,
            "fecha_operacion": fac.fecha_operacion.isoformat() if fac.fecha_operacion else None,
            "monto_adelantado": _f(fac.monto_adelantado),
            "costo_factoring": _f(fac.costo_factoring),
            "retencion": _f(fac.retencion),
            "banco": fac.banco,
            "estado": fac.estado,
            "fecha_liquidacion": fac.fecha_liquidacion.isoformat() if fac.fecha_liquidacion else None,
            "usuario_id": fac.usuario_id,
            "usuario_liquidacion_id": fac.usuario_liquidacion_id,
            "observaciones": fac.observaciones,
        },
    }


def _facturas_de_oc(db: Session, oc_id: int, empresa: Optional[str] = None) -> List[ContFacturaCliente]:
    q = (
        db.query(ContFacturaCliente)
        .options(
            selectinload(ContFacturaCliente.items),
            selectinload(ContFacturaCliente.cobranzas),
            selectinload(ContFacturaCliente.factoring),
            joinedload(ContFacturaCliente.despacho),
        )
        .filter(ContFacturaCliente.oc_cliente_id == oc_id)
    )
    if empresa:  # defensa en profundidad (consistente con listar_facturas / get_kpis)
        q = q.filter(ContFacturaCliente.empresa == empresa)
    return q.order_by(ContFacturaCliente.id.asc()).all()


def _mercaderia_pendiente_bruto(items_db, pmap: dict, qty_fact: dict, totales: dict) -> float:
    """Bruto (c/IVA) de la mercadería AÚN sin facturar: Σ cantidad pendiente por ítem
    × su precio de venta vigente. La cantidad pendiente = cantidad − Σ facturada
    (las tandas parciales restan lo suyo; clamp a 0 por correcciones manuales).
    Se valúa al pricing VIGENTE a propósito: lo pendiente se facturará al precio del
    momento; lo YA facturado no participa (quedó congelado en sus facturas)."""
    neto_total = _f(totales.get("subtotal_neto_clp"))
    bruto_total = _f(totales.get("total_con_iva_clp"))
    factor_iva = (bruto_total / neto_total) if neto_total > TOL else 1.19
    pend_neto = 0.0
    for it in items_db:
        cant = _f(it.cantidad)
        if cant <= 0:
            continue
        qty_pend = max(cant - _f(qty_fact.get(it.id, 0.0)), 0.0)
        if qty_pend <= 0:
            continue
        pend_neto += _f((pmap.get(it.id) or {}).get("total_venta_clp")) * (qty_pend / cant)
    return pend_neto * factor_iva


def _resumen_cobranza(
    facturas: List[ContFacturaCliente],
    por_facturar_clp: Optional[float] = None,
) -> dict:
    facturado = sum(_f(f.monto_bruto) for f in facturas)
    cobrado = sum(_f(f.monto_pagado) for f in facturas)
    cobrado_cliente = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                          if not _es_medio_factoring(c.medio))
    saldo = round(facturado - cobrado, 2)
    # estado EN VIVO (no el persistido): detecta facturas que vencieron con el tiempo
    hay_vencida = any(
        _estado_pago(f, _f(f.monto_pagado), _f(f.saldo)) == "vencida" for f in facturas
    )
    if not facturas:
        estado = "sin_factura"
    elif saldo <= TOL:
        estado = "cobrada"
    elif hay_vencida:
        estado = "vencida"
    elif cobrado > TOL:
        estado = "parcial"
    else:
        estado = "por_cobrar"
    out = {
        "n_facturas": len(facturas),
        "facturado_clp": round(facturado, 0),
        "cobrado_clp": round(cobrado, 0),
        "cobrado_cliente_clp": round(cobrado_cliente, 0),
        "por_cobrar_clp": round(max(saldo, 0), 0),
        "estado_cobranza": estado,
    }
    # "Por facturar" viene PRECALCULADO desde la mercadería físicamente sin facturar
    # (ver _mercaderia_pendiente_bruto) menos el anticipo aún por descontar. NO se
    # deriva de (total vivo − Σ brutos congelados): el total de la OC se recalcula
    # con el TC del día y las facturas quedan congeladas al emitir, así que esa
    # resta inventa "por facturar" fantasma en OCs cerradas cada vez que se mueve
    # el dólar (y el redondeo de IVA por tandas deja polvo de $1-3). Con la base
    # física, una OC totalmente facturada da 0 POR CONSTRUCCIÓN.
    if por_facturar_clp is not None:
        out["por_facturar_clp"] = round(max(_f(por_facturar_clp), 0), 0)
    return out


# ─── Adelantos de cliente (cont_adelanto) ─────────────────────────────────────
# El adelanto lo INFORMA Comercial (Cierre de Venta o Ventas), lo APRUEBA Tesorería
# (confirma la plata recibida; endpoint en tesoreria/router.py) y se APLICA aquí como
# cobranza medio='adelanto' al emitir facturas. Dos vías excluyentes por adelanto:
#   A) sin factura de anticipo → se aplica a las facturas del despacho real.
#   B) con factura de anticipo → se aplica a ESA factura; las facturas del despacho
#      real llevan línea de DESCUENTO negativa (ver _construir_factura).

def _total_bruto_venta(db: Session, cot_id: int) -> float:
    """Total con IVA de la venta (misma fuente que /ventas: pricing_service)."""
    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    _items, _pmap, totales = _precios_de_cotizacion(db, cot_id, cfg_dict)
    return _f(totales.get("total_con_iva_clp"))


def _adelantos_de_oc(db: Session, oc_id: int, con_anulados: bool = False) -> List[ContAdelanto]:
    q = db.query(ContAdelanto).filter(ContAdelanto.oc_cliente_id == oc_id)
    if not con_anulados:
        q = q.filter(ContAdelanto.estado != "anulado")
    return q.order_by(ContAdelanto.id.asc()).all()


def _adelantos_conciliados_ids(db: Session, adelanto_ids: List[int]) -> set:
    """IDs de adelantos ya conciliados con un abono del banco (enlace en Tesorería).
    El 'conciliado' se DERIVA de la existencia del enlace (igual que las cobranzas)."""
    if not adelanto_ids:
        return set()
    rows = (db.query(ConciliacionIngreso.adelanto_id)
            .filter(ConciliacionIngreso.adelanto_id.in_(adelanto_ids))
            .all())
    return {r[0] for r in rows}


def _monto_comprometido_adelanto(adel: ContAdelanto) -> float:
    """Monto que 'ocupa' el adelanto para el tope Σ adelantos ≤ total venta:
    el monto real si Tesorería ya aprobó; si no, lo informado por Comercial."""
    if adel.estado == "aprobado":
        return _f(adel.monto)
    return _f(adel.monto_esperado)


def _serialize_adelanto(adel: ContAdelanto, conciliado: bool = False) -> dict:
    monto = _f(adel.monto)
    aplicado = _f(adel.monto_aplicado)
    fa = adel.factura_anticipo
    return {
        "id": adel.id,
        "oc_cliente_id": adel.oc_cliente_id,
        "estado": adel.estado,
        "monto_esperado": _f(adel.monto_esperado),
        "pct": adel.pct,
        "monto": monto,
        "monto_aplicado": aplicado,
        "pendiente_aplicar": round(max(monto - aplicado, 0.0), 2),
        "fecha_pago": adel.fecha_pago.isoformat() if adel.fecha_pago else None,
        "banco": adel.banco,
        "numero_operacion": adel.numero_operacion,
        "conciliado_banco": conciliado,
        "factura_anticipo_id": adel.factura_anticipo_id,
        "factura_anticipo_folio": fa.numero_factura if fa else None,
        "observaciones": adel.observaciones,
        "fecha_aprobacion": adel.fecha_aprobacion.isoformat() if adel.fecha_aprobacion else None,
        "created_at": adel.created_at.isoformat() if adel.created_at else None,
    }


def _serialize_adelantos_de_oc(db: Session, oc_id: int) -> List[dict]:
    adelantos = _adelantos_de_oc(db, oc_id, con_anulados=True)
    conciliados = _adelantos_conciliados_ids(db, [a.id for a in adelantos])
    return [_serialize_adelanto(a, a.id in conciliados) for a in adelantos]


def _aplicar_adelantos_pendientes(db: Session, oc: OcCliente,
                                  factura: ContFacturaCliente,
                                  usuario_id: Optional[int] = None) -> float:
    """Aplica adelantos APROBADOS de la OC a `factura` como cobranzas medio='adelanto'.
    Llamar con la OC ya bloqueada (with_for_update) — desde crear_factura y desde la
    aprobación en Tesorería. NO hace commit (el llamador cierra la transacción).

      · factura de anticipo → SOLO adelantos ligados a ella (vía B).
      · factura normal      → adelantos sin factura de anticipo (vía A) más el
        EXCEDENTE de adelantos ligados cuyo anticipo ya quedó saldado: un adelanto
        mayor que el bruto de su anticipo es plata del cliente que debe rebajar el
        despacho real (misma regla que vía A: el resto sigue a la próxima factura).
    Cap por el SALDO actual (convive con cobranzas manuales previas); el remanente
    queda en `monto − monto_aplicado` para la siguiente factura.
    INVARIANTE: monto_aplicado == Σ cobranzas medio='adelanto' de ese adelanto.
    Devuelve el total aplicado (0.0 si no había nada que aplicar)."""
    # Mismo guard que registrar_cobranza: con factoring VIGENTE el saldo de la
    # factura es la retención del factor, NO deuda del cliente — aplicar el
    # adelanto aquí la dejaría 'pagada' y la liquidación liberaría $0. El
    # adelanto queda pendiente para la siguiente factura (o tras liquidar).
    if factura.factoring and factura.factoring.estado == "vigente":
        return 0.0
    q = (db.query(ContAdelanto)
         .filter(ContAdelanto.oc_cliente_id == oc.id,
                 ContAdelanto.estado == "aprobado")
         .with_for_update())
    if factura.es_anticipo:
        q = q.filter(ContAdelanto.factura_anticipo_id == factura.id)
    adelantos = q.order_by(ContAdelanto.id.asc()).all()
    if not adelantos:
        return 0.0
    # Para la factura normal, un adelanto ligado (vía B) solo entra por su EXCEDENTE
    # y recién cuando su factura de anticipo quedó saldada — antes de eso el saldo
    # del anticipo tiene prioridad sobre esa plata.
    anticipos_saldados: set = set()
    if not factura.es_anticipo:
        ids_anticipos = {a.factura_anticipo_id for a in adelantos if a.factura_anticipo_id}
        if ids_anticipos:
            anticipos = (db.query(ContFacturaCliente)
                         .options(selectinload(ContFacturaCliente.cobranzas))
                         .filter(ContFacturaCliente.id.in_(ids_anticipos))
                         .all())
            anticipos_saldados = {
                a.id for a in anticipos
                if round(_f(a.monto_bruto) - sum(_f(c.monto) for c in a.cobranzas), 2) <= TOL_PAGO
            }
    pagado = sum(_f(c.monto) for c in factura.cobranzas)
    saldo = round(_f(factura.monto_bruto) - pagado, 2)
    total_aplicado = 0.0
    for adel in adelantos:
        if saldo <= TOL_PAGO:
            break
        if (not factura.es_anticipo and adel.factura_anticipo_id
                and adel.factura_anticipo_id not in anticipos_saldados):
            continue
        pendiente = round(_f(adel.monto) - _f(adel.monto_aplicado), 2)
        aplicar = round(min(pendiente, saldo), 2)
        if aplicar <= TOL_PAGO:
            continue
        db.add(ContCobranza(
            factura_id=factura.id, adelanto_id=adel.id,
            fecha=adel.fecha_pago or date.today(), monto=aplicar,
            medio=MEDIO_ADELANTO, banco=adel.banco,
            numero_operacion=adel.numero_operacion,
            observaciones="Aplicación automática de adelanto aprobado por Tesorería",
            usuario_id=usuario_id,
        ))
        adel.monto_aplicado = round(_f(adel.monto_aplicado) + aplicar, 2)
        saldo = round(saldo - aplicar, 2)
        total_aplicado = round(total_aplicado + aplicar, 2)
    if total_aplicado > 0:
        db.flush()
        db.refresh(factura)
        _recompute_factura(factura)
    return total_aplicado


def _anticipos_pendientes_de_descuento(db: Session, oc_id: int, empresa: str):
    """Facturas de ANTICIPO de la OC con neto aún no descontado en facturas del despacho
    real. Devuelve [(factura_anticipo, pendiente_neto)] en orden de emisión. El pendiente
    se DERIVA: neto del anticipo − Σ(−total_neto) de las líneas de descuento que lo
    referencian (borrar una factura final restaura el pendiente solo, vía cascade)."""
    anticipos = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.oc_cliente_id == oc_id,
                ContFacturaCliente.empresa == empresa,
                ContFacturaCliente.es_anticipo == 1)
        .order_by(ContFacturaCliente.id.asc())
        .all()
    )
    if not anticipos:
        return []
    rows = (
        db.query(ContFacturaClienteItem.anticipo_factura_id, ContFacturaClienteItem.total_neto)
        .filter(ContFacturaClienteItem.anticipo_factura_id.in_([a.id for a in anticipos]))
        .all()
    )
    descontado: dict = {}
    for aid, tot in rows:
        descontado[aid] = descontado.get(aid, 0.0) + (-_f(tot))  # líneas negativas
    out = []
    for a in anticipos:
        pend = round(_f(a.monto_neto) - descontado.get(a.id, 0.0), 2)
        if pend > TOL:
            out.append((a, pend))
    return out


def _periodo_filter(created_at, periodo: Optional[str]) -> bool:
    if not periodo or not created_at:
        return True
    hoy = date.today()
    d = created_at.date() if hasattr(created_at, "date") else created_at
    if periodo == "semana":
        return (hoy - d).days <= 7
    if periodo == "mes":
        return d.year == hoy.year and d.month == hoy.month
    if periodo == "anio":
        return d.year == hoy.year
    return True


# ─── Ventas (agrupado por OC, expandible a ítem — SOLO datos de venta) ─────────
@router.get("/ventas")
def listar_ventas(
    q: Optional[str] = None,
    periodo: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista las ventas agregadas por OC de cliente, cada una con su resumen de cobranza.
    `q` busca en cliente/N° OC/N° cotización/RUT; `periodo` filtra por fecha de la
    cotización (semana | mes | anio)."""
    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    empresa = getattr(current_user, "empresa", None) or "mineria"
    ocs = db.query(OcCliente).options(joinedload(OcCliente.cotizacion)).all()
    # Prefetch en LOTE (anti N+1): ítems de todas las cotizaciones y facturas de todas
    # las OC en 2 queries totales, en vez de 2+ queries por cada OC del listado.
    cot_ids = {oc.cotizacion.id for oc in ocs if oc.cotizacion}
    items_by_cot: dict = {}
    if cot_ids:
        for it in db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all():
            items_by_cot.setdefault(it.cotizacion_id, []).append(it)
    facturas_by_oc: dict = {}
    oc_ids = [oc.id for oc in ocs]
    if oc_ids:
        fs = (db.query(ContFacturaCliente)
              .options(selectinload(ContFacturaCliente.cobranzas),
                       selectinload(ContFacturaCliente.factoring))
              .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids),
                      ContFacturaCliente.empresa == empresa)
              .order_by(ContFacturaCliente.id.asc()).all())
        for f in fs:
            facturas_by_oc.setdefault(f.oc_cliente_id, []).append(f)
    # Para "por facturar" (misma base FÍSICA que el detalle) en UNA query extra:
    # las líneas de factura traen a la vez las cantidades facturadas por ítem y
    # los descuentos de anticipo ya aplicados (líneas negativas con anticipo_factura_id).
    qty_fact_by_oc: dict = {}      # oc_id -> {item_id: qty facturada}
    desc_por_anticipo: dict = {}   # factura_anticipo_id -> neto ya descontado
    if oc_ids:
        fitem_rows = (
            db.query(ContFacturaCliente.oc_cliente_id,
                     ContFacturaClienteItem.item_cotizacion_id,
                     ContFacturaClienteItem.cantidad,
                     ContFacturaClienteItem.anticipo_factura_id,
                     ContFacturaClienteItem.total_neto)
            .join(ContFacturaCliente, ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
            .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids),
                    ContFacturaCliente.empresa == empresa)
            .all()
        )
        for oc_id_r, item_id, cant, ant_id, tot_neto in fitem_rows:
            if item_id:
                m = qty_fact_by_oc.setdefault(oc_id_r, {})
                m[item_id] = m.get(item_id, 0.0) + _f(cant)
            if ant_id:
                desc_por_anticipo[ant_id] = desc_por_anticipo.get(ant_id, 0.0) + (-_f(tot_neto))
    # Adelantos por OC en UNA query (badge en Ventas: informados / aprobados / por aplicar)
    adelantos_by_oc: dict = {}
    if oc_ids:
        for a in (db.query(ContAdelanto)
                  .filter(ContAdelanto.oc_cliente_id.in_(oc_ids),
                          ContAdelanto.estado != "anulado").all()):
            adelantos_by_oc.setdefault(a.oc_cliente_id, []).append(a)
    result = []
    for oc in ocs:
        cot = oc.cotizacion
        if not cot:
            continue
        if not _periodo_filter(cot.created_at, periodo):
            continue
        items_db, _pmap, totales = _precios_de_cotizacion(
            db, cot.id, cfg_dict, items_db=items_by_cot.get(cot.id, []))
        if not items_db:
            continue
        if q:
            ql = q.lower()
            hay = " ".join([cot.cliente or "", oc.numero_oc or "", cot.numero or "", cot.rut_cliente or ""]).lower()
            if ql not in hay:
                continue
        facs_oc = facturas_by_oc.get(oc.id, [])
        # Anticipos con neto aún no descontado, en bruto (mismo criterio del detalle)
        anticipo_pend = 0.0
        for f in facs_oc:
            if f.es_anticipo:
                neto_fa = _f(f.monto_neto)
                pend = neto_fa - desc_por_anticipo.get(f.id, 0.0)
                if pend > TOL:
                    factor = (_f(f.monto_bruto) / neto_fa) if neto_fa > TOL else 1.0
                    anticipo_pend += pend * factor
        mercaderia_pend = _mercaderia_pendiente_bruto(
            items_db, _pmap, qty_fact_by_oc.get(oc.id, {}), totales)
        resumen = _resumen_cobranza(
            facs_oc, por_facturar_clp=mercaderia_pend - anticipo_pend)
        adels = adelantos_by_oc.get(oc.id, [])
        resumen_adelantos = {
            "n": len(adels),
            "por_aprobar": sum(1 for a in adels if a.estado == "informado"),
            "aprobado_clp": round(sum(_f(a.monto) for a in adels if a.estado == "aprobado"), 0),
            "pendiente_aplicar_clp": round(sum(
                max(_f(a.monto) - _f(a.monto_aplicado), 0.0)
                for a in adels if a.estado == "aprobado"), 0),
        }
        result.append({
            "adelantos": resumen_adelantos,
            "oc_cliente_id": oc.id,
            "cotizacion_id": cot.id,
            "numero_oc": oc.numero_oc,
            "numero_cotizacion": cot.numero,
            "cliente": cot.cliente or "",
            "rut_cliente": cot.rut_cliente or "",
            "fecha_oc": oc.fecha_oc,
            "fecha_venta": cot.created_at.isoformat() if cot.created_at else None,
            "cond_pago": oc.cond_pago,
            "total_items": len(items_db),
            "total_neto_clp": round(_f(totales.get("subtotal_neto_clp")), 0),
            "iva_clp": round(_f(totales.get("iva_clp")), 0),
            "total_con_iva_clp": round(_f(totales.get("total_con_iva_clp")), 0),
            **resumen,
        })
    result.sort(key=lambda v: (v.get("fecha_venta") is None, v.get("fecha_venta") or ""), reverse=True)
    return result


@router.get("/ventas/{oc_id}")
def detalle_venta(
    oc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detalle de una venta (OC): por cada ítem, su precio de venta, las guías de despacho
    (con estado de firma y N° de expedición) y las facturas asociadas. Incluye además las
    facturas serializadas y el resumen de cobranza de la OC. 404 si la OC no existe."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    oc = db.query(OcCliente).filter(OcCliente.id == oc_id).first()
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "Venta (OC) no encontrada")
    cot = oc.cotizacion
    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    items_db, pmap, totales = _precios_de_cotizacion(db, cot.id, cfg_dict)

    # ítem -> guías (despachos no anulados, para visibilidad: incluye en preparación)
    desp_rows = (
        db.query(DespachoItem, Despacho)
        .join(Despacho, Despacho.id == DespachoItem.despacho_id)
        .filter(Despacho.oc_cliente_id == oc.id, Despacho.estado != "anulado")
        .all()
    )
    guias_por_item = {}
    for di, d in desp_rows:
        guias_por_item.setdefault(di.item_cotizacion_id, []).append({
            "despacho_item_id": di.id, "despacho_id": d.id,
            "numero_despacho": d.numero_despacho, "numero_guia": d.numero_guia,
            "estado": d.estado, "qty_despachada": _f(di.qty_despachada),
            "guia_firmada": bool(d.guia_firmada),
            "numero_expedicion": d.numero_expedicion,
            "guia_firmada_archivo": d.guia_firmada_archivo,
        })

    # ítem -> facturas
    fac_rows = (
        db.query(ContFacturaClienteItem, ContFacturaCliente)
        .join(ContFacturaCliente, ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
        .filter(ContFacturaCliente.oc_cliente_id == oc.id)
        .all()
    )
    facturas_por_item = {}
    for fi, f in fac_rows:
        facturas_por_item.setdefault(fi.item_cotizacion_id, []).append({
            "factura_id": f.id, "numero_factura": f.numero_factura,
            "fecha_emision": f.fecha_emision.isoformat() if f.fecha_emision else None,
            "plazo_dias": f.plazo_dias,
            "fecha_vencimiento": f.fecha_vencimiento.isoformat() if f.fecha_vencimiento else None,
            "estado_pago": f.estado_pago, "cantidad": _f(fi.cantidad),
        })

    items_out = []
    for it in items_db:
        ci = pmap.get(it.id, {})
        items_out.append({
            "id": it.id,
            "item_num": it.item_num,
            "numero_parte": it.numero_parte or "",
            "descripcion": it.descripcion or "",
            "marca": it.marca or "",
            "cantidad": _f(it.cantidad),
            "precio_unit_venta_clp": round(_f(ci.get("precio_venta_clp")), 0),
            "total_venta_clp": round(_f(ci.get("total_venta_clp")), 0),
            "estado_item": it.estado_item or "ingresado",
            "guias": guias_por_item.get(it.id, []),
            "facturas": facturas_por_item.get(it.id, []),
        })

    facturas = _facturas_de_oc(db, oc.id, empresa)
    # Anticipo aún NO descontado en facturas del despacho real, en BRUTO (con el IVA
    # de cada factura de anticipo: cubre facturas exentas con factor 1.0). Explica la
    # diferencia entre "mercadería sin facturar" y por_facturar_clp en la pantalla.
    anticipo_por_descontar = 0.0
    for fa, pend_neto in _anticipos_pendientes_de_descuento(db, oc.id, empresa):
        neto_fa = _f(fa.monto_neto)
        factor = (_f(fa.monto_bruto) / neto_fa) if neto_fa > TOL else 1.0
        anticipo_por_descontar += pend_neto * factor
    # Por facturar = mercadería físicamente pendiente − anticipo por descontar
    # (el anticipo ya es plata facturada contra esa mercadería futura)
    qty_fact = {}
    for fi, _fac in fac_rows:
        if fi.item_cotizacion_id:
            qty_fact[fi.item_cotizacion_id] = qty_fact.get(fi.item_cotizacion_id, 0.0) + _f(fi.cantidad)
    mercaderia_pend = _mercaderia_pendiente_bruto(items_db, pmap, qty_fact, totales)
    resumen = _resumen_cobranza(
        facturas, por_facturar_clp=mercaderia_pend - anticipo_por_descontar
    )
    resumen["anticipo_por_descontar_clp"] = round(anticipo_por_descontar, 0)
    # Cifra autoritativa para la UI (nota de la barra y sección "Por facturar"):
    # evita que el frontend la reconstruya como por_facturar + anticipo, que con
    # anticipo > mercadería (clamp) sobredeclararía la mercadería pendiente.
    resumen["mercaderia_pendiente_clp"] = round(max(mercaderia_pend, 0), 0)
    return {
        "oc_cliente_id": oc.id,
        "cotizacion_id": cot.id,
        "numero_oc": oc.numero_oc,
        "asesor_id": oc.asesor_id,
        "numero_cotizacion": cot.numero,
        "cliente": cot.cliente or "",
        "rut_cliente": cot.rut_cliente or "",
        "fecha_oc": oc.fecha_oc,
        "cond_pago": oc.cond_pago,
        "fecha_entrega": oc.fecha_entrega.isoformat() if oc.fecha_entrega else None,
        "total_neto_clp": round(_f(totales.get("subtotal_neto_clp")), 0),
        "iva_clp": round(_f(totales.get("iva_clp")), 0),
        "total_con_iva_clp": round(_f(totales.get("total_con_iva_clp")), 0),
        "items": items_out,
        "facturas": [_serialize_factura(f) for f in facturas],
        "resumen": resumen,
        "adelantos": _serialize_adelantos_de_oc(db, oc.id),
    }


@router.get("/ventas/{oc_id}/despachos-facturables")
def despachos_facturables(
    oc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Guías de despacho FACTURABLES de la OC: despachos cerrados (estado 'despachado')
    Y con la guía FIRMADA (entregada y firmada por el cliente), con saldo aún facturable.
    Alimenta el selector del modal 'Emitir factura' para que no ofrezca guías ya facturadas.
    Cada guía incluye numero_expedicion y guia_firmada_archivo (para verla antes de emitir)."""
    fact_di = _qty_facturada_por_despacho_item(db, oc_id)
    by_desp = {}
    for di, d in _despacho_items_de_oc(db, oc_id):
        facturable = _f(di.qty_despachada) - fact_di.get(di.id, 0.0)
        e = by_desp.setdefault(d.id, {
            "id": d.id, "numero_despacho": d.numero_despacho,
            "numero_guia": d.numero_guia, "numero_expedicion": d.numero_expedicion,
            "guia_firmada_archivo": d.guia_firmada_archivo,
            "items_count": 0, "facturable": 0.0,
        })
        e["items_count"] += 1
        e["facturable"] += max(facturable, 0.0)
    # TOL_QTY (unidades), no TOL (pesos): 'facturable' es una CANTIDAD; con TOL=0.5 una
    # guía con saldo fraccional ≤0.5 unidades desaparecía del selector aunque crear_factura
    # sí la aceptaba (mismo criterio que la derivación de líneas).
    return [e for e in by_desp.values() if e["facturable"] > TOL_QTY]


# ─── Adelantos de cliente: informar / listar / editar / anular ────────────────
class AdelantoInformarIn(BaseModel):
    """Adelanto INFORMADO por Comercial. Acepta la OC directa o la cotización (el Cierre
    de Venta no siempre conoce el id de la OC recién creada). Basta monto_esperado o pct
    (si solo viene pct, el monto esperado se deriva del total de la venta)."""
    oc_cliente_id: Optional[int] = None
    cotizacion_id: Optional[int] = None
    monto_esperado: Optional[float] = Field(None, gt=0)
    pct: Optional[int] = Field(None, ge=1, le=100)
    observaciones: Optional[str] = None


class AdelantoEditarIn(BaseModel):
    monto_esperado: Optional[float] = Field(None, gt=0)
    pct: Optional[int] = Field(None, ge=1, le=100)
    observaciones: Optional[str] = None


def _resolver_oc(db: Session, oc_cliente_id: Optional[int],
                 cotizacion_id: Optional[int], lock: bool = False) -> OcCliente:
    if not oc_cliente_id and not cotizacion_id:
        raise HTTPException(400, "Indica la OC (oc_cliente_id) o la cotización (cotizacion_id)")
    q = db.query(OcCliente)
    if oc_cliente_id:
        q = q.filter(OcCliente.id == oc_cliente_id)
    else:
        # La OC más reciente de esa cotización (el Cierre de Venta la acaba de crear)
        q = q.filter(OcCliente.cotizacion_id == cotizacion_id).order_by(OcCliente.id.desc())
    if lock:
        q = q.with_for_update()
    oc = q.first()
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "Venta (OC) no encontrada")
    return oc


def _validar_tope_adelantos(db: Session, oc: OcCliente, monto_nuevo: float,
                            excluir_id: Optional[int] = None) -> None:
    """Σ adelantos comprometidos (no anulados) + el nuevo ≤ total de la venta (+TOL).
    Si el total de la venta no se puede calcular (venta sin ítems), no bloquea."""
    total_venta = _total_bruto_venta(db, oc.cotizacion_id)
    if total_venta <= 0:
        return
    comprometido = sum(
        _monto_comprometido_adelanto(a)
        for a in _adelantos_de_oc(db, oc.id)
        if a.id != excluir_id
    )
    if comprometido + monto_nuevo > total_venta + TOL_PAGO:
        disponible = max(total_venta - comprometido, 0.0)
        raise HTTPException(
            400,
            f"La suma de adelantos excede el total de la venta "
            f"(disponible {disponible:,.0f} de {total_venta:,.0f})".replace(",", "."),
        )


@router.post("/ventas/adelantos")
def informar_adelanto(
    payload: AdelantoInformarIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """COMERCIAL informa que la venta tiene un adelanto (desde el Cierre de Venta o desde
    Ventas para una OC ya cerrada). Crea el registro en estado 'informado'; el pago lo
    confirma después Tesorería (aprobación) y ahí recién se aplica a facturas.
    Una OC puede tener VARIOS adelantos; el tope es el total de la venta."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    # Lock de la OC: serializa informes/aprobaciones concurrentes sobre la misma venta
    oc = _resolver_oc(db, payload.oc_cliente_id, payload.cotizacion_id, lock=True)
    if not payload.monto_esperado and not payload.pct:
        raise HTTPException(400, "Indica el monto esperado del adelanto o el porcentaje")
    monto_esperado = _f(payload.monto_esperado)
    if not monto_esperado and payload.pct:
        total_venta = _total_bruto_venta(db, oc.cotizacion_id)
        if total_venta <= 0:
            raise HTTPException(400, "No se pudo derivar el monto desde el % (venta sin total); indica el monto esperado")
        monto_esperado = round(total_venta * payload.pct / 100.0, 0)
    _validar_tope_adelantos(db, oc, monto_esperado)
    adel = ContAdelanto(
        empresa=empresa,
        oc_cliente_id=oc.id,
        estado="informado",
        monto_esperado=monto_esperado,
        pct=payload.pct,
        observaciones=payload.observaciones,
        usuario_informa_id=getattr(current_user, "id", None),
    )
    db.add(adel)
    db.commit()
    db.refresh(adel)
    return _serialize_adelanto(adel)


@router.get("/ventas/{oc_id}/adelantos")
def listar_adelantos_de_venta(
    oc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Adelantos de la venta (incluye anulados, para trazabilidad) con estado derivado:
    conciliado_banco (enlace en Tesorería) y pendiente_aplicar (monto − aplicado)."""
    oc = db.query(OcCliente).filter(OcCliente.id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Venta (OC) no encontrada")
    return _serialize_adelantos_de_oc(db, oc.id)


@router.patch("/adelantos/{adelanto_id}")
def editar_adelanto(
    adelanto_id: int,
    payload: AdelantoEditarIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edita lo INFORMADO por Comercial (monto esperado / % / observaciones). Un adelanto
    APROBADO se corrige re-aprobándolo en Tesorería (y solo si no está aplicado ni
    conciliado); uno anulado no se edita."""
    # Orden GLOBAL de locks: OC → adelanto (el mismo de crear_factura y de la
    # aprobación en Tesorería) — tomar primero el adelanto arriesgaba deadlock.
    ref = db.query(ContAdelanto).filter(ContAdelanto.id == adelanto_id).first()
    if not ref:
        raise HTTPException(404, "Adelanto no encontrado")
    oc = db.query(OcCliente).filter(OcCliente.id == ref.oc_cliente_id).with_for_update().first()
    adel = (db.query(ContAdelanto)
            .filter(ContAdelanto.id == adelanto_id)
            .populate_existing().with_for_update().first())
    if not adel:
        raise HTTPException(404, "Adelanto no encontrado")
    if adel.estado == "anulado":
        raise HTTPException(409, "El adelanto está anulado; no se puede editar")
    if adel.estado == "aprobado" and (payload.monto_esperado or payload.pct):
        raise HTTPException(409, "El adelanto ya fue aprobado por Tesorería; corrígelo re-aprobándolo allá")
    if payload.monto_esperado is not None or payload.pct is not None:
        monto_esperado = _f(payload.monto_esperado) or _f(adel.monto_esperado)
        if payload.pct and not payload.monto_esperado:
            total_venta = _total_bruto_venta(db, oc.cotizacion_id) if oc else 0.0
            if total_venta > 0:
                monto_esperado = round(total_venta * payload.pct / 100.0, 0)
        if oc:
            _validar_tope_adelantos(db, oc, monto_esperado, excluir_id=adel.id)
        adel.monto_esperado = monto_esperado
        if payload.pct is not None:
            adel.pct = payload.pct
    if payload.observaciones is not None:
        adel.observaciones = payload.observaciones
    db.commit()
    db.refresh(adel)
    conciliado = adel.id in _adelantos_conciliados_ids(db, [adel.id])
    return _serialize_adelanto(adel, conciliado)


@router.post("/adelantos/{adelanto_id}/anular")
def anular_adelanto(
    adelanto_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Anula un adelanto que no prosperó (el cliente nunca depositó, o se informó por
    error). Bloqueado si ya se aplicó a una factura (revertir esa cobranza primero) o si
    está conciliado con el banco (desconciliar en Tesorería primero)."""
    adel = (db.query(ContAdelanto)
            .filter(ContAdelanto.id == adelanto_id)
            .with_for_update().first())
    if not adel:
        raise HTTPException(404, "Adelanto no encontrado")
    if adel.estado == "anulado":
        return _serialize_adelanto(adel)
    if _f(adel.monto_aplicado) > TOL:
        raise HTTPException(409, "El adelanto ya fue aplicado a una factura; revierta esa cobranza antes de anularlo")
    if adel.id in _adelantos_conciliados_ids(db, [adel.id]):
        raise HTTPException(409, "El adelanto está conciliado con un abono del banco; desconcílielo en Tesorería primero")
    adel.estado = "anulado"
    db.commit()
    db.refresh(adel)
    return _serialize_adelanto(adel)


# ─── Facturas / Cobranzas / Factoring ─────────────────────────────────────────
class FacturaItemIn(BaseModel):
    """Una línea a facturar. cantidad/precio son opcionales: si faltan, se toman de lo
    despachado y del precio de venta calculado. despacho_item_id liga la línea a una guía."""
    item_cotizacion_id: int
    despacho_item_id: Optional[int] = None
    cantidad: Optional[float] = Field(None, gt=0)
    precio_unit_neto: Optional[float] = Field(None, gt=0)


class FacturaCreate(BaseModel):
    """Emisión de una factura. Indicar 'despacho_id' (se derivan las líneas de esa guía
    firmada) O 'items' explícitos. numero_factura es el folio SII (único por empresa).
    rut_cliente: completa el RUT del cliente si la venta no lo tiene (campo por llenar
    en el modal de emisión) — se guarda en la venta para dejarla completa."""
    oc_cliente_id: int
    despacho_id: Optional[int] = None
    numero_factura: Optional[str] = None
    tipo_doc: str = "factura"
    fecha_emision: Optional[str] = None
    condicion_pago: Optional[str] = None
    plazo_dias: Optional[int] = Field(None, ge=0)
    items: Optional[List[FacturaItemIn]] = None
    observaciones: Optional[str] = None
    rut_cliente: Optional[str] = None
    # Razón social: mismo trato que el RUT — campo por llenar en el modal cuando la
    # venta no la trae; se guarda en la venta para dejarla completa.
    razon_social_cliente: Optional[str] = None
    # Factura de ANTICIPO: respalda un adelanto ante el SII, NO exige guía firmada.
    # monto_neto_anticipo es el neto de la factura (el IVA se calcula); adelanto_ids
    # liga los adelantos que respalda (vía B: su plata cae aquí como cobranza automática
    # y las facturas del despacho real llevan el descuento).
    es_anticipo: bool = False
    monto_neto_anticipo: Optional[float] = Field(None, gt=0)
    adelanto_ids: Optional[List[int]] = None
    descripcion_anticipo: Optional[str] = None


class FacturaPreview(BaseModel):
    """Datos mínimos para PREVISUALIZAR una factura antes de emitir (no persiste)."""
    oc_cliente_id: int
    despacho_id: Optional[int] = None
    items: Optional[List[FacturaItemIn]] = None
    rut_cliente: Optional[str] = None
    razon_social_cliente: Optional[str] = None
    es_anticipo: bool = False
    monto_neto_anticipo: Optional[float] = Field(None, gt=0)
    adelanto_ids: Optional[List[int]] = None
    descripcion_anticipo: Optional[str] = None


@router.get("/facturas")
def listar_facturas(
    estado: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista las facturas (filtrables por estado_pago y por texto `q`) y la ANTIGÜEDAD de
    cartera: saldo por cobrar en buckets 0-30 / 31-60 / 61-90 / 91+ días desde la emisión."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    facturas = (
        db.query(ContFacturaCliente)
        .options(*_FACTURA_EAGER)
        .filter(ContFacturaCliente.empresa == empresa)
        .order_by(ContFacturaCliente.id.desc())
        .all()
    )
    out = []
    aging = {"0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "91_mas": 0.0}
    hoy = date.today()
    for f in facturas:
        oc = f.oc_cliente
        cot = oc.cotizacion if oc else None
        d = _serialize_factura(f)
        d["cliente"] = (cot.cliente if cot else None) or ""
        d["rut_cliente"] = (cot.rut_cliente if cot else None) or ""
        d["numero_oc"] = oc.numero_oc if oc else None
        # filtra por el estado EN VIVO del serializador (no el persistido, que puede
        # estar obsoleto para 'vencida')
        if estado and d["estado_pago"] != estado:
            continue
        if q:
            ql = q.lower()
            hay = " ".join([d["cliente"], d.get("numero_factura") or "", d.get("numero_oc") or "", d["rut_cliente"]]).lower()
            if ql not in hay:
                continue
        saldo = d["saldo"]
        if saldo > TOL and f.fecha_emision:
            dias = (hoy - f.fecha_emision).days
            if dias <= 30:
                aging["0_30"] += saldo
            elif dias <= 60:
                aging["31_60"] += saldo
            elif dias <= 90:
                aging["61_90"] += saldo
            else:
                aging["91_mas"] += saldo
        out.append(d)
    return {"facturas": out, "antiguedad": {k: round(v, 0) for k, v in aging.items()}}


def _precios_congelados_guia(db: Session, despacho_id: Optional[int]):
    """Precios unitarios CONGELADOS en la guía electrónica (52) ya emitida de este despacho.
    La factura DEBE reflejar lo enviado al SII en la guía, no un recálculo con la config del
    cotizador de HOY: si cambió el dólar entre el despacho y la facturación, recalcular
    descuadraría la factura contra la guía ya emitida.

    Devuelve (por_despacho_item, por_parte):
      - por_despacho_item {despacho_item_id: precio}: match 1:1 EXACTO por línea (clave
        primaria, vía externalId del detalle). Robusto ante n° de parte repetido.
      - por_parte {numero_parte: precio}: fallback para guías antiguas sin externalId
        (descarta n° de parte duplicados, que no se pueden mapear con certeza).
    Ambos vacíos si no hay guía electrónica emitida (→ la factura recalcula: guías manuales)."""
    if not despacho_id:
        return {}, {}
    try:
        from wasabil_dte.models import WasabilDte, STATUS_EMITIDO
    except Exception:
        return {}, {}
    try:
        dte = (
            db.query(WasabilDte)
            .filter(WasabilDte.despacho_id == despacho_id,
                    WasabilDte.status_id == STATUS_EMITIDO)
            .first()
        )
    except Exception:
        return {}, {}  # tabla ausente / BD sin migrar: cae al recálculo (no rompe la emisión)
    if not dte or not dte.payload_json:
        return {}, {}
    try:
        detalles = json.loads(dte.payload_json).get("details", [])
    except (ValueError, TypeError):
        return {}, {}
    por_di, por_parte, vistos_parte = {}, {}, set()
    for d in detalles:
        if d.get("price") is None:
            continue
        ext = d.get("externalId")
        if ext is not None:
            try:
                por_di[int(ext)] = d["price"]
            except (ValueError, TypeError):
                pass
        code = (d.get("code") or "").strip()
        if code:
            if code in vistos_parte:
                por_parte.pop(code, None)   # duplicado → no confiable por n° de parte
            else:
                vistos_parte.add(code)
                por_parte[code] = d["price"]
    return por_di, por_parte


def _construir_factura(db: Session, payload, oc: OcCliente, cot, empresa: str) -> dict:
    """Deriva y valida las líneas + montos de una factura SIN persistir ni tomar locks.
    Fuente ÚNICA de verdad: el preview y crear_factura la usan (lo que se previsualiza es
    EXACTAMENTE lo que se emite). Las reglas de negocio se acumulan en `problemas`
    (bloqueantes); no lanza excepciones (salvo estructurales ya cubiertas por el llamador).
    Devuelve validadas=[(it, ln, cantidad, precio2, total)], líneas de display, receptor,
    neto/iva/bruto, problemas (bloqueantes) y advertencias (informativas)."""
    problemas: List[str] = []
    advertencias: List[str] = []

    # ── RUT del receptor: el SII lo exige. payload.rut_cliente permite completarlo
    #    (campo por llenar en el modal) si la venta no lo trae. ──
    rut_payload = _rut_saneado(getattr(payload, "rut_cliente", None))
    rut_venta = _rut_saneado(cot.rut_cliente)
    rut_norm = rut_payload or rut_venta
    rut_mostrar = (getattr(payload, "rut_cliente", None) or cot.rut_cliente or "").strip()
    if not rut_norm:
        problemas.append("Falta el RUT del cliente: complétalo para emitir la factura")
    elif not _rut_valido(rut_norm):
        problemas.append(f"El RUT '{rut_mostrar}' no es válido (revisa el dígito verificador)")
    elif (rut_payload and rut_venta and _rut_valido(rut_venta)
          and _rut_canonico(rut_payload) != _rut_canonico(rut_venta)):
        # Ambos RUT válidos y REALMENTE distintos (comparación canónica: con/sin guión
        # es el mismo RUT): se factura con el ingresado, avisando — la venta conserva
        # su RUT (corregirlo allá si el de la venta está malo).
        advertencias.append(
            f"El RUT ingresado ({rut_mostrar}) difiere del registrado en la venta "
            f"({cot.rut_cliente}): se facturará con el ingresado"
        )

    # ── Razón social del receptor: mismo trato que el RUT (la factura la exige).
    #    Si la venta no la trae, el modal la pide (campo por llenar) y crear_factura
    #    la guarda en la venta para dejarla completa. ──
    razon_social = ((getattr(payload, "razon_social_cliente", None) or "").strip()
                    or (cot.cliente or "").strip())
    if not razon_social:
        problemas.append("Falta la razón social del cliente en la venta: complétala para emitir")

    # ── Referencias del documento (avisos, no bloqueantes) ──
    if not (oc.numero_oc or "").strip():
        advertencias.append("La OC no tiene número: la factura quedará sin referencia de OC "
                            "(complétalo en la venta antes de emitir si el cliente lo exige)")

    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    items_db, pmap, _ = _precios_de_cotizacion(db, cot.id, cfg_dict)
    items_by_id = {i.id: i for i in items_db}
    # Precios congelados de la guía electrónica ya emitida (si la hay): la factura debe
    # cuadrar con la guía 52 enviada al SII, no recalcular con la config de hoy.
    congel_di, congel_parte = _precios_congelados_guia(db, payload.despacho_id)
    hay_congelados = bool(congel_di or congel_parte)
    n_congeladas = 0

    # Despachos DESPACHADOS de la OC (lo que se puede facturar)
    desp_items = _despacho_items_de_oc(db, oc.id)
    di_by_id = {di.id: di for di, _d in desp_items}
    desp_qty_item = _qty_despachada_por_item(db, oc.id)
    fact_qty_item = _qty_facturada_por_item(db, oc.id)
    fact_qty_di = _qty_facturada_por_despacho_item(db, oc.id)

    # La guía (despacho_id) se valida SIEMPRE: la factura no puede quedar ligada a una
    # guía ajena o sin firmar.
    desp = None
    guia_ok = False
    if payload.despacho_id:
        desp = db.query(Despacho).filter(
            Despacho.id == payload.despacho_id, Despacho.oc_cliente_id == oc.id
        ).first()
        if not desp:
            problemas.append("Despacho no encontrado para esta OC")
        elif desp.estado != "despachado" or not desp.guia_firmada:
            problemas.append("Solo se puede facturar una guía de despacho FIRMADA "
                             "(entregada y firmada por el cliente)")
        else:
            guia_ok = True
            if not (desp.numero_guia or "").strip():
                advertencias.append("El despacho no tiene N° de guía registrado: la factura "
                                    "quedará sin referencia de guía (complétalo en Despachos)")

    # Determinar líneas a facturar
    lineas: List[FacturaItemIn] = []
    if payload.items:
        lineas = payload.items
    elif guia_ok:
        # Derivar líneas acotando por guía (despacho_item) Y por ítem físico
        usado_deriv = {}
        for di in desp.items:
            disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0)
            disp_item = (
                desp_qty_item.get(di.item_cotizacion_id, 0.0)
                - fact_qty_item.get(di.item_cotizacion_id, 0.0)
                - usado_deriv.get(di.item_cotizacion_id, 0.0)
            )
            disponible = min(disp_di, disp_item)
            if disponible > TOL_QTY:
                lineas.append(FacturaItemIn(
                    item_cotizacion_id=di.item_cotizacion_id,
                    despacho_item_id=di.id, cantidad=round(disponible, 4),
                ))
                usado_deriv[di.item_cotizacion_id] = usado_deriv.get(di.item_cotizacion_id, 0.0) + disponible
        if not lineas:
            problemas.append("El despacho ya fue facturado por completo")
    if not lineas and not problemas:
        problemas.append("Debe indicar ítems o un despacho a facturar")

    # Validación por línea con acumuladores unificados (guía + ítem) en este request
    usado_di, usado_item = {}, {}
    validadas, display = [], []
    for ln in lineas:
        it = items_by_id.get(ln.item_cotizacion_id)
        if not it:
            problemas.append(f"Ítem {ln.item_cotizacion_id} no pertenece a esta OC"); continue
        cantidad = ln.cantidad if ln.cantidad is not None else _f(it.cantidad)
        if cantidad <= 0:
            problemas.append(f"Cantidad inválida para {it.numero_parte}"); continue
        # Tope a nivel de ÍTEM (lo despachado y aún no facturado) — en TODAS las rutas
        despachado_item = desp_qty_item.get(ln.item_cotizacion_id, 0.0)
        if despachado_item <= 0:
            problemas.append(f"{it.numero_parte} no ha sido despachado; no se puede facturar"); continue
        disponible = (
            despachado_item
            - fact_qty_item.get(ln.item_cotizacion_id, 0.0)
            - usado_item.get(ln.item_cotizacion_id, 0.0)
        )
        # Tope adicional a nivel de GUÍA si se indicó despacho_item_id
        if ln.despacho_item_id is not None:
            di = di_by_id.get(ln.despacho_item_id)
            if not di or di.item_cotizacion_id != ln.item_cotizacion_id:
                problemas.append(f"Guía/despacho inválido para {it.numero_parte}"); continue
            disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0) - usado_di.get(di.id, 0.0)
            disponible = min(disponible, disp_di)
        if cantidad > disponible + TOL_QTY:
            problemas.append(f"{it.numero_parte}: cantidad excede lo despachado/no facturado "
                             f"(disp {max(disponible,0):.0f})"); continue

        ci = pmap.get(ln.item_cotizacion_id, {})
        parte = (it.numero_parte or "").strip()
        if ln.precio_unit_neto is not None:
            precio = ln.precio_unit_neto  # precio explícito del payload manda
        elif ln.despacho_item_id is not None and ln.despacho_item_id in congel_di:
            precio = _f(congel_di[ln.despacho_item_id]); n_congeladas += 1  # match 1:1
        elif parte and parte in congel_parte:
            precio = _f(congel_parte[parte]); n_congeladas += 1  # fallback guía antigua
        else:
            precio = _f(ci.get("precio_venta_clp"))  # recálculo (guía manual / sin match)
        if precio <= 0:  # antes '< 0': una línea en $0 se auto-marcaba 'pagada'
            problemas.append(f"{it.numero_parte}: sin precio de venta, no se puede facturar en $0"); continue

        usado_item[ln.item_cotizacion_id] = usado_item.get(ln.item_cotizacion_id, 0.0) + cantidad
        if ln.despacho_item_id is not None:
            usado_di[ln.despacho_item_id] = usado_di.get(ln.despacho_item_id, 0.0) + cantidad
        p2 = _precio2(precio)
        total = _total_linea(precio, cantidad)  # redondea precio a 2 dec ANTES de ×qty (== guía)
        validadas.append((it, ln, cantidad, p2, total))
        display.append({
            "item_cotizacion_id": it.id, "numero_parte": it.numero_parte,
            "descripcion": it.descripcion, "cantidad": cantidad,
            "precio_unit_neto": p2, "total_neto": total,
        })

    # precio_de_guia: True SOLO si TODAS las líneas tomaron el precio congelado (banner
    # honesto). Si hay guía emitida pero alguna línea se recalculó (n° de parte sin match),
    # se avisa: esa parte podría no cuadrar con la guía ya enviada al SII.
    todas_congeladas = bool(validadas) and n_congeladas == len(validadas)
    if hay_congelados and validadas and n_congeladas < len(validadas):
        advertencias.append(
            "Algunas líneas no se pudieron cuadrar con el precio de la guía electrónica "
            "ya emitida y se recalcularon: revisa que el monto coincida con la guía 52."
        )
    neto_items = float(sum(t for *_r, t in validadas))

    # ── Descuento AUTOMÁTICO por anticipos facturados (facturas es_anticipo=1 de la
    #    OC con neto aún no descontado). Cada descuento es una línea NEGATIVA que
    #    referencia el folio de la factura de anticipo; así Σ brutos de las facturas
    #    de la OC nunca supera el total de la venta y el cliente no paga dos veces. ──
    descuentos = []
    if validadas and not problemas:
        restante = neto_items
        for fa, pend in _anticipos_pendientes_de_descuento(db, oc.id, empresa):
            if restante <= TOL:
                break
            d = round(min(pend, restante), 2)
            if d <= TOL:
                continue
            folio = fa.numero_factura or f"#{fa.id}"
            descuentos.append({"anticipo_factura_id": fa.id, "folio": folio, "monto_neto": d})
            display.append({
                "item_cotizacion_id": None, "numero_parte": "DESCUENTO",
                "descripcion": f"Descuento anticipo Factura N° {folio}",
                "cantidad": 1, "precio_unit_neto": -d, "total_neto": -d,
                "anticipo_factura_id": fa.id,
            })
            restante = round(restante - d, 2)
    neto = round(neto_items - sum(d["monto_neto"] for d in descuentos), 2)
    if descuentos and neto <= TOL:
        advertencias.append(
            "El descuento por anticipo deja esta factura en $0 (el anticipo ya cubría "
            "todo lo despachado): verifica que sea lo esperado antes de emitir."
        )
    iva = _iva_clp(neto) if neto else 0.0

    # Tope Σ brutos ≤ total venta cuando alguna línea trae PRECIO EXPLÍCITO del
    # payload: los precios derivados (guía congelada / pricing) respetan el
    # invariante por construcción, pero un precio del payload podría inflar la
    # factura por sobre la venta (espejo del tope de _construir_factura_anticipo).
    if validadas and not problemas and any(
            ln.precio_unit_neto is not None for _it, ln, *_r in validadas):
        total_venta = _total_bruto_venta(db, cot.id)
        if total_venta > 0:
            facturado = sum(_f(f.monto_bruto) for f in _facturas_de_oc(db, oc.id, empresa))
            if facturado + neto + iva > total_venta + TOL_PAGO:
                disponible = max(total_venta - facturado, 0.0)
                problemas.append(
                    f"La factura excede el total de la venta "
                    f"(disponible bruto {disponible:,.0f} de {total_venta:,.0f})".replace(",", ".")
                )

    # Giro y dirección del receptor (solo lectura, para que el kit de emisión esté
    # completo): del maestro de Clientes por RUT, con la dirección de la venta como
    # respaldo. Si no hay giro, el preview lo muestra vacío (se completa en Clientes).
    # Match CANÓNICO en SQL (sin puntos/espacios/guión): cruza cualquier formato de
    # RUT sin cargar la tabla completa.
    giro, direccion = None, (cot.direccion_cliente or "").strip() or None
    if rut_norm:
        rut_sql = func.upper(func.replace(func.replace(func.replace(
            Cliente.rut, ".", ""), " ", ""), "-", ""))
        cli = db.query(Cliente).filter(rut_sql == _rut_canonico(rut_norm)).first()
        if cli:
            giro = (cli.giro or "").strip() or None
            direccion = (cli.direccion or "").strip() or direccion

    return {
        "desp": desp, "validadas": validadas, "lineas": display,
        "receptor": {
            "rut": rut_mostrar, "rut_normalizado": rut_norm,
            "razon_social": razon_social,
            "rut_en_venta": bool(_rut_saneado(cot.rut_cliente)),
            "giro": giro, "direccion": direccion,
        },
        "neto": neto, "iva": iva, "bruto": neto + iva,
        "descuentos": descuentos,  # [(factura de anticipo, folio, neto a descontar)]
        "precio_de_guia": todas_congeladas,  # todas las líneas cuadran con la guía 52
        "advertencias": advertencias,
        "problemas": problemas,
    }


def _construir_factura_anticipo(db: Session, payload, oc: OcCliente, cot, empresa: str) -> dict:
    """Deriva y valida una factura de ANTICIPO (misma forma de salida que
    _construir_factura, para reutilizar preview/emisión/serialización). EXCEPCIÓN a la
    regla rectora: NO exige guía de despacho firmada — respalda un adelanto del cliente
    ante el SII. Una sola línea 'Anticipo OC …' con el neto indicado; sin ítems físicos
    (no toca los topes por ítem/guía). Tope: Σ brutos de las facturas de la OC (anticipos
    incluidos) ≤ total de la venta."""
    problemas: List[str] = []
    advertencias: List[str] = []

    rut_norm = _rut_saneado(getattr(payload, "rut_cliente", None)) or _rut_saneado(cot.rut_cliente)
    rut_mostrar = (getattr(payload, "rut_cliente", None) or cot.rut_cliente or "").strip()
    if not rut_norm:
        problemas.append("Falta el RUT del cliente: complétalo para emitir la factura")
    elif not _rut_valido(rut_norm):
        problemas.append(f"El RUT '{rut_mostrar}' no es válido (revisa el dígito verificador)")
    # Mismo trato que la factura normal (_construir_factura): la única excepción
    # de la vía B es la guía firmada, no la completitud del receptor. Si la venta
    # no trae la razón social, el modal la pide y crear_factura la guarda.
    razon_social = ((getattr(payload, "razon_social_cliente", None) or "").strip()
                    or (cot.cliente or "").strip())
    if not razon_social:
        problemas.append("Falta la razón social del cliente en la venta: complétala para emitir la factura")

    neto = round(_f(getattr(payload, "monto_neto_anticipo", None)), 2)
    if neto <= 0:
        problemas.append("Indica el monto NETO del anticipo (mayor a 0)")

    # Tope: lo ya facturado (todas las facturas de la OC: anticipos brutos + finales ya
    # con descuento) + este anticipo no puede superar el total de la venta.
    iva = _iva_clp(neto) if neto > 0 else 0.0
    bruto = round(neto + iva, 2)
    total_venta = _total_bruto_venta(db, cot.id)
    if neto > 0 and total_venta > 0:
        facturado = sum(_f(f.monto_bruto) for f in _facturas_de_oc(db, oc.id, empresa))
        if facturado + bruto > total_venta + TOL_PAGO:
            disponible = max(total_venta - facturado, 0.0)
            problemas.append(
                f"El anticipo excede lo aún no facturado de la venta "
                f"(disponible bruto {disponible:,.0f} de {total_venta:,.0f})".replace(",", ".")
            )

    # Adelantos que respalda (vía B): deben ser de esta OC, no anulados, sin factura de
    # anticipo previa y sin aplicaciones (cambiar de vía solo con monto_aplicado == 0).
    adelanto_ids = list(getattr(payload, "adelanto_ids", None) or [])
    adelantos = []
    if adelanto_ids:
        adelantos = (db.query(ContAdelanto)
                     .filter(ContAdelanto.id.in_(adelanto_ids)).all())
        found = {a.id for a in adelantos}
        for aid in adelanto_ids:
            if aid not in found:
                problemas.append(f"Adelanto {aid} no encontrado")
        for a in adelantos:
            if a.oc_cliente_id != oc.id:
                problemas.append(f"El adelanto {a.id} no pertenece a esta OC")
            elif a.estado == "anulado":
                problemas.append(f"El adelanto {a.id} está anulado")
            elif a.factura_anticipo_id:
                problemas.append(f"El adelanto {a.id} ya está respaldado por otra factura de anticipo")
            elif _f(a.monto_aplicado) > TOL:
                problemas.append(f"El adelanto {a.id} ya fue aplicado a una factura; revierta esa cobranza primero")

    descripcion = (getattr(payload, "descripcion_anticipo", None)
                   or f"Anticipo OC {oc.numero_oc or oc.id}").strip()
    display = [{
        "item_cotizacion_id": None, "numero_parte": "ANTICIPO",
        "descripcion": descripcion, "cantidad": 1,
        "precio_unit_neto": neto, "total_neto": neto,
    }]
    return {
        "desp": None, "validadas": [], "lineas": display,
        "receptor": {
            "rut": rut_mostrar, "rut_normalizado": rut_norm,
            "razon_social": razon_social,
            "rut_en_venta": bool(_rut_saneado(cot.rut_cliente)),
        },
        "neto": neto, "iva": iva, "bruto": bruto,
        "descuentos": [],
        "descripcion_anticipo": descripcion,
        "adelantos": adelantos,
        "precio_de_guia": False,
        "advertencias": advertencias,
        "problemas": problemas,
    }


@router.post("/facturas/preview")
def preview_factura(
    payload: FacturaPreview,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Previsualiza la factura ANTES de emitir: líneas, montos (neto/IVA half-up/total),
    receptor y problemas bloqueantes. NO persiste ni congela nada. `puede_emitir` es True
    solo si no hay problemas de datos (el folio se valida aparte, al emitir)."""
    oc = db.query(OcCliente).filter(OcCliente.id == payload.oc_cliente_id).first()
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "OC Cliente no encontrada")
    empresa = getattr(current_user, "empresa", None) or "mineria"
    if payload.es_anticipo:
        datos = _construir_factura_anticipo(db, payload, oc, oc.cotizacion, empresa)
    else:
        datos = _construir_factura(db, payload, oc, oc.cotizacion, empresa)
    return {
        "puede_emitir": not datos["problemas"],
        "problemas": datos["problemas"],
        "advertencias": datos["advertencias"],
        "receptor": datos["receptor"],
        "lineas": datos["lineas"],
        "totales": {"neto": datos["neto"], "iva": datos["iva"], "bruto": datos["bruto"]},
        "precio_de_guia": datos["precio_de_guia"],
        "es_anticipo": bool(payload.es_anticipo),
        "descuentos": datos.get("descuentos", []),
    }


@router.post("/facturas")
def crear_factura(
    payload: FacturaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE una factura a cliente. Dos modos: `payload.despacho_id` (deriva las líneas SOLO
    de una guía despachada y FIRMADA) o `payload.items` explícitos. Reglas: folio único y
    obligatorio (tipo 'factura'); RUT del cliente válido; no facturar más de lo despachado-
    y-no-facturado (doble tope por ÍTEM y por GUÍA); congela montos (neto, IVA 19%, bruto)."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    # Lock de fila de la OC: serializa la facturación concurrente de la misma venta
    # (evita que dos requests lean el mismo "ya facturado" y sobre-facturen).
    oc = (
        db.query(OcCliente)
        .filter(OcCliente.id == payload.oc_cliente_id)
        .with_for_update()
        .first()
    )
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "OC Cliente no encontrada")
    cot = oc.cotizacion

    # Folio SII: obligatorio para tipo 'factura' (hoy se digita a mano del DTE emitido);
    # único por empresa.
    tipo_doc = payload.tipo_doc or "factura"
    if payload.es_anticipo and tipo_doc != "factura":
        raise HTTPException(400, "La factura de anticipo debe ser tipo 'factura' (no boleta)")
    folio = (payload.numero_factura or "").strip()
    if tipo_doc == "factura" and not folio:
        raise HTTPException(400, "Ingresa el folio SII de la factura (o cámbialo a boleta)")
    if folio:
        dup = db.query(ContFacturaCliente).filter(
            ContFacturaCliente.empresa == empresa,
            ContFacturaCliente.numero_factura == folio,
        ).first()
        if dup:
            raise HTTPException(409, f"El folio {folio} ya existe para esta empresa")

    if payload.es_anticipo:
        datos = _construir_factura_anticipo(db, payload, oc, cot, empresa)
    else:
        datos = _construir_factura(db, payload, oc, cot, empresa)
    if datos["problemas"]:
        raise HTTPException(409, " · ".join(datos["problemas"]))

    # Completar el RUT en la venta si venía vacío O INVÁLIDO (el campo por llenar del
    # modal deja la venta lista; un RUT guardado con dígito verificador malo se corrige
    # con el validado que se acaba de usar para emitir)
    if _rut_saneado(payload.rut_cliente) and (
        not _rut_saneado(cot.rut_cliente) or not _rut_valido(cot.rut_cliente)
    ):
        cot.rut_cliente = payload.rut_cliente.strip()
    # Razón social: mismo trato — el campo por llenar del modal completa la venta
    # (solo si venía vacía; una razón social ya guardada no se pisa desde aquí)
    razon_payload = (getattr(payload, "razon_social_cliente", None) or "").strip()
    if razon_payload and not (cot.cliente or "").strip():
        cot.cliente = razon_payload

    fecha_emision = _parse_date(payload.fecha_emision) or _hoy_chile()
    # `is not None`: plazo 0 días (contado) también debe generar vencimiento (= emisión)
    fecha_venc = (fecha_emision + timedelta(days=int(payload.plazo_dias))
                  if payload.plazo_dias is not None else None)

    factura = ContFacturaCliente(
        empresa=empresa,
        oc_cliente_id=oc.id, cotizacion_id=cot.id,
        despacho_id=None if payload.es_anticipo else payload.despacho_id,
        es_anticipo=1 if payload.es_anticipo else 0,
        numero_factura=folio or None, tipo_doc=tipo_doc,
        fecha_emision=fecha_emision, condicion_pago=payload.condicion_pago,
        plazo_dias=payload.plazo_dias, fecha_vencimiento=fecha_venc,
        observaciones=payload.observaciones, usuario_id=getattr(current_user, "id", None),
    )
    try:
        db.add(factura)
        db.flush()
        if payload.es_anticipo:
            # Línea única "Anticipo OC …" (sin ítem físico ni guía)
            db.add(ContFacturaClienteItem(
                factura_id=factura.id, numero_parte="ANTICIPO",
                descripcion=datos["descripcion_anticipo"],
                cantidad=1, precio_unit_neto=datos["neto"], total_neto=datos["neto"],
            ))
            # Vínculo adelanto → factura de anticipo (vía B). Re-validado BAJO LOCK del
            # adelanto: anular/editar solo bloquean la fila del adelanto (no la OC), así
            # que el chequeo del preview pudo quedar obsoleto.
            if payload.adelanto_ids:
                adelantos_link = (db.query(ContAdelanto)
                                  .filter(ContAdelanto.id.in_(payload.adelanto_ids))
                                  .with_for_update().all())
                if len(adelantos_link) != len(set(payload.adelanto_ids)):
                    raise HTTPException(409, "Alguno de los adelantos indicados ya no existe")
                for a in adelantos_link:
                    if (a.oc_cliente_id != oc.id or a.estado == "anulado"
                            or a.factura_anticipo_id or _f(a.monto_aplicado) > TOL):
                        raise HTTPException(
                            409, f"El adelanto {a.id} cambió de estado; recarga e intenta de nuevo")
                    a.factura_anticipo_id = factura.id
        else:
            for it, ln, cantidad, p2, total in datos["validadas"]:
                db.add(ContFacturaClienteItem(
                    factura_id=factura.id, item_cotizacion_id=ln.item_cotizacion_id,
                    despacho_item_id=ln.despacho_item_id,
                    numero_parte=it.numero_parte, descripcion=it.descripcion,
                    cantidad=cantidad, precio_unit_neto=p2, total_neto=total,
                ))
            # Líneas de DESCUENTO por anticipo facturado (negativas, referencian a la
            # factura de anticipo — el pendiente de descontar se deriva de estas líneas)
            for dsc in datos.get("descuentos", []):
                db.add(ContFacturaClienteItem(
                    factura_id=factura.id, anticipo_factura_id=dsc["anticipo_factura_id"],
                    numero_parte="DESCUENTO",
                    descripcion=f"Descuento anticipo Factura N° {dsc['folio']}",
                    cantidad=1, precio_unit_neto=-dsc["monto_neto"],
                    total_neto=-dsc["monto_neto"],
                ))
        factura.monto_neto = datos["neto"]
        factura.iva = datos["iva"]
        factura.monto_bruto = datos["bruto"]
        db.flush()
        _recompute_factura(factura)
        # Aplicación automática de adelantos APROBADOS (cobranza medio='adelanto'):
        # a la factura de anticipo le caen SUS adelantos (vía B); a la normal, los que
        # no tienen factura de anticipo (vía A). Bajo el lock de la OC ya tomado.
        _aplicar_adelantos_pendientes(db, oc, factura,
                                      usuario_id=getattr(current_user, "id", None))
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as e:
        db.rollback()
        if "uq_cont_factura_empresa_folio" in str(getattr(e, "orig", e)):
            raise HTTPException(409, "Folio de factura duplicado para esta empresa")
        raise HTTPException(409, "No se pudo guardar la factura (conflicto de integridad)")
    db.refresh(factura)
    return _serialize_factura(factura)


class CobranzaIn(BaseModel):
    """Un pago real del cliente. medio: transferencia|cheque|efectivo (las cobranzas de
    factoring NO van por aquí; se generan desde el panel de factoring)."""
    fecha: Optional[str] = None
    monto: float = Field(..., gt=0)
    medio: str = "transferencia"
    banco: Optional[str] = None
    numero_operacion: Optional[str] = None
    observaciones: Optional[str] = None


@router.post("/facturas/{factura_id}/cobranzas")
def registrar_cobranza(
    factura_id: int,
    payload: CobranzaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Registra un pago real del cliente. Bloquea la factura (lock de fila), rechaza medios de
    factoring y el SOBRE-PAGO (recalcula el saldo desde las cobranzas reales). Si la factura
    tiene factoring vigente, exige liquidarlo antes. Recalcula saldo y estado."""
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
        .with_for_update()
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    if payload.monto <= 0:
        raise HTTPException(400, "El monto debe ser mayor a 0")
    if _es_medio_factoring(payload.medio):
        raise HTTPException(400, "Las cobranzas de factoring se gestionan desde el panel de factoring")
    if (payload.medio or "") == MEDIO_ADELANTO:
        raise HTTPException(400, "Las cobranzas de adelanto las genera el sistema al aplicar un adelanto aprobado por Tesorería")
    if factura.factoring and factura.factoring.estado == "vigente":
        raise HTTPException(409, "La factura tiene un factoring vigente; liquide el factoring antes de registrar cobranzas")
    # Recalcular el saldo desde las cobranzas reales dentro de la transacción (no del campo cacheado)
    pagado_actual = sum(_f(c.monto) for c in factura.cobranzas)
    saldo_actual = round(_f(factura.monto_bruto) - pagado_actual, 2)
    if payload.monto > saldo_actual + TOL_PAGO:
        raise HTTPException(400, f"El monto excede el saldo pendiente ({max(saldo_actual, 0):.0f})")
    db.add(ContCobranza(
        factura_id=factura.id, fecha=_parse_date(payload.fecha) or date.today(),
        monto=payload.monto, medio=payload.medio or "transferencia",
        banco=payload.banco, numero_operacion=payload.numero_operacion,
        observaciones=payload.observaciones, usuario_id=getattr(current_user, "id", None),
    ))
    db.flush()
    db.refresh(factura)
    _recompute_factura(factura)
    db.commit()
    db.refresh(factura)
    return _serialize_factura(factura)


@router.delete("/facturas/{factura_id}/cobranzas/{cobranza_id}")
def eliminar_cobranza(
    factura_id: int,
    cobranza_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revierte un pago real (no de factoring; esos se revierten desde el panel de factoring)
    y recalcula el saldo/estado de la factura."""
    # Orden GLOBAL de locks: OC → factura → adelanto (el mismo de aprobar_adelanto y
    # eliminar_factura). Serializar por la OC primero deja la protección contra
    # deadlocks en el ORDENAMIENTO y no en invariantes de negocio.
    ref = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    if not ref:
        raise HTTPException(404, "Factura no encontrada")
    if ref.oc_cliente_id:
        db.query(OcCliente).filter(OcCliente.id == ref.oc_cliente_id).with_for_update().first()
    # Bloquea la factura (lock de fila) ANTES de borrar, igual que registrar_cobranza,
    # para que el recálculo de saldo no compita con un pago concurrente.
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    c = db.query(ContCobranza).filter(
        ContCobranza.id == cobranza_id, ContCobranza.factura_id == factura_id
    ).first()
    if not c:
        raise HTTPException(404, "Cobranza no encontrada")
    if _es_medio_factoring(c.medio):
        raise HTTPException(400, "Las cobranzas de factoring se revierten desde el panel de factoring")
    # Espejo del candado de registrar_cobranza: con la factura CEDIDA la asignación
    # de pagos está congelada (la retención del factor se calculó sobre ella y
    # Tesorería la publica en flujo-caja) — revertir aquí la desfasaría.
    if factura.factoring and factura.factoring.estado == "vigente":
        raise HTTPException(409, "La factura tiene un factoring vigente; liquide el factoring antes de revertir cobranzas")
    conciliada = (db.query(ConciliacionIngreso)
                  .filter(ConciliacionIngreso.cobranza_id == c.id).first())
    if conciliada:
        raise HTTPException(409, "La cobranza está conciliada con el banco; desconcíliela en Tesorería primero")
    # Reversión de una aplicación de adelanto: devolver el monto al adelanto de origen
    # (bajo lock) para que quede disponible para otra factura. Mantiene el INVARIANTE
    # monto_aplicado == Σ cobranzas medio='adelanto' del adelanto.
    if c.medio == MEDIO_ADELANTO and c.adelanto_id:
        adel = (db.query(ContAdelanto)
                .filter(ContAdelanto.id == c.adelanto_id)
                .with_for_update().first())
        if adel:
            adel.monto_aplicado = round(max(_f(adel.monto_aplicado) - _f(c.monto), 0.0), 2)
    db.delete(c)
    db.flush()
    db.refresh(factura)
    _recompute_factura(factura)
    db.commit()
    return {"ok": True}


class FactoringIn(BaseModel):
    """Cesión de la factura a un factor. monto_adelantado <= cupo (bruto - pagos reales);
    si retencion no viene, se deriva = cupo - adelanto."""
    empresa_factoring: Optional[str] = None
    id_operacion: Optional[str] = None
    fecha_operacion: Optional[str] = None
    monto_adelantado: float = Field(0, ge=0)
    costo_factoring: float = Field(0, ge=0)
    retencion: Optional[float] = Field(None, ge=0)
    banco: Optional[str] = None
    observaciones: Optional[str] = None


@router.post("/facturas/{factura_id}/factoring")
def set_factoring(
    factura_id: int,
    payload: FactoringIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crea o actualiza el factoring (1 por factura). Valida adelanto <= cupo (bruto - pagos
    reales), deriva la retención si falta, y genera SOLO la cobranza de ADELANTO
    (medio 'factoring_adelanto'). Registra quién lo hizo. No editable si ya está liquidado."""
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
        .with_for_update()
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    if factura.es_anticipo:
        raise HTTPException(409, "No se puede hacer factoring de una factura de anticipo (respalda un adelanto ya recibido)")
    fac = factura.factoring
    if fac and fac.estado == "liquidada":
        raise HTTPException(400, "El factoring ya fue liquidado; no se puede modificar")

    bruto = _f(factura.monto_bruto)
    pagado_no_fact = sum(_f(c.monto) for c in factura.cobranzas if not _es_medio_factoring(c.medio))
    if payload.monto_adelantado < 0:
        raise HTTPException(400, "El adelanto no puede ser negativo")
    cupo = bruto - pagado_no_fact
    if payload.monto_adelantado > cupo + TOL_PAGO:
        raise HTTPException(400, f"El adelanto excede el saldo financiable ({cupo:.0f})")
    retencion = payload.retencion
    if retencion is None:
        retencion = round(max(cupo - payload.monto_adelantado, 0), 0)

    if not fac:
        fac = ContFactoring(factura_id=factura.id)
        db.add(fac)
    fac.usuario_id = getattr(current_user, "id", None)
    fac.empresa_factoring = payload.empresa_factoring
    fac.id_operacion = payload.id_operacion
    fac.fecha_operacion = _parse_date(payload.fecha_operacion) or date.today()
    fac.monto_adelantado = payload.monto_adelantado
    fac.costo_factoring = payload.costo_factoring
    fac.retencion = retencion
    fac.banco = payload.banco
    fac.observaciones = payload.observaciones
    fac.estado = "vigente"
    fac.fecha_liquidacion = None

    # Reemplazar solo la cobranza de ADELANTO (nunca la de retención liquidada).
    # Si esa cobranza ya está conciliada con un abono del banco en Tesorería, se
    # rechaza: borrarla dejaría el movimiento bancario conciliado sin destino.
    for c in list(factura.cobranzas):
        if c.medio == MEDIO_FACT_ADELANTO:
            conciliada = (db.query(ConciliacionIngreso)
                          .filter(ConciliacionIngreso.cobranza_id == c.id).first())
            if conciliada:
                raise HTTPException(409, "El adelanto del factoring está conciliado con el banco; desconcílielo en Tesorería antes de modificar el factoring")
            db.delete(c)
    db.flush()
    if payload.monto_adelantado > 0:
        db.add(ContCobranza(
            factura_id=factura.id, fecha=fac.fecha_operacion, monto=payload.monto_adelantado,
            medio=MEDIO_FACT_ADELANTO, banco=payload.banco, numero_operacion=payload.id_operacion,
            observaciones=f"Adelanto factoring {payload.empresa_factoring or ''}".strip(),
            usuario_id=getattr(current_user, "id", None),
        ))
    db.flush()
    db.refresh(factura)
    _recompute_factura(factura)
    db.commit()
    db.refresh(factura)
    return _serialize_factura(factura)


@router.post("/facturas/{factura_id}/factoring/liquidar")
def liquidar_factoring(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Liquida el factoring vigente: libera el saldo pendiente REAL como retención (cobranza
    'factoring_retencion'), cerrando la factura en saldo 0, y marca estado 'liquidada' con
    quién y cuándo."""
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
        .with_for_update()
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    fac = factura.factoring
    if not fac or fac.estado != "vigente":
        raise HTTPException(400, "No hay factoring vigente para liquidar")
    # Liberar el saldo pendiente REAL (no un valor fijo) para cerrar exacto en 0
    pagado_actual = sum(_f(c.monto) for c in factura.cobranzas)
    liberar = round(max(_f(factura.monto_bruto) - pagado_actual, 0.0), 2)
    # La retención refleja SIEMPRE lo realmente liberado por el factor en esta liquidación
    fac.retencion = liberar
    if liberar > TOL:
        db.add(ContCobranza(
            factura_id=factura.id, fecha=date.today(), monto=liberar,
            medio=MEDIO_FACT_RETENCION, banco=fac.banco, numero_operacion=fac.id_operacion,
            observaciones="Liquidación retención factoring", usuario_id=getattr(current_user, "id", None),
        ))
    fac.estado = "liquidada"
    fac.fecha_liquidacion = date.today()
    fac.usuario_liquidacion_id = getattr(current_user, "id", None)
    db.flush()
    db.refresh(factura)
    _recompute_factura(factura)
    db.commit()
    db.refresh(factura)
    return _serialize_factura(factura)


@router.delete("/facturas/{factura_id}")
def eliminar_factura(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Borrado SEGURO de una factura: se rechaza (409) si tiene factoring (vigente o liquidado)
    o cobranzas reales — primero hay que revertir esos pagos. El cascade borra las líneas,
    nunca pagos reales ni operaciones de factoring."""
    # Orden GLOBAL de locks: OC → adelanto/factura. Serializar por la OC primero
    # evita el deadlock con la aprobación de adelantos en Tesorería (que también
    # parte por la OC y luego toca adelantos y facturas de la misma venta).
    ref = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    if not ref:
        raise HTTPException(404, "Factura no encontrada")
    if ref.oc_cliente_id:
        db.query(OcCliente).filter(OcCliente.id == ref.oc_cliente_id).with_for_update().first()
    # Lock de fila: sin él, una cobranza registrada entre el chequeo y el DELETE se
    # borraría en cascada (registrar_cobranza también bloquea la factura).
    # populate_existing: datos frescos bajo el lock, no los de la lectura previa.
    factura = (db.query(ContFacturaCliente)
               .filter(ContFacturaCliente.id == factura_id)
               .populate_existing().with_for_update().first())
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    # Borrado seguro: no destruir pagos reales ni operaciones de factoring (vigentes o liquidadas)
    if factura.factoring:
        raise HTTPException(409, "La factura tiene una operación de factoring; no se puede eliminar")
    if any(not _es_medio_factoring(c.medio) for c in factura.cobranzas):
        raise HTTPException(409, "Revierta las cobranzas antes de eliminar la factura")
    if factura.es_anticipo:
        # Una factura de anticipo ya DESCONTADA en facturas del despacho real no se borra:
        # dejaría descuentos colgando (y la FK de la línea de descuento lo impediría igual).
        descontada = (db.query(ContFacturaClienteItem)
                      .filter(ContFacturaClienteItem.anticipo_factura_id == factura.id)
                      .first())
        if descontada:
            raise HTTPException(409, "La factura de anticipo ya fue descontada en otra factura; elimine primero esa factura")
        # Los adelantos que respaldaba vuelven a la vía A (sin factura de anticipo).
        # Sin aplicaciones que revertir: si las hubiera, el chequeo de cobranzas de
        # arriba ya bloqueó el borrado.
        db.query(ContAdelanto).filter(
            ContAdelanto.factura_anticipo_id == factura.id
        ).update({"factura_anticipo_id": None})
    db.delete(factura)  # cascade elimina líneas (sin pagos reales ni factoring)
    db.commit()
    return {"ok": True}


@router.get("/kpis")
def get_kpis(
    periodo: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Indicadores de cobranza (filtrables por `periodo`: semana|mes|anio sobre fecha_emisión).
    Glosario:
      facturado_clp          : total bruto facturado.
      cobrado_clp            : total ingresado (incluye adelanto/retención de factoring).
      cobrado_cliente_clp    : SOLO pagos reales del cliente (sin factoring).
      anticipo_factoring_clp : dinero del factor (adelanto + retención liquidada).
      por_cobrar_clp         : saldo pendiente.
      vencido_clp            : saldo vencido (excluye facturas con factoring vigente).
      en_factoring_clp       : bruto de facturas con factoring vigente."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    facturas = [
        f for f in db.query(ContFacturaCliente)
        .options(selectinload(ContFacturaCliente.cobranzas), selectinload(ContFacturaCliente.factoring))
        .filter(ContFacturaCliente.empresa == empresa)
        .all()
        if _periodo_filter(f.fecha_emision or f.created_at, periodo)
    ]
    hoy = date.today()
    facturado = sum(_f(f.monto_bruto) for f in facturas)
    cobrado = sum(_f(f.monto_pagado) for f in facturas)
    cobrado_cliente = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                          if not _es_medio_factoring(c.medio))
    por_cobrar = sum(_f(f.saldo) for f in facturas if _f(f.saldo) > TOL)
    # Vencido: excluye facturas con factoring vigente (el riesgo de cobro lo tomó el factor)
    vencido = sum(_f(f.saldo) for f in facturas
                  if _f(f.saldo) > TOL and f.fecha_vencimiento and f.fecha_vencimiento < hoy
                  and not (f.factoring and f.factoring.estado == "vigente"))
    en_factoring = sum(_f(f.monto_bruto) for f in facturas
                       if f.factoring and f.factoring.estado == "vigente")
    anticipo_factoring = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                             if _es_medio_factoring(c.medio))
    return {
        "n_facturas": len(facturas),
        "facturado_clp": round(facturado, 0),
        "cobrado_clp": round(cobrado, 0),
        "cobrado_cliente_clp": round(cobrado_cliente, 0),
        "anticipo_factoring_clp": round(anticipo_factoring, 0),
        "por_cobrar_clp": round(por_cobrar, 0),
        "vencido_clp": round(vencido, 0),
        "en_factoring_clp": round(en_factoring, 0),
    }
