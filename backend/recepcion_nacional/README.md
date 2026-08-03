# Recepción Nacional — Backend

Módulo **aislado y aditivo** para el **camino físico de la compra nacional**: cuando
un proveedor chileno llega con su **camión y su guía de despacho**, Bodega registra
"cuánto llegó" por ítem. Ese acumulado por ítem alimenta el **tope físico** de
Despachos (regla de oro G6): no se puede despachar/facturar más de lo que el
proveedor entregó.

A diferencia del embarque consolidado, **NO clona líneas ni fuerza reclamos**: es un
simple libro de recepciones sucesivas. Entregas parciales = filas nuevas sobre el
remanente (el tope suma). El reclamo por faltante es una acción opcional, no
automática.

## Punto de contacto con el código compartido (uno solo)

El único toque a código compartido es un **UNION aditivo y direccionalmente seguro**
en `routers/despachos.py::_qty_recibida_utilizable` (nuestro tope endurecido G6, no
el del programador): suma `Σ recepcion_nacional_item.qty_recibida` (recepción
`cerrada`, estado utilizable) en el **mismo dict** por `item_cotizacion_id`. Para un
ítem nacional solo puede **bajar** el tope (de "todo lo vendido" a
`min(vendido, recibido)`); no toca los ítems internacionales (fuente distinta).

## Archivos

| Archivo | Rol |
|---|---|
| `models.py` | Tablas `recepcion_nacional`, `recepcion_nacional_item` + constantes `RECEPCION_UTILIZABLE`/`ESTADOS_VALIDOS` (vocabulario VERBATIM del tope) |
| `service.py` | Helpers (`_f`, `parse_date_estricta`, `empresa_de`) + `serialize_recepcion` |
| `router.py` | `APIRouter(prefix="/recepcion-nacional")` con candado `require_empresa("mineria")` |
| `init_db.py` | Crea las 2 tablas + ALTER `oc_proveedor.tipo_origen` (idempotente, no depende de main.py) |

## 1) Crear las tablas + la columna `tipo_origen`

Desde `backend/` (con el venv del proyecto):

```bash
./venv/bin/python -m recepcion_nacional.init_db
```

Crea **solo** las 2 tablas del módulo (`checkfirst=True`) y agrega, de forma
idempotente, la columna `oc_proveedor.tipo_origen` (`'internacional'` | `'nacional'`,
default `'internacional'`; el histórico queda internacional sin migrar datos) + su
índice. Re-ejecutable sin efecto.

> **Orden de deploy (Fase 1):** correr **este** `init_db` primero (crea `tipo_origen`) y
> luego `python -m compras_contab.init_db` (crea `cont_compra_item` + FK
> `cont_compra.oc_proveedor_id`), **antes** de reiniciar el backend. Ver el checklist en
> `backend/compras_contab/README.md`.

## 2) Activar la API (toca main.py — 2 líneas)

`backend/main.py` — agregar el import junto al de los demás módulos:

```python
from recepcion_nacional.router import router as recepcion_nacional_router
```

y el montaje junto a los demás `include_router`:

```python
app.include_router(recepcion_nacional_router, prefix="/api")  # /api/recepcion-nacional
```

## Endpoints (`/api/recepcion-nacional`)

Todos con candado `require_empresa("mineria")` (403 a otra empresa). El backend es la
autoridad: valida pertenencia a la OC, que la OC sea nacional, y no confía en el
cliente para el tope.

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `` | registrar entrega (una entrega = una recepción); `cerrar=true` deja los utilizables `en_bodega` y avisa a Ventas (B6/B7) |
| POST | `/{id}/cerrar` | cerrar una recepción `abierta` (transiciona los utilizables a `en_bodega`) y avisa a Ventas (B6/B7) |
| DELETE | `/{id}` | anular: `abierta` → borra sin guardas; `cerrada` → 409 si hay despacho/costeo dependiente, si es segura revierte `en_bodega`→`comprado` y borra (CASCADE) |
| GET | `/pendientes/{ocp_id}` | ítems `comprado`/`en_bodega` de la OC nacional + remanente por recibir (fuente del modal) |
| GET | `` | listar recepciones (opcional `ocp_id`) |
| GET | `/{id}` | detalle de una recepción |

## Modelo de datos

- `recepcion_nacional`: cabecera de la entrega (OC-Proveedor, `numero_guia_proveedor`,
  `fecha`, `estado` `abierta`/`cerrada`, `documento` guía escaneada, `fecha_cierre`).
- `recepcion_nacional_item`: cuánto llegó de cada ítem (`item_cotizacion_id`,
  `qty_recibida`, `estado_recepcion` con el vocabulario del tope; snapshot
  `numero_parte`/`descripcion`). FKs SET NULL hacia Ventas/Compras (borrar allá no
  bloquea el histórico de bodega); CASCADE cabecera→líneas.

### Vocabulario de estados (VERBATIM del tope físico)

`RECEPCION_UTILIZABLE = (completo, danado_utilizable, sobrante, faltante)` — suman al
tope (lo que llegó bueno). `no_llego` y `danado_no_utilizable` aportan 0. Un
`sobrante` (recibí más que lo vendido) queda topeado a lo vendido por
`_tope_fisico` (`min(vendido, recibido)`).

## Aviso a Ventas al cerrar la entrega (2026-07-30)

Cerrar una entrega nacional avisa **«OC Cliente N lista para despacho»** (o «Plazo
crítico» si quedó parcial y el plazo está a ≤3 días) igual que la vía embarque. Antes
no: la OC quedaba lista y Ventas no se enteraba hasta el barrido de las 06:00 del día
siguiente (era **latencia**, no pérdida).

No se duplicaron las reglas: `_avisar_ventas_listas` (en `router.py`) llama a
`routers/bodega.py::_evaluar_ocs_cliente_por_items`, la variante **por `item_ids`** del
mismo `_notificar_ocs_cliente` que usa la recepción de embarque (la vía embarque sigue
entrando por `_evaluar_ocs_cliente(embarque_id, …)`, sin cambios). El aviso va siempre
**después del commit** y dentro de un `try/except` con `rollback`: un fallo de
notificación no puede tumbar —ni ensuciar la respuesta de— una recepción ya guardada.

## Deploy a medias: la comprobación de costeo se apaga sola

Anular una recepción `cerrada` pregunta si el ítem está costeado en una compra activa
(`cont_compra_item`, de `compras_contab`). Si ese `init_db` no se corrió, MySQL
devuelve **1146** (tabla inexistente) y antes eso era un **500** en la cara del
operador. Ahora `_costeo_por_item_disponible` lo pregunta al principio del endpoint,
**antes de tomar cualquier lock** (su `rollback` de rescate soltaría los locks si se
preguntara más abajo, reabriendo el write-skew que el guard cierra), y si la tabla no
existe la comprobación se apaga: sin costeo desplegado no hay nada que proteger.

## Fuera de alcance (diferido por el dueño)

- Rentabilidad / `costo_por_item` unificada (el costo por ítem vive en
  `compras_contab.cont_compra_item`).
- Reclamos automáticos al proveedor nacional (el reclamo por faltante es acción
  opcional, no automática).
- Devoluciones / notas de crédito, flete local prorrateado, moneda ≠ CLP, embarque
  sintético, port a Monza.

## Cómo deshacer

Quitar las 2 líneas de `main.py`, revertir el UNION de `routers/despachos.py`
(`_qty_recibida_utilizable`) y borrar la carpeta `backend/recepcion_nacional/`. Para
eliminar las tablas (opcional):

```sql
DROP TABLE recepcion_nacional_item; DROP TABLE recepcion_nacional;
ALTER TABLE oc_proveedor DROP COLUMN tipo_origen;
```

## Guard anti-embarque (2026-07-18, hallazgo del dueño probando en vivo)

Un ítem asignado a una OC-Proveedor **nacional** no puede entrar al pipeline de
embarque por NINGUNO de los **4** caminos, y son 4 (no 3): `POST
/compras/items/preparar` (`routers/compras.py:999`),
`POST /compras/items/preparar-parcial` (`:1021`), `POST /compras/pre-embarques`
—crear el pre-embarque ya con `item_ids`, el camino que se descubrió último
(`:1133`)— y `POST /compras/pre-embarques/{id}/items` (`:1308`). Los 4
lo rechazan con 400 y el mensaje *"Ítem(s) de compra NACIONAL no pasan por
embarque: … Regístrelos con 'Registrar entrega nacional' en Seguimiento"*
(helper `_rechazar_items_nacionales` en `routers/compras.py`, aditivo). Las 4
rutas tienen sonda de endpoint en `tests/test_integration.py` (sección 13). La UI
además no ofrece checkboxes de selección en los grupos nacionales de
Seguimiento (el botón global "Preparar seleccionados" era el camino que se
los llevaba). El backend es la autoridad: una llamada directa al API tampoco
puede colarlos.
