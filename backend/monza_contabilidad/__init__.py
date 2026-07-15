"""Módulo Contabilidad MonzaParts — Ventas + Facturas/Cobranzas/Factoring.

Aislado y SOLO para MonzaParts (empresa 'automotriz'). Es el espejo del módulo de
Grupo AM (backend/routers/contabilidad.py), pero apuntando a las tablas monza_*:
  - Venta = MonzaCotizacion (estado 'vendida'/'despachado'); el cliente y los montos
    viven en la cotización y sus ítems (precio ya calculado, sin pricing_service).
  - Despachos/guías = MonzaDespacho + MonzaDespachoItem (se factura una guía
    'despachado', con tope por cantidad despachada; la firma es OPCIONAL/registrable).

Tablas nuevas (no tocan nada existente): monza_cont_factura_cliente / _item /
monza_cont_cobranza / monza_cont_factoring.

Activación: main.py importa `monza_contabilidad.router` y lo monta sin prefix
(el router ya trae prefix=/api/monza/contabilidad). Protegido por
require_empresa("automotriz").
"""
