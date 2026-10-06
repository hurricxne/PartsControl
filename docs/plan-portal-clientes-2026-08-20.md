# Portal de Clientes — MachParts (Grupo AM)
## Plan de implementación · versión 2 (definitiva)

| | |
|---|---|
| **Fecha** | 2026-08-20 |
| **Reemplaza a** | `docs/plan-portal-clientes-2026-08-19.md` (v1, superado) |
| **Árbol de trabajo** | `Parts control actual/PartsControl-main/` — HEAD `b964f6b` |
| **Marca** | Solo MachParts / Grupo AM (`empresa = "mineria"`). MonzaParts queda fuera |
| **Estado** | Esperando aprobación. **Cero código escrito.** |
| **Método** | Enjambre de 4 expertos (UX de producto, accesibilidad, arquitectura, búsqueda) sobre el árbol vivo |

---

## 0. Cómo leer este documento

Está escrito para que **un programador humano pueda construirlo y depurarlo sin haber estado en
estas conversaciones**. Por eso:

- Cada afirmación técnica lleva su **`archivo:línea`**. Si no tiene referencia, es una decisión, no
  un hecho, y está marcada como tal.
- Cada convención dice **de dónde se copia** en este mismo repo. El portal no inventa estilo.
- Cada regla que parece arbitraria explica **el bug que la originó**.

> ### ⚠️ Advertencia de método, leer antes de tocar nada
> La raíz del proyecto tiene un `backend/` y un `frontend-src/` que son **una foto del 3 de julio**.
> El código vivo está en **`Parts control actual/PartsControl-main/`**, seis semanas más nuevo
> (`compras.py`: 1.753 líneas en la foto vs. **2.368** en el vivo; 20 archivos de test vs. **129**).
>
> La v1 de este plan reportó como "críticos abiertos" el `register` público y el `SECRET_KEY` por
> defecto: **ambos ya estaban arreglados hacía un mes**. Nunca planificar contra la raíz.

---

## 1. Qué es y qué no es

**Es** una plataforma web de **solo lectura**, con usuario y clave, donde un cliente de Grupo AM
consulta sus órdenes de compra: los ítems, sus precios, la fecha prometida de cada uno, en qué
estado va cada uno, sus documentos y las fotos de lo despachado.

**No es** un canal de contratación, no permite emitir ni modificar órdenes, no muestra saldos ni
pagos ni cobranza, y no reemplaza la documentación formal.

La frase que gobierna cada decisión de diseño:

> El portal responde una pregunta: **¿en qué va cada ítem que compré y cuándo lo comprometieron?**

---

## 2. Requisitos del dueño

| # | Requisito | Dónde se resuelve |
|---|---|---|
| **R1** | Plataforma con usuario y clave para que los clientes vean sus órdenes | §4 Identidad · §11 Fase 1 |
| **R2** | Ver todos los ítems de cada orden, con **precios** y **fechas prometidas** | §8.3 Detalle de la orden · §7 Fecha prometida |
| **R3** | Ver el **estado de cada ítem**, qué está entregado y qué no, y **dónde vienen los que faltan** | §6 Estados · §8.2 Dashboard |
| **R4** | **Dashboard** con lo comprado en dinero, lo facturado y otros KPI | §8.2 |
| **R5** | Conectar el estado **"en aduana"** del embarque si existe | §6.1 — existe, y ya está conectado |
| **R6** | **Buscador** por N° de orden de compra y por N° de parte, **tolerante al guion** | §9 |

### Decisiones cerradas (registro)

| # | Decisión | Fecha |
|---|---|---|
| D1 | El portal es para **procurement**; sí ven montos | 08-20 |
| D2 | Montos = **valor de sus pedidos** (precio unitario, total de línea, total de OC) en CLP | 08-20 |
| D3 | **Notas de crédito: no se muestran** | 08-20 |
| D4 | **Factoring: no se muestra.** La cesión se notifica por el canal legal | 08-20 |
| D5 | **Solo lectura.** "Nosotros subimos todo" | 08-20 |
| D6 | Guías y facturas **sí** se muestran, como documentos del pedido | 08-20 |
| D7 | Las **fotos se sacan al despacho**, no en la recepción de bodega | 08-20 |
| D8 | El cliente ve **estado**, no fechas estimadas de llegada | 08-20 |
| D9 | La **cláusula legal** se trabaja con el abogado del dueño | 08-20 |
| D10 | **% de cumplimiento de fecha: se difiere** para después del piloto | 08-20 |
| D11 | **Fecha prometida por ÍTEM**, congelada al cerrar la venta (§7) | 08-20 |

---

## 3. Hallazgos verificados en el código

Todo lo de esta sección fue leído en el árbol vivo. Es la base sobre la que se apoya el plan.

### 3.1 Lo que YA existe y no hay que construir

| Pieza | Dónde | Qué significa para el portal |
|---|---|---|
| **Cadena de trazabilidad por FK reales** | `models/models.py:203,256,289,432,522,590` | `items_cotizacion` → OC proveedor → pre-embarque → embarque → recepción → despacho → factura. Se une por llave, **no por texto**. Es la parte difícil y ya está hecha |
| **Traductor de estados del ítem** | `despachos.py:135-151` (`_STATE_BUCKETS`, `_bucket_of`) | Colapsa 9 estados en 6 buckets. **Los mismos que ve el dueño** en su pantalla de Despachos |
| **Estado del embarque, con "en aduana"** | `compras.py:2005-2011` · `EmbarquesPage.tsx:76-92` | `en_bodega_proveedor → en_transito → en_aduana → en_bodega → despachado`, con guardas de transición |
| **Contrato de buscador documentado** | `docs/spec-buscadores-bodega-despachos-2026-08-05.md` · `despachos.py:171-304` | 7 helpers probados, incluida la tolerancia al guion |
| **Captura de fotos** | `bodega.py:548,596` · `BodegaPage.tsx:387` | Subida y borrado con cámara del teléfono. **Ojo: hoy son evidencia de daño** (§10) |
| **Cálculo de facturado** | `contabilidad.py:1002,1023` (`facturado_clp`) | El KPI de R4 ya está calculado |
| **Días hábiles** | `compras.py:116` (`add_business_days`) | Convierte el plazo cotizado en fecha |
| **Congelado al cierre de venta** | `compras.py:545-573` (`pricing_snapshot`) | El patrón exacto a copiar para D11, con su migración |
| **Descarga de archivo contenida** | `despachos.py:1516-1522` | Regex + `abspath` + verificación de prefijo. **El patrón seguro** |

### 3.2 Lo que está roto o falta

| # | Hallazgo | Evidencia | Impacto |
|---|---|---|---|
| **H1** | **`clientes` está vacía** y `cotizaciones` no tiene `cliente_id`; el cliente vive como texto libre | `models/models.py:37-42`; tabla `Cliente` `:326-339` huérfana | Bloquea el aislamiento. La Fase 1 es **trabajo de datos**, no de código |
| **H2** | **`plazo_entrega_max` se sobrescribe al comprar** | `compras.py:914`, `:1428` | Sin congelar, la fecha prometida se mueve sola. Lo resuelve D11 (§7) |
| **H3** | **~90-105 endpoints** reciben un `{id}` y no verifican pertenencia | `despachos.py:1151`, `contabilidad.py:1027`, `compras.py:762`, `:622` | El portal **no reutiliza ningún endpoint**: router aislado (§5) |
| **H4** | **`/uploads/bodega` se sirve sin token**, con nombres adivinables | `main.py:155`; nombres en `bodega.py:580` | **BLOQUEANTE** antes de publicar cualquier foto |
| **H5** | **Sin límite de peticiones en el login** | `routers/auth.py:28-41` | Fuerza bruta contra cuentas externas |
| **H6** | **`api.ts` no maneja el 403** | `services/api.ts:18` (solo 401) | Un cliente bloqueado ve el cascarón interno con tablas vacías |
| **H7** | **CORS con `localhost:5173`** en producción | `config.py:10` | Residuo de desarrollo |
| **H8** | **Grupo AM no tiene bitácora de acciones de usuario** | Solo existe en Monza (`monza_models.py:448-461`) | El portal la necesita para registrar descargas |
| **H9** | El buscador **no colapsa el N° de OC del cliente**, solo el N° de parte | `despachos.py:298-301` | R6 pide tolerancia al guion también en la OC (§9.3) |
| **H10** | La **pasada B del buscador es global**: si cualquier fila matchea, nunca corre | `despachos.py:1137-1145` | Puede ocultar la fila con guion (§9.3) |
| **H11** | `oc_cliente.fecha_entrega` **es editable sin guarda** | `compras.py:634-635`; el 409 de `:662-682` solo cubre `numero_oc`/`fecha_oc` | Riesgo menor, mitigación opcional en §7.4 |
| **H12** | **Naranjo de marca `#ea6c0d` = 3.16:1** sobre blanco | Cálculo del auditor de accesibilidad | **Falla WCAG AA** para texto pequeño (§12) |

---
## 4. Identidad y aislamiento

### 4.1 Modelo

Tres ejes ortogonales: `empresa` (marca) · `rol` (interno/externo) · el puente (a qué cliente ve).

```sql
users.rol            ENUM('interno','portal') NOT NULL DEFAULT 'interno'
usuario_cliente      (usuario_id FK users, cliente_id FK clientes,
                      perfil ENUM('procurement','bodega'),
                      UNIQUE(usuario_id, cliente_id))
cotizaciones.cliente_id  INTEGER FK clientes.id NULL   -- backfill por RUT saneado
```

**Por qué tabla puente y no un `cliente_id` en `users`:** una minera tiene varios contactos y un
contacto cubre varias faenas o RUT. Un 1:1 obliga a reescribir el guard después.

**Por qué `perfil`:** el bodeguero de faena necesita ver qué guía viene en camino y **no** debe ver
montos. Sin esto, es todo o nada y el cliente no le da cuenta a su bodeguero.

> **`role_guard.py` ya existe** (`backend/role_guard.py`) con su checklist de activación escrita.
> Sirve como base, **pero es permisivo: si `rol is None` deja pasar** (`:33-36`). Un guard que falla
> ABIERTO es peor que ninguno. El portal usa su propio guard, que falla cerrado.

### 4.2 El guard del portal

`backend/portal/deps.py`, copiando el acierto de `empresa_guard.py:17-31` (dependencia de **router**,
imposible de olvidar; falla cerrado) pero devolviendo el conjunto de IDs permitidos:

```python
@dataclass(frozen=True)
class PortalScope:
    usuario_id: int
    cliente_ids: frozenset[int]     # si queda vacío → 403. NUNCA "sin filtro = todo"
    perfil: str                     # 'procurement' | 'bodega'

def require_portal(...) -> PortalScope: ...
```

**Las 6 reglas del aislamiento** (cada una es verificable en revisión de código):

1. **Router propio** `/api/portal/*` con `dependencies=[Depends(require_portal)]`. Los ~100 endpoints
   con IDOR (H3) quedan fuera del alcance **por construcción**, no por parches.
2. **403** si `rol != 'portal'`, si el puente devuelve 0 filas, o si `is_active != 1`.
3. **404, no 403, para un recurso ajeno.** Un 403 confirma que el ID existe.
4. **El texto libre nunca filtra.** Solo `cliente_id` entero. `cotizaciones.cliente` es texto y
   `rut_cliente` es nullable y sucio: filtrar por ahí es un bypass trivial.
5. **DTO propios.** Reusar un dict interno es la fuga más probable. Ningún serializador del portal
   importa de `routers/`.
6. **Doble candado siempre:** el guard declarativo en el router **y** el filtro por cliente en cada
   consulta. Convención tomada de `compras_contab/router.py:35-37`.

### 4.3 El backfill (H1) — es trabajo de datos

`clientes` tiene 0 filas. La Fase 1 **no es "crear una columna"**, es:

1. Poblar `clientes` a mano con las mineras reales (son ~9 en el respaldo del 19-jul).
2. Normalizar `rut_cliente` con los helpers que ya existen: `_rut_canonico` / `_rut_valido`
   (`contabilidad.py:191,197`).
3. Poblar `cotizaciones.cliente_id` por RUT saneado.
4. **Reporte de huérfanas** para revisión humana.

Lo que no matchee queda `NULL`. Como el filtro es `cliente_id IN (...)`, **el dato sucio desaparece
del portal solo**: falla cerrado sin código extra. Ojo con no meter a un contratista bajo el RUT de
la minera madre.

---

## 5. Arquitectura del módulo

### 5.1 Estructura de archivos

Molde exacto de `backend/tesoreria/` y `backend/compras_contab/`.

```
backend/portal/
├── __init__.py              Docstring: qué es, que es SOLO LECTURA, de qué tablas lee
├── README.md                Archivos · Crear tablas · Activar la API · Endpoints ·
│                            Tests · Modelo de datos · Checklist de deploy · Cómo deshacer
├── models.py                SOLO tablas propias: portal_acceso, portal_descarga_log,
│                            foto_articulo. Cero columnas en tablas ajenas
├── schemas.py               Pydantic con max_length alineado a la columna
├── deps.py                  require_portal() → PortalScope. ÚNICO lugar que resuelve identidad
├── busqueda.py              filtro_q_portal() — sin dependencia de FastAPI (§9)
├── service.py               Serializadores + constantes + _log()
├── router.py                APIRouter(prefix="/portal", dependencies=[...])
├── router_documentos.py     Descarga de PDF y fotos (separado por riesgo y por tamaño)
├── init_db.py               Crea las tablas propias + migración aditiva
└── tests/
    ├── test_aislamiento.py  Cliente A no ve nada de B
    ├── test_busqueda.py     Los 6 casos de §9.5
    └── test_integration.py  Recorrido completo

frontend-src/src/portal/     Molde de frontend-src/src/tesoreria/
├── api.ts                   Reusa la instancia axios compartida
├── types.ts                 Espejo del JSON del backend
├── PortalLayout.tsx
├── DashboardPage.tsx · PedidosPage.tsx · OrdenPage.tsx · ItemPage.tsx
└── README.md                Instructivo de cableado y de reversión
```

**Cableado:** 2 archivos, como manda el repo. `main.py` (import con alias + `include_router` con
comentario de la ruta, molde `main.py:35,99-100`) y `App.tsx` + `PrivateRoute` con `requireRol`.

### 5.2 Convenciones obligatorias

| Tema | Regla | De dónde se copia |
|---|---|---|
| **Errores** | `HTTPException(404, "mensaje en español")`, posicional. Sin envelope propio | Estilo dominante del repo |
| **Esquema a medias** | 503 **con el comando exacto**: *"corre python -m portal.init_db y reinicia"*. Solo cuando MySQL responde 1146/1054 (no existe la tabla / la columna); cualquier otro error de la base es un 500 con su traceback. El 503 lo arma la clase de ruta `RutaDelPortal` (`deps.py`), no `require_portal` | `monza_contabilidad/router.py:598-603` |
| **Nunca en silencio** | Cero `except: pass` sobre un filtro | `monza_router_logs.py:41-45` — un `try/except pass` sobre un filtro de fecha hacía que la auditoría mostrara **todo** creyendo mostrar un rango |
| **Logging** | `logger = logging.getLogger("portal")`, formato %-lazy con IDs. El paquete configura ese logger en INFO al importarse (`portal/__init__.py`): sin eso los INFO no llegan a `pm2 logs`. `print` solo en `init_db.py` | `contabilidad.py:78,3305` |
| **Consultas** | **EXISTS correlacionado, jamás `.join()`** sobre tablas hijas | `despachos.py:246-250`: *"un join a la colección multiplica la fila de la cabecera, infla `query.count()` y el LIMIT se come los cupos con duplicados"* |
| **Paginación** | `{total, page, page_size, items}`, `page_size` con tope **50** | `compras_contab/router.py:43-44,221,245` |
| **N+1** | `joinedload` para cabecera + colecciones; `contains_eager` cuando el join ya filtra | `monza_router_leads.py:381-384` |
| **`.first()`** | Es la convención del repo (973 usos). No introducir `.one_or_none()` | — |
| **Migraciones** | Sin Alembic. `SHOW COLUMNS` → set → `ADD COLUMN` si falta. Docstring con QUÉ AGREGA / POR QUÉ / IDEMPOTENTE / **EFECTO SI NO SE CORRE** / Uso | `migrations/despacho_qty_firmada.py:1-38` |

> **Trampa conocida del repo:** la migración va **antes** de reiniciar el backend. El ORM ya declara
> la columna, y un `SELECT` contra la tabla vieja revienta con `1054 Unknown column`.

### 5.3 Las 8 reglas de debuggabilidad

Pensadas para que alguien pueda depurar esto a las 3 AM sin contexto previo. Todas son
**verificables**, no aspiracionales.

| # | Regla | Cómo se verifica |
|---|---|---|
| **1** | **Máximo 400 líneas por archivo.** Si `router.py` crece, se parte | `wc -l backend/portal/*.py` |
| **2** | **Docstring de cabecera en todo archivo**: prefijo de ruta, quién entra, flujo numerado | `head -1` de cada `.py` es `"""` |
| **3** | **Los comentarios explican el POR QUÉ y citan el incidente, con fecha.** Prohibido comentar el *qué* | Revisión de código |
| **4** | **Doble candado**: guard en el router **y** filtro por cliente en cada consulta | `grep -c "cliente_ids" portal/*.py` ≥ nº de consultas |
| **5** | **Fallar cerrado y ruidoso.** Recurso ajeno → 404. Esquema a medias → 503 con el comando (solo ante 1146/1054; otro error de la base es un 500 con traceback) | `grep -n "except.*: *pass" portal/` vacío |
| **6** | **Toda descarga deja rastro antes de entregar el byte**: fila en `portal_descarga_log` + `commit` **antes** del `FileResponse` | Test que descarga y cuenta filas |
| **7** | **Un solo `_log()`** en `service.py` | El defecto a NO copiar: Monza tiene el helper duplicado en 6 routers |
| **8** | **Un test por guard**, autolimpiante, con doble entrypoint | Cada `HTTPException(403\|404)` tiene su `check()` |

> **El pecado del repo que no se repite:** `routers/contabilidad.py` tiene 3.495 líneas,
> `monza_contabilidad/router.py` 3.243. Nadie depura eso de madrugada.

---
## 6. Estados (R3, R5)

### 6.1 Son dos máquinas de estado en paralelo

| Máquina | Dónde vive | Valores |
|---|---|---|
| **Ítem** (`items_cotizacion.estado_item`) | `despachos.py:135-145` | `ingresado`, `cerrado`, `comprado`, `preparado`, `pre_embarcado`, `embarcado`, `en_bodega`, `despachado`, `reclamo_proveedor` |
| **Embarque** (`embarques.estado`) | `compras.py:2005-2011` · `EmbarquesPage.tsx:83` | `en_bodega_proveedor` → `en_transito` → **`en_aduana`** → `en_bodega` → `despachado` |

> **Sobre R5 — corrección registrada:** en una revisión anterior reporté que "en aduana" no existía.
> **Era falso.** `embarques.estado` es un `String(50)` **libre**, sin enum en el modelo, así que el
> vocabulario real vive en el frontend. **Lección: buscar en `backend/` Y en `frontend-src/src/`
> antes de afirmar que algo no existe.**

El desfase entre ambas máquinas **es información real, no un error**: `compras.py:2038-2043` lo dice
explícito — el embarque en `en_bodega` solo significa que la carga llegó; el ítem recién pasa a
`en_bodega` cuando Bodega **cierra** la recepción. Ese hueco es "llegó a Chile, lo estamos recibiendo".

### 6.2 Tabla de traducción

Se cruzan por la cadena de FK que ya existe: `item_cotizacion` → `embarque_items.item_cotizacion_id`
→ `embarques.estado`.

| Lo que ve el cliente | Estado del ítem | Estado del embarque |
|---|---|---|
| Pedido confirmado | `ingresado`, `cerrado` | — |
| En proceso de compra | `comprado` | — |
| Listo en origen | `preparado`, `pre_embarcado` | `en_bodega_proveedor` |
| En tránsito | `embarcado` | `en_transito` |
| **En internación aduanera** | `embarcado` | **`en_aduana`** |
| Llegó a Chile, en recepción | `embarcado` | `en_bodega` |
| En nuestra bodega, Santiago | `en_bodega` | — |
| Despachado a faena **+ fotos** | `despachado` | — |
| Facturado | *derivado, §6.3* | — |
| No llegó en este envío — en gestión | `reclamo_proveedor` | — |

El portal **reutiliza `_bucket_of`** (`despachos.py:150`) y solo agrega el diccionario de traducción.
Si el bucket cambia adentro, cambia afuera: **cliente y dueño ven el mismo estado por construcción.**

`reclamo_proveedor` no nombra al proveedor ni promete fecha nueva. Y por §10, no se publica foto de
un ítem con reclamo vivo.

### 6.3 "Facturado" no es un estado, se deriva

No existe `estado_item = 'facturado'`. Pero `cont_factura_cliente_item.item_cotizacion_id`
(`models.py:590`) dice qué línea entró en qué factura, y `contabilidad.py:2242` ya maneja "despachado
y aún no facturado". El portal lo **calcula**: sin columna nueva, sin migración.

---

## 7. La fecha prometida por ítem (R2, D11)

### 7.1 El problema

La cotización guarda **días hábiles de plazo por ítem** (`items_cotizacion.plazo_entrega_max`) y ya
existe `add_business_days` (`compras.py:116`). Pero **ese plazo se sobrescribe al comprar**
(`compras.py:914`, `:1428`), así que la fecha se movería sola cada vez que Abastecimiento asigna una
OC de proveedor.

Y el helper que un programador tomaría por instinto, `_item_deadline` (`despachos.py:154-166`), usa
justamente ese campo. **El portal no lo usa.**

### 7.2 La solución robusta: congelar en el Cierre de Venta

`crear_oc_cliente` (`compras.py:545-573`) es **el punto exacto**. En ese instante el plazo todavía
tiene el valor cotizado, y esa función ya:

- es **idempotente** (devuelve la OC existente si se reintenta, `:548-555`),
- **ya congela** la foto de precios `pricing_snapshot` con guarda `if not cot.pricing_snapshot`,
- ya marca los ítems `ingresado → cerrado`.

Se agrega, en la misma transacción y con la misma guarda:

```
items_cotizacion.fecha_promesa_cliente   DATE NULL
```
calculada como `add_business_days(cotizacion.created_at.date(), plazo_entrega_max)`.

> Superado por la Fase 2 (`docs/fase-2-portal-fecha-promesa-2026-08-30.md`): el ancla es el
> **día del cierre** (hoy de Chile), no `created_at`, y el plazo es `plazo_prometido`
> (`plazo_entrega_max` o, si el ítem no tiene, el plazo máximo por defecto del cotizador).

**Por qué es robusto y no un parche:**

1. Se escribe **una sola vez**, en un punto que ya es idempotente.
2. Copia un patrón **ya probado en producción** (`pricing_snapshot`), con su migración de molde.
3. El reparto de líneas ya arrastra los campos del ítem al clon (`compras.py:1129`): el split hereda
   la fecha sin código extra.
4. **Nadie la actualiza después.** Ni `asignar_items` ni la asignación parcial la tocan.
5. **Freeze-forward:** las OC cerradas antes quedan en `NULL` y caen a `oc_cliente.fecha_entrega`.
   Es el mismo criterio de "solo de aquí en adelante" que el dueño ya aprobó para el tipo de cambio.

### 7.3 Qué fecha se muestra dónde

| Pantalla | Fecha | Fuente |
|---|---|---|
| Detalle de la orden, por ítem | Fecha prometida del ítem | `items_cotizacion.fecha_promesa_cliente`, con respaldo `oc_cliente.fecha_entrega` (ventas cerradas sin congelar o plazo que no sirve para calcular) |
| Mis pedidos, por orden | **"Orden completa el ..."** | La **última** fecha prometida entre los ítems pendientes |
| Dashboard | "Próxima fecha comprometida" | La **primera** fecha prometida entre los ítems pendientes |
| Seguimiento del ítem | La fecha prometida de ese ítem, destacada | Ídem |

**Hitos cumplidos: su fecha real** (es un hecho). **Hitos futuros: sin fecha.** El portal nunca
estima cuándo llega algo.

### 7.4 Cabo suelto conocido (H11)

`oc_cliente.fecha_entrega` es editable sin guarda. Si alguien la edita, el cliente ve otra fecha sin
enterarse. Riesgo menor (pasa rara vez, siempre por decisión humana).
**Mitigación opcional, no incluida en el alcance:** tabla append-only de cambios, para mostrar
*"fecha comprometida actualizada el 6 de agosto"*.

---

## 8. Las pantallas

Cinco. La navegación es **Dashboard → Mis pedidos → Orden → Ítem**, con un **buscador global** en la
barra superior que permite saltarse los niveles intermedios.

### 8.1 Reglas transversales de la interfaz

Del panel de UX, todas concretas:

| Regla | Por qué |
|---|---|
| **Decir "ítems", nunca "líneas"** | Para el cliente una línea es un renglón de su OC en SAP. Los splits se reagrupan por `(cotizacion_id, item_num, numero_parte)`: si pidió 14 renglones, el portal dice 14 |
| **El Seguimiento del ítem abre en panel lateral, pero con URL propia** (`/portal/oc/4821/item/1R-0750`) | Procurement **reenvía el link por correo** al jefe de mantención. Un modal sin URL mata ese uso |
| **Migas de pan completas y clickeables**, y botón "← Volver a OC-4821" con nombre | Una flecha sola compite con el botón del navegador |
| **Botón "copiar" junto al N° de parte y al folio de la guía** | Lo pegan en su ERP diez veces al día |
| **Fechas con contexto**: `mar 12 ago · en 3 días` | Una fecha sola obliga a calcular |
| **"Actualizado hace 8 minutos"** visible en cada pantalla | Sin eso, ante la duda llama por teléfono y el portal no ahorró nada |
| **El Excel exporta lo filtrado**, con el filtro en el nombre (`OC-4821-pendientes-29ago.xlsx`) | — |
| **El N° de OC del cliente en el título de la pestaña y en la URL** | Lo encuentra entre 30 pestañas |
| **Ejecutivo asignado con nombre y WhatsApp al pie**, con el N° de OC precargado | El portal no reemplaza al humano: le da mejor contexto |

### 8.2 Dashboard (R4)

Todo en CLP y solo de las órdenes de ese cliente.

| Indicador | Fuente |
|---|---|
| **Comprado** | Σ del valor de sus OC |
| **Facturado** | `contabilidad.py:1002,1023` (`facturado_clp`) |
| **Por entregar** | Valor de los ítems no despachados |
| **Dónde vienen mis N ítems pendientes** | Reparto por estado (§6.2). **El más útil del tablero: responde R3 de un vistazo** |
| **Avance de entrega** | Ítems entregados vs. total |
| **Mis órdenes** | Abiertas y entregadas |

- Las barras del desglose son **clickeables** y llevan a Mis pedidos ya filtrado. Es el menú real.
- Subtítulo fijo con el período: *"Todo lo que nos ha comprado desde el 1 de enero de 2026"*, con
  selector de año. Sin eso el cliente asume "del mes" y se equivoca.
- **Jerarquía, no simetría:** "Por entregar" es la cifra grande; las otras dos, secundarias. Tres
  tarjetas idénticas *prometen* una aritmética que no cuadra (hay parciales, hay facturado sin
  despachar) y cuando un número no cuadra, el portal pierde credibilidad.

> **Objeción del panel, registrada:** recomiendan quitar "Facturado" por ser un número huérfano sin
> saldos ni notas de crédito, que invita a la pregunta *"¿y cuánto debo?"* que el portal decidió no
> responder. **Se mantiene porque es un requisito explícito del dueño (R4).**

### 8.3 Detalle de la orden (R2, R3)

**Cabecera:** total, entregado, avance, "orden completa el ...".
**Filtros:** Todas · Pendientes · Entregadas.
**Buscador** de N° de parte dentro de la orden.

**Cuatro columnas visibles, no nueve** — el ancho se lo comen la descripción y los montos:

| Ítem | Cantidad | Estado | Monto |
|---|---|---|---|
| **1R-0716** <br><small>Filtro de aceite motor</small> | 12 | ● En tránsito <br><small>comprometido **12 ago**</small> 📷 | **$1.037.040** <br><small>$86.420 c/u</small> |

- El N° de línea de la OC del cliente va como prefijo tenue (`#7 · 1R-0716`).
- Si la fecha pasó y no está entregado: **"vencida el 12 ago"** en rojo. Es un hecho, no una estimación.
- **Orden por defecto: pendientes primero, por fecha prometida ascendente** (lo más atrasado arriba).
  Es el orden de la ansiedad, que es la razón por la que abre el portal. Segundo orden a un clic:
  **"El orden de mi OC"** (`item_num`), indispensable para cuadrar contra SAP.
- **Con 60 ítems: sin paginación** (rompe Ctrl+F). Encabezado y filtros pegajosos, contador vivo, y
  las entregadas plegadas en acordeón al final: `▸ 43 ítems ya entregados`.
- **Toda la fila** abre el Seguimiento.

### 8.4 Seguimiento del ítem

Línea de tiempo con los estados de §6.2, documentos (guía con PDF, factura con folio y datos) y las
fotos del despacho (§10).

### 8.5 Textos de los estados vacíos

Es donde mueren los portales. Van literales:

| Situación | Texto |
|---|---|
| Sin órdenes | *"Todavía no tienes órdenes en el portal. Cuando confirmemos tu primera orden de compra, la vas a ver acá. Si crees que falta una, escríbele a tu ejecutivo: **[NOMBRE] · [TELÉFONO]**."* |
| Orden sin ítems | *"Esta orden todavía no tiene ítems cargados. La estamos preparando. Vuelve a intentar más tarde o consúltale a tu ejecutivo."* |
| Búsqueda sin resultados | *"No encontramos «1R-0716» en esta orden. Revisa si el número lleva guiones o prueba con una parte del código."* + botón **`Buscar en todas mis órdenes`** |
| Ítem sin foto | *"Todavía no hay fotos de este ítem. Las tomamos al momento del despacho."* |
| Documento no emitido | *"La factura de este despacho aún no se emite. Va a aparecer acá con su folio y fecha apenas se emita en el SII."* |
| Error de carga | *"No pudimos cargar tus pedidos. Puede ser tu conexión. **[Reintentar]** Si sigue igual, avísale a tu ejecutivo."* |
| Sesión expirada | *"Tu sesión se cerró por seguridad. Ingresa de nuevo y te devolvemos a esta misma pantalla."* — **y que efectivamente vuelva a la misma URL** |

### 8.6 Móvil (375px — faena, guantes, sol, 4G intermitente)

- **Detalle de la orden: tarjetas, NO scroll horizontal ni columnas prioritarias.** El scroll
  horizontal es donde muere este portal: con sol y guantes nadie descubre que hay columnas a la derecha.
- **Dashboard:** una sola cifra ("Por entregar") y los tres estados con más pendientes, tocables.
- **Login:** `Mantener sesión iniciada` marcado por defecto, sesión larga. Reloguearse con guantes es
  la causa nº 1 de abandono.
- **Sin conexión:** mostrar la última respuesta cacheada con el sello **"Datos del 12 ago, 14:32 ·
  sin conexión"** en vez de una pantalla en blanco. El botón de Excel se deshabilita explicando por qué.
- Toques ≥48px. Cero hover, cero tooltips.
- **Perfil bodega:** montos ocultos por completo.

---
## 9. El buscador (R6)

### 9.1 El contrato ya existe — no se inventa uno nuevo

Documentado en `docs/spec-buscadores-bodega-despachos-2026-08-05.md`, implementado en
`despachos.py:171-304`. **Cada regla nació de un bug real:**

| Helper | Línea | Regla | El bug que la originó |
|---|---|---|---|
| `_normalizar_q` | `:194-199` | strip, colapsar espacios, truncar a 64, `<2 chars ⇒ no filtrar`, máx 4 tokens (AND entre tokens, OR entre campos) | Pegar `" 2026-0045"` daba cero. Y el filtro viejo concatenaba campos en un solo texto: `"codelco 4500"` matcheaba si el cliente terminaba en "codelco" y la OC empezaba con "4500", sin que ningún campo tuviera la frase |
| `_variantes_token` | `:202-212` | token crudo **+** token sin prefijo de UI (`COT- OC- OCP- EMB- DSP- N° #`) | La base guarda `2026-0001` pero la pantalla imprime `COT-2026-0001`. El operador copia lo que ve y obtenía cero |
| `_patron` | `:215-221` | escapa `\ % _` **siempre**; se usa como `.like(pat, escape="\\")` | No es inyección (el valor va bindeado), es comportamiento: **sin escape, teclear `_` devuelve la tabla entera**, y `100_4567` matchea `100-4567` *y* `100X4567` — dos partes distintas. Es el defecto de la lista que cuesta plata |
| `.like()` y no `.ilike()` | `:187-188` | Las columnas son `utf8mb4_unicode_ci`: ya insensibles a mayúsculas **y tildes**. `.ilike()` compila a `lower(col)` y **anula el índice** a cambio de nada | Divergencia real: la collation ignora tildes, `str.lower()` de Python no → "camion" encontraba en un listado y no en el otro |
| **`_colapsar`** | `:224-228` | `re.sub(r"[-\s]", "", tok).upper()` — quita guiones y espacios, **no puntos** | `excel_service.py:144` solo hace `.strip()`: `7T1997` y `7T-1997` conviven como filas distintas. Se resuelve buscando, no con un UPDATE masivo |
| `_np_colapsado_sql` | `:231-234` | `REPLACE(REPLACE(UPPER(col),'-',''),' ','')` sobre la **columna** | La otra mitad: normaliza el dato histórico en vuelo |
| `_matchea` | `:237-243` | Insignia de "por qué salió esta fila", calculada **solo sobre la página ya paginada** (≤50 filas) | Sin la insignia el operador relee la fila entera y ahí agarra la caja equivocada |

**Pasada A y pasada B** (`despachos.py:1137-1145`):
- **A** (siempre): infijo literal sobre todos los campos.
- **B**: corre **solo si A devolvió 0** y algún token tiene dígitos. Compara ambos lados **colapsados**.
  Es un full scan reconocido (el `REPLACE` anula el índice): por eso es red de seguridad, nunca la
  primera pasada.

**Reutilización:** los helpers son importables sin ciclo, y el patrón ya está probado —
`bodega.py:1061-1065` los importa **dentro del cuerpo de la función**. Se reutilizan **literales**:
`_normalizar_q`, `_variantes_token`, `_patron`, `_colapsar`, `_np_colapsado_sql`, `_matchea`.

**NO se reutiliza `_filtro_q_oc_clientes`**: filtra por `Cotizacion.cliente` y `.rut_cliente`
(inútil en el portal y peor, permitiría enumerar por RUT) y sus EXISTS de embarque y transportista
exponen logística interna. Se copia **la forma**, no el cuerpo.

> **Ojo:** el `_normalizar_q` de Monza (`monza_router_despachos.py:226`) devuelve **dict**; el de
> Grupo AM devuelve **lista**. No son intercambiables. El portal usa el de Grupo AM.

### 9.2 Qué busca el portal

| Buscable | Tabla.columna |
|---|---|
| N° de OC **del cliente** | `oc_cliente.numero_oc` (`models.py:159`) — **sin índice hoy; agregarlo** |
| N° de parte | `items_cotizacion.numero_parte` (`:86`, indexado) |
| Descripción | `items_cotizacion.descripcion` (`:85`) **y** `nombre_cat` (`:99`) — la pantalla imprime `descripcion or nombre_cat` |
| Marca | `items_cotizacion.marca` (`:87`) |

**Prohibidos por internos:** `cotizaciones.numero` y su prefijo `COT-`, `cotizaciones.cliente` y
`.rut_cliente` (habilitan enumeración cruzada), todo `oc_proveedor.*`, `embarques.numero` y
`.awb_numero`, `facturas_proveedor.invoice_no`, `despachos.transportista` y `.numero_expedicion`, y
todo campo de costo.

### 9.3 Los dos huecos que hay que tapar

Verificados empíricamente, no supuestos:

**Hueco 1 — la OC del cliente nunca se colapsa (H9).** La pasada B solo toca `numero_parte`. Una OC
guardada `4500-123456` buscada como `4500123456` da cero en A y en B. **R6 pide tolerancia al guion
también en la OC.** Arreglo: agregar `_np_colapsado_sql(OcCliente.numero_oc)` a la rama colapsada.

**Hueco 2 — la pasada B es global, no por token (H10).** Basta que *cualquier* fila matchee en A
para que B nunca corra: buscar `1R0716` mientras otra fila contiene `1R0716` en la descripción
**oculta** la fila `1R-0716`. Arreglo: en el portal, B corre cuando el token con dígitos no encontró
match **en su propio campo**, no cuando el total global es 0.

*(Bonus verificado: `1r 0716` con espacio funciona hoy por accidente — `_normalizar_q` lo parte en
dos tokens y ambos son subcadenas de `1R-0716`. Con otro corte, falla. El colapsado lo resuelve bien.)*

### 9.4 Diseño — el orden importa por seguridad

```python
# 1. PRIMERO el candado del cliente, sobre `base`
base = (db.query(OcCliente)
          .join(OcCliente.cotizacion)
          .options(contains_eager(OcCliente.cotizacion))
          .filter(Cotizacion.cliente_id.in_(scope.cliente_ids)))   # ← el ancla

# 2. DESPUÉS la búsqueda
tokens = _normalizar_q(q)
query  = filtro_q_portal(base, tokens) if tokens else base
total  = query.count()

# 3. Pasada B: se reconstruye DESDE `base`, no desde `query`
if tokens and total == 0 and _hay_token_con_digitos(tokens):
    query = filtro_q_portal(base, tokens, con_colapsado=True)
    total, normalizado = query.count(), True
```

> **Esta es la línea que hay que entender antes de tocar el buscador.** El candado del cliente va en
> `base` **porque la pasada B se reconstruye desde `base`**. Si el candado se aplicara encima de
> `query`, la pasada B lo perdería y devolvería órdenes de otros clientes. Es exactamente el modo de
> falla que prueba el caso 2 de §9.5.

**Respuesta uniforme:** `{items, total, page, page_size, q_efectivo, normalizado}`. Con
`normalizado = true` la pantalla dice: *"Buscaste 1R0716; también busqué 1R-0716"*.

**Orden:** `fecha_promesa IS NULL, fecha_promesa ASC, oc_cliente.id DESC` — el desempate por `id`
evita repetir o perder filas con OFFSET.

### 9.5 Los 6 casos de prueba

Cada uno con su **sonda de poder discriminante**: quitar el arreglo y ver que el test falle.

| # | Caso | Sonda |
|---|---|---|
| **1** | **Seguridad — parte ajena.** Cliente A busca el `numero_parte` exacto de una OC de B → `total == 0` | Mover el filtro de cliente de `base` a `query` → el test debe ponerse rojo |
| **2** | **Seguridad — vía pasada B.** La fila de B tiene `1R-0716`; A busca `1R0716` (A da 0 → B corre) → sigue devolviendo 0 | Ídem |
| **3** | **Guion en la parte.** `1R0716`, `1r0716`, `1R-0716` y `1r 0716` devuelven la misma fila; los colapsados traen `normalizado: true` | — |
| **4** | **Guion en la OC del cliente.** OC `4500-123456` buscada como `4500123456` la encuentra | **Hoy falla**: es el arreglo del hueco 1 |
| **5** | **Comodines.** `_` solo → 0 filas. `100_4567` → solo la parte con guion bajo literal, nunca `100-4567` | Quitar `escape="\\"` → debe fallar |
| **6** | **Paginación honesta.** Una OC con 3 ítems que matchean aparece **una** vez, `total == 1` | Cambiar el `EXISTS` por `.join()` → `total` salta a 3 y el test falla |

---

## 10. Fotos de los artículos (D7)

### 10.1 Dos objetos distintos, no un interruptor

| | Evidencia de daño (ya existe) | Foto del artículo (nueva) |
|---|---|---|
| Tabla | `recepcion_item_fotos` — **intacta** | `foto_articulo` |
| Para qué | Reclamo al proveedor | Que el cliente reconozca lo suyo |
| Quién la ve | Solo interno | El cliente |
| Momento | Recepción en bodega | **Despacho** |
| Archivo | Original | **Derivado**: lado largo 1600 px, ~300 KB, **EXIF borrado**, nombre aleatorio |

**Por qué tabla nueva y no una columna `visible_cliente`:** un interruptor sobre la misma fila se
marca mal algún día, y ese error es silencioso e irreversible (una vez descargada, borrarla no la
des-publica). Con dos tablas, publicar una foto de daño **no es un error posible**.

**Por qué en el despacho y no en la recepción:** ahí la pieza ya salió del embalaje de origen, así
que el riesgo de filtrar al proveedor cae **por diseño**, no por la puntería del bodeguero. En
recepción, lo que sale en el encuadre es: etiqueta del consolidador, caja de la marca aftermarket,
**el packing slip con el precio unitario de compra**, "Made in", AWB, y el fondo con cajas de otras
mineras.

**Por qué el derivado:** el EXIF lleva el GPS de la bodega y una hora que puede contradecir el hito
declarado. Y una foto de teléfono pesa 3-8 MB: en 4G de faena no carga. Requiere **Pillow**, que no
está en `requirements.txt`.

**Cómo se sirve:** endpoint autenticado que verifica pertenencia, copiando `despachos.py:1516-1522`.
**No** por `/uploads/bodega` (H4).

### 10.2 La foto que se pide

Máximo 2 por ítem (nadie mira la tercera): la pieza **desembalada**, sobre superficie neutra, con una
tarjeta visible con el **N° de OC del cliente y su código de parte**, y la **cantidad completa en el
encuadre**. Eso la convierte en prueba de identidad y cantidad, que es para lo que sirve.

**No se publica** foto de un ítem con reclamo vivo al proveedor.

---

## 11. Seguridad

| # | Control | Estado |
|---|---|---|
| S1 | `POST /api/auth/register` exige sesión | ✅ Ya resuelto (`routers/auth.py:44-52`, 22-jul) |
| S2 | `SECRET_KEY` con fail-fast al arrancar | ✅ Ya resuelto (`config.py:52-54`) |
| S3 | **Cerrar `/uploads/bodega`** (H4) | **BLOQUEANTE** — antes de publicar la primera foto |
| S4 | **Límite de peticiones en el login** (H5) | Fase 0 |
| S5 | **Sacar `localhost:5173` del CORS de producción** (H7) | Fase 0 |
| S6 | **Rama 403 en `api.ts`** (H6) | Fase 0 |
| S7 | Router aislado + `PortalScope` que falla cerrado | Fase 1 |
| S8 | 404 (no 403) para recurso ajeno | Fase 3 |
| S9 | **Bitácora de descargas** `portal_descarga_log` (H8) | Fase 3 |
| S10 | Marca de agua con el nombre del usuario en cada exportación | Fase 4 |
| S11 | Corte automático de la cuenta a los 60 días sin uso | Fase 1 |

### Lo que NUNCA sale al cliente

`margen_pct`, `precio_finning`, `precio_cat`, `nombre_cat` como precio, `url_cat`, `peso_unit_lbs`;
**toda** `configuracion_cotizador` (TC interno) y `parts_cache`; **el nombre del proveedor**
(`oc_proveedor.proveedor`, `.pais`); **toda** `facturas_proveedor`; **todo** `emb_pricing*` (costo
landed, FOB, CIF, fletes); `embarques.factura_comercial` y `.forwarder`; `reclamos_proveedor`;
`payload_json`/`respuesta_json` de Wasabil. **Fuera por decisión:** saldos, pagos, `estado_pago`,
factoring, notas de crédito.

**Moneda: CLP y solo CLP.** Nunca dos precios CLP a distinto TC en la misma vista — de ahí se
despeja el margen. Y toda cifra lleva el código de moneda explícito: en Chile CLP y USD comparten
el signo `$`, y un monto ambiguo destruye más confianza que un bug.

**Protección del patrón, no solo del campo:** los hitos no dicen la marca del fabricante ni la ciudad
de origen. Bloquear el campo del proveedor no sirve si el patrón —consolidador, ruta, lead time—
queda a la vista.

---

## 12. Accesibilidad

Auditoría WCAG 2.2 AA. **Los 5 arreglos mínimos antes de lanzar**, todos de marcado o tokens:

| # | Arreglo | Criterio | Por qué este |
|---|---|---|---|
| **1** | **Token `--orange-ink: #b4530a` para todo texto pequeño y enlaces.** El naranjo de marca `#ea6c0d` da **3.16:1** sobre blanco: falla texto normal (necesita 4.5:1). `#b4530a` da **5.02:1**. `#ea6c0d` queda solo para rellenos, barras, bordes y texto ≥24px | 1.4.3 | Aparece en las 5 pantallas y afecta a cualquiera con baja visión o mirando a pleno sol en faena. Se arregla en una variable CSS |
| **2** | **`<th scope="col">` + N° de parte como `<th scope="row">` + enlace real por fila.** Nunca `<tr onClick>` | 1.3.1, 2.1.1 | Sin esto las dos pantallas centrales son inutilizables con lector de pantalla |
| **3** | **Anillo de foco propio** (`outline: 3px solid #1c1917; outline-offset: 2px` + halo cálido). El azul por defecto de Tailwind choca con la marca | 2.4.7 | Un bloque CSS habilita toda la operación por teclado |
| **4** | **`aria-label` y área ≥24×24 px en íconos sin texto** (foto, flecha de fila) | 4.1.2, 2.5.8 | Un botón vacío es un punto muerto; el objetivo pequeño castiga al bodeguero con guantes |
| **5** | **Estado con palabra + forma** (✓ entregado, ● en tránsito, ○ pendiente), y `role="status"` que anuncie el resultado del filtro | 1.4.1, 4.1.3 | Cubre daltonismo y evita el silencio tras filtrar, que se lee como "se rompió" |

**Además, obligatorio en el marcado:**
- La **línea de tiempo es un `<ol>`**, con `aria-current="step"` en el hito actual y el estado en
  texto (`<span class="sr-only">Cumplido:</span>`), no en la forma del círculo.
- Las **barras del dashboard** llevan la cifra como **texto visible** al lado; la barra va
  `aria-hidden="true"`.
- `font-variant-numeric: tabular-nums slashed-zero` en toda columna numérica; montos a la derecha.
- **N° de parte en fuente monoespaciada con cero rasgado** — `1R-0716` vs `1R-O716` es un error caro.
  Y nunca `text-transform: uppercase` sobre el código: destruye la pista de forma. Si la búsqueda
  debe ser tolerante, que lo sea el backend; la pantalla muestra el dato canónico.
- **Estimado vs. hecho:** un hecho lleva fecha y documento (`Entregado · 12 ago · Guía 1.284`); una
  estimación lleva etiqueta léxica. Nunca mezclados en la misma columna sin la etiqueta.
- El foco tras un filtro **no se mueve**: solo se anuncia.
- Header pegajoso con `scroll-margin-top` para no tapar la fila enfocada (2.4.11).

---
## 13. Modelo de datos: todo lo que se agrega

**Nada se modifica en tablas existentes salvo dos columnas aditivas.** Ningún `DROP`, ningún cambio
de tipo, ningún `NOT NULL` nuevo.

| Objeto | Tipo | Dónde | Para qué |
|---|---|---|---|
| `users.rol` | Columna `ENUM('interno','portal') NOT NULL DEFAULT 'interno'` | Existente | Separa interno de externo |
| `cotizaciones.cliente_id` | Columna `INTEGER NULL FK clientes.id` | Existente | El ancla del aislamiento |
| `items_cotizacion.fecha_promesa_cliente` | Columna `DATE NULL` | Existente | La fecha prometida congelada (§7) |
| `usuario_cliente` | **Tabla nueva** | `portal/models.py` | Puente usuario ↔ cliente ↔ perfil |
| `foto_articulo` | **Tabla nueva** | `portal/models.py` | Foto publicable, separada de la evidencia (§10) |
| `portal_descarga_log` | **Tabla nueva** | `portal/models.py` | Quién descargó qué, cuándo, desde dónde |
| Índice en `oc_cliente.numero_oc` | Índice | Existente | El buscador (§9.2) |

**Migraciones** (una por columna, idempotentes, con el docstring de molde de
`migrations/despacho_qty_firmada.py:1-38`):

```
backend/migrations/portal_users_rol.py
backend/migrations/portal_cotizacion_cliente_id.py
backend/migrations/portal_fecha_promesa_cliente.py
backend/portal/init_db.py          ← crea las 3 tablas nuevas
```

Cada docstring debe declarar **EFECTO SI NO SE CORRE**, con su severidad. Ejemplo real:
*"el modelo ya declara la columna → error 1054 «Unknown column» en el detalle de orden del portal"*.

Todas se agregan a `deploy/audit_schema.py` y al checklist de deploy.

---

## 14. Fases

| Fase | Qué entrega | Complejidad | Depende de |
|---|---|---|---|
| **0 — Higiene** | Cerrar `/uploads/bodega` (S3, **bloqueante**); límite de peticiones en login; CORS de producción; rama 403 en `api.ts` | Baja | — |
| **1 — Identidad y datos** | Poblar `clientes` a mano; `cliente_id` + backfill por RUT + reporte de huérfanas; `users.rol`; `usuario_cliente`; `portal_guard`; pantalla interna "crear cuenta de portal"; corte a 60 días | **Media** — el grueso es trabajo de datos | 0 |
| **2 — Fecha prometida** | `items_cotizacion.fecha_promesa_cliente` + congelado en `crear_oc_cliente` + migración | Baja | — |
| **3 — API del portal** | `backend/portal/` completo: guard, buscador, 6 endpoints, bitácora de descargas, tests | Media | 1, 2 |
| **4 — Frontend** | `frontend-src/src/portal/`: 4 pantallas + móvil + accesibilidad | Media-Alta | 3 |
| **5 — Fotos al despacho** | `foto_articulo` + derivado con Pillow + botón en Despachos + galería | Media | 3 |
| **6 — Correos** | Tres, con el N° de OC del cliente en el asunto: cambio de estado a *en tránsito*, llegada a bodega, despacho con guía | Baja | 4 |
| **7 — Piloto** | Un cliente grande, tres meses | Baja | Todas |

### Endpoints de la Fase 3

| Método y ruta | Devuelve |
|---|---|
| `GET /api/portal/dashboard` | Los 6 indicadores de §8.2 |
| `GET /api/portal/ordenes` | Lista paginada, con `q` (§9) |
| `GET /api/portal/ordenes/{id}` | Cabecera + todos los ítems con precio, fecha prometida y estado |
| `GET /api/portal/items/{id}` | Seguimiento: línea de tiempo, documentos, fotos |
| `GET /api/portal/documentos/{id}` | Descarga con verificación de pertenencia + bitácora |
| `GET /api/portal/ordenes/exportar.xlsx` | Excel de lo filtrado |

---

## 15. Pruebas

**Molde del repo** (`routers/tests/test_despachos_guards.py`, `tesoreria/tests/test_integration.py`):
no hay `conftest.py` ni fixtures; los tests corren contra la base MySQL local real, con una constante
`MARK` que prefija todos los datos, un helper `check(nombre, cond)` que acumula fallos, `_limpiar()`
en `finally`, y **doble entrypoint** (`def test_x(): run()` + `if __name__ == "__main__"`).

> **Detalle no obvio, copiarlo:** el override de autenticación debe hacer `db.execute(text("SELECT 1"))`
> en la misma sesión, porque el `SELECT` del guard abre el read view antes de cualquier lock
> (`tesoreria/tests/test_integration.py:47-56`).

| Suite | Qué prueba |
|---|---|
| `test_aislamiento.py` | Cliente A no ve, no lista y no descarga nada de B. **404, no 403.** Sin cliente resuelto → 403 |
| `test_busqueda.py` | Los 6 casos de §9.5, cada uno con su sonda discriminante |
| `test_fecha_promesa.py` | La fecha se congela al cerrar la venta y **no se mueve** al comprar ni al partir la línea |
| `test_estados.py` | Los 10 estados del cliente salen del cruce ítem × embarque, incluido `en_aduana` |
| `test_fugas.py` | Ningún DTO del portal contiene los campos prohibidos de §11 — **por nombre de campo** |

**La regla de oro de las pruebas de este plan:** cada arreglo lleva una **sonda de poder
discriminante** — se quita el arreglo y se verifica que el test se ponga rojo. Una prueba que pasa
igual con y sin el arreglo no prueba nada.

**El gate es `pytest` pelado** desde `backend/`. Enumerar carpetas a mano da un subconjunto que
*parece* completo y no lo es.

---

## 16. Riesgos

| Riesgo | Prob. | Impacto | Mitigación |
|---|---|---|---|
| Fuga de costo, margen o proveedor al cliente | Alta si se reutiliza código | Crítico | Router aislado + DTO propios + `test_fugas.py` que prohíbe campos por nombre |
| Un cliente ve datos de otro | Alta sin Fase 1 | Crítico | `PortalScope` que falla cerrado + candado sobre `base` (§9.4) + `test_aislamiento.py` |
| El backfill de `clientes` sale mal y mezcla contratistas con la minera madre | Media | Alto | Reporte de huérfanas + revisión humana antes de dar de alta cuentas |
| Una foto filtra al proveedor | Media | Alto | Foto al despacho (no en recepción) + derivado sin EXIF + tabla separada |
| Fecha prometida que se mueve | Baja tras §7 | Alto | Congelado en el Cierre de Venta + `test_fecha_promesa.py` |
| Datos internos desactualizados: un ítem congelado 40 días | Media | Alto | Si un ítem no se mueve en X días, el portal lo dice y escala internamente |
| El portal genera más llamadas de las que ahorra | Media | Medio | Piloto con un cliente; salida humana visible en toda pantalla |
| El `#ea6c0d` se cuela como color de texto | Alta | Medio | Token separado `--orange-ink` + revisión de contraste |

---

## 17. Deploy

Orden estricto. **Las migraciones van ANTES de reiniciar el backend** (trampa conocida: el ORM ya
declara la columna y un `SELECT` contra la tabla vieja revienta con 1054).

```
1. Respaldo de la base
2. python -m migrations.portal_users_rol
3. python -m migrations.portal_cotizacion_cliente_id
4. python -m migrations.portal_fecha_promesa_cliente
5. python portal/init_db.py
6. Backfill de clientes + cotizaciones.cliente_id  → revisar el reporte de huérfanas
7. python deploy/audit_schema.py                   → dice qué falta
8. Reiniciar el backend
9. Build del frontend
10. Alta de la primera cuenta de portal y prueba de humo con un cliente real
```

---

## 18. Lo que este portal NO hace

Dicho explícitamente para que nadie lo asuma:

- No muestra saldos, pagos, estado de cobranza ni factoring.
- No muestra notas de crédito (no existen en el sistema).
- No permite al cliente escribir: ni subir su OC, ni pedir cotización, ni aprobar.
- No muestra fechas estimadas de llegada, solo la comprometida.
- No muestra el nombre del fabricante ni la ciudad de origen en los hitos.
- No muestra el % de cumplimiento de fecha (diferido por decisión del dueño).
- No tiene integración con SAP ni Ariba. El Excel cumple esa función.

---

## 19. La condición de continuidad

Puesta por el propio gerente de procurement en la revisión:

> Si más del **25%** de los ítems se entrega después de la **fecha comprometida** que el portal
> mostró, el portal deja de ser creíble.

Si al mes 3 del piloto se cruza ese umbral, **se apaga el portal y se arregla el proceso.**
No se agregan pantallas.

---

## Anexo — Créditos del enjambre

| Experto | Aportó |
|---|---|
| **UX de producto B2B + investigación de usabilidad** | Los 3 momentos de confusión del primer minuto; el panel lateral con URL propia (procurement reenvía links); 4 columnas en vez de 9; el orden "pendientes primero por fecha"; los textos exactos de estados vacíos; tarjetas en móvil en vez de scroll horizontal; los 7 detalles que deciden el uso |
| **Arquitectura de accesibilidad** | El cálculo real del contraste del naranjo (3.16:1, falla AA); el marcado de tabla y de línea de tiempo; el anillo de foco; números tabulares y monoespaciada para N° de parte; estimado vs. hecho |
| **Arquitectura de software** | La estructura exacta del paquete; los patrones de error, logging, consultas y migraciones con su `archivo:línea`; el esqueleto del test de aislamiento; las 8 reglas de debuggabilidad |
| **Búsqueda y normalización** | El contrato existente con el bug que originó cada regla; los dos huecos (OC sin colapsar, pasada B global); el orden candado-antes-que-búsqueda; los 6 casos de prueba con sondas |
