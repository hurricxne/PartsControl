"""Módulo Tesorería de MonzaParts — aislado y aditivo.

SOLO MonzaParts (empresa 'automotriz'). Concentra el manejo del dinero real del banco
en 3 sub-áreas (una pestaña cada una en el frontend):

  1. APROBACIONES — los adelantos (ej. 50%) que Comercial informa al cerrar una venta
     llegan acá; Tesorería revisa que la plata esté en el banco y DA LA ORDEN
     (adelanto_verificado=1) → recién ahí Abastecimiento puede comprar (el cortafuego
     vive en monza_router_abastecimiento y NO cambia). Ventas-Contab lo muestra solo
     lectura. La operación escribe monza_cont_adelanto (tabla de monza_contabilidad;
     dependencia documentada, misma regla de negocio).

  2. CONCILIACIÓN BANCARIA — espejo del módulo de Grupo AM (conciliacion_bancaria):
     cuentas bancarias, importar cartolas (CSV/XLSX), y cruzar:
       · CARGOS  ↔ Comprobantes de Egreso de Compras (monza_cont_egreso), y
       · ABONOS  ↔ Adelantos verificados (monza_cont_adelanto) — plus de Monza.

  3. FLUJO DE CAJA (NIC 7) — proyección de salidas (vencimientos de Compras) vs
     entradas (facturas por cobrar) en ventanas de días, solo lectura.

Tablas nuevas (aditivas): monza_tes_cuenta_bancaria, monza_tes_cartola,
monza_tes_movimiento, monza_tes_conciliacion. No se altera ninguna tabla existente.

Activación: main.py importa `monza_tesoreria.router` y lo monta sin prefix (el router
ya trae prefix=/api/monza/tesoreria). Candado require_empresa("automotriz").
Puesta en marcha: `python -m monza_tesoreria.init_db` (una vez por entorno).
"""
