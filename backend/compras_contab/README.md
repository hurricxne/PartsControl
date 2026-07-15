# Compras / Cuentas por Pagar (AP) — Backend

Módulo **aislado y aditivo**: NO modifica ningún archivo existente. Espeja el lado
de Cuentas por Cobrar (`routers/contabilidad.py` + tablas `cont_*`). Es el lugar
para **registrar todas las compras y gastos** del día a día y llevar su estado de
pago, dejando preparado el terreno para la conciliación bancaria.

## Archivos

| Archivo | Rol |
|---|---|
| `models.py` | Tablas `cont_plan_cuenta`, `cont_compra`, `cont_egreso`, `cont_egreso_detalle` (sobre el `Base` compartido) |
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
`cont_egreso_detalle`, `checkfirst=True`); no toca tablas existentes. Idempotente: se
puede correr varias veces. El plan de cuentas se carga aparte con
`python -m compras_contab.import_plan_cuentas`.

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
  anulación, y punteros suaves a embarque (`embarque_id`, `emb_pricing_gasto_id`, `factura_proveedor_id`).
- `cont_egreso`: Comprobante de Egreso = UNA salida real de dinero (banco/medio/N°op/tc/moneda/
  monto_total_clp) que se concilia con el banco (`conciliado`/`fecha_mov_bancario`/`referencia_bancaria`).
- `cont_egreso_detalle`: asignación egreso → compra (`monto_clp`, `tc_aplicado`, `monto_origen`).
  Un egreso paga 1..N compras (caso "un movimiento paga varios gastos").

## Fuera de alcance (fases futuras)

- Multi-imputación (repartir un gasto en varias cuentas), campos/conciliación SII (RCV).
- Asientos automáticos (libro diario/mayor), Estado de Flujos NIC 7, banco-puente 1.1.05.
- FIFO de divisas USD a proveedores SWIFT (hoy `ControlTCPage` es solo una maqueta).
- (La conciliación bancaria existe en el módulo `conciliacion_bancaria`, que cruza los egresos con la cartola.)

## Cómo deshacer

Quitar las 2 líneas de `main.py` (si se cablearon) y borrar la carpeta
`backend/compras_contab/`. Para eliminar las tablas (opcional):

```sql
DROP TABLE cont_egreso_detalle; DROP TABLE cont_egreso; DROP TABLE cont_compra; DROP TABLE cont_plan_cuenta;
```
