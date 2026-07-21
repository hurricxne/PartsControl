"""Lógica del módulo Compras / Cuentas por Pagar (autocontenida).

Los helpers se copian del molde de Cuentas por Cobrar (routers/contabilidad.py) para
que el módulo sea independiente. El "pago" de una compra ya NO es una fila propia: son
las asignaciones (cont_egreso_detalle) de los Comprobantes de Egreso que la pagan.
"""
from datetime import datetime, date
from typing import Optional

IVA_RATE = 0.19      # IVA Chile (19%)
TOL = 0.5            # tolerancia en CLP para clasificar saldos
TOL_PAGO = 1.0       # holgura de 1 CLP en topes de pago (redondeo)
DIAS_POR_VENCER = 7  # días para marcar 'por_vencer' en el semáforo

TIPOS_GASTO = ["cogs", "gasto_operacional", "gasto_no_operacional", "otros"]
TIPO_GASTO_LABEL = {
    "cogs": "Costo de venta",
    "gasto_operacional": "Gasto operacional",
    "gasto_no_operacional": "Gasto no operacional",
    "otros": "Otros",
}
CATEGORIAS_SUGERIDAS = [
    "Mercadería importada", "Gastos de importación", "Flete internacional",
    "Aduana/agencia", "Impuestos", "Distribución nacional", "Arriendo",
    "Servicios básicos", "Honorarios", "Sueldos", "Software/Suscripciones",
    "Bancarios/Financieros", "Marketing", "Mantención", "No operacional", "Otros",
]
MEDIOS_PAGO = ["transferencia", "cheque", "efectivo", "tarjeta"]
ESTADOS_PAGO = ["pendiente", "parcial", "pagado", "vencido", "anulado"]

# Cuenta contable por defecto (CÓDIGO real del plan del dueño) según (origen, tipo_gasto).
CUENTA_DEFAULT_CODIGO = {
    ("EMBARQUE", "cogs"): "1.3.02",              # mercadería en tránsito (se capitaliza)
    ("MANUAL", "cogs"): "1.3.01",               # existencias / mercadería
    ("NACIONAL", "cogs"): "1.3.01",             # compra nacional → Existencias (activo), no gasto
    ("MANUAL", "gasto_operacional"): "6.2.04",  # gastos generales de oficina
    ("MANUAL", "gasto_no_operacional"): "6.4.01",  # gastos no operativos / otros
    ("MANUAL", "otros"): "6.4.01",
}


def cuenta_default_codigo(origen, tipo_gasto) -> Optional[str]:
    return CUENTA_DEFAULT_CODIGO.get(((origen or "MANUAL").upper(), tipo_gasto))


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s)[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _periodo_filter(fecha_ref, periodo: Optional[str]) -> bool:
    if not periodo or not fecha_ref:
        return True
    hoy = date.today()
    d = fecha_ref.date() if hasattr(fecha_ref, "date") else fecha_ref
    if periodo == "semana":
        return (hoy - d).days <= 7
    if periodo == "mes":
        return d.year == hoy.year and d.month == hoy.month
    if periodo == "anio":
        return d.year == hoy.year
    return True


def _semaforo(fecha_venc: Optional[date], saldo: float) -> str:
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


def _estado_pago(compra, pagado: float, saldo: float) -> str:
    if getattr(compra, "anulado", False):
        return "anulado"
    if saldo <= TOL:
        return "pagado"
    venc = compra.fecha_vencimiento
    vencida = bool(venc and venc < date.today())
    if pagado > TOL:
        return "vencido" if vencida else "parcial"
    return "vencido" if vencida else "pendiente"


def _recompute_compra(compra) -> None:
    """Recalcula pagado/saldo/estado SIEMPRE desde las asignaciones de egreso reales."""
    total = _f(compra.monto_total_clp)
    pagado = sum(_f(d.monto_clp) for d in compra.egreso_detalles)
    saldo = round(max(total - pagado, 0.0), 2)  # nunca negativo persistido
    compra.monto_pagado_clp = round(pagado, 2)
    compra.saldo_clp = saldo
    compra.estado_pago = _estado_pago(compra, pagado, saldo)


# ── Helpers de request ────────────────────────────────────────────────────────
def empresa_de(user) -> str:
    return getattr(user, "empresa", None) or "mineria"


def parse_date_estricta(s, *, campo: str) -> Optional[date]:
    if s is None or s == "":
        return None
    d = _parse_date(s)
    if d is None:
        raise ValueError(f"Fecha inválida en '{campo}': {s!r} (use AAAA-MM-DD)")
    return d


# ── Serializadores ────────────────────────────────────────────────────────────
def _serialize_pago(d) -> dict:
    """Una 'línea de pago' de la compra = una asignación de egreso (detalle + su egreso).
    El egreso lleva los datos bancarios y la conciliación."""
    e = d.egreso
    return {
        "id": d.id,                      # id del detalle
        "egreso_id": d.egreso_id,
        "monto_clp": _f(d.monto_clp),
        "tc_aplicado": _f(d.tc_aplicado),
        "monto_origen": _f(d.monto_origen),
        "fecha": e.fecha.isoformat() if e and e.fecha else None,
        "medio": e.medio if e else None,
        "banco": e.banco if e else None,
        "numero_operacion": e.numero_operacion if e else None,
        "moneda": e.moneda if e else None,
        "conciliado": bool(e.conciliado) if e else False,
        "fecha_mov_bancario": e.fecha_mov_bancario.isoformat() if e and e.fecha_mov_bancario else None,
        "n_compras": len(e.detalles) if e else 1,   # >1 → egreso consolidado (paga varias compras)
    }


def serialize_egreso(e, *, compras_map: Optional[dict] = None) -> dict:
    """Comprobante de egreso completo (cabecera + detalle de compras que paga)."""
    return {
        "id": e.id,
        "fecha": e.fecha.isoformat() if e.fecha else None,
        "medio": e.medio,
        "cuenta_origen_id": e.cuenta_origen_id,
        "cuenta_origen_codigo": e.cuenta_origen.codigo if getattr(e, "cuenta_origen", None) else None,
        "banco": e.banco,
        "numero_operacion": e.numero_operacion,
        "beneficiario": e.beneficiario,
        "beneficiario_rut": e.beneficiario_rut,
        "glosa": e.glosa,
        "moneda": e.moneda,
        "tc": _f(e.tc),
        "monto_origen": _f(e.monto_origen),
        "monto_total_clp": _f(e.monto_total_clp),
        "conciliado": bool(e.conciliado),
        "conciliado_at": e.conciliado_at.isoformat() if e.conciliado_at else None,
        "fecha_mov_bancario": e.fecha_mov_bancario.isoformat() if e.fecha_mov_bancario else None,
        "referencia_bancaria": e.referencia_bancaria,
        "detalles": [
            {
                "id": d.id, "compra_id": d.compra_id, "monto_clp": _f(d.monto_clp),
                **((compras_map or {}).get(d.compra_id, {})),
            }
            for d in e.detalles
        ],
    }


def _serialize_compra_item(it) -> dict:
    """Línea de costeo por ítem de una compra nacional (cont_compra_item)."""
    return {
        "id": it.id,
        "item_cotizacion_id": it.item_cotizacion_id,
        "oc_proveedor_item_id": it.oc_proveedor_item_id,
        "numero_parte": it.numero_parte,
        "descripcion": it.descripcion,
        "cantidad": _f(it.cantidad),
        "precio_unit": _f(it.precio_unit),
        "costo_unit_clp": _f(it.costo_unit_clp),
        "costo_total_clp": _f(it.costo_total_clp),
    }


def _serialize_compra(compra) -> dict:
    saldo = _f(compra.saldo_clp)
    return {
        "id": compra.id,
        "empresa": compra.empresa,
        "origen": compra.origen,
        "tipo_gasto": compra.tipo_gasto,
        "tipo_gasto_label": TIPO_GASTO_LABEL.get(compra.tipo_gasto, compra.tipo_gasto),
        "categoria": compra.categoria,
        "cuenta_contable_id": compra.cuenta_contable_id,
        "cuenta_codigo": compra.cuenta.codigo if getattr(compra, "cuenta", None) else None,
        "cuenta_nombre": compra.cuenta.nombre if getattr(compra, "cuenta", None) else None,
        "es_anticipo": bool(compra.es_anticipo),
        "proveedor_id": compra.proveedor_id,
        "acreedor": compra.acreedor,
        "proveedor_rut": compra.proveedor_rut,
        "fecha": compra.fecha.isoformat() if compra.fecha else None,
        "referencia": compra.referencia,
        "descripcion": compra.descripcion,
        "numero_documento": compra.numero_documento,
        "tipo_doc": compra.tipo_doc,
        "moneda": compra.moneda,
        "tc": _f(compra.tc),
        "monto_neto": _f(compra.monto_neto),
        "iva": _f(compra.iva),
        "monto_total": _f(compra.monto_total),
        "monto_total_clp": _f(compra.monto_total_clp),
        "condicion_pago": compra.condicion_pago,
        "plazo_dias": compra.plazo_dias,
        "fecha_vencimiento": compra.fecha_vencimiento.isoformat() if compra.fecha_vencimiento else None,
        # SIEMPRE recalculado al servir: el estado persistido no transiciona a 'vencido'
        # con el paso del tiempo (solo se actualiza al escribir la compra/pagos).
        "estado_pago": _estado_pago(compra, _f(compra.monto_pagado_clp), saldo),
        "monto_pagado_clp": _f(compra.monto_pagado_clp),
        "saldo_clp": saldo,
        "semaforo": _semaforo(compra.fecha_vencimiento, saldo),
        "dias_vencimiento": (compra.fecha_vencimiento - date.today()).days if compra.fecha_vencimiento else None,
        "anulado": bool(compra.anulado),
        "motivo_anulacion": compra.motivo_anulacion,
        "embarque_id": compra.embarque_id,
        "emb_pricing_gasto_id": compra.emb_pricing_gasto_id,
        "factura_proveedor_id": compra.factura_proveedor_id,
        "oc_proveedor_id": getattr(compra, "oc_proveedor_id", None),
        "observaciones": compra.observaciones,
        "created_at": compra.created_at.isoformat() if compra.created_at else None,
        "updated_at": compra.updated_at.isoformat() if getattr(compra, "updated_at", None) else None,
        "usuario_id": getattr(compra, "usuario_id", None),
        "pagos": [_serialize_pago(d) for d in sorted(compra.egreso_detalles, key=lambda x: x.id)],
        "items": [_serialize_compra_item(it) for it in sorted(compra.items, key=lambda x: x.id)],
    }
