# Paquete «fotos-de-recepción» — qué se cambió y cómo se verifica (2026-10-06)

Alcance: las fotos de recepción de bodega de **Grupo AM** (`backend/routers/bodega.py`,
`BodegaPage.tsx`, `FotoBodega.tsx`, `FotosItemModal.tsx`) y dos cascarones que llamaban a
mano la limpieza del caché de fotos. **MonzaParts no se toca** (queda como decisión del
dueño, al final).

Sin migración ni `init_db`: para desplegar basta reiniciar el backend y publicar el build
del frontend. Conviene publicarlos **juntos**: el frontend nuevo es el que muestra los
mensajes 400/409 del borrado de fotos; el anterior sigue funcionando, pero los pinta como
«Error al eliminar foto». Revertir cualquiera de los dos no deja datos en un estado que
el otro no entienda.

---

## 1. Qué se arregló, por ítem

### P3 — Subir o borrar una foto respeta el estado y el candado de la recepción

**Antes.** `upload_foto` y `delete_foto` no miraban si la recepción estaba cerrada ni
tomaban su candado. Una pantalla abierta desde antes del cierre podía borrar la foto que
respalda un reclamo al proveedor; y una foto subida mientras otro operador guardaba el
mismo ítem creaba una **segunda fila** de ese ítem en `recepcion_embarque_items`
(`_resolver_ri` no veía la fila sin confirmar y `upload_foto` insertaba otra). Además,
`upload_foto` era `async def`: cualquier espera por el candado detenía el único proceso
del servidor para todos los usuarios.

**Ahora** (`backend/routers/bodega.py`):

- `_recepcion_abierta_con_lock(db, recepcion_id)` (nuevo, bajo el rótulo «UPLOAD FOTO»):
  lee la recepción con `FOR UPDATE` y responde **400 «Recepción no encontrada o ya
  cerrada»** si no existe o ya cerró. Mismo SELECT y mismo 400 que `marcar_item`.
- `upload_foto`: pasa de `async def` a **`def`** (FastAPI la corre en un hilo aparte y la
  espera por el candado no detiene el servidor) y llama al helper como **primera
  sentencia**, antes de `_resolver_ri`. Se quitó la relectura plana de la recepción que
  había dentro de `if not ri:`; la pertenencia usa el `rec` del helper.
- `delete_foto`: llama al helper como primera sentencia, y además **no deja borrar la
  última foto de un ítem guardado como dañado** (409 «Un ítem guardado como dañado
  necesita al menos una foto: sube la de reemplazo antes de borrar esta, o cambia su
  estado y guarda.»). Es la misma regla que `marcar_item` exige al guardar; se cuenta
  bajo el candado de la recepción.
- `_resolver_ri`: solo docstring (la precondición del candado que ahora cumplen sus dos
  llamadores).

**Cambios de contrato** (solo alcanzables por API o por una pantalla atrasada): recepción
inexistente o cerrada en `upload_foto` responde 400 (antes 404 solo si el ítem no estaba
empezado, y nada si estaba cerrada); `delete_foto` con recepción cerrada responde 400 (a
propósito no 404: la pantalla trata el 404 como «esa foto ya no existe» y la saca de la
lista); última foto de un ítem dañado → 409.

**Pantalla** (`frontend-src/src/pages/BodegaPage.tsx`, `RecepcionItemRow.handleDeleteFoto`):
el `catch` muestra el `detail` del backend (un 400 o un 409 ya no se pinta como «Error
al eliminar foto»); el 404 sigue sacando la foto de la lista. En `handleFoto`, la
variable del `catch` pasa de `e` a `err` (tapaba al `e` del evento que el `finally` usa) y
su comentario deja de citar un 409 que el backend no emite.

### P20 — Archivo y fila van juntos en `upload_foto`

**Antes.** Si fallaba la escritura del archivo (disco lleno, permisos) o el commit, quedaba
un archivo suelto en `uploads/bodega` que nadie podía pedir ni borrar (`serve_foto` y
`delete_foto` parten de la fila), y la respuesta era un 500 de texto plano.

**Ahora.** `_descartar_archivo(ruta)` (nuevo, justo antes de `upload_foto`) borra el
archivo; `upload_foto` envuelve la escritura en `try/except OSError` (borra el archivo,
anota el errno en el log con el prefijo `[bodega]` y responde **500 con `detail`** «No se
pudo guardar la foto en el servidor. Intenta de nuevo; si se repite, avisa a soporte.»)
y envuelve el `db.commit()` (rollback, borra el archivo y relanza). El camino de éxito no
cambia. El orden archivo → fila del **borrado** no se toca.

Es el único tramo del paquete que cambia el orden original del programador (escribir y
después confirmar). Es separable de P3: si se prefiere no tocarlo en esta entrega, se
retiran `_descartar_archivo` y los dos `try/except` sin afectar al resto.

### C10, C11, P10 — Comentarios y docstrings de `bodega.py` y de la suite auth

Solo texto, sin lógica. En `bodega.py`: el comentario del import de `require_empresa`
(decía que el candado estaba en una sola ruta; lo llevan `buscar_items` y las cuatro
rutas de fotos), el de `_FOTOS_DIR` (tildes), el docstring y el comentario del caso
ambiguo de `_resolver_ri` (un 409 que nunca estuvo en git, «la historia, 4ª versión»), el
comentario del nombre de archivo en `upload_foto`, los tres comentarios de `delete_foto`
(uno afirmaba que `cerrar_recepcion` exige fotos: no las revisa), el encabezado «SERVIR
FOTOS», el docstring de `_ruta_confinada_foto` y el punto 1 y el comentario de
`Cache-Control` de `serve_foto`.

En `routers/tests/test_bodega_fotos_auth.py`: cabecera (QUÉ PROTEGE en presente; SONDAS
corregidas: `_paso_6a`, `_paso_6b` y `_paso_7` se atribuían a `_resolver_ri`, que
`delete_foto` no llama) y docstrings de `_paso_3`, `_paso_5`, `_paso_6a`, `_paso_6b`,
`_paso_7`, `_paso_8`, `_paso_9`, `_paso_10` y `_paso_12`.

**Desviación del plan, encontrada con el arnés:** la sonda de `_paso_1` decía «quitar
`Depends(get_current_user)` de `serve_foto`». Esa mutación sola deja la etapa VERDE,
porque `require_empresa` (a nivel de ruta) ya incluye `get_current_user` y corta al
anónimo. La línea quedó redactada con las DOS capas (quitar `require_empresa` de la ruta
y `Depends(get_current_user)` del parámetro), que es la mutación que sí la pone roja.

**Segunda desviación, menor:** el `finally` de `_paso_9` borraba sus filas pero no los
archivos de sus dos subidas, así que cada corrida dejaba dos archivos de 12 bytes
huérfanos en `uploads/bodega`. Ahora los borra junto con las filas (los ocho que dejaron
las corridas de esta pasada se eliminaron; quedan dos anteriores, `recepcion_1020715_…`
y `recepcion_1020716_…`, del 2026-10-05, que no son de esta pasada y se dejaron).

### C29 — El campo `url` de las fotos (solo documentar)

Sin cambio de contrato. En `upload_foto` (sobre `foto = RecepcionItemFoto(`) se explica
que `url_foto` guarda la ruta histórica `/uploads/bodega/<archivo>`, que viaja en el
campo `url` de las respuestas, sirve para ubicar el archivo al depurar y por HTTP solo
resuelve con `SERVIR_FOTOS_BODEGA_SIN_AUTH=true`; y una línea de puntero sobre la
entrada `"fotos"` de `get_recepcion` y de `list_reclamos`.

### C17 — `riId` y la `key` de las filas de recepción (`BodegaPage.tsx`)

- Se borró `useEffect(() => { setRiId(item.id) }, [item.id])`: era código muerto (cada
  `load` de `RecepcionPanel` muestra el spinner y desmonta todas las filas, así que el
  valor inicial siempre nace de un `item` fresco) y su comentario instalaba un modelo
  falso del ciclo de vida. `riId` se conserva.
- La `key` de cada `<RecepcionItemRow>` pasa de `item.id` a
  `` item.recepcion_item_id != null ? `ri-${…}` : `ei-${…}` ``: `item.id` mezcla el id
  de la fila de recepción con el del ítem de embarque, y dos filas de una misma recepción
  pueden compartir ese número (es el dato que P3 evita de aquí en adelante y que puede
  existir ya en producción).

### C37 — Tipo de la respuesta de `subirFoto` (`services/api.ts`)

Interfaz nueva `FotoSubida` `{ id, url, recepcion_item_id }` junto a `FotoRecepcionItem`, y
`subirFoto` devuelve `api.post<FotoSubida>(...)`. El `build` corre `tsc` en modo
estricto, así que un nombre mal escrito en `handleFoto` (`data.foto_id`,
`data.recepcion_item`) deja de compilar en vez de dar `undefined` en silencio.

### C18 — Lo que venía viajando no vuelve al caché al cambiar la sesión (`FotoBodega.tsx`)

`limpiarCacheFotos` ahora sube la generación de cada petición en vuelo
(`for (const id of enVuelo.keys()) generacion.set(id, gen(id) + 1)`): una foto que estaba
viajando al cerrar sesión se descarta al llegar (y su blob se revoca) en vez de entrar a
un caché ya vaciado. El comentario que describía un mecanismo inexistente se fue.

### C35 — El caché de fotos se vacía con CADA cambio de token, no desde cada botón

`FotoBodega.tsx` se suscribe al store de sesión (`useAuthStore.subscribe`, justo debajo de
`limpiarCacheFotos`) y vacía el caché cuando cambia `token`: botón de salir de cualquier
cascarón, un 401, un inicio de sesión, o la otra pestaña que entra con otra cuenta (el
oyente `storage` de `stores/authStore.ts`). `limpiarCacheFotos` deja de exportarse
(`tsc` falla con TS2614 si alguien la vuelve a importar) y ya no hace `montadas.clear()`
(con miniaturas que siguen en pantalla, vaciar el conteo dejaba que `podar()` revocara una
foto visible). Se quitan el import y la llamada manual en `pages/DashboardLayout.tsx`
(`handleLogout`) y en `portal/PortalLayout.tsx` (`cerrarSesion`). `MonzaLayout.tsx` no se
toca: no llamaba y su `logout()` cambia el token igual.

Estos tres archivos van en un mismo commit: por separado no compila.

### C36, P19 — La fecha de cada foto en la galería de Despachos es el día de Chile

`FotosItemModal.tsx` pierde su `fmtFecha` local (leía como hora local un ISO que el
servidor guarda en UTC y sin zona: una foto de las 20:30 se rotulaba con el día siguiente)
y pinta `fmtFechaServidor(f.fecha)` de `utils/format.ts`, el mismo formateador que usa la
fecha del despacho en esa pantalla. Cambio visible: «20 ago 2026» pasa a «20-08-2026»; una
foto sin fecha pinta «—». No se toca la pestaña «Foto despacho» ni el encabezado.

---

## 2. Archivos tocados

| Archivo | Símbolos | Ítems |
|---|---|---|
| `backend/routers/bodega.py` | `_recepcion_abierta_con_lock` (nuevo), `_descartar_archivo` (nuevo), `upload_foto`, `delete_foto`, `_resolver_ri` (docstring), comentarios de `_FOTOS_DIR`, import de `require_empresa`, «SERVIR FOTOS», `_ruta_confinada_foto`, `serve_foto`, `get_recepcion` y `list_reclamos` (una línea de comentario cada uno) | P3, P20, C10/C11/P10, C29 |
| `backend/routers/tests/test_bodega_fotos_candado.py` | **nuevo** (5 etapas, 29 comprobaciones) | P3, P20 |
| `backend/routers/tests/test_bodega_fotos_auth.py` | `_crear_datos` (`emb_b`), `_fotos_del_item` y `_paso_6d` (nuevos), `run`, cabecera y docstrings | P3, C10/C11/P10 |
| `backend/routers/tests/test_fotos_bodega_espejo_frontend.py` | **nuevo** (3 pruebas: caché, fecha, estática) | C18, C35, C36/P19 |
| `backend/portal/tests/mutaciones/bodega.py` | **nuevo**: 13 mutaciones del candado + 19 de auth | todas las del backend |
| `frontend-src/src/services/api.ts` | `FotoSubida` (nueva), `bodegaAPI.subirFoto` | C37 |
| `frontend-src/src/pages/BodegaPage.tsx` | `RecepcionItemRow` (`riId`, `handleFoto`, `handleDeleteFoto`, comentario del bloque Fotos), `RecepcionPanel` (key) | P3, C17, C37 |
| `frontend-src/src/components/FotoBodega.tsx` | encabezado, `generacion`, `limpiarCacheFotos` (sin `export`), suscripción al store, comentarios de `cargar` y `onError` | C18, C35 |
| `frontend-src/src/pages/DashboardLayout.tsx` | `handleLogout` e import | C35 |
| `frontend-src/src/portal/PortalLayout.tsx` | `cerrarSesion` e import (archivo sin commit de otro equipo: solo esas 6 líneas) | C35 |
| `frontend-src/src/components/FotosItemModal.tsx` | `fmtFecha` (eliminada), figcaption de la pestaña de recepción, import (archivo staged de otro equipo: solo esas líneas) | C36/P19 |
| `docs/fase-0-portal-cambios-2026-08-20.md`, `docs/fase-1-portal-identidad-2026-10-02.md` | correcciones de texto que el plan nombra (ver §7) | — |

Todos los archivos del programador se editaron con reemplazos exactos; `git diff
--numstat` muestra solo las líneas realmente cambiadas (todo en LF, como estaba).

---

## 3. Cómo verificar

Todo corre desde `backend/`, **con uvicorn detenido**, de a una suite (comparten la base
local; cada una limpia por su propia marca).

```bash
cd backend
./venv/bin/python -m pytest routers/tests/test_bodega_fotos_candado.py -q      # 1 passed (29 comprobaciones)
./venv/bin/python -m pytest routers/tests/test_bodega_fotos_auth.py -q         # 1 passed (57 comprobaciones)
./venv/bin/python -m pytest routers/tests/test_fotos_bodega_espejo_frontend.py -q   # 3 passed (necesita node + npm install en frontend-src; si faltan, se salta diciendo por qué)
./venv/bin/python -m pytest routers/tests/test_bodega_despachos_flujo.py -q    # 1 passed
./venv/bin/python -m pytest routers/tests/test_paridad_inversa_bodega.py -q    # 1 passed
./venv/bin/python -m pytest tests_infra/test_suites_visibles.py -q             # 2 passed
```

```bash
cd frontend-src
npx tsc -p tsconfig.json --noEmit     # 0 errores
npx tsc -b                            # sin diagnósticos (no regeneró vite.config.js)
```

### Pruebas primero

Las tres suites se escribieron **antes** del arreglo y se corrieron contra el código de
ese momento:

- candado: 17 comprobaciones rojas (`_paso_1` a `_paso_4`; `_paso_0` verde). La etapa 3
  confirmó las dos razones del plan: durante la espera el servidor no atendió el GET
  («sin respuesta») y nacieron **2 filas** del mismo ítem.
- espejo: `test_cache_de_fotos_es_de_una_sesion` roja (8 comprobaciones) y
  `test_estatico_la_galeria_usa_el_formateador_compartido` roja en su primera aserción;
  `test_fecha_de_la_foto_es_el_dia_de_chile` verde (amarra una regla que ya existía).
- auth: verde, incluida la etapa nueva `_paso_6d` (sonda de una condición que ya existía).

Con los arreglos aplicados, todo lo de arriba pasa.

### Mutaciones (arnés del repo)

```bash
cd backend
./venv/bin/python -m portal.tests.mutaciones fotos_candado   # 13 de 13 mutaciones detectadas
./venv/bin/python -m portal.tests.mutaciones fotos_auth      # 19 de 19 mutaciones detectadas
```

El arnés copia `backend/` a una carpeta temporal, rompe el código a propósito (una
mutación por regla) y exige que la suite se ponga roja. La lista es
`backend/portal/tests/mutaciones/bodega.py`. Las de auth incluyen las sondas del
encabezado de la suite (es lo que se reescribió en C10/C11/P10) y las tres búsquedas de
`_resolver_ri` más la pertenencia de `upload_foto` (`_paso_6d`).

### Mutaciones del frontend (a mano, sobre una copia temporal)

El arnés copia solo `backend/`, así que las mutaciones que viven en `.tsx`/`.ts` se
verificaron sobre una copia temporal de `frontend-src` (nunca sobre el árbol de trabajo),
corriendo el pytest espejo apuntado a esa copia:

| Mutación | Resultado |
|---|---|
| C18: quitar `for (const id of enVuelo.keys()) generacion.set(id, gen(id) + 1)` | roja |
| C35: quitar `useAuthStore.subscribe(...)` | roja |
| C35: quitar la condición `estado.token !== anterior.token` | roja |
| C35: volver a poner `montadas.clear()` | roja |
| C36: figcaption con `new Date(f.fecha).toLocaleDateString(...)` (vuelta completa) | roja (primera aserción) |
| C36: vuelta parcial `f.fecha ? fmtFechaServidor(f.fecha) : new Date(f.fecha)…` | roja (segunda aserción) |
| P19: `fmtFechaServidor` sin el `+ 'Z'` | roja (con TZ=America/Santiago) |
| P19: `fmtFechaServidor` sin `timeZone: 'America/Santiago'` | roja (con TZ=UTC) |
| control: `const AHORA = new Date()` en otra parte del modal | sigue verde (la estática no prohíbe `Date` en el archivo) |
| control: `new Date(f.fecha_publicacion)` en la pestaña de despacho | sigue verde |

Y con `tsc` sobre otra copia: `data.foto_id` → TS2339; `data.recepcion_item` → TS2551;
volver a importar `limpiarCacheFotos` en `DashboardLayout.tsx` → TS2614. **3 de 3.**

### Sondas manuales (no ejecutadas: no se levantó `vite` ni un navegador)

- P3: en una recepción abierta, ítem guardado como dañado con una sola foto: la X muestra
  «Un ítem guardado como dañado necesita al menos una foto…» y la miniatura sigue. Con dos
  pestañas: cerrar la recepción en una y borrar una foto en la otra muestra «Recepción no
  encontrada o ya cerrada».
- C17: con `npm run dev`, una recepción donde el id de un ítem marcado coincide con el id
  de EmbarqueItem de uno sin marcar no muestra «two children with the same key».

---

## 4. Cómo revertir

- **Todo el paquete:** `git checkout HEAD -- backend/routers/bodega.py
  backend/routers/tests/test_bodega_fotos_auth.py frontend-src/src/services/api.ts
  frontend-src/src/pages/BodegaPage.tsx frontend-src/src/components/FotoBodega.tsx
  frontend-src/src/pages/DashboardLayout.tsx` y borrar los tres archivos nuevos
  (`test_bodega_fotos_candado.py`, `test_fotos_bodega_espejo_frontend.py`,
  `portal/tests/mutaciones/bodega.py`). `PortalLayout.tsx` y `FotosItemModal.tsx` son de
  otro equipo y no están en HEAD como están hoy: ahí se revierten a mano solo las líneas
  de este paquete (volver a importar y llamar `limpiarCacheFotos()` en `cerrarSesion`;
  volver a `fmtFecha` local). Sin migración, no hay datos que deshacer.
- **Solo P20** (si el programador prefiere no tocar ese tramo): quitar `_descartar_archivo`
  y los dos `try/except` de `upload_foto`, y la etapa `_paso_4` de la suite del candado
  con sus 3 mutaciones.
- **FotoBodega + DashboardLayout + PortalLayout** se revierten juntos: por separado no
  compila (TS2614).

---

## 5. Deuda que queda anotada

**Programador (Grupo AM):**
- Índice `UNIQUE (recepcion_id, embarque_item_id)` en `recepcion_embarque_items` como
  segunda capa (regla de `database.py` para la unicidad). Sin Alembic exige migración
  manual y falla si producción ya tiene duplicados; antes correr
  `SELECT recepcion_id, embarque_item_id, COUNT(*) FROM recepcion_embarque_items GROUP BY 1, 2 HAVING COUNT(*) > 1`.
- `crear_recepcion_item` inserta sin mirar si el ítem ya tiene fila (no tiene llamador
  en el frontend): otra fuente de filas duplicadas.
- `marcar_item` y `crear_recepcion_item` llevan el mismo SELECT bloqueante copiado en
  línea y podrían llamar a `_recepcion_abierta_con_lock` sin cambiar contrato. Trampa:
  `test_paridad_inversa_bodega.py` busca el texto `with_for_update` en el fuente de
  `marcar_item`; esa sonda se cambia en el mismo commit.
- `upload_foto` sigue validando solo la extensión y sin tope de tamaño (la documentación
  decía que lo resolvía la Fase 5 del portal; ese derivado se construyó por otro camino y
  no toca esta subida).
- `fmtDate` local de `BodegaPage.tsx` muestra el ETA del embarque un día antes en Chile
  (`fecha_llegada_est` llega como 'AAAA-MM-DD' y `new Date('2026-08-20')` se lee en UTC).
  `fmtDate` de `utils/format.ts` ya trae la corrección.
- Los ítems dañados que ya hubieran quedado sin foto por borrados anteriores no se
  revalidan.

**Portal:**
- `guardar` de `backend/portal/fotos.py` tiene el mismo hueco que tenía `upload_foto`: deja
  el archivo parcial si falla la escritura y responde 500 sin detalle.
- `limpiarArchivosDelPortal` (`portal/archivos.ts`) solo la llama `PortalLayout.cerrarSesion`;
  puede colgarse de la misma suscripción al store.
- `frontend-src/src/portal/README.md` («Una sesión por navegador»): agregar que el cambio
  de sesión desde otra pestaña también vacía el caché de fotos de bodega. No se editó acá
  (archivo del portal, en edición por otro equipo).

**MonzaParts:** `upload` y `borrar` de `monza_router_documentos.py` no miran si la
recepción del `recepcion_item` está cerrada ni la regla de foto del ítem dañado que
`monza_router_bodega.py` exige al marcar (`entidad='recepcion_item'`).

**Checklist de deploy:** confirmar una vez que el MySQL de producción estampa en UTC:
`SELECT @@global.time_zone, @@session.time_zone, NOW(), UTC_TIMESTAMP();` (`NOW()` debe
igualar a `UTC_TIMESTAMP()`). De esa premisa dependen `fmtFechaServidor`,
`fmtFechaHoraChile` y `dia_chile` de `backend/portal/tiempo.py`, no solo este modal.

---

## 6. Decisiones que quedan para el dueño

- Fotos de una recepción ya CERRADA: ¿se pueden agregar o borrar después del cierre? Hoy la pantalla lo impide, pero el servidor lo permitía: alguien con la pantalla abierta desde antes del cierre podía borrar la foto que respalda un reclamo al proveedor, sin dejar rastro. Mientras tanto se aplica lo conservador: una recepción cerrada no acepta fotos nuevas ni borrados. Si algún día hace falta corregir una foto después del cierre, se construye una puerta aparte que deje anotado quién, cuándo y por qué.
- Ítem guardado como DAÑADO: ¿puede quedar sin ninguna foto mientras la recepción sigue abierta? El sistema exige foto para guardarlo, pero después dejaba borrarla. Mientras tanto se aplica lo conservador: la última foto de un ítem dañado no se puede borrar; para reemplazarla, primero se sube la nueva y después se borra la anterior (o se cambia el estado del ítem y se guarda). La pantalla muestra ese mensaje.
- MonzaParts tiene la misma puerta abierta en su bodega: se puede agregar o borrar la foto de un ítem de una recepción cerrada, y borrar la única foto de un ítem dañado. Este paquete no toca Monza. Mientras tanto Monza queda como está, anotado como deuda; decidir si se hace el espejo.
- La subida de fotos ahora borra el archivo si falla al guardarlo (antes quedaba un archivo suelto ocupando disco que nadie podía ver ni borrar). Ese tramo es código original del programador. Mientras tanto se aplica, porque esa función ya se modifica por el candado y el cambio solo actúa cuando algo falla; si el programador prefiere no tocarlo en esta entrega, ese arreglo (P20) se saca sin afectar al resto.
- La subida de fotos de bodega acepta archivos de cualquier tamaño y solo revisa la extensión. La documentación decía que eso se resolvía con las fotos del portal, pero esas se construyeron por otro camino y esta subida quedó igual. Mientras tanto no cambia (solo la usa personal interno de Grupo AM); decidir si se le pone un tope de tamaño.
- En la ventana de fotos de Despachos, la fecha de cada foto pasa de «20 ago 2026» a «20-08-2026» (el mismo formato de la fecha del despacho en esa pantalla) y ahora muestra el día de Chile en que se tomó: hoy una foto subida de noche sale con el día siguiente. Mientras tanto queda el formato de la casa; si se prefiere el mes en letras, se cambia en un solo lugar.
- El candado por empresa del resto de la bodega (todo lo que no es fotos ni el buscador de ítems) sigue siendo la decisión pendiente que ya estaba anotada. Este paquete no lo cambia; solo corrige el comentario que decía que el candado estaba en una sola ruta.

---

## 7. Correcciones hechas a la documentación anterior

En `docs/fase-0-portal-cambios-2026-08-20.md`: §3 y §8 (las dos suites nuevas y el
tamaño real de la de auth), C1 (el campo `url` y el 500 con detalle de `upload_foto`), §10
fila 13 y las tablas de deuda de §10 y §11 (el derivado de la Fase 5 no cubre esta subida;
`serve_foto` sin 304 sigue abierta), nota tras la tabla de §12 (el 409 de la fila A ya no
existe; `riId` solo va en la URL de las fotos), nota al final de §13 (el comentario de
`_resolver_ri` ya no remite a «la historia, 4ª versión»), §14 filas 1-3 (quién exige la
foto del dañado; el backend rechaza con 400 en recepción cerrada; la pestaña «Foto
despacho» ya existe y la fecha se pinta en día de Chile) y §15 (se cita `upload_foto` en
vez de un número de línea; `limpiarCacheFotos` la dispara la suscripción, no el logout).

En `docs/fase-1-portal-identidad-2026-10-02.md`, «Una sesión por navegador»: el cambio de
sesión desde otra pestaña también vacía el caché de fotos de bodega.
