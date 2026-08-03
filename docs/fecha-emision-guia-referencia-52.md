# Fecha de emisión de la guía de despacho (referencia 52 de la factura)

**Fecha:** 2026-07-30 · **Marcas:** MachParts / Grupo AM **y** MonzaParts (espejo)
**Migración obligatoria:** `python -m migrations.despacho_fecha_guia` **antes de reiniciar**

---

## 1. El problema en una frase

La factura electrónica (DTE 33) tiene que citar la guía de despacho en una **referencia
tipo 52** con el **folio** de la guía **y la fecha en que esa guía se emitió**. Cuando la
guía se emite **fuera de PartsControl** —en el portal del SII o con talonario en papel— y
en el sistema sólo se registra su número, esa fecha **no existía como dato**: el código
usaba `despacho.fecha_despacho` como sustituto.

`fecha_despacho` **no es la fecha de la guía**. Es el instante en que se cerró el despacho
en PartsControl, escrito por el reloj del servidor en el endpoint `cerrar`. Emitir la guía
un día y cerrar el despacho en el sistema otro es lo habitual, así que el DTE 33 salía
**real, al SII, referenciando la guía con una fecha que la guía no tiene**.

Un DTE emitido no se corrige: el error aparecía con la factura ya en la contabilidad del
cliente.

### Las cuatro fechas de un despacho (fuente de la confusión)

| Campo | Qué significa | Quién la escribe |
|---|---|---|
| `fecha_creacion` (GA) / `fecha` (Monza) | Cuándo se armó el despacho en PartsControl | Servidor, al crear |
| `fecha_despacho` | Cuándo se **cerró** el despacho (salió la mercadería) | Servidor, en `cerrar` |
| `fecha_firma` | Cuándo el **cliente firmó** la guía recibida | Operador (opcional) |
| **`fecha_guia`** ← **nueva** | Cuándo se **emitió la guía ante el SII** | **Operador**, a mano |

Sólo la última sirve para la referencia 52.

---

## 2. Qué se hizo

1. Columna nueva **`fecha_guia DATE NULL`** en `despachos` (MachParts) y en
   `monza_despachos` (MonzaParts).
2. Campo **"Fecha de emisión de la guía"** en el modal *Agregar transportista / Editar*
   de Despachos, en las dos marcas, junto al N° de guía.
3. La referencia 52 de la factura toma la fecha **sólo** de `fecha_guia`.
   **Ya no lee `fecha_despacho` en ningún camino.**
4. Si la guía es de papel y **no tiene `fecha_guia`, la emisión de la factura al SII se
   BLOQUEA** con un mensaje que dice qué falta y dónde cargarlo.

### Por qué bloquear y no seguir con la fecha vieja

Sustituir una fecha desconocida por otra "parecida" es exactamente lo que causó el
problema. La regla de este módulo —y la decisión explícita del dueño al pedir el
cambio— es que **un guard que falla abierto es peor que ninguno**: ante un documento
tributario irreversible con un dato ambiguo, se bloquea y se pide el dato.

### Guía electrónica (Wasabil): no cambia nada

Si la guía 52 se emitió por el sistema, la fecha sale del `documentDate` del propio DTE
—la fecha tributaria verdadera— y `fecha_guia` **se ignora**. En la interfaz el campo
aparece deshabilitado, igual que el N° de guía.

---

## 3. Archivos tocados

### Base de datos

| Archivo | Qué |
|---|---|
| `backend/migrations/despacho_fecha_guia.py` | **NUEVO.** Agrega `fecha_guia` a `despachos` y `monza_despachos`. Idempotente; si una tabla no existe, avisa y sigue |
| `backend/models/models.py` → `Despacho` | `fecha_guia = Column(Date, nullable=True)` |
| `backend/monza_models.py` → `MonzaDespacho` | idem |

### Backend MachParts

| Archivo | Qué |
|---|---|
| `backend/routers/despachos.py` | `DespachoUpdate.fecha_guia` (texto `AAAA-MM-DD`, tri-estado) · `_parse_fecha_guia()` valida y convierte · el `PUT` la persiste como `Date` · sale serializada en la lista y en el detalle |
| `backend/wasabil_dte/router.py` | `_fecha_guia_papel()` **nuevo** (fuente única) · lo usan los **dos** resolvedores de la referencia 52 |

### Backend MonzaParts *(paquetes propios — sin imports cruzados con MachParts)*

| Archivo | Qué |
|---|---|
| `backend/monza_router_despachos.py` | `ActualizarDespachoBody.fecha_guia` · `_parse_fecha_guia()` propio · `PUT` y serializaciones |
| `backend/monza_wasabil_dte/router.py` | `_fecha_guia_papel()` propio · lo usa `_referencia_guia_de_despacho` |

### Frontend

| Archivo | Qué |
|---|---|
| `frontend-src/src/pages/DespachosPage.tsx` | Campo de fecha en `EditarDespachoModal` · aviso ámbar «Falta fecha de la guía» en la tarjeta · `Input` acepta `type`/`max` |
| `frontend-src/src/pages/MonzaDespachosPage.tsx` | Campo de fecha en `EditarCabeceraModal` · aviso «⚠ sin fecha» en la columna N° Guía |
| `frontend-src/src/pages/FacturasPage.tsx` · `MonzaFacturasPage.tsx` | El selector de guía del modal «Emitir factura» marca «(sin fecha)» |
| `backend/routers/contabilidad.py` · `backend/monza_contabilidad/router.py` | `fecha_guia` en `/despachos-facturables`, que alimenta ese selector |

**Por qué el aviso también está en Facturas:** quien factura (Contabilidad) no es quien
carga la guía (Bodega). Sin el aviso en el selector, el bloqueo aparecía recién al apretar
*Emitir*, después de elegir la guía y llenar el formulario.

### Tests

| Archivo | Qué |
|---|---|
| `backend/wasabil_dte/tests/test_fecha_guia_papel.py` | **NUEVO.** MachParts |
| `backend/monza_wasabil_dte/tests/test_fecha_guia_papel.py` | **NUEVO.** MonzaParts |

---

## 4. Nota para quien mantenga esto: hay DOS gemelos en MachParts

`backend/wasabil_dte/router.py` resuelve la referencia 52 en **dos lugares distintos**, y
**los dos están en el camino de la emisión**:

| Función | Rol | La corren |
|---|---|---|
| `_preparar_emision_factura` (bloque en línea) | **Guard** previo: decide si se puede emitir | `POST /facturas/preview` y la puerta de `POST /facturas/emitir` |
| `_guia_referencia_de_factura` (vía `_armar_payload_factura`) | **Arma el documento** que viaja al SII | `POST /facturas/emitir` y el reintento |

Si se arregla uno y no el otro, el guard dice *«puede emitir»* y el SII recibe otra fecha.
Por eso los dos llaman al **mismo** `_fecha_guia_papel()`, y la sección 4 de la suite de
tests es una **sonda anti-deriva**: lee el código fuente de ambos y falla si alguno vuelve
a leer `fecha_despacho`.

MonzaParts tiene **un solo** resolvedor (`_referencia_guia_de_despacho`), así que no
arrastra este riesgo.

---

## 5. Separación entre marcas

MachParts y MonzaParts **no comparten** nada de esto: tablas distintas, módulos SII
distintos, validadores distintos. El código está **duplicado a propósito** —MachParts ya
emite documentos tributarios reales y parametrizar un solo módulo para las dos marcas
pondría en riesgo al que ya funciona—. La sección 7 de cada suite lo verifica:

- cada marca tiene su `_fecha_guia_papel` en su propio módulo;
- el resolvedor de MachParts no menciona modelos de Monza;
- el de Monza consulta `MonzaDespacho` y no importa `models.models`.

---

## 6. Deploy

### 6.1 El comando

Desde `backend/`, con el venv del servidor, **antes de reiniciar uvicorn**:

```bash
python -m migrations.despacho_fecha_guia
```

Idempotente: correrlo de nuevo no rompe nada. No toca ni un dato existente.

Salida esperada la primera vez:

```
[migracion] despachos.fecha_guia agregada
[migracion] monza_despachos.fecha_guia agregada
[migracion] completada
```

### 6.2 Si se salta

Los modelos ya declaran la columna, así que **el ORM la pide en cada `SELECT`** de la
entidad despacho. MySQL responde `1054 Unknown column 'despachos.fecha_guia'` y se caen
con **HTTP 500** la pantalla de **Despachos de las dos marcas** y la emisión de facturas
al SII. El backend arranca igual: la falla aparece al abrir la pantalla, no en el log de
arranque.

(**Bodega NO se cae**, comprobado compilando el SQL real: `routers/bodega.py` ni siquiera
importa `Despacho`, y `monza_router_bodega.py` selecciona columnas puntuales de
`monza_despacho_items`. Se aclara porque la primera versión de este doc decía lo
contrario.)

Ya está agregado a `docs/CHECKLIST-DEPLOY-2026-07-20.md` §1.a (y referenciado desde §1.b),
así que `deploy/audit_schema.py --pasos` lo da por documentado.

### 6.3 Verificación después del deploy

⚠️ Este comando va **desde el docroot** (la carpeta que contiene `backend/` y `deploy/`),
**no** desde `backend/` como el de §6.1: `deploy/` es hermano de `backend/`.

```bash
backend/venv/bin/python deploy/audit_schema.py
```

No debe reportar columnas faltantes en `despachos` ni en `monza_despachos`. Verificado con
sonda: si se borran las columnas, el auditor las delata con
`[despachos] COLUMNA FALTANTE fecha_guia (DATE)` y sale con RC=1.

---

## 7. Efecto en el día a día (esto hay que avisarlo al usuario)

**Los despachos con guía en papel que ya existen quedan con `fecha_guia` vacía** —no hay
backfill: inventar la fecha es justo el bug que se está cerrando—.

Consecuencia concreta: **al intentar facturar uno de esos despachos al SII, se bloquea**
con este mensaje:

> La guía en papel N° 12345 no tiene registrada su FECHA DE EMISIÓN: cárgala en
> Despachos → Editar (botón del transportista) y vuelve a facturar. La referencia a la
> guía que lleva la factura ante el SII debe ir con la fecha en que se EMITIÓ la guía —
> no la de la firma del cliente ni la del cierre del despacho en el sistema, que es lo
> que se usaba antes y salía equivocado.

Se resuelve en dos clics y una sola vez por despacho. Para que no sorprenda recién al
facturar, la pantalla de **Despachos marca en ámbar** los que están en esa situación
(«Falta fecha de la guía» en MachParts, «⚠ sin fecha» en MonzaParts).

**No afecta** a las guías emitidas electrónicamente ni a las facturas ya emitidas.

### Consulta para medir el impacto antes del deploy

```sql
SELECT COUNT(*) FROM despachos d
 WHERE d.numero_guia IS NOT NULL AND d.numero_guia <> '' AND d.estado <> 'anulado'
   AND d.fecha_guia IS NULL
   AND NOT EXISTS (SELECT 1 FROM wasabil_dte w
                   WHERE w.despacho_id = d.id AND w.tipo_dte = 52 AND w.status_id = 3);
```

Los despachos de MachParts que van a pedir la fecha. Las dos últimas condiciones importan:
sin `fecha_guia IS NULL` cuenta los que ya están resueltos, y sin el `NOT EXISTS` cuenta
también las guías **electrónicas** — al emitirlas el sistema copia el folio del SII a
`numero_guia`, así que "tiene N° de guía" no significa "es de papel". En un sitio que emite
electrónicamente, la consulta sin ese filtro puede contar casi todo.

El equivalente de Monza es el mismo, sobre `monza_despachos` y `monza_wasabil_dte`.

---

## 8. Validación de lo que se teclea

`_parse_fecha_guia` (uno por marca) corre en el `PUT` y rechaza con **400**:

| Caso | Motivo |
|---|---|
| Formato que no sea `AAAA-MM-DD` | La fecha va a un documento tributario |
| Fecha **futura** | Una guía no se emite mañana; casi siempre es un tipeo |
| Fecha de **más de 2 años** | Cazafallas del error clásico: el año equivocado |
| Vacío o `null` | **Se acepta**: borra la fecha (y vuelve a bloquear la facturación) |

El selector del navegador también topa en hoy (`max`), pero eso es comodidad: la
validación que manda es la del backend.

**Tri-estado del `PUT`:** un `PUT` que **no menciona** `fecha_guia` no la toca (gracias a
`exclude_unset`). Sólo la borra si se manda explícitamente `null`. Está cubierto por
tests (`8e` en MachParts, `6e` en Monza).

**Lo que la malla de 2 años NO ataja, y qué lo ataja:** un typo de **un año**
(`2025-07-15` por `2026-07-15`) cae muy dentro de los 730 días y pasa limpio. Bajar el
tope bloquearía regularizaciones legítimas, así que el control que cubre el error hacia
adelante es el de la sección siguiente.

---

## 8-bis. La guía no puede estar fechada después de su factura

Lo encontró la revisión adversarial de este mismo cambio. `fecha_emision` de la factura es
un campo **libre, sin ningún tope** (`FacturaCreate.fecha_emision`, un `<input type="date">`
sin `min`/`max`), y `armar_referencias_factura` **no recibía** esa fecha, así que nadie
cruzaba las dos: backdatando la factura se llegaba a un **DTE 33 REAL que declara haberse
emitido ANTES que la guía que dice amparar**. Ninguna de las dos fechas es inválida por sí
sola — lo inválido es el orden.

Ahora `armar_referencias_factura` recibe `fecha_documento` y **bloquea** si
`guia_fecha > fecha_documento`, en las dos marcas
(`wasabil_dte/service.py`, `monza_wasabil_dte/service.py`). El mismo día es válido.

Los dos caminos pasan el valor que el documento va a llevar de verdad:

| Camino | De dónde sale `fecha_documento` |
|---|---|
| Guard (`_preparar_emision_factura`) | `_parse_date(payload.fecha_emision) or hoy_chile()` — la misma fórmula con que se persiste |
| Documento real (`_armar_payload_factura`) | `factura.fecha_emision or hoy_chile()` — el mismo valor que `armar_factura` pone en `documentDate` |

**Queda abierto (decisión del dueño):** `fecha_emision` sigue sin tope hacia el **futuro**.
Se puede emitir una factura fechada el año que viene. Acotarlo toca también el registro
manual de facturas ya emitidas, que sí necesita fechas pasadas, así que no se cambió aquí.

---

## 9. Cómo correr los tests

```bash
python -m pytest wasabil_dte/tests/test_fecha_guia_papel.py monza_wasabil_dte/tests/test_fecha_guia_papel.py -q
```

Los casos están armados para **fallar si se revierte el arreglo** (sondas de poder
discriminante): el escenario pone la fecha de la guía y la del cierre del despacho
**distintas a propósito**, así que con el código viejo la primera sección se cae.
