# Embarques Pricing — MonzaParts (costo landed)

Módulo **aislado** y **solo para MonzaParts** (`empresa = "automotriz"`). Es el espejo del
módulo de Grupo AM (`backend/embarques_pricing/`): misma metodología de **costo landed**
(shipping prorrateado por peso, gastos locales prorrateados por CIF), apuntando a las
tablas `monza_*`.

## Qué hace

Calcula, por cada ítem de un embarque, cuánto **cuesta puesto en bodega** (landed cost)
sumando FOB, flete y gastos de internación, todo llevado a CLP:

```
FOB Total (ME)  = cantidad × FOB unit                       (moneda extranjera: USD/EUR)
FOB CLP         = FOB Total × TC
Shipping CLP    = flete_total × (peso_i / Σ peso)           ← prorrateo por PESO
CIF CLP         = FOB CLP + Shipping CLP
Gastos Loc CLP  = total_gastos × (CIF_i / Σ CIF)            ← prorrateo por CIF
Costo Total CLP = CIF CLP + Gastos Loc CLP
Costo Unit CLP  = Costo Total / cantidad
```

`total_gastos` capitaliza los netos de **Desconsolidación, Almacenaje, Agencia de Aduana,
Arancel/Derechos y Otros**. **Excluye** el IVA y el **IVA Importación** (son recuperables,
no son costo).

## Integración con Logística (no invasiva)

- Lee los embarques que crea **Logística** (`monza_embarques` + `monza_embarque_items` →
  `monza_cotizacion_items`). No los modifica.
- El registro de pricing se crea **diferido** la primera vez que Contabilidad abre el
  embarque (idempotente, a prueba de carreras por el `UNIQUE(embarque_id)`).
- **FOB y peso por ítem**: el _default_ sale del ítem de cotización (`costo` / `peso_kg`) y
  **los dos son editables a mano** (igual que en Grupo AM). El dueño sube el FOB real, el
  flete y los gastos.

### FOB real del proveedor (¿el costo está calculado con lo que pagaste?)

Monza **no tiene tabla de facturas de proveedor** (a diferencia de Grupo AM, que las lee de
`FacturaProveedorItem`). Por eso el **FOB real entra acá**, al costear el embarque: no hay
de dónde leerlo automáticamente. El default es el **costo estimado** de la cotización
(`MonzaCotizacionItem.costo`), que **nunca se toca** desde este módulo — ese costo es la
base del precio de venta (`costo × markup`) y pisarlo movería el precio de una venta ya
cerrada.

Lo que se agrega es **saber de dónde salió el número tecleado**, con la marca
`fob_es_factura` del payload:

| `fob_origen` | Qué significa | De dónde sale |
|---|---|---|
| `factura` | Precio **real** de la factura del proveedor | el usuario lo cargó y marcó "de factura" |
| `manual` | Corrección a mano (sin factura que la respalde) | el usuario lo cargó sin marcar |
| `cotizacion` | Costo **estimado** del ítem de cotización | default, `costo > 0` |
| `auto` | Sin dato (la cotización no trae costo y nadie cargó el FOB) | default, `costo = 0` |

**La precedencia no cambia**: `factura` y `manual` valen exactamente lo mismo frente al
costo de la cotización (los dos son "el humano tecleó un número") y ambos requieren
**valor > 0** para pisar el default — un override en 0 no bloquea el costo de la cotización
que llegue o cambie después. La única diferencia es informativa, y es la que importa para
el dueño: dice si el costo landed está calculado con **lo que pagó de verdad** o todavía
con el estimado.

En pantalla, bajo el FOB de cada ítem hay una casilla **"de factura"** y la etiqueta de
estado (`de factura` / `de cotización` / `manual` / `sin dato`), más un atajo
**"Marcar los FOB cargados como «de factura»"** para el caso real (llegó la factura y todos
los números tecleados son los suyos). Al **cerrar**, el origen queda **congelado** en el
snapshot junto al costo.

### Peso editable (y por qué importa)

El **peso gobierna el prorrateo del flete**, así que un peso mal cargado en la cotización
deforma el costo landed de **todos** los ítems del embarque. Por eso Contabilidad puede
corregirlo: al guardar, el flete se **re-prorratea** y la **Σ shipping queda intacta** (no
se pierde ni se inventa plata). En pantalla la celda "Peso kg" es editable y muestra
`de cotización` / `manual`, con un botón para volver al peso de la cotización.

FOB y peso son overrides **independientes** que comparten la misma fila
`monza_emb_pricing_item`. Los flags del payload son **tri-estado**:

| `fob_manual` / `peso_manual` | Significado |
|---|---|
| `true` (+ valor) | fijar el valor a mano (`fob_origen` = `factura`\|`manual`, `peso_origen` = `manual`) |
| `false` | quitar el override → volver al dato de la cotización (`auto`) |
| ausente / `null` | **no tocar ese campo** (el usuario no lo editó) |

El tri-estado no es un detalle: sin él, editar **solo el peso** revertiría en silencio un
FOB manual ya guardado (y al revés) — y, ahora, degradaría un FOB marcado como **"de
factura"** a simple corrección a mano. Un `peso_manual=true` con valor `0` se ignora (una
pieza física pesa > 0) y cae al peso de la cotización; un peso negativo se rechaza con 422.

`fob_es_factura` viaja **siempre junto al valor** (solo se lee cuando `fob_manual=true`).
Si viene ausente, el origen queda en `manual`: es el comportamiento histórico y el
conservador — nunca se le atribuye a una factura un número que nadie marcó como tal. El
reseteo (`fob_manual=false`) suelta el override sea `manual` **o** `factura`.

### Aviso de moneda mezclada (defensivo, no bloquea)

El pricing maneja **una sola moneda y un solo TC para todo el embarque**
(`monza_emb_pricing.moneda`, sembrada con la del **primer** ítem en
`integration.moneda_de_embarque`). El dueño confirmó que un embarque **siempre** viaja en
una sola moneda, aunque consolide varios proveedores (Fastmark), así que hoy esto no hace
daño. Por si algún día llega mezclado, el detalle devuelve `advertencias: []` con un aviso
en lenguaje del dueño, que la pantalla muestra en una banda ámbar.

Es **aviso y no `409`** a propósito: el embarque ya llegó físicamente y este módulo es el
único lugar donde se registra su costo — bloquear el guardado dejaría mercadería recibida
**sin costear**, que es peor que costearla avisando. El aviso se mantiene visible después
de guardar y con el pricing cerrado.

> **Deuda conocida (afecta a las DOS marcas).** El defecto de raíz sigue en pie: la moneda
> del embarque se toma del primer ítem y se aplica a todos, y Grupo AM tiene exactamente el
> mismo patrón (un único `tc_header` para todos los ítems en `embarques_pricing/router.py`).
> Arreglarlo de raíz sería moneda + TC **por ítem**, que cambia el modelo de datos y el
> cálculo en las dos marcas. Se deja documentado y **visible**, no arreglado.

## Datos (3 tablas nuevas, aditivas)

| Tabla | Rol |
|---|---|
| `monza_emb_pricing` | 1 fila por embarque: TC, flete, estado (`borrador`/`calculado`/`cerrado`). |
| `monza_emb_pricing_gasto` | 6 líneas canónicas de **GASTOS LOCALES** (neto, IVA, factura, banco). |
| `monza_emb_pricing_item` | **Snapshot** del costo landed por ítem (se congela al calcular/cerrar → auditable). Guarda también el origen del FOB y del peso (`fob_origen` / `peso_origen`), que es lo que hace persistente el override. |

No tocan ninguna tabla existente. Dinero en `Numeric` (decimal exacto).
`monza_emb_pricing.embarque_id` tiene FK con **ON DELETE CASCADE** a `monza_embarques`.

## API — `prefix /api/monza/embarques-pricing` (candado `automotriz`)

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | Lista todos los embarques con estado y costo total de su pricing. `?q=` filtra por número/forwarder/AWB. |
| GET | `/{embarque_id}` | Detalle; **auto-crea** el pricing + 6 gastos si falta. Devuelve encabezado, gastos, ítems calculados, totales y `advertencias`. |
| PUT | `/{embarque_id}` | Guarda encabezado + gastos (6 canónicas) + overrides de **FOB y/o peso** manual (con `fob_es_factura` para marcar el FOB real del proveedor); recalcula y persiste el snapshot. `409` si está cerrado; `400` si un `embarque_item_id` no pertenece a este embarque. |
| POST | `/{embarque_id}/cerrar` | Congela el pricing (`cerrado`). Exige TC > 0 y costo landed > 0. |
| POST | `/{embarque_id}/reabrir` | Vuelve a `calculado`/`borrador` para volver a editar. |

Reglas tributarias: **Arancel** e **IVA Importación** quedan siempre con IVA 0 (el arancel
no lleva IVA aparte; el IVA Importación es el IVA mismo). El backend reescribe siempre las
**6 líneas canónicas** de gasto en cada guardado (es la autoridad del esquema).

Robustez del cálculo (no se "pierde" plata): si Σ pesos = 0 el flete se prorratea por FOB;
si tampoco hay FOB, en partes iguales. Si Σ CIF = 0, los gastos se reparten en partes
iguales. La suma de los prorrateos siempre da el total.

**Las 3 rutas de ESCRITURA (PUT / cerrar / reabrir) releen la cabecera del pricing con
`populate_existing().with_for_update()`** y reintentan deadlock/lock-timeout (1213/1205),
que es la regla de la casa para toda decisión de plata: el costo landed que se congela ES
plata. Sin el lock, dos `POST /cerrar` simultáneos leían los dos `estado != 'cerrado'`,
recalculaban los dos y el segundo PISABA el snapshot del primero — dos costos distintos
congelados para el mismo embarque, sin rastro. Con el lock el segundo espera, relee
`cerrado` y recibe el 409 de siempre. Espejo del módulo de Grupo AM.

## Puesta en marcha

```bash
cd backend
python -m monza_embarques_pricing.init_db      # crea las 3 tablas + migra columnas (idempotente)
```

`main.py` ya importa y monta el router (antes del `create_all`), así que en local las
tablas también se crean al levantar el backend.

⚠️ **En un entorno donde las tablas YA existen** (producción, o local con datos), `create_all`
**no altera** tablas existentes: hay que correr `init_db` **antes de reiniciar** el backend
para que agregue las columnas aditivas. Sin eso el módulo revienta con `Unknown column`.
El script es idempotente (avisa y no hace nada si la columna ya está). Columnas que migra:

| Columna | Para qué |
|---|---|
| `monza_emb_pricing_item.peso_origen` | origen del peso del prorrateo (`auto` / `manual`) |
| `monza_config.desconsolidado_clp` | gasto local por defecto: desconsolidación |
| `monza_config.bodegaje_clp` | gasto local por defecto: almacenaje |
| `monza_config.costo_agencia_minimo_clp` | gasto local por defecto: agencia de aduana |

Las 3 de `monza_config` nacen en **0**, así que hasta que el contador las cargue el
comportamiento es idéntico al de antes (las 6 líneas de gastos en 0). Con ellas cargadas,
cada pricing nuevo nace con los gastos de internación precargados (más su IVA en las 3
afectas) en vez de en 0 — el agujero era que `cerrar_pricing` CONGELA un landed sin gastos
con solo exigir `costo_total > 0`.

✅ El **FOB real del proveedor** (`fob_origen='factura'`) **no necesita migración**:
`monza_emb_pricing_item.fob_origen` ya existe desde el día 1 y es `VARCHAR(20)`, así que
solo gana un valor nuevo. No se creó ninguna columna, tabla ni endpoint.

## Tests

```bash
cd backend
./venv/bin/python monza_embarques_pricing/tests/test_service.py        # matemática pura
./venv/bin/python monza_embarques_pricing/tests/test_integration.py    # API + BD + candado
./venv/bin/python monza_embarques_pricing/tests/test_paridad_pricing.py  # seed de gastos + lock
```

`test_paridad_pricing.py` cubre la **precarga de gastos desde MonzaConfig** y el **lock del
pricing** con sus tres sondas de poder discriminante dentro de la suite: re-inyecta el seed
viejo (las 6 en 0), fuerza `bloquear=False` y quita `populate_existing()` — en los tres
casos los dos cierres concurrentes pasan, que es la carrera que el arreglo cierra.

`test_integration.py` siembra 5 embarques, ejerce listado/detalle/guardar/cerrar/reabrir,
verifica el **cuadre de prorrateos**, el **override de FOB manual** y el **candado de
empresa** (mineria → 403), y limpia todo lo que creó al terminar (**94 checks**). Su
**paso 11** (espejo del paso 12 de Grupo AM) cubre el **peso editable**: re-prorrateo con
Σ shipping intacta, reseteo al peso de la cotización, manual ≤ 0 ignorado / negativo → 422,
**independencia FOB↔peso** (editar uno no revierte el otro), congelado al cerrar
(`peso_default` == valor congelado) y el fallback por FOB cuando la cotización trae todos
los pesos en 0.

El **paso 12** cubre el **origen del FOB**: marcado como `factura` (y que el costo landed lo
use), corrección `manual` sin regresión, payload **sin el flag** → `manual`, reseteo a
`cotizacion` / `auto`, valor 0 que no bloquea, negativo → 422, la **trampa del tri-estado**
(editar solo el peso no degrada `factura` a `manual`) y congelado al cerrar + reapertura.
El **paso 13** cubre el **aviso de moneda mezclada**: aparece, nombra las dos monedas y
**no bloquea** ni el guardado ni el cierre.

## Diferencias con Grupo AM

| | Grupo AM | MonzaParts |
|---|---|---|
| Embarque origen | `Embarque` + `EmbarqueItem` (con `oc_proveedor_id`) | `MonzaEmbarque` + `MonzaEmbarqueItem` (sueltos, sin FK) |
| FOB default | Factura Proveedor → Cotización → 0 | Costo del ítem de cotización → 0 (sin dato) |
| FOB real (`fob_origen='factura'`) | **derivado**: se lee de `FacturaProveedorItem` | **declarado**: no hay tabla de facturas de proveedor, lo carga y lo marca Contabilidad al costear (`fob_es_factura`) |
| Peso | libras (`peso_unit_lbs`), editable | kilos (`peso_kg`), editable |
| Override de ítem ajeno | (sin validar) | `400` si el `embarque_item_id` no es del embarque |
| TC / config | `ConfiguracionCotizador` | `MonzaConfig` (`tc_usd_clp` / `tc_eur_clp`) |
| Defaults de gastos | desde Config | desde Config, **mismos nombres de columna** (`desconsolidado_clp` / `bodegaje_clp` / `costo_agencia_minimo_clp`), IVA de `MonzaConfig.iva_pct`. Nacen en **0**: no se copiaron los montos de Grupo AM |

La **matemática del landed es idéntica** (función pura `calcular_landed`); solo cambian las
fuentes de datos y la unidad de peso (el prorrateo es proporcional, la unidad no altera el
resultado).
