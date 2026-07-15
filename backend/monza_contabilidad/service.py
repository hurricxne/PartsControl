"""Lógica pura del módulo Contabilidad MonzaParts: cálculo de IVA, saldo, estado de
pago, semáforo de vencimiento y serializadores. Sin sesión de BD → testeable en
aislamiento (mismas reglas que el módulo de Grupo AM)."""
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List

logger = logging.getLogger("monza_contabilidad")

# ── Constantes (alineadas con el módulo de Grupo AM) ───────────────────────────
IVA_DEFAULT = 0.19    # IVA Chile si la cotización/config no traen iva_pct
TOL = 0.5             # tolerancia CLP para clasificar saldos (pagada / al_día)
TOL_QTY = 0.001       # tolerancia para comparaciones de cantidades
TOL_PAGO = 1.0        # holgura de 1 CLP en topes de pago/adelanto (redondeo)
DIAS_POR_VENCER = 7   # días para marcar 'por_vencer' en el semáforo
MEDIO_FACT_ADELANTO = "factoring_adelanto"
MEDIO_FACT_RETENCION = "factoring_retencion"
MEDIO_ADELANTO = "adelanto"  # cobranza generada al aplicar un adelanto verificado a la factura


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_date(s: Optional[str]) -> Optional[date]:
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


def _es_medio_factoring(medio: Optional[str]) -> bool:
    return bool(medio and medio.startswith("factoring"))


def iva_rate_de(cot, cfg) -> float:
    """Tasa de IVA (fracción, ej 0.19). MonzaConfig/MonzaCotizacion guardan iva_pct
    como porcentaje (ej 19); se normaliza a fracción. Cae a IVA_DEFAULT si no hay dato."""
    raw = None
    if cot is not None:
        raw = getattr(cot, "iva_pct", None)
    if not raw and cfg is not None:
        raw = getattr(cfg, "iva_pct", None)
    raw = _f(raw)
    if raw <= 0:
        logger.warning(
            "IVA no definido en cotización ni MonzaConfig; usando IVA_DEFAULT=%.0f%%. "
            "Revisa MonzaConfig.iva_pct.", IVA_DEFAULT * 100,
        )
        return IVA_DEFAULT
    return raw / 100.0 if raw > 1 else raw


def _semaforo(fecha_venc: Optional[date], saldo: float) -> str:
    """al_dia (saldada) | sin_fecha | vencida | por_vencer (<= DIAS_POR_VENCER) | vigente."""
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


def _estado_pago(factura, pagado: float, saldo: float) -> str:
    """El saldo manda: saldada → 'pagada'. Con saldo y factoring vigente → 'factorizada'."""
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


def _recompute_factura(factura) -> None:
    """Recalcula monto_pagado / saldo / estado_pago desde las cobranzas reales."""
    bruto = _f(factura.monto_bruto)
    pagado = sum(_f(c.monto) for c in factura.cobranzas)
    saldo = round(max(bruto - pagado, 0.0), 2)   # nunca negativo persistido
    factura.monto_pagado = round(pagado, 2)
    factura.saldo = saldo
    factura.estado_pago = _estado_pago(factura, pagado, saldo)


def _serialize_factura(factura) -> dict:
    bruto = _f(factura.monto_bruto)
    pagado = _f(factura.monto_pagado)
    saldo = _f(factura.saldo)
    fac = factura.factoring
    return {
        "id": factura.id,
        "numero_factura": factura.numero_factura,
        "tipo_doc": factura.tipo_doc,
        "cotizacion_id": factura.cotizacion_id,
        "numero_cotizacion": factura.numero_cotizacion,
        "cliente": factura.cliente_nombre or "",
        "rut_cliente": factura.rut_cliente or "",
        "despacho_id": factura.despacho_id,
        "numero_guia": factura.numero_guia,
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


def _resumen_cobranza(facturas: List) -> dict:
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


def estado_adelanto(cot, adelanto) -> dict:
    """Estado del adelanto de una venta, para la UI y para Abastecimiento.

    Reglas (simples y debuggeables):
      - requiere_adelanto = cotización.pct_adelanto > 0 (lo informa Comercial al cerrar).
      - 'verificado'  = existe un registro MonzaContAdelanto (Contabilidad lo verificó).
      - 'por_verificar' = requiere pero aún sin registro.
      - 'no_aplica'   = no requiere adelanto.
    """
    pct = int(getattr(cot, "pct_adelanto", 0) or 0)
    verificado = adelanto is not None
    # Si ya existe un registro verificado, el adelanto sigue aplicando aunque luego cambien
    # el pct (defensa ante inconsistencias); el estado lo manda la existencia del registro.
    requiere = pct > 0 or verificado
    if verificado:
        estado = "verificado"
    elif requiere:
        estado = "por_verificar"
    else:
        estado = "no_aplica"
    return {
        "requiere_adelanto": requiere,
        "pct_adelanto": pct,
        "estado_adelanto": estado,
        "adelanto": None if adelanto is None else {
            "id": adelanto.id,
            "monto": _f(adelanto.monto),
            "monto_aplicado": _f(adelanto.monto_aplicado),
            "fecha_pago": adelanto.fecha_pago.isoformat() if adelanto.fecha_pago else None,
            "banco": adelanto.banco,
            "numero_operacion": adelanto.numero_operacion,
            "observaciones": adelanto.observaciones,
            "fecha_verificacion": adelanto.fecha_verificacion.isoformat() if adelanto.fecha_verificacion else None,
        },
    }


def _periodo_filter(fecha, periodo: Optional[str]) -> bool:
    if not periodo or not fecha:
        return True
    hoy = date.today()
    d = fecha.date() if hasattr(fecha, "date") else fecha
    if periodo == "semana":
        return (hoy - d).days <= 7
    if periodo == "mes":
        return d.year == hoy.year and d.month == hoy.month
    if periodo == "anio":
        return d.year == hoy.year
    return True


def periodo_floor(periodo: Optional[str]) -> Optional[date]:
    """Cota inferior (fecha) para un período, usable como pre-filtro en SQL antes del
    filtro fino _periodo_filter. None si no aplica."""
    if not periodo:
        return None
    hoy = date.today()
    if periodo == "semana":
        return hoy - timedelta(days=7)
    if periodo == "mes":
        return date(hoy.year, hoy.month, 1)
    if periodo == "anio":
        return date(hoy.year, 1, 1)
    return None
