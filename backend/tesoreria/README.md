# Módulo Tesorería (Grupo AM)

Evolución del módulo **Conciliación Bancaria**: Tesorería es quien **revisa, aprueba y
concilia** lo que los otros módulos registran.

## Responsabilidades

1. **Por pagar / aprobar pagos** — Compras/CxP registra las compras (pago futuro,
   inmediato o parcial). Tesorería ve la cola de compras con saldo (por vencimiento)
   y **da la orden del pago**: crea el Comprobante de Egreso (`POST /tesoreria/pagos`),
   reusando la MISMA regla de negocio de Compras (`compras_contab.router._crear_egreso`:
   locks anti doble-pago, tope por saldo, recálculo de estados). Una sola fuente de verdad.
2. **Aprobación de adelantos de cliente** — Comercial **informa** el adelanto (Cierre de
   Venta o Ventas de Contabilidad → `cont_adelanto` en estado `informado`); Tesorería lo
   ve en `GET /tesoreria/aprobaciones` y lo **aprueba** (`POST /tesoreria/adelantos/{id}/aprobar`)
   confirmando la plata recibida (monto real, fecha, banco, N° operación) **sin exigir
   cartola**. Al aprobar, el adelanto se **aplica solo** a las facturas de la venta como
   cobranza `medio='adelanto'` (regla en `routers/contabilidad._aplicar_adelantos_pendientes`,
   una sola fuente de verdad; si la factura aún no existe, la aplica `crear_factura`).
   Una OC puede tener **varios** adelantos; el tope es el total de la venta.
3. **Conciliación bancaria** — carga cartolas (CSV/XLSX) y cruza 1:1 exacto (±TOL):
   - `cargo` ↔ `cont_egreso` (egreso de Compras): marca ambos conciliados.
   - `abono` ↔ `cont_cobranza` (ingreso de caja registrado en Facturas y Cobranzas):
     el "conciliado" de la cobranza se **deriva** del enlace `conc_conciliacion_ingreso`
     (no se agregan columnas a tablas de otros módulos).
   - `abono` ↔ `cont_adelanto` (adelanto **aprobado**): misma derivación, 1:1
     (UNIQUE `adelanto_id`). **Anti-doble-conteo**: las cobranzas `medio='adelanto'`
     se EXCLUYEN de sugerencias/conciliar/pendientes de ingresos — son la APLICACIÓN
     contable de un adelanto ya recibido, no un depósito nuevo; su plata se concilia
     por la vía abono↔adelanto (regla espejo de MonzaParts).
4. **Flujo de caja (NIC 7)** — proyección por buckets de vencimiento (vencido / 0-7 /
   8-30 / 31-60 / 61+ / sin fecha): salidas (Compras por pagar) vs entradas (facturas
   por cobrar; las factorizadas se excluyen y se informa la retención del factor).
   Los adelantos se informan **aparte** de los buckets: `adelantos_por_aprobar` (aún no
   son plata segura) y `adelantos_recibidos_sin_aplicar` (plata ya en el banco; las
   próximas facturas nacerán con ese monto descontado).

## Tablas

Las tablas **conservan el prefijo `conc_`** (continuidad de datos de producción; el
módulo se renombró, los datos no — no hay migración de renombre):

| Tabla | Rol |
|---|---|
| `conc_cuenta_bancaria` | catálogo de cuentas bancarias |
| `conc_cartola` | lote de movimientos importados (auditable) |
| `conc_movimiento` | un movimiento del banco (cargo/abono) |
| `conc_conciliacion` | enlace cargo ↔ `cont_egreso` (+ snapshot `fecha_egreso_previa` / `referencia_egreso_previa`: al conciliar la cartola pisa la fecha/ref del egreso, y desconciliar RESTAURA lo que el operador tenía) |
| `conc_conciliacion_ingreso` | enlace abono ↔ `cont_cobranza` **o** `cont_adelanto` (exactamente uno: CHECK + UNIQUE por destino) |
| `cont_adelanto` | adelanto de cliente por OC (vive en `models/models.py`; estados `informado → aprobado → anulado`) |

Crear tablas faltantes y migrar columnas aditivas (`es_anticipo`, `anticipo_factura_id`,
`adelanto_id`, `cobranza_id` nullable, `fecha_egreso_previa`/`referencia_egreso_previa`):
`python -m tesoreria.init_db` (desde `backend/`, idempotente — correr también en
producción al desplegar, ANTES de reiniciar el backend).

## Integridad entre módulos

- Compras rechaza borrar/revertir un egreso **conciliado** (409: desconciliar primero).
- Facturas y Cobranzas rechaza borrar una cobranza **conciliada** (409: desconciliar
  primero en Tesorería) — guard en `routers/contabilidad.py::eliminar_cobranza`.
- Un adelanto **aplicado** no se puede re-aprobar ni anular (revertir su cobranza
  `medio='adelanto'` primero); uno **conciliado**, tampoco (desconciliar primero).
- Revertir una cobranza `medio='adelanto'` devuelve el monto a `cont_adelanto.monto_aplicado`
  (INVARIANTE: `monto_aplicado == Σ` cobranzas `'adelanto'` de ese adelanto, vía
  `cont_cobranza.adelanto_id`).
- Todo el módulo está candado a Grupo AM: `require_empresa("mineria")` + filtro
  `empresa` en cada query.

## Adelantos: las dos vías (resumen)

- **Vía A (sin factura de anticipo)**: informar → aprobar → conciliar abono↔adelanto →
  al emitir la factura del despacho real, cobranza automática `medio='adelanto'`
  (cap por saldo; el remanente pasa a la siguiente factura).
- **Vía B (con factura de anticipo)**: en Facturas y Cobranzas se emite una **factura de
  anticipo** (`es_anticipo=1`, ÚNICA excepción a "solo se factura guía firmada") ligada
  al adelanto (`cont_adelanto.factura_anticipo_id`). La cobranza `'adelanto'` cae en ESA
  factura (queda pagada al aprobar) y las facturas del despacho real llevan **línea de
  descuento negativa** que referencia su folio (`cont_factura_cliente_item.anticipo_factura_id`).
  Invariante: Σ brutos de las facturas de la OC == total de la venta.

Tests: `tests_contabilidad/test_adelantos.py` (vía A + Tesorería + conciliación) y
`tests_contabilidad/test_factura_anticipo.py` (vía B + descuento).
