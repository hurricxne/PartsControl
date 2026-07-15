# Módulo Tesorería (Grupo AM)

Evolución del módulo **Conciliación Bancaria**: Tesorería es quien **revisa, aprueba y
concilia** lo que los otros módulos registran.

## Responsabilidades

1. **Por pagar / aprobar pagos** — Compras/CxP registra las compras (pago futuro,
   inmediato o parcial). Tesorería ve la cola de compras con saldo (por vencimiento)
   y **da la orden del pago**: crea el Comprobante de Egreso (`POST /tesoreria/pagos`),
   reusando la MISMA regla de negocio de Compras (`compras_contab.router._crear_egreso`:
   locks anti doble-pago, tope por saldo, recálculo de estados). Una sola fuente de verdad.
2. **Conciliación bancaria** — carga cartolas (CSV/XLSX) y cruza 1:1 exacto (±TOL):
   - `cargo` ↔ `cont_egreso` (egreso de Compras): marca ambos conciliados.
   - `abono` ↔ `cont_cobranza` (ingreso de caja registrado en Facturas y Cobranzas):
     el "conciliado" de la cobranza se **deriva** del enlace `conc_conciliacion_ingreso`
     (no se agregan columnas a tablas de otros módulos).
3. **Flujo de caja (NIC 7)** — proyección por buckets de vencimiento (vencido / 0-7 /
   8-30 / 31-60 / 61+ / sin fecha): salidas (Compras por pagar) vs entradas (facturas
   por cobrar; las factorizadas se excluyen y se informa la retención del factor).

## Tablas

Las tablas **conservan el prefijo `conc_`** (continuidad de datos de producción; el
módulo se renombró, los datos no — no hay migración de renombre):

| Tabla | Rol |
|---|---|
| `conc_cuenta_bancaria` | catálogo de cuentas bancarias |
| `conc_cartola` | lote de movimientos importados (auditable) |
| `conc_movimiento` | un movimiento del banco (cargo/abono) |
| `conc_conciliacion` | enlace cargo ↔ `cont_egreso` |
| `conc_conciliacion_ingreso` | **nueva**: enlace abono ↔ `cont_cobranza` (UNIQUE por cobranza) |

Crear tablas faltantes: `python -m tesoreria.init_db` (desde `backend/`).

## Integridad entre módulos

- Compras rechaza borrar/revertir un egreso **conciliado** (409: desconciliar primero).
- Facturas y Cobranzas rechaza borrar una cobranza **conciliada** (409: desconciliar
  primero en Tesorería) — guard en `routers/contabilidad.py::eliminar_cobranza`.
- Todo el módulo está candado a Grupo AM: `require_empresa("mineria")` + filtro
  `empresa` en cada query.
