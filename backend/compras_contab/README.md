# Compras / Cuentas por Pagar (AP) — Backend

Módulo **aislado y aditivo**: NO modifica ningún archivo existente. Espeja el lado
de Cuentas por Cobrar (`routers/contabilidad.py` + tablas `cont_*`). Es el lugar
para **registrar todas las compras y gastos** del día a día y llevar su estado de
pago, dejando preparado el terreno para la conciliación bancaria.

## Archivos

| Archivo | Rol |
|---|---|
| `models.py` | Tablas `cont_plan_cuenta`, `cont_compra`, `cont_egreso`, `cont_egreso_detalle`, `cont_compra_item` (sobre el `Base` compartido) |
| `schemas.py` | Schemas Pydantic (`CompraCreate`, `PagoIn`, `PagoInline`, `EgresoCreate`, `EgresoUpdate`, `AnularIn`) |
| `service.py` | Helpers + `_recompute_compra` / `_serialize_compra` / `serialize_egreso` (copiados del molde AR) |
| `router.py` | `APIRouter(prefix="/compras-contab")` con los endpoints |
| `import_plan_cuentas.py` | importa el plan de cuentas NIIF desde el Excel del dueño (UPSERT) |
| `init_db.py` | Crea las tablas del módulo en aislamiento (no depende de main.py) |

## 1) Crear las tablas (sin cablear nada todavía)

Desde `backend/` (con el venv del proyecto):

```bash
./venv/bin/python -m compras_contab.init_db
```

Crea **solo** las tablas del módulo (`cont_plan_cuenta`, `cont_compra`, `cont_egreso`,
`cont_egreso_detalle`, `cont_compra_item`, `checkfirst=True`); no toca tablas existentes.
Además migra —de forma ADITIVA— la columna nueva `cont_compra.oc_proveedor_id` (FK suave
a la OC-Proveedor, pista de cabecera de la compra nacional) con su índice y su FK
`ON DELETE SET NULL`. Idempotente: se puede correr varias veces (el índice/FK se detectan
por COLUMNA, no por nombre, para no duplicarlos en una BD fresca creada por `create_all`).
El plan de cuentas se carga aparte con `python -m compras_contab.import_plan_cuentas`.

## 2) Activar la API (cuando se decida; toca main.py)

`backend/main.py` — agregar el import junto al de `embarques_pricing` (~línea 28):

```python
from compras_contab.router import router as compras_contab_router
```

y el montaje junto a los demás `include_router` (~línea 64):

```python
app.include_router(compras_contab_router, prefix="/api")  # /api/compras-contab
```

Las tablas también se crean solas con el `Base.metadata.create_all()` de arranque
una vez que el módulo queda importado (igual que `embarques_pricing`).

## Endpoints (`/api/compras-contab`)

Todas las lecturas y mutaciones operan en el **scope de la empresa** del usuario
(multi-empresa real); el listado y los KPIs/antigüedad se **filtran y agregan en SQL**
(paginado, sin cargar toda la tabla a memoria, Decimal exacto).

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `` | listado **paginado** (`page`, `page_size`) + filtros (`tipo`, `estado_pago`, `categoria`, `periodo`, `q`, `proveedor_id`, `incluir_anulados`) + antigüedad de cartera. Devuelve `{compras, total, page, page_size, antiguedad}` |
| POST | `` | registrar compra (contado → pago automático por el total; o `pago` inline) |
| GET | `/kpis` | comprado / pagado / por pagar / vencido / por tipo (agregado en SQL) |
| GET | `/catalogos` | tipos de gasto, estados, categorías sugeridas, medios de pago, `iva_rate`, proveedores |
| GET | `/costos-embarque` | **solo lectura**: gastos de embarque desde Embarques Pricing |
| GET | `/oc-nacionales` | OCs `tipo_origen='nacional'` y sus ítems costeables (cantidad, recibido, ya costeado, `disponible_costear`) — fuente del detalle por ítem del front |
| GET | `/{id}` | detalle de una compra |
| POST | `/{id}/pagos` | pagar UNA compra (crea un egreso de 1 detalle, parcial/total) |
| PATCH | `/{id}/pagos/{pago_id}` | editar fecha en el banco / referencia del egreso de ese pago |
| DELETE | `/{id}/pagos/{pago_id}` | revertir el egreso al que pertenece ese pago |
| POST | `/{id}/anular` | anular (soft; rechaza si tiene pagos) |
| DELETE | `/{id}` | borrado seguro (solo si no tiene pagos) |
| GET / POST | `/egresos` | listar / crear Comprobante de Egreso **consolidado** (paga 1..N compras) |
| PATCH / DELETE | `/egresos/{id}` | editar (fecha banco/ref) / revertir un egreso completo |
| GET | `/catalogos` | tipos, categorías, medios, **plan de cuentas imputable** + default por tipo, proveedores |

## Tests

Suite versionada en `compras_contab/tests/` (auto-ejecutable, sin pytest):

```bash
./venv/bin/python compras_contab/tests/test_service.py       # unitarios puros
./venv/bin/python compras_contab/tests/test_integration.py   # integración (DB local, limpia lo que crea)
```

## Modelo de datos

- `cont_plan_cuenta`: plan de cuentas NIIF (catálogo importado del Excel, ver `import_plan_cuentas.py`).
- `cont_compra`: clasificación (`origen`, `tipo_gasto`, `categoria`), **imputación contable**
  (`cuenta_contable_id` → cont_plan_cuenta, `es_anticipo`), acreedor
  (`proveedor_id`/`acreedor`/`proveedor_rut`), documento, montos
  (`moneda`/`tc`/`monto_neto`/`iva`/`monto_total`/`monto_total_clp`), condición de pago,
  vencimiento, estado **derivado de los egresos** (`estado_pago`/`monto_pagado_clp`/`saldo_clp`),
  anulación, y punteros suaves a embarque (`embarque_id`, `emb_pricing_gasto_id`, `factura_proveedor_id`)
  y a la OC-Proveedor nacional (`oc_proveedor_id`, FK suave `ON DELETE SET NULL`; solo pista/filtro
  de cabecera — el costo real por ítem vive en `cont_compra_item`).
- `cont_compra_item`: **costo por ítem de una compra NACIONAL** (la factura ES el costo; sin
  flete-por-peso ni gastos-por-CIF — espejo conceptual de `emb_pricing_item`, SIN prorrateo).
  Liga cada línea de la factura al repuesto vendido (`item_cotizacion_id`, clave de costeo) con
  `cantidad`, `precio_unit` (moneda factura), `costo_unit_clp` y `costo_total_clp` (= NETO de la
  línea × `compra.tc`). El **IVA es crédito fiscal recuperable → NO capitaliza** (distinto del
  `iva_importacion` internacional). `CASCADE` al borrar la compra.
- `cont_egreso`: Comprobante de Egreso = UNA salida real de dinero (banco/medio/N°op/tc/moneda/
  monto_total_clp) que se concilia con el banco (`conciliado`/`fecha_mov_bancario`/`referencia_bancaria`).
- `cont_egreso_detalle`: asignación egreso → compra (`monto_clp`, `tc_aplicado`, `monto_origen`).
  Un egreso paga 1..N compras (caso "un movimiento paga varios gastos").

## Compra NACIONAL con costo por ítem (Fase 1)

Un proveedor chileno llega con su camión, su **guía de despacho** y su **factura** (pago
contado o crédito 30/60), sin embarque de por medio. La factura ES el costo de esos
repuestos. Diseño consolidado completo: `docs/plan-compras-nacionales-2026-07-18.md`
(§4 = flujo pantalla por pantalla). Piezas que aporta esta fase:

- **`OcProveedor.tipo_origen`** (`'internacional'` | `'nacional'`) — fuente única del origen;
  la crea `recepcion_nacional/init_db`. El toggle se elige al **crear la OC** (Panel Compras).
- **Recepción nacional** (módulo aislado `recepcion_nacional/`): Bodega registra "cuánto llegó"
  por ítem; al cerrar, alimenta el **tope físico** de Despachos (no se puede despachar/facturar
  más de lo recibido). Ver `backend/recepcion_nacional/README.md`.
- **`cont_compra_item`**: costo por ítem = **NETO en CLP** (`origen='NACIONAL'`).
- **Cuenta por defecto** `('NACIONAL','cogs')` → **`1.3.01` Existencias (activo)**, no gasto (se
  capitaliza como inventario; el COGS es reporte al vender, no un asiento).
- **Guards del alta de `cont_compra_item`** (backend = autoridad):
  - **A** — ítem con costo internacional (`emb_pricing_item`) NO puede costearse nacional → **409**
    (y viceversa, vía el candado en `recepcion_nacional`/`emb`), anti **doble costeo**.
  - **B/C** — Σ cantidad costeada por ítem (compras activas + esta) ≤ **recibido nacional** → **409**.
    Serializado con `SELECT ... FOR UPDATE` sobre las filas `ItemCotizacion` + relectura con lock
    (patrón despachos/pagos) → dos costeos concurrentes del mismo ítem no sobre-costean.
  - **D** — Σ líneas costeadas (CLP) ≤ **neto CLP** de la factura (cobertura parcial permitida) → **400**.
  - **E** — cada ítem costeado **pertenece** a la OC-Proveedor referenciada → **400**.
  - Anular la compra **libera** su costeo (las líneas van con la compra).
- **CxP / Tesorería (ya existe, se reusa):** `cont_compra` nacional → **/por-pagar** de Tesorería
  (contado = egreso automático el mismo día; crédito = `fecha_vencimiento`) → **pago/egreso**
  (`_crear_egreso`, locks anti doble-pago) → **conciliación** cargo↔egreso. CLP → `tc=1`, sin NIC 21.

### Checklist de deploy (Fase 1) — correr ANTES de reiniciar en prod

Desde `backend/` con el venv del proyecto, en este orden (patrón `tesoreria`/`embarques_pricing`):

```bash
./venv/bin/python -m recepcion_nacional.init_db   # 1) columna OcProveedor.tipo_origen + 2 tablas
./venv/bin/python -m compras_contab.init_db        # 2) tabla cont_compra_item + FK cont_compra.oc_proveedor_id
```

Ambos son **idempotentes** (re-ejecutables sin efecto). Recién después, reiniciar el backend.
Fuera de alcance de Fase 1 (diferido por el dueño): módulo de rentabilidad, devoluciones/NC del
proveedor, costo estimado antes de la factura, flete local prorrateado, compra nacional en moneda ≠ CLP.

## Fuera de alcance (fases futuras)

- Multi-imputación (repartir un gasto en varias cuentas), campos/conciliación SII (RCV).
- Asientos automáticos (libro diario/mayor), Estado de Flujos NIC 7, banco-puente 1.1.05.
- FIFO de divisas USD a proveedores SWIFT (hoy `ControlTCPage` es solo una maqueta).
- (La conciliación bancaria existe en el módulo `conciliacion_bancaria`, que cruza los egresos con la cartola.)

## Cómo deshacer

Quitar las 2 líneas de `main.py` (si se cablearon) y borrar la carpeta
`backend/compras_contab/`. Para eliminar las tablas (opcional):

```sql
DROP TABLE cont_egreso_detalle; DROP TABLE cont_egreso; DROP TABLE cont_compra_item; DROP TABLE cont_compra; DROP TABLE cont_plan_cuenta;
```
