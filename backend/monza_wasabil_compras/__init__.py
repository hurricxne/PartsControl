"""Libro de compras del SII — espejo local de los documentos RECIBIDOS de Wasabil (MonzaParts).

Paquete NUEVO y aislado de MonzaParts (empresa 'automotriz'), espejo deliberado de
`wasabil_compras/` de Grupo AM — regla de la casa: la duplicación entre marcas es a
propósito (paquete propio, cliente propio, tablas propias monza_sii_libro_*); JAMÁS se
parametriza el módulo de GA ni se importa nada de `wasabil_compras/` ni de `wasabil_dte/`.
Tampoco comparte código con `monza_wasabil_dte/` (el módulo de EMISIÓN Monza): este
paquete es de SOLO LECTURA por diseño — su cliente no tiene, ni tendrá, ninguna función
capaz de crear, emitir, anular ni modificar un documento tributario.

Por qué existe (plan: docs/plan-libro-compras-sii-2026-08-03.md + addendum):
  - Responder «¿qué facturas de proveedor existen ante el SII y NO están en el ERP?»
    (la bandeja de la Fase A2).
  - Dejar de re-digitar los gastos de embarque que Wasabil ya tiene (Fase B).
  - Clasificar cada documento: COSTO POR VENTA (capitaliza al landed) o CENTRO DE COSTOS
    (gasto del período) — ver docs/sentido-financiero-libro-compras-2026-08-06.md.

Fuente de la verdad del API: docs/wasabil-api-llms-full-2026-08-05.txt (foto congelada de
https://app.wasabil.com/llms-full.txt, que se regenera en cada deploy de Wasabil).
"""
