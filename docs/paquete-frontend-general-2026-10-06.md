# Paquete «frontend-general» — qué se cambió y cómo se verifica (2026-10-06)

Alcance: la política de sesión y permiso que comparten los dos clientes axios
(`frontend-src/src/services/authErrors.ts`), los ayudantes de lectura de errores que el
Portal de Clientes y Bodega usan en sus `catch`, y la prueba espejo que ata los textos del
frontend a los del backend. Sin migración ni `init_db`: para desplegar basta publicar el
build del frontend y reiniciar el backend (los cambios del backend son comentarios y una
prueba; el único que se ve es el del frontend).

Ningún archivo de `backend/auth.py` ni de `services/api.ts` / `services/monzaApi.ts` cambia.
Los archivos del programador que sí se tocan (`pages/BodegaPage.tsx`, `empresa_guard.py`,
`role_guard.py`) reciben solo las líneas que se listan en §2.

---

## 1. Qué se arregló, por ítem

### P14 — Una clave mal tipeada en el login ya no cierra la sesión que había (arreglado)

**Antes.** `manejarErrorAuth` (`services/authErrors.ts`) trataba CUALQUIER 401 como «la
sesión no vale»: cerraba la sesión del store y de `localStorage['machparts-auth']`. El 401
de `POST /auth/login` no habla de la sesión —juzga el correo y la clave del formulario— y
aun así la cerraba; como `stores/authStore.ts` hace que todas las pestañas adopten lo que
`logout()` escribe en localStorage, una clave mal tipeada en una pestaña dejaba fuera a
las demás.

**Ahora** (`services/authErrors.ts`):

- `RUTA_LOGIN = '/auth/login'` (constante nueva del módulo, no exportada): la ruta tal
  como la pide `authAPI.login`, sin `baseURL`.
- `manejarErrorAuth`: primera decisión de la función, antes de leer `status` y `detail`:
  `if (esAxios && (err.config?.url ?? '').endsWith(RUTA_LOGIN)) return 'ninguna'`.
  `clasificarError` no cambia; el contrato «NUNCA lanza» se respeta.
- Encabezado: sección nueva «EL LOGIN QUEDA FUERA» con la regla para endpoints nuevos (un
  401 nuevo que no hable de la sesión cerraría la sesión del usuario: usar 400/403/409).
  Las frases que narraban historia pasan a presente sin perder el porqué.

La exclusión aplica SOLO a esa petición: el 401 de `/auth/me` (misma carpeta de rutas) y
el de cualquier ruta de Monza siguen cerrando la sesión (paso 4 de la prueba).
`LoginPage` no cambia: su `catch` ya muestra el toast del 401.

### P12 — Código muerto `_resetRedireccion` (arreglado)

Se borra la función exportada `_resetRedireccion` y su comentario. No tenía importadores
(`grep -rIn resetRedireccion` en el repo sin `node_modules`/`dist` solo encontraba la
definición) y el build de producción ya la eliminaba. El porqué que insinuaba queda
escrito donde sirve, en el comentario de `yaRedirigiendo`: no hace falta volver a ponerla
en `false` porque `window.location.replace` recarga la página y el módulo empieza de cero.

### C40 — Los textos gemelos frontend ↔ backend tienen quien los vigile (arreglado)

`DETALLES_AMBITO` y `DETALLES_SESION` del frontend repiten textos que emite el backend. Si
un lado cambia y el otro no, el 403 cae en `negocio`: el aviso deja de salir sin que nada
falle a la vista. Ahora:

- `backend/routers/tests/test_auth_errors_espejo_frontend.py` (NUEVA): dos funciones.
  `test_cinturon_estatico` (sin node) lee el `.ts` y compara sus literales con lo que
  entrega el backend: los guards se EJECUTAN para que digan su `detail` (`require_empresa`
  con un usuario de otra empresa, `require_rol` con un rol que no alcanza), y
  `auth.DETALLE_CUENTA_PORTAL` y `auth_errors.DETALLES_SIN_SESION` se importan; también
  exige que `RUTA_LOGIN` coincida con la ruta que pide `authAPI.login` en `services/api.ts`.
  `test_espejo_compilado` compila con esbuild los dos clientes axios reales (con sus
  interceptores), el store real y `authErrors.ts`, y los ejecuta en Node reemplazando
  solo la red, `window`/`localStorage` y el toast: paso 1 textos gemelos (también con
  sufijo), paso 2 login, paso 3 403 de ámbito por los dos clientes (un solo id de aviso),
  paso 4 401 por los dos clientes y una sola redirección. No usa la base. Sin node o sin
  `npm install`: skip CON MOTIVO; el cinturón corre igual.
- `services/authErrors.ts`, encabezado: sección final «LOS TEXTOS DE ESTE ARCHIVO SON
  GEMELOS DE LOS DEL BACKEND», que nombra los cuatro pares y la prueba.
- `backend/empresa_guard.py::require_empresa` y `backend/role_guard.py::require_rol`: una
  línea de comentario más sobre el `detail` («Lo vigila
  routers/tests/test_auth_errors_espejo_frontend.py»), sin efecto en ejecución.
- `backend/auth_errors.py`, docstring: que las dos listas sigan iguales lo exige la prueba.

**Nota para el programador (en lugar de un comentario en tu archivo):**
`DETALLE_CUENTA_PORTAL` (`backend/auth.py`) tiene gemelo en
`frontend-src/src/services/authErrors.ts` (`DETALLES_AMBITO`): si cambias ese texto,
`backend/routers/tests/test_auth_errors_espejo_frontend.py` avisa («auth.DETALLE_CUENTA_PORTAL
emite … y DETALLES_AMBITO no lo cubre») y hay que cambiarlo en los dos lados. No le pusimos
comentario a `auth.py` para no meter un hunk nuestro en tu archivo.

### C38 — `estadoHttp` y `detalleDe` salen del portal (arreglado)

Los dos ayudantes son genéricos de axios y vivían en `portal/api.ts`; `BodegaPage` (fuera
del portal) leía los errores con casts a mano. Ahora:

- `frontend-src/src/services/httpErrors.ts` (NUEVO): `estadoHttp` y `detalleDe`, mudados
  sin cambiar una letra, con sus comentarios. Solo importa `axios` (sin ciclo).
- `frontend-src/src/portal/api.ts`: `import axios from 'axios'` (solo lo usaban los dos
  ayudantes) pasa a `import { detalleDe, estadoHttp } from '../services/httpErrors'`; las
  dos funciones se quitan y al final queda `export { detalleDe, estadoHttp }` con su
  comentario. Los 14 importadores del portal no cambian. `motivoDeBloqueo` (función del
  portal que usa los dos ayudantes) queda donde estaba y sigue encontrándolos: por eso la
  reexportación es un import más un `export { … }` y no un `export … from`.
- `frontend-src/src/pages/BodegaPage.tsx`: import nuevo de los dos ayudantes;
  `handleFoto` → `toast.error(detalleDe(err) ?? 'Error al subir foto')`;
  `handleDeleteFoto` → `if (estadoHttp(err) === 404) {` y
  `toast.error(detalleDe(err) ?? 'Error al eliminar foto')`. Desaparecen los tres casts a
  mano. El único caso de borde que cambia: un `detail` hecho solo de espacios pasa a
  mostrar el texto genérico.

Sin prueba propia: es una mudanza y un renombre; lo comprueba `npx tsc -b` (0 errores).

### C15 — La descarga inicial del portal (decisión del dueño; SIN cambio de código)

No se cambia nada y no se crea `Raiz.tsx` ni `test_descarga_inicial.py`. El diseño queda
escrito y medido en §6 para aplicarlo tal cual si se aprueba. Mientras tanto se anotó
como deuda declarada en `docs/fase-1-portal-identidad-2026-10-02.md` §10 y como decisión
pendiente en `docs/fase-3-4-5-portal-pedidos-2026-10-04.md` §8, y el checklist de deploy
(§3) trae la verificación de compresión de nginx que vale con o sin el cambio.

### C16 — Tema del portal (solo documentar; SIN cambio de código)

El portal ya nace en tema claro la primera vez que una cuenta portal entra desde un
navegador (`PortalLayout::useTemaClaroLaPrimeraVez`) y trae botón de tema en la cabecera
(`CabeceraPortal.tsx`). Quedó registrado como decisión de producto en
`docs/fase-3-4-5-portal-pedidos-2026-10-04.md` §8 (punto 9), con el efecto lateral de que
el tema se guarda en la misma llave que el ERP (`machparts-theme`) y el detalle de la
primera visita (login oscuro, portal claro).

---

## 2. Archivos tocados

### Frontend

| Archivo | Símbolo | Cambio |
|---|---|---|
| `frontend-src/src/services/authErrors.ts` | encabezado; `RUTA_LOGIN` (nueva); `manejarErrorAuth`; `yaRedirigiendo` (comentario); `_resetRedireccion` (borrada) | P14, P12, C40 |
| `frontend-src/src/services/httpErrors.ts` | `estadoHttp`, `detalleDe` | **NUEVO** (C38) |
| `frontend-src/src/portal/api.ts` | import de `axios` → import de los ayudantes; `estadoHttp` y `detalleDe` quitados; `export { detalleDe, estadoHttp }` al final | C38 (cambio por símbolo; `motivoDeBloqueo` y todo lo demás sin reordenar) |
| `frontend-src/src/pages/BodegaPage.tsx` *(del programador)* | import de `httpErrors`; `handleFoto` (catch); `handleDeleteFoto` (catch) | C38: 1 línea de import + 2 `catch` (sin otros cambios) |

### Backend

| Archivo | Símbolo | Cambio |
|---|---|---|
| `backend/routers/tests/test_auth_errors_espejo_frontend.py` | `test_cinturon_estatico`, `test_espejo_compilado` | **NUEVO** (C40, P14, P12) |
| `backend/empresa_guard.py` *(código propio)* | `require_empresa` | +1 línea de comentario sobre el `detail` |
| `backend/role_guard.py` *(código propio)* | `require_rol` | +1 línea de comentario sobre el `detail` |
| `backend/auth_errors.py` | docstring | nombra la prueba que exige las listas iguales |
| `backend/portal/tests/mutaciones/__init__.py` | `copiar_frontend`, `ruta_en_copia` (nuevas); `correr`, `mutar`, `main` | el arnés copia también `frontend-src` (ver §3) |
| `backend/portal/tests/mutaciones/frontend.py` | `MUTACIONES` | **NUEVO**: las 11 mutaciones de este paquete |
| `backend/portal/tests/mutaciones/cierre.py`, `bodega.py` | docstring | ya no dicen que el arnés copia solo `backend/` |

### Documentación

`docs/fase-0-portal-cambios-2026-08-20.md` (tabla de pruebas, tabla de clases de error,
§8), `docs/fase-1-portal-identidad-2026-10-02.md` (§9 prueba manual, §10 deuda),
`docs/fase-3-4-5-portal-pedidos-2026-10-04.md` (§8 puntos 8 y 9),
`docs/CHECKLIST-DEPLOY-2026-07-20.md` (§3 verificación de nginx) y este documento.

**Sin tocar, a propósito:** `backend/auth.py` (archivo del programador, en CRLF, con otro
trabajo sin commit encima), `services/api.ts`, `services/monzaApi.ts`, `App.tsx`,
`main.tsx`, `vite.config.ts`, y toda la carpeta `frontend-src/src/portal/` salvo `api.ts`
(la carpeta está en edición por otra persona).

---

## 3. Cómo verificar

### Suite nueva

```bash
cd backend
./venv/bin/python -m pytest routers/tests/test_auth_errors_espejo_frontend.py -q
```

Resultado obtenido: **2 passed en 0,45 s** (cinturón estático + espejo compilado, con los
13 checks del espejo en OK). Antes del arreglo, con el código de ese momento, la misma
suite nacía ROJA en los dos: el espejo con
`{'pedido': '/api/auth/login', 'token': None, 'guardado': None}` (el login cerraba la
sesión) y el cinturón por falta de `RUTA_LOGIN`.

Necesita node y `npm install` en `frontend-src` (usa el esbuild de
`frontend-src/node_modules/.bin`); sin ellos el espejo se salta diciendo por qué y el
cinturón corre igual. No toca la base: importar `auth` crea el engine sin conectarse.

Las suites que ya ejercitan los guards siguen verdes:
`routers/tests/test_auth_errors_clasificacion.py` (1 passed) y
`./venv/bin/python -c "import empresa_guard, role_guard, auth_errors"`.

### Mutaciones (el arnés del repo)

```bash
cd backend
./venv/bin/python -m portal.tests.mutaciones auth_errors_espejo
```

Resultado obtenido: **11 de 11 mutaciones detectadas** (base verde; todas «roja»):

| Mutación (lista `portal/tests/mutaciones/frontend.py`) | Archivo mutado | Paso que cae |
|---|---|---|
| el 401 del login vuelve a cerrar la sesión vigente (sin la exclusión) | `authErrors.ts` | 2 |
| la exclusión se ensancha a todo `/auth/` | `authErrors.ts` | 4 |
| `RUTA_LOGIN` cambia sin cambiar `authAPI.login` | `authErrors.ts` | 2 y cinturón |
| `authAPI.login` cambia de ruta sin cambiar `RUTA_LOGIN` | `services/api.ts` | 2 y cinturón |
| el cliente de MachParts deja de llamar a la política desde su interceptor | `services/api.ts` | 3 y 4 |
| el cliente de MonzaParts deja de llamar a la política desde su interceptor | `services/monzaApi.ts` | 3 y 4 |
| dos 401 seguidos navegan dos veces (sin `yaRedirigiendo = true`) | `authErrors.ts` | 4 |
| los avisos de ámbito pierden el id que los funde en uno | `authErrors.ts` | 3 |
| el `detail` de `empresa_guard` cambia sin tocar `DETALLES_AMBITO` | `empresa_guard.py` | 1 y cinturón |
| `DETALLES_AMBITO` pierde el texto de `empresa_guard` | `authErrors.ts` | 1 y cinturón |
| `DETALLES_SESION` pierde «Invalid authentication credentials» | `authErrors.ts` | 1 y cinturón |

**Cómo muta el frontend el arnés.** Hasta ahora copiaba solo `backend/`, y las mutaciones
que vivían en el frontend se verificaban a mano sobre una copia. Ahora
`portal/tests/mutaciones/__init__.py` copia también `frontend-src` junto a su copia de
`backend/` (`src/` copiado, `node_modules` enlazado) y apunta `PARTSCONTROL_FRONTEND_SRC` a
esa copia; una lista nombra un archivo del frontend desde la raíz del repo
(`frontend-src/src/…`) y el arnés lo muta ahí (`ruta_en_copia`). El árbol de trabajo no se
toca nunca (se confirmó con `git status` antes y después). Efecto lateral, deseado: las
listas `cierre_espejo` y `clientes_espejo` ya no necesitan la variable de entorno para que
sus espejos no se salten (`clientes_espejo` se volvió a correr: 2 de 2 detectadas). Las
mutaciones del frontend de los paquetes anteriores pueden entrar a sus listas con el mismo
prefijo.

### Compilación del frontend

```bash
cd frontend-src && npx tsc -b
```

Resultado obtenido: **0 errores** (exit 0), antes y después de los cambios.

### Prueba manual (2 minutos)

1. Pestaña A con sesión dentro del ERP; pestaña B en `/login` con una clave mala → B muestra
   «Email o contraseña incorrectos» y A sigue adentro.
2. En A, con el token vencido (o borrado en el backend), cualquier pantalla → va al login
   una sola vez.
3. Bodega → Recepción → subir una foto con la recepción ya cerrada → el aviso muestra el
   `detail` del backend, no «Error al subir foto».

---

## 4. Cómo revertir

- **P14 + P12 + C40** (un solo commit, «A»): `git revert` de ese commit. Toca
  `services/authErrors.ts`, la prueba nueva, los dos comentarios de los guards y el
  docstring de `auth_errors.py`. Si se revierte solo el `.ts` a mano, hay que quitar también
  la prueba (quedaría roja en el cinturón, que es lo que debe pasar).
- **C38** (commit «B»): `git revert`. Toca `services/httpErrors.ts`, la cola y el import de
  `portal/api.ts` y los dos `catch` de `BodegaPage.tsx`. Si se quiere deshacer solo
  `BodegaPage`, basta volver a los casts: `httpErrors.ts` puede quedar (el portal lo usa por
  la reexportación).
- **El arnés** (`__init__.py`, `frontend.py`): revertir deja a `frontend.py` sin cómo mutar el
  frontend (sus 10 mutaciones del frontend pasan a ERROR «no hay frontend-src»): van juntos.
- **C15 y C16**: nada que revertir (solo documentación).

---

## 5. Deuda que queda anotada

- **Documentación en `frontend-src/src/portal/README.md` y en `PortalLayout.tsx`** (carpeta en
  edición por otra persona; no se tocó). Cuando esa edición cierre, conviene:
  - §Cableado, línea de `authErrors.ts`: «el 401 cierra la sesión (las pantallas no lo
    tratan)» → agregar «menos el del login, que lo muestra `LoginPage`»; y que sus textos
    son gemelos de los del backend, vigilados por
    `routers/tests/test_auth_errors_espejo_frontend.py`.
  - §Cableado: agregar `services/httpErrors.ts` (lo usan el portal y los dos `catch` de
    fotos de `BodegaPage`).
  - Tabla de archivos, fila `api.ts`: que reexporta `detalleDe` y `estadoHttp`, que viven
    en `services/httpErrors.ts`.
  - §Cómo deshacer: `httpErrors.ts` se queda (`BodegaPage` lo importa).
  - «Reglas que no se ven en el código»: la primera vez que una cuenta portal entra desde un
    navegador, el portal pasa a tema claro (`PortalLayout::useTemaClaroLaPrimeraVez`, marca
    `machparts-portal-tema-elegido`); después manda el botón de la cabecera. El tema se
    guarda en la misma llave que usa el ERP (`machparts-theme`): probar una cuenta de
    cliente en el navegador de trabajo deja también el ERP en claro esa primera vez — otro
    motivo para probar en incógnito.
  - `PortalLayout.tsx`, comentario de `useTemaClaroLaPrimeraVez`: «su único botón estaba
    dentro del ERP» → en presente: «fuera del portal, su único botón está dentro del ERP».
- **Puerta vecina, que no es de este paquete:** en el trabajo sin commit el token del portal
  va atado a la clave (`auth.token_para`; `CambiarClaveForm` guarda el token nuevo). Una
  petición en vuelo con el token anterior volvería con 401 y cerraría la sesión recién
  renovada. Hoy es una carrera de milisegundos (esa pantalla no deja consultas en vuelo). Si
  se quiere cerrar, el lugar es `manejarErrorAuth`: ignorar el 401 cuando la cabecera
  `Authorization` de la petición fallida existe y es DISTINTA del token vigente.
- `routers/tests/test_auth_errors_clasificacion.py` guarda copias a mano de los textos de
  los guards; podría pedírselos al backend como hace `_detail_de` en la prueba nueva.
- Las mutaciones del frontend de los paquetes `cierre-de-venta-y-fechas` y
  `fotos-de-recepcion` siguen verificadas a mano; ahora pueden entrar a sus listas del arnés.

---

## 6. Decisiones que quedan para el dueño

Mientras no se decidan, rige lo que dice cada punto.

**C15 — Separar lo que descarga el cliente del portal de lo que descarga el personal.**
Hoy el teléfono de un cliente baja el sistema interno completo antes de ver su portal:
2,45 MB (unos 597 KB comprimido). Ya está diseñado y probado en una copia que baje solo
el login y el portal: 384 KB (unos 120 KB comprimido), cinco veces menos; el personal no
nota diferencia. Para hacerlo hay que tocar dos archivos del programador: main.tsx
(2 líneas) y App.tsx (se le sacan el login y las rutas del portal; sus rutas del ERP
quedan tal cual). OPCIÓN CONSERVADORA QUE RIGE MIENTRAS NO DECIDAS: no se aplica; el
portal funciona igual, solo demora más la primera carga con mala señal, y queda anotado
como pendiente. RECOMENDACIÓN: aprobarlo como un cambio aparte (se deshace con un solo
paso) antes de abrir el portal a clientes reales; la alternativa es entregarle el diseño
escrito al programador para que lo aplique él.

*Para el programador, si se aprueba:* el diseño es una sola frontera entre mundos. Un
archivo raíz nuevo (`Raiz.tsx`) lleva lo que baja cualquiera (login, portal y los dos
guards `PrivateRoute`/`PortalRoute`, mudados desde `App.tsx`) y carga `App.tsx` —que sigue
siendo el enrutador del ERP, con sus imports y rutas intactos— con
`lazy(() => import('./App'))` detrás de `PrivateRoute`, con un `Suspense` («Cargando
PartsControl…») y una pantalla de fallo con «Reintentar» (`window.location.reload()`).
`main.tsx` cambia 2 líneas (`import Raiz from './Raiz'`, `<Raiz />`). `vite.config.ts` no
se toca: la división sale del `import()` dinámico y el `cp -r dist/assets/*` del deploy ya
copia los dos archivos; no borrar los `.js` anteriores durante el deploy (una pestaña
abierta todavía puede pedir el suyo). Va acompañado de
`backend/portal/tests/test_descarga_inicial.py` (le pide el grafo de módulos a esbuild y
exige que desde `main.tsx` solo se alcance una lista cerrada de archivos, y que `App.tsx`
cuelgue solo por import dinámico). Se aplica al final, cuando cierren los cambios en curso
sobre `src/portal/` y el bloque `/portal` de `App.tsx`, y se mide con `npm run build` antes
y después.

**C16 — Tema del portal.** El trabajo en curso ya lo dejó así: la primera vez que un
cliente entra desde un navegador el portal aparece en tema CLARO (pensado para leerlo a
pleno sol) y trae un botón para pasarlo a oscuro. RIGE ESO mientras no digas otra cosa. Si
prefieres que nazca oscuro como el ERP, o un solo tema fijo sin botón, es un cambio chico.
Un detalle a saber: quien pruebe una cuenta de cliente en su navegador de trabajo verá que
su ERP también queda en claro esa primera vez (se devuelve con el botón del ERP; probando
en una ventana de incógnito no pasa).

---

## 7. Desviaciones respecto del plan y correcciones a documentación anterior

- **Los guards ya traían un comentario-puntero** a `DETALLES_AMBITO` (dos líneas, del
  trabajo sin commit). En vez del bloque de tres líneas previsto, se les agregó UNA línea con
  la prueba que lo vigila: mismo contenido, sin duplicar.
- **`portal/api.ts` cambió después de escribirse el plan**: apareció `motivoDeBloqueo`, que
  usa `estadoHttp` y `detalleDe`. Un `export … from` no deja esos nombres en el módulo, así
  que la reexportación quedó como `import { … }` arriba (en el lugar del import de `axios`)
  y `export { detalleDe, estadoHttp }` abajo. Nada más se movió.
- **`BodegaPage.tsx`**: el renombre del `catch` (`e` → `err`) ya lo había hecho el paquete de
  fotos; acá solo entran el import y los tres casts → ayudantes. En `handleDeleteFoto` ese
  paquete también leía el `detail` para el caso distinto de 404: pasó a `detalleDe` para
  que no quedara un cast a medias.
- **El arnés de mutaciones se extendió** para mutar una copia de `frontend-src` (§3). El
  plan pedía verificar cada mutación con el arnés; la alternativa era una copia a mano por
  mutación, fuera del repo. Las cabeceras de `cierre.py` y `bodega.py` se pusieron al día.
- **`docs/fase-0-portal-cambios-2026-08-20.md`, tabla de clases de error:** la fila `ambito`
  decía «Un aviso y volver a SU inicio»; la política no navega en ese caso (encabezado de
  `authErrors.ts`, «POR QUÉ EL 403 DE ÁMBITO NO NAVEGA»). Corregida junto con la fila
  `sesion` que pedía el plan.
- **`frontend-src/src/portal/README.md` y `PortalLayout.tsx` no se tocaron** (carpeta en
  edición por otra persona); lo que les falta está en §5.
