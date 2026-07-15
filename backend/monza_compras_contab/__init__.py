"""Módulo Compras / Cuentas por Pagar (AP) de MonzaParts — aislado y aditivo.

SOLO MonzaParts (empresa 'automotriz'). Es el espejo del módulo de Grupo AM
(backend/compras_contab/) con la misma metodología NIIF/NIC 7, apuntando a tablas
monza_*:
  - Proveedores: monza_proveedores (catálogo de Abastecimiento, solo lectura).
  - Costos de embarque: overlay de SOLO LECTURA sobre monza_emb_pricing_gasto
    (los gastos se anotan en Embarques Pricing y acá se ven reflejados automáticamente).
  - Plan de cuentas propio: monza_cont_plan_cuenta (importado del mismo Excel NIIF).

Tablas nuevas (aditivas): monza_cont_plan_cuenta, monza_cont_compra, monza_cont_egreso,
monza_cont_egreso_detalle. El pago de una compra son las asignaciones de sus
Comprobantes de Egreso (una salida real de dinero puede pagar varias compras y luego
se concilia con el banco — flujo NIC 7).

Activación: main.py importa `monza_compras_contab.router` y lo monta sin prefix
(el router ya trae prefix=/api/monza/compras-contab). Candado require_empresa("automotriz").
Puesta en marcha: `python -m monza_compras_contab.init_db` y
`python -m monza_compras_contab.import_plan_cuentas` (una vez por entorno).
"""
