# Compras / Cuentas por Pagar — MonzaParts (AP, NIIF/NIC 7)

Módulo **aislado** y **solo para MonzaParts** (`empresa = "automotriz"`). Es el espejo del
módulo de Grupo AM (`backend/compras_contab/`) sobre tablas `monza_cont_*` propias:
registrar compras/gastos del día a día, imputarlas a una **cuenta contable NIIF**, llevar
su **condición y estado de pago**, y pagarlas vía **Comprobantes de Egreso** (una salida
real de dinero que puede pagar varias compras — el flujo de salida **NIC 7** y la unidad
que luego se concilia con el banco).

## Reglas de negocio clave

- **Pagar o no al crear**: cada movimiento se registra `contado` (genera automáticamente
  un egreso por el total ese día, o el pago inline que se indique) o `credito` (queda
  como cuenta por pagar con vencimiento = fecha + plazo).
- **Tipo de gasto**: `cogs` (costo de venta) / `gasto_operacional` / `gasto_no_operacional`
  / `otros`. Cada (origen, tipo) sugiere una **cuenta NIIF por defecto** (el IVA va
  automático a crédito fiscal): EMBARQUE+cogs → `1.3.02` mercadería en tránsito (NIC 2),
  MANUAL+cogs → `1.3.01`, gasto operacional → `6.2.04`, etc.
- **Costos de embarque automáticos**: los gastos se anotan UNA sola vez en
  **Embarques Pricing** (`monza_emb_pricing_gasto`) y acá se ven **reflejados en vivo**
  (overlay solo lectura `/costos-embarque`). Desde ahí se registran como compra pagable
  con 1 clic (`origen=EMBARQUE` + `emb_pricing_gasto_id`); el sistema **impide duplicar**
  el mismo gasto (409) y marca en el overlay cuáles ya están registrados.
- **Estado de pago derivado**: `pendiente | parcial | pagado | vencido | anulado` se
  recalcula SIEMPRE desde las asignaciones de egreso reales (nunca se edita a mano).
- **Anulación soft**: una compra con pagos no se puede anular (revertir pagos primero);
  tras anular se puede re-registrar el mismo N° de documento (unicidad solo entre activas,
  vía columna generada `numero_documento_activo`).
- **Moneda extranjera (NIC 21)**: compras en USD/EUR llevan TC obligatorio; el egreso
  guarda tc/monto en la moneda del pago para la diferencia de cambio.
- **Compra NACIONAL con costeo por ítem** (Fase 8, espejo GA): una compra con
  `origen=NACIONAL` + `oc_proveedor_id` + `items[]` guarda el costo POR ÍTEM en
  `monza_cont_compra_item` — **la factura ES el costo** (NETO de la línea en CLP; el IVA
  es crédito fiscal recuperable → NO capitaliza, distinto del iva_importacion
  internacional). Cuenta default NACIONAL+cogs → `1.3.01` Existencias (NIC 2). Guards
  bajo lock canónico (`MonzaCotizacionItem` id ASC + `populate_existing().with_for_update()`,
  lecciones G13): **A)** ítem con costo internacional (`monza_emb_pricing_item`) → 409;
  **B/C)** Σ cantidad costeada en compras ACTIVAS ≤ recibido nacional utilizable
  (recepciones CERRADAS de `monza_recepcion_nacional`) con `TOL_QTY=0.001` en UNIDADES
  → 409 "registre primero la recepción"; **D)** Σ líneas ≤ neto CLP (+1 CLP; cobertura
  parcial LEGAL) → 400; **E)** pertenencia ítem↔OC vía
  `MonzaCotizacionItem.oc_proveedor_id` directo (adaptación Monza: sin tabla
  `OcProveedorItem` ni `oc_proveedor_item_id`) → 400. Crear compra reintenta deadlocks
  1213/1205 ×3 (gap locks de InnoDB deadlockean incluso ítems distintos). Anular la
  compra LIBERA el cupo costeado (los guards filtran `anulado=False`).

## Datos (5 tablas nuevas, aditivas)

| Tabla | Rol |
|---|---|
| `monza_cont_plan_cuenta` | Plan de cuentas NIIF propio (importado del Excel del dueño; metadatos NIC 7 `actividad_flujo`/`efectivo_equiv` y NIC 21 `monetaria`). |
| `monza_cont_compra` | 1 fila por compra/gasto (clasificación, cuenta imputada, montos congelados, condición y estado de pago derivado). |
| `monza_cont_egreso` | Comprobante de Egreso: UNA salida real de dinero (con campos de conciliación bancaria para el futuro módulo Monza). |
| `monza_cont_egreso_detalle` | Asignación egreso → compra (cuánto pagó a cada una). |
| `monza_cont_compra_item` | Costo por ítem de la compra NACIONAL (snapshot n° parte/descr., cantidad, precio_unit, costo_unit/total CLP). Sin `oc_proveedor_item_id` (adaptación Monza). |

No tocan ninguna tabla existente. Dinero en `Numeric`. InnoDB explícito (locks).
Punteros suaves: `proveedor_id → monza_proveedores`, `embarque_id → monza_embarques`,
`emb_pricing_gasto_id → monza_emb_pricing_gasto`, `oc_proveedor_id → monza_oc_proveedor`
(todos `SET NULL`; la columna `oc_proveedor_id` en BD viva la crea `init_db`).

## API — `prefix /api/monza/compras-contab` (candado `automotriz`)

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | Listado paginado + filtros (tipo/estado/categoría/período/q/proveedor) + antigüedad 0-30/31-60/61-90/91+. |
| POST | `/` | Crear compra. `contado` → egreso automático; `pago` inline opcional; valida TC, duplicados (409), punteros a embarque. |
| GET | `/kpis` | Total comprado / pagado / por pagar / vencido / por tipo de gasto. |
| GET | `/catalogos` | Tipos de gasto, categorías, medios, proveedores (monza_proveedores), plan de cuentas imputable, cuenta default por tipo. |
| GET | `/costos-embarque` | **Overlay en vivo** de los gastos de Embarques Pricing + marca de cuáles ya son compra (`compra_id`). |
| GET | `/oc-nacionales` | Catálogo de OC `tipo_origen='nacional'` con sus ítems costeables: `cantidad`, `recibido`, `ya_costeado`, `disponible_costear = max(min(recibido, cantidad) − ya_costeado, 0)`. Alimenta el modo "compra nacional por ítem" del front. |
| GET/POST | `/egresos` | Listar / crear egreso CONSOLIDADO (una salida paga varias compras). |
| PATCH/DELETE | `/egresos/{id}` | Completar fecha banco/referencia · revertir egreso completo (rechaza si conciliado). |
| GET | `/{id}` | Detalle de la compra con sus pagos. |
| POST | `/{id}/pagos` | Pagar UNA compra (parcial o total; valida sobre-pago con lock de fila). |
| PATCH/DELETE | `/{id}/pagos/{pago_id}` | Editar fecha banco · revertir el egreso al que pertenece el pago. |
| POST | `/{id}/anular` | Anulación soft (motivo); rechaza si tiene pagos. |
| DELETE | `/{id}` | Borrado duro solo sin pagos. |

## Puesta en marcha (una vez por entorno)

**ORDEN DE DEPLOY (Fase 8)**: correr PRIMERO `monza_recepcion_nacional/init_db` (crea
las tablas de recepción + la columna `monza_oc_proveedor.tipo_origen`) y DESPUÉS este
init_db, AMBOS antes de reiniciar el backend.

```bash
cd backend
python -m monza_recepcion_nacional.init_db          # 1° — recepción nacional + tipo_origen
python -m monza_compras_contab.init_db              # 2° — crea las 5 tablas + migra oc_proveedor_id (idempotente)
python -m monza_compras_contab.import_plan_cuentas  # importa el plan NIIF del Excel (upsert)
```

`main.py` ya monta el router (create_all también crea las tablas al levantar; la
columna `oc_proveedor_id` en una BD ya poblada SOLO la crea el init_db — create_all no
altera tablas existentes).

## Tests

```bash
cd backend
./venv/bin/python monza_compras_contab/tests/test_service.py       # lógica pura (semáforo, estados, cuentas default)
./venv/bin/python monza_compras_contab/tests/test_integration.py   # API + BD + overlay embarques + candado
./venv/bin/python monza_compras_contab/tests/test_nacional.py      # costeo por ítem nacional + guards + circuito Tesorería + carreras G13
```

La integración ejerce: contado auto-egreso, crédito/pagos/sobre-pago/revertir, unicidad y
re-registro tras anular, USD/TC, validaciones 422, consolidado (pagar 2 compras con 1
egreso y revertirlo), overlay de embarques (reflejo automático → registrar como compra →
dedup 409 → marca `compra_id`), KPIs y candado mineria→403. Limpia todo al terminar.

## Diferencias con Grupo AM

| | Grupo AM | MonzaParts |
|---|---|---|
| Tablas | `cont_*` con columna `empresa` | `monza_cont_*` propias (separación POR TABLA, criterio Monza) |
| Proveedores | `proveedores` | `monza_proveedores` (catálogo de Abastecimiento) |
| Embarques | `emb_pricing_gasto` / `embarques` | `monza_emb_pricing_gasto` / `monza_embarques` |
| Proveedor del embarque | vía EmbarqueItem → OcProveedor | vía MonzaEmbarqueItem → MonzaCotizacionItem → MonzaOcProveedor |
| Plan de cuentas | `cont_plan_cuenta` (empresa=mineria) | `monza_cont_plan_cuenta` (mismo Excel NIIF, tabla propia) |
| Extra Monza | — | overlay marca gastos ya registrados (`compra_id`) + dedup 409 por `emb_pricing_gasto_id` |

La lógica de negocio (estados, egresos, NIC 7/21, unicidad, KPIs) es idéntica.
