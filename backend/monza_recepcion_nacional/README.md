# Recepción Nacional MonzaParts — Backend

Módulo **aislado y aditivo** para el **camino físico de la compra nacional** de
MonzaParts (espejo 1:1 de `backend/recepcion_nacional/` de Grupo AM): cuando un
proveedor chileno llega con su **camión y su guía de despacho**, Bodega registra
"cuánto llegó" por ítem. Ese acumulado por ítem alimenta el **tope físico** de
Despachos (regla de oro F2/G6): no se puede despachar/facturar más de lo que el
proveedor entregó.

A diferencia del embarque consolidado, **NO clona líneas ni fuerza reclamos**: es un
simple libro de recepciones sucesivas. Entregas parciales = filas nuevas sobre el
remanente (el tope suma). El reclamo por faltante es una acción opcional, no
automática.

## Adaptación estructural Monza (NO copiable de GA)

Monza **no tiene tabla `OcProveedorItem`** — el vínculo ítem↔OC es directo vía
`MonzaCotizacionItem.oc_proveedor_id` (`monza_models.py`). Por eso:

- la **pertenencia** de un ítem a la OC se valida por esa columna,
- la tabla `monza_recepcion_nacional_item` **no lleva** `oc_proveedor_item_id`,
- el estado de línea es `estado_linea` (no `estado_item`).

## Punto de contacto con el código compartido (uno solo)

El único toque a código compartido es un **UNION aditivo y direccionalmente seguro**
en `monza_router_despachos.py::_qty_recibida_utilizable` (nuestro tope endurecido
F2, no el del programador): suma `Σ monza_recepcion_nacional_item.qty_recibida`
(recepción `cerrada`, estado utilizable) en el **mismo dict** por
`item_cotizacion_id`. Para un ítem nacional solo puede **bajar** el tope (de "todo
lo vendido" a `min(vendido, recibido)`); no toca los ítems internacionales (fuente
distinta — la disjunción la garantiza el guard anti-embarque).

## Archivos

| Archivo | Rol |
|---|---|
| `models.py` | Tablas `monza_recepcion_nacional`, `monza_recepcion_nacional_item` + constantes `RECEPCION_UTILIZABLE`/`ESTADOS_VALIDOS` (vocabulario VERBATIM del tope) |
| `service.py` | Helpers (`_f`, `parse_date_estricta`) + `serialize_recepcion` |
| `router.py` | `APIRouter(prefix="/api/monza/recepcion-nacional")` con candado `require_empresa("automotriz")` |
| `init_db.py` | Crea las 2 tablas + ALTER `monza_oc_proveedor.tipo_origen` (idempotente, no depende de main.py) |

## 1) Crear las tablas + la columna `tipo_origen`

Desde `backend/` (con el venv del proyecto):

```bash
./venv/bin/python -m monza_recepcion_nacional.init_db
```

Crea **solo** las tablas que falten (`checkfirst=True`) y agrega, de forma
idempotente, la columna `monza_oc_proveedor.tipo_origen` (`'internacional'` |
`'nacional'`, default `'internacional'`; el histórico queda internacional sin
migrar datos) + su índice. Re-ejecutable sin efecto.

> **Orden de deploy (Fase 8 Monza):** correr **este** `init_db` primero (crea
> `tipo_origen`) y luego `./venv/bin/python -m monza_compras_contab.init_db`
> (crea `monza_cont_compra_item` + FK `monza_cont_compra.oc_proveedor_id`),
> **AMBOS antes** de reiniciar el backend.

## 2) Activar la API (toca main.py — 2 líneas)

`backend/main.py` — agregar el import junto al de los demás routers monza:

```python
from monza_recepcion_nacional.router import router as monza_recep_nac_router
```

y el montaje junto a los demás `include_router` monza, **SIN prefix extra** (el
APIRouter ya trae `/api/monza/...`; OJO: GA en cambio monta con `prefix="/api"`):

```python
app.include_router(monza_recep_nac_router)  # /api/monza/recepcion-nacional
```

## Endpoints (`/api/monza/recepcion-nacional`)

Todos con candado `require_empresa("automotriz")` (403 a otra empresa). El backend
es la autoridad: valida pertenencia a la OC, que la OC sea nacional, y no confía en
el cliente para el tope.

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `` | registrar entrega (una entrega = una recepción); `cerrar=true` deja los utilizables `en_bodega` |
| POST | `/{id}/cerrar` | cerrar una recepción `abierta` (transiciona los utilizables a `en_bodega`) |
| DELETE | `/{id}` | anular: `abierta` → borra sin guardas; `cerrada` → 409 si hay despacho/costeo dependiente, si es segura revierte `en_bodega`→`comprado` y borra (CASCADE) |
| GET | `/pendientes/{ocp_id}` | ítems `comprado`/`en_bodega` de la OC nacional + remanente por recibir (fuente del modal) |
| GET | `` | listar recepciones (opcional `ocp_id`) |
| GET | `/{id}` | detalle de una recepción |

## Modelo de datos

- `monza_recepcion_nacional`: cabecera de la entrega (OC-Proveedor,
  `numero_guia_proveedor`, `fecha`, `estado` `abierta`/`cerrada`, `documento` guía
  escaneada, `fecha_cierre`).
- `monza_recepcion_nacional_item`: cuánto llegó de cada ítem (`item_cotizacion_id`,
  `qty_recibida`, `estado_recepcion` con el vocabulario del tope; snapshot
  `numero_parte`/`descripcion`). FKs SET NULL hacia Ventas/Compras (borrar allá no
  bloquea el histórico de bodega); CASCADE cabecera→líneas.

### Vocabulario de estados (VERBATIM del tope físico)

`RECEPCION_UTILIZABLE = (completo, danado_utilizable, sobrante, faltante)` — suman al
tope (lo que llegó bueno). `no_llego` y `danado_no_utilizable` aportan 0. Un
`sobrante` (recibí más que lo vendido) queda topeado a lo vendido por
`_tope_fisico` (`min(vendido, recibido)`).

## Guard anti-embarque (espejo del hallazgo del dueño en GA)

Un ítem asignado a una OC-Proveedor **nacional** no puede entrar al pipeline de
embarque por NINGÚN camino. Las **3 rutas HTTP** que llaman al helper
`_rechazar_items_nacionales` (`monza_router_abastecimiento.py`) y responden 400 son:

| Ruta | Call site |
|---|---|
| `POST /api/monza/abastecimiento/preparar` | `monza_router_abastecimiento.py` (vía legada, línea completa) |
| `POST /api/monza/abastecimiento/items/preparar-parcial` | `monza_router_abastecimiento.py` (con cantidades) |
| `POST /api/monza/logistica/embarques` | `monza_router_logistica.py` (crear embarque, completo o parcial) |

Monza no tiene más entradas al pipeline físico (no existe pre-embarque). Esa disjunción
es la que hace correcto el UNION del tope físico. Las 3 tienen sonda **de endpoint** (no
solo de función): `tests/test_deploy_parcial_y_guard_parcial.py` cubre `preparar-parcial`
y `tests/test_integration.py` (§13) las otras dos.

## Deploy a medias: la comprobación de costeo se apaga sola

`DELETE /{id}` consulta `monza_cont_compra_item` para no dejar Σ costeado > recibido. Esa
tabla la crea `monza_compras_contab.init_db`, y en MonzaParts **nunca se autocrea**
(`monza_compras_contab` se importa DENTRO del gate `MONZA_CONTAB_ENABLED`, o sea después
del `create_all`). Por eso `_costeo_por_item_disponible` pregunta si la tabla existe
**antes de tomar cualquier lock**: si MySQL responde 1146, ese módulo jamás costeó nada
que proteger y la comprobación se apaga en vez de devolver un 500. Va antes de los locks
a propósito — el `db.rollback()` que deja la sesión usable soltaría los locks del guard y
reabriría el write-skew que el guard existe para cerrar.

## Fuera de alcance (diferido por el dueño)

- Rentabilidad / costo por ítem unificada (el costo por ítem vive en
  `monza_compras_contab.monza_cont_compra_item`).
- Reclamos automáticos al proveedor nacional (acción opcional, no automática).
- Devoluciones / notas de crédito, flete local prorrateado, moneda ≠ CLP,
  embarque sintético.
- Notificación de "llegada" del PATCH de estado de la OC: sigue apuntando a bodega
  de embarques (no aplica al nacional; deuda menor anotada, no se toca sin el dueño).

## Cómo deshacer

Quitar las 2 líneas de `main.py`, revertir el UNION de
`monza_router_despachos.py` (`_qty_recibida_utilizable`) y borrar la carpeta
`backend/monza_recepcion_nacional/`. Para eliminar las tablas (opcional):

```sql
DROP TABLE monza_recepcion_nacional_item; DROP TABLE monza_recepcion_nacional;
ALTER TABLE monza_oc_proveedor DROP COLUMN tipo_origen;
```
