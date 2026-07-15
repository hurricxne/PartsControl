"""Módulo Tesorería (Grupo AM) — aislado y aditivo.

Evolución del módulo Conciliación Bancaria: Tesorería es quien REVISA, APRUEBA y
CONCILIA lo que los otros módulos registran:
  · Compras/CxP registra compras (pago futuro, inmediato o parcial) y sus egresos.
  · Facturas y Cobranzas registra los ingresos de caja (cobranzas de facturas).
  · Tesorería aprueba los pagos por vencer (da la orden → crea el egreso), concilia
    cargos↔egresos y abonos↔cobranzas contra la cartola del banco, y proyecta el
    flujo de caja (NIC 7).

Las tablas conservan el prefijo conc_ (continuidad de datos en producción; NO hay
migración de renombre). Solo se agrega conc_conciliacion_ingreso (abono↔cobranza).
"""
