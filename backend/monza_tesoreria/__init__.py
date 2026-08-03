"""Módulo Tesorería de MonzaParts — aislado y aditivo.

SOLO MonzaParts (empresa 'automotriz'). Concentra el manejo del dinero real del banco
en 4 sub-áreas (una pestaña cada una en el frontend):

  1. APROBACIONES — los adelantos (ej. 50%) que Comercial informa al cerrar una venta
     llegan acá; Tesorería revisa que la plata esté en el banco y DA LA ORDEN
     (adelanto_verificado=1) → recién ahí Abastecimiento puede comprar (el cortafuego
     vive en monza_router_abastecimiento y NO cambia). Al aprobar, el adelanto se
     APLICA de inmediato a las facturas ya emitidas de la venta (cobranza
     medio='adelanto', espejo GA); si la factura viene después, la aplica
     crear_factura de monza_contabilidad. Ventas-Contab lo muestra solo lectura.
     La operación escribe monza_cont_adelanto (tabla de monza_contabilidad;
     dependencia documentada, misma regla de negocio).

  2. POR PAGAR / APROBAR PAGOS — cola de compras con saldo (registradas en Compras/CxP
     con pago futuro o parcial). Tesorería DA LA ORDEN del pago: crea el Comprobante
     de Egreso reusando `_crear_egreso` de monza_compras_contab (una sola fuente de
     verdad: locks anti doble-pago, tope por saldo, recálculo).

  3. CONCILIACIÓN BANCARIA — espejo del módulo Tesorería de Grupo AM: cuentas
     bancarias, importar cartolas (CSV/XLSX) con anti-duplicados, y cruzar 1:1 exacto:
       · CARGOS  ↔ Comprobantes de Egreso de Compras (monza_cont_egreso),
       · ABONOS  ↔ Adelantos verificados (monza_cont_adelanto) — plus de Monza, y
       · ABONOS  ↔ Cobranzas de Facturas y Cobranzas (monza_cont_cobranza, vía
         monza_tes_conciliacion_ingreso; se excluye medio='adelanto': la aplicación
         de un adelanto no es un depósito nuevo).

  4. FLUJO DE CAJA (NIC 7) — proyección de salidas (vencimientos de Compras) vs
     entradas (facturas por cobrar + adelantos por aprobar/recibidos sin aplicar)
     en ventanas de días, solo lectura.

Tablas nuevas (aditivas): monza_tes_cuenta_bancaria, monza_tes_cartola,
monza_tes_movimiento, monza_tes_conciliacion, monza_tes_conciliacion_ingreso.
No se altera ninguna tabla existente.

Activación: main.py importa `monza_tesoreria.router` y lo monta sin prefix (el router
ya trae prefix=/api/monza/tesoreria). Candado require_empresa("automotriz").
Puesta en marcha: `python -m monza_tesoreria.init_db` (idempotente; correr ANTES de
reiniciar el backend en cada deploy — incluye la migración aditiva del snapshot de
conciliación).
"""
