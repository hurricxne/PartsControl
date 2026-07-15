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
"""
from datetime import datetime, date, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, selectinload, joinedload
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from database import get_db
from models.models import (
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ConfiguracionCotizador, User,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContFactoring,
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


def _resumen_cobranza(facturas: List[ContFacturaCliente]) -> dict:
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
    return {
        "n_facturas": len(facturas),
        "facturado_clp": round(facturado, 0),
        "cobrado_clp": round(cobrado, 0),
        "cobrado_cliente_clp": round(cobrado_cliente, 0),
        "por_cobrar_clp": round(max(saldo, 0), 0),
        "estado_cobranza": estado,
    }


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
        resumen = _resumen_cobranza(facturas_by_oc.get(oc.id, []))
        result.append({
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
    return {
        "oc_cliente_id": oc.id,
        "cotizacion_id": cot.id,
        "numero_oc": oc.numero_oc,
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
        "resumen": _resumen_cobranza(facturas),
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
    return [e for e in by_desp.values() if e["facturable"] > TOL]


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
    firmada) O 'items' explícitos. numero_factura es el folio SII (único por empresa)."""
    oc_cliente_id: int
    despacho_id: Optional[int] = None
    numero_factura: Optional[str] = None
    tipo_doc: str = "factura"
    fecha_emision: Optional[str] = None
    condicion_pago: Optional[str] = None
    plazo_dias: Optional[int] = Field(None, ge=0)
    items: Optional[List[FacturaItemIn]] = None
    observaciones: Optional[str] = None


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


@router.post("/facturas")
def crear_factura(
    payload: FacturaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE una factura a cliente. Dos modos: `payload.despacho_id` (deriva las líneas SOLO
    de una guía despachada y FIRMADA) o `payload.items` explícitos. Reglas: folio único por
    empresa; no facturar más de lo despachado-y-no-facturado (doble tope por ÍTEM y por GUÍA,
    con acumuladores dentro del request); congela montos (neto, IVA 19%, bruto)."""
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
    empresa = getattr(current_user, "empresa", None) or "mineria"
    # Folio único por empresa
    if payload.numero_factura:
        dup = db.query(ContFacturaCliente).filter(
            ContFacturaCliente.empresa == empresa,
            ContFacturaCliente.numero_factura == payload.numero_factura,
        ).first()
        if dup:
            raise HTTPException(409, f"El folio {payload.numero_factura} ya existe para esta empresa")
    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    items_db, pmap, _ = _precios_de_cotizacion(db, cot.id, cfg_dict)
    items_by_id = {i.id: i for i in items_db}

    # Despachos DESPACHADOS de la OC (lo que se puede facturar)
    desp_items = _despacho_items_de_oc(db, oc.id)
    di_by_id = {di.id: di for di, _d in desp_items}
    desp_by_item = {}
    for di, _d in desp_items:
        desp_by_item.setdefault(di.item_cotizacion_id, []).append(di)
    desp_qty_item = _qty_despachada_por_item(db, oc.id)
    fact_qty_item = _qty_facturada_por_item(db, oc.id)
    fact_qty_di = _qty_facturada_por_despacho_item(db, oc.id)

    # La guía (despacho_id) se valida SIEMPRE que venga en el payload — también con
    # ítems explícitos: la factura no puede quedar ligada a una guía ajena o sin firmar.
    desp = None
    if payload.despacho_id:
        desp = db.query(Despacho).filter(
            Despacho.id == payload.despacho_id, Despacho.oc_cliente_id == oc.id
        ).first()
        if not desp:
            raise HTTPException(404, "Despacho no encontrado para esta OC")
        if desp.estado != "despachado" or not desp.guia_firmada:
            raise HTTPException(400, "Solo se puede facturar una guía de despacho FIRMADA (entregada y firmada por el cliente)")

    # Determinar líneas a facturar
    lineas: List[FacturaItemIn] = []
    if payload.items:
        lineas = payload.items
    elif desp is not None:
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
            # TOL_QTY (unidades), no TOL (pesos): con TOL=0.5 un saldo fraccional
            # de hasta media unidad quedaba infacturable en silencio
            if disponible > TOL_QTY:
                lineas.append(FacturaItemIn(
                    item_cotizacion_id=di.item_cotizacion_id,
                    despacho_item_id=di.id,
                    cantidad=round(disponible, 4),
                ))
                usado_deriv[di.item_cotizacion_id] = usado_deriv.get(di.item_cotizacion_id, 0.0) + disponible
        if not lineas:
            raise HTTPException(409, "El despacho ya fue facturado por completo")
    if not lineas:
        raise HTTPException(400, "Debe indicar ítems o un despacho a facturar")

    # Validación por línea con acumuladores unificados (guía + ítem) en este request
    usado_di = {}     # despacho_item_id -> qty usada
    usado_item = {}   # item_cotizacion_id -> qty usada (tope global del ítem)
    validadas = []
    for ln in lineas:
        it = items_by_id.get(ln.item_cotizacion_id)
        if not it:
            raise HTTPException(400, f"Ítem {ln.item_cotizacion_id} no pertenece a esta OC")
        cantidad = ln.cantidad if ln.cantidad is not None else _f(it.cantidad)
        if cantidad <= 0:
            raise HTTPException(400, f"Cantidad inválida para {it.numero_parte}")

        # Tope a nivel de ÍTEM (lo despachado y aún no facturado) — en TODAS las rutas
        despachado_item = desp_qty_item.get(ln.item_cotizacion_id, 0.0)
        if despachado_item <= 0:
            raise HTTPException(400, f"{it.numero_parte} no ha sido despachado; no se puede facturar")
        disponible = (
            despachado_item
            - fact_qty_item.get(ln.item_cotizacion_id, 0.0)
            - usado_item.get(ln.item_cotizacion_id, 0.0)
        )
        # Tope adicional a nivel de GUÍA si se indicó despacho_item_id
        if ln.despacho_item_id is not None:
            di = di_by_id.get(ln.despacho_item_id)
            if not di or di.item_cotizacion_id != ln.item_cotizacion_id:
                raise HTTPException(400, f"Guía/despacho inválido para {it.numero_parte}")
            disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0) - usado_di.get(di.id, 0.0)
            disponible = min(disponible, disp_di)

        if cantidad > disponible + TOL_QTY:
            raise HTTPException(409, f"{it.numero_parte}: cantidad excede lo despachado/no facturado (disp {max(disponible,0):.0f})")

        if ln.despacho_item_id is not None:
            usado_di[ln.despacho_item_id] = usado_di.get(ln.despacho_item_id, 0.0) + cantidad
        usado_item[ln.item_cotizacion_id] = usado_item.get(ln.item_cotizacion_id, 0.0) + cantidad

        ci = pmap.get(ln.item_cotizacion_id, {})
        precio = ln.precio_unit_neto if ln.precio_unit_neto is not None else _f(ci.get("precio_venta_clp"))
        if precio < 0:
            raise HTTPException(400, f"Precio inválido para {it.numero_parte}")
        validadas.append((it, ln, cantidad, precio))

    fecha_emision = _parse_date(payload.fecha_emision) or date.today()
    # `is not None`: plazo 0 días (contado) también debe generar vencimiento (= emisión)
    fecha_venc = (fecha_emision + timedelta(days=int(payload.plazo_dias))
                  if payload.plazo_dias is not None else None)

    factura = ContFacturaCliente(
        empresa=empresa,
        oc_cliente_id=oc.id, cotizacion_id=cot.id, despacho_id=payload.despacho_id,
        numero_factura=payload.numero_factura, tipo_doc=payload.tipo_doc or "factura",
        fecha_emision=fecha_emision, condicion_pago=payload.condicion_pago,
        plazo_dias=payload.plazo_dias, fecha_vencimiento=fecha_venc,
        observaciones=payload.observaciones, usuario_id=getattr(current_user, "id", None),
    )
    try:
        db.add(factura)
        db.flush()
        neto = 0.0
        for it, ln, cantidad, precio in validadas:
            total = round(precio * cantidad, 0)
            neto += total
            db.add(ContFacturaClienteItem(
                factura_id=factura.id, item_cotizacion_id=ln.item_cotizacion_id,
                despacho_item_id=ln.despacho_item_id,
                numero_parte=it.numero_parte, descripcion=it.descripcion,
                cantidad=cantidad, precio_unit_neto=round(precio, 2), total_neto=total,
            ))
        iva = round(neto * IVA_RATE, 0)
        factura.monto_neto = neto
        factura.iva = iva
        factura.monto_bruto = neto + iva
        db.flush()
        _recompute_factura(factura)
        db.commit()
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
    # Bloquea la factura (lock de fila) ANTES de borrar, igual que registrar_cobranza,
    # para que el recálculo de saldo no compita con un pago concurrente.
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
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
    conciliada = (db.query(ConciliacionIngreso)
                  .filter(ConciliacionIngreso.cobranza_id == c.id).first())
    if conciliada:
        raise HTTPException(409, "La cobranza está conciliada con el banco; desconcíliela en Tesorería primero")
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
    # Lock de fila: sin él, una cobranza registrada entre el chequeo y el DELETE se
    # borraría en cascada (registrar_cobranza también bloquea la factura).
    factura = (db.query(ContFacturaCliente)
               .filter(ContFacturaCliente.id == factura_id)
               .with_for_update().first())
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    # Borrado seguro: no destruir pagos reales ni operaciones de factoring (vigentes o liquidadas)
    if factura.factoring:
        raise HTTPException(409, "La factura tiene una operación de factoring; no se puede eliminar")
    if any(not _es_medio_factoring(c.medio) for c in factura.cobranzas):
        raise HTTPException(409, "Revierta las cobranzas antes de eliminar la factura")
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
