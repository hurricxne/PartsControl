# Fase 0 del Portal de Clientes — qué se cambió y por qué

**Fecha:** 2026-08-20 · **Alcance:** solo MachParts / Grupo AM · **Migraciones de base de datos:** ninguna

> ## Para el programador que mantiene este sistema
>
> Este documento existe porque estos cambios tocan **código compartido** que tú mantienes
> (`main.py`, `config.py`, `routers/auth.py`, `routers/bodega.py`, `services/api.ts`,
> `services/monzaApi.ts`). Acá está **todo** lo que se modificó, **por qué**, **cómo
> verificarlo** y **cómo revertirlo**.
>
> **Nada de esto cambia el esquema de la base de datos.** No hay que correr migraciones.
>
> Los dos cambios de comportamiento visible están detrás de un **flag de `.env`**, así que
> se revierten sin desplegar código: `SERVIR_FOTOS_BODEGA_SIN_AUTH=true` y
> `LOGIN_RATE_LIMIT_ENABLED=false`, más `pm2 restart machparts-api`.

---

## 1. Por qué se hizo esto ahora

Se está construyendo un **portal para que los clientes vean sus órdenes**, usando el mismo
login. Eso convierte deuda de seguridad que hoy es interna en riesgo real hacia afuera: el
día que entre el primer usuario externo, cualquier agujero deja de ser teórico.

La "Fase 0" son los cuatro cerrojos que hay que cerrar **antes** de que exista el primer
usuario externo. Ninguno depende del portal: los cuatro mejoran el sistema tal como está hoy.

---

## 2. Resumen de los cuatro cambios

| # | Problema | Arreglo | Riesgo del cambio |
|---|---|---|---|
| **C1** | `uploads/bodega` se servía como carpeta **pública**, con nombres predecibles | Endpoint nuevo `GET /api/bodega/fotos/{id}` que exige sesión. El mount público queda detrás de un flag, apagado | Bajo — el único consumidor era una pantalla que **ya estaba rota** (§5) |
| **C2** | El login **no tenía tope de intentos**: fuerza bruta ilimitada y DoS de CPU por bcrypt | Limitador en memoria con dos llaves (email e IP), `backend/rate_limit_login.py` | Bajo — solo cuenta FALLOS; un login correcto borra el rastro |
| **C3** | `CORS_ORIGINS` incluye `http://localhost:5173` **también en producción** | No se cambia el default (rompería el entorno de desarrollo). Se **avisa en el log de arranque** y se documenta la variable en `.env.example` | Ninguno — solo agrega una línea al log |
| **C4** | El frontend **no manejaba el 403** en ninguna parte (`grep 403` daba cero) | Política única compartida por los dos clientes axios, en `services/authErrors.ts` | Medio — es el que más conviene probar a mano (§7) |

---

## 3. Archivos tocados

### Backend

| Archivo | Cambio | Líneas |
|---|---|---|
| `backend/rate_limit_login.py` | **NUEVO.** El limitador, con su política y sus modos de falla documentados en el encabezado | ~210 |
| `backend/config.py` | +2 flags (`LOGIN_RATE_LIMIT_ENABLED`, `SERVIR_FOTOS_BODEGA_SIN_AUTH`) y el aviso de CORS al final | +20 |
| `backend/routers/auth.py` | `login` recibe `Request` y llama al limitador (3 líneas); endpoint nuevo de diagnóstico | +35 |
| `backend/routers/bodega.py` | `_FOTOS_DIR`, `_ruta_confinada_foto()` y `serve_foto()`; 2 imports | +75 |
| `backend/main.py` | El mount de fotos queda condicionado; manejador que traduce el 403 "sin sesión" a 401 | +50 |
| `backend/.env.example` | Documenta las 2 variables nuevas y **exige** definir `CORS_ORIGINS` en producción | +20 |
| `backend/auth_errors.py` | **NUEVO.** El criterio del 403-sin-sesión, aislado para poder probarlo sin levantar la app | ~45 |
| `backend/routers/tests/test_login_rate_limit.py` | **NUEVO.** 5 etapas | ~170 |
| `backend/routers/tests/test_bodega_fotos_auth.py` | **NUEVO.** QUIÉN puede: sesión, marca, confinamiento y pertenencia (al 2026-10-06: 16 etapas, 57 comprobaciones) | ~720 |
| `backend/routers/tests/test_bodega_fotos_candado.py` | **NUEVO (2026-10-06).** CUÁNDO se puede subir o borrar: recepción cerrada, última foto de un ítem dañado, candado, archivo y fila juntos (5 etapas, 29 comprobaciones) | ~390 |
| `backend/routers/tests/test_fotos_bodega_espejo_frontend.py` | **NUEVO (2026-10-06).** Pytest espejo del frontend: el caché de `<FotoBodega>` y la fecha de la galería, compilados con esbuild y corridos en Node (necesita node y `npm install`; si faltan se salta diciendo por qué) | ~250 |
| `backend/routers/tests/test_auth_errors_clasificacion.py` | **NUEVO.** 5 etapas | ~90 |
| `backend/routers/tests/test_auth_errors_espejo_frontend.py` | **NUEVO (2026-10-06).** Pytest espejo de `services/authErrors.ts`: compila con esbuild los dos clientes axios, el store y la política, y los corre en Node contra los textos REALES del backend (los guards se ejecutan para que digan su `detail`). Necesita node y `npm install` en frontend-src; si faltan, el espejo queda en skip con motivo y el cinturón estático corre igual. No usa la base | ~330 |

### Frontend

| Archivo | Cambio | Líneas |
|---|---|---|
| `frontend-src/src/services/authErrors.ts` | **NUEVO.** La política de 401/403, compartida | ~130 |
| `frontend-src/src/components/FotoBodega.tsx` | **NUEVO.** Miniatura autenticada, con caché y manejo de memoria | ~150 |
| `frontend-src/src/services/api.ts` | El interceptor llama a la política compartida | ~8 |
| `frontend-src/src/services/monzaApi.ts` | Ídem, y de paso arregla que borraba localStorage sin llamar a `logout()` | ~8 |
| `frontend-src/src/pages/BodegaPage.tsx` | Usa `<FotoBodega>`; arregla 2 bugs preexistentes (§5) | ~15 |

---

## 4. Detalle de cada cambio

### C1 · Fotos de bodega con sesión

**Antes:** `main.py` montaba `uploads/bodega` como `StaticFiles`. Los nombres son
`recepcion_{id}_{timestamp}.jpg` (`bodega.py`, en `upload_foto`): el `id` es un entero
chico y el timestamp tiene 86.400 valores posibles por día. Enumerable.

**Ahora:** `GET /api/bodega/fotos/{foto_id}` exige sesión y **direcciona por id de base**.
El nombre del archivo **nunca viaja en la URL**, así que su predictibilidad deja de
importar. El archivo se resuelve desde la fila y se confina dentro de `uploads/bodega`
con el mismo patrón que ya usa `serve_doc` en `routers/despachos.py`: regex estricta →
`abspath` → verificación de prefijo.

**Por qué NO se migró la columna `url_foto`:** guarda `/uploads/bodega/x.jpg`. Un
`os.path.basename()` cubre ese formato y el pelado, así que una línea resuelve histórico
y futuro. Y hay una razón más fuerte: `delete_foto` hace `url_foto.lstrip("/")` relativo
al directorio de trabajo. Si se migrara la columna a nombre pelado, **el borrado dejaría
archivos huérfanos en disco, en silencio, para siempre**.

**Por qué el frontend no puede usar `<img src>` directo:** un `<img>` no envía el header
`Authorization`. La imagen se pide con axios (que sí lo agrega), se convierte en blob
local y ese se pinta. Es el mismo camino que ya usa `abrirDocumento()` para las guías.

**Agregado el 2026-10-06 (paquete «fotos-de-recepción», ver
`docs/paquete-fotos-de-recepcion-2026-10-06.md`):** las respuestas de `get_recepcion`,
`list_reclamos` y `upload_foto` conservan el campo `url` con la ruta histórica
`/uploads/bodega/<archivo>`. Es solo informativa —por HTTP no resuelve salvo con
`SERVIR_FOTOS_BODEGA_SIN_AUTH=true`— y sirve para ubicar el archivo en
`backend/uploads/bodega` cuando `serve_foto` responde «Archivo no encontrado». Y
`upload_foto` borra el archivo si falla la escritura o el commit y responde 500 con un
mensaje legible; la causa exacta queda en el log de pm2 con el prefijo `[bodega]`.

### C2 · Tope de intentos en el login

**Dos llaves que miden cosas distintas:**

| Llave | Umbral | Qué frena |
|---|---|---|
| email | 5 fallos / 15 min | Adivinar la clave de una cuenta |
| IP | 10 **emails distintos** fallidos / 15 min | Rociado de contraseñas |

**La segunda no cuenta fallos, cuenta emails distintos.** Es deliberado: una minera entera
sale por un NAT con una sola IP. Si contáramos fallos por IP, 30 personas equivocándose el
lunes en la mañana bloquearían a toda la empresa. Hay una prueba dedicada a esto
(`_paso_3_el_nat_no_se_castiga`).

**Cómo cuenta, que no es decorativo:** `reservar_intento()` comprueba el tope **y anota
el intento en la misma operación**, antes de tocar la base y antes del bcrypt. Dos razones:
(a) si se anotara después, el ataque seguiría costando un hash completo por intento aunque
lo rechacemos — un DoS de CPU con el limitador puesto; (b) con dos operaciones separadas,
las 40 peticiones que caben en el threadpool de FastAPI pasan el control **antes de que
ninguna alcance a anotar**, y el tope real pasa de 5 a ~45. Un login correcto borra su
marca (`limpiar_exito`), así que entrar bien no deja rastro.

**El mensaje del 401 se dejó intacto**: sigue sin distinguir "email que no existe" de
"clave mala", así que el login no sirve para enumerar cuentas.

**Modo de falla que hay que conocer:** el limitador vive en **memoria del proceso**. Un
`pm2 restart` borra los contadores (aceptable: un atacante externo no puede provocarlo).
Y **con más de un worker** cada uno lleva su propio contador, así que el presupuesto real
pasa a ser N×5. El deploy actual es pm2 en fork mode = 1 proceso.

> **Confirmalo en 5 segundos:** `pm2 describe machparts-api` → `exec mode: fork_mode`,
> `instances: 1`.
>
> **Síntoma de que hay varios workers:** un usuario ve 429 al entrar, pero
> `GET /api/auth/rate-limit/estado?email=...` responde `"bloqueado": false`.

**Para depurar sin leer logs:** `GET /api/auth/rate-limit/estado?email=juan@grupoam.cl&ip=200.1.2.3`
(exige sesión **y** marca `mineria`) devuelve intentos, si está bloqueado, cuántos segundos
faltan y el **PID del proceso**. Ese PID es el diagnóstico rápido de la hipótesis de arriba:
si dos llamadas seguidas devuelven PID distintos, hay más de un worker.
La IP se pasa explícita a propósito: una versión anterior tomaba la IP del operador que
consultaba, no la de la persona bloqueada.

### C3 · CORS

El default de `CORS_ORIGINS` incluye `http://localhost:5173` para que `npm run dev`
funcione sin configurar nada. En producción eso deja autorizado un origen de desarrollo,
con `allow_credentials=True`.

**No se cambió el default** — rompería el entorno de desarrollo de cualquiera que clone el
repo. En cambio:
1. `config.py` **avisa en el log de arranque** mientras haya un origen de desarrollo
   autorizado: `[config] AVISO CORS: hay origenes de desarrollo autorizados [...]`.
2. `.env.example` documenta `CORS_ORIGINS` con el valor de producción, **comentado**
   (descomentarlo en la plantilla rompería el entorno de desarrollo de quien la copie).

**Acción para el deploy:** agregar `CORS_ORIGINS=["https://machparts.bigcode.cl"]` al
`.env` del servidor. El aviso del log desaparece cuando está bien puesto.

### C4 · El 403 en el frontend

Había dos problemas encadenados:

1. **`grep 403` en `frontend-src/src` daba cero.** Un usuario sin permiso se quedaba dentro
   del cascarón interno con las tablas vacías y sin explicación.
2. **`auth.py` usa `HTTPBearer()`**, que responde **403 "Not authenticated"** —no 401—
   cuando falta el header. O sea: "no tengo sesión" y "mi sesión no alcanza" llegaban al
   frontend con el mismo código.

**El arreglo tiene dos mitades:**

- **Backend** (`main.py`): un manejador traduce a 401 **solo** los dos detalles exactos que
  emite la librería (`"Not authenticated"`, `"Invalid authentication credentials"`).
  Los 403 propios del negocio (`empresa_guard`, `role_guard`) siguen llegando como 403.
- **Frontend** (`services/authErrors.ts`): clasifica en tres y actúa distinto.

| Clase | Qué es | Qué hace |
|---|---|---|
| `sesion` | 401 —salvo el del propio login (`POST /auth/login`), que no toca la sesión—, o el 403 de la librería | `logout()` + ir al login |
| `ambito` | Los mensajes de `empresa_guard` / `role_guard` / `auth.DETALLE_CUENTA_PORTAL` | Un aviso, **sin desloguear y sin navegar** (el porqué está en el encabezado de `authErrors.ts`) |
| `negocio` | Cualquier otro 403 | **Nada.** Lo maneja el `catch` de la pantalla |

**El default es `negocio` a propósito.** Si mañana agregas un 403 nuevo en el backend, el
interceptor lo ignora en vez de sacar al usuario de la pantalla a mitad de un guardado.

**Anti-bucle:** hay un candado de "una sola redirección por carga de página", y si el 403
viene desde la propia casa del usuario solo se muestra el aviso. Sin eso, un usuario
`automotriz` que cae en `/` entraría en bucle infinito con el catch-all de `App.tsx`.

---

## 5. Tres bugs preexistentes que aparecieron y se arreglaron de paso

No los introdujo este trabajo; se encontraron al revisar y se arreglaron porque el cambio
los tocaba de frente.

| # | Bug | Evidencia | Efecto |
|---|---|---|---|
| **B1** | El backend serializa `{"id", "url"}` pero `BodegaPage.tsx` declaraba `fotos: {id, url_foto}` | `routers/bodega.py` (dos serializadores) vs. el tipo del front | **La clave `url_foto` nunca llegaba**: las fotos ya guardadas pintaban `<img src={undefined}>`. Solo se veían las recién subidas en la misma sesión |
| **B2** | Al subir, el front leía `data.foto_id`; el backend devuelve `data.id` | `BodegaPage.tsx`, handler de subida | El id quedaba `undefined`: el botón de borrar no funcionaba hasta recargar la página |
| **B3** | `monzaApi.ts` hacía `localStorage.removeItem` sin llamar a `logout()` del store | `services/monzaApi.ts`, interceptor 401 | El token seguía vivo en memoria hasta el reload |

> **Nota honesta:** B1 hacía que el cambio C1 fuera casi gratis. El único consumidor de la
> carpeta pública ya estaba roto para las fotos históricas, así que cerrarla no le quitó
> nada a nadie — y de paso ahora las fotos viejas **sí se ven**.

---

## 6. Cómo revertir

| Cambio | Cómo se revierte | Tiempo |
|---|---|---|
| **C1** fotos | `SERVIR_FOTOS_BODEGA_SIN_AUTH=true` en `.env` + `pm2 restart machparts-api`. El endpoint nuevo es aditivo: no hay nada que deshacer | 30 s, sin deploy |
| **C2** login | `LOGIN_RATE_LIMIT_ENABLED=false` en `.env` + restart. O solo `pm2 restart` para vaciar los contadores | 30 s, sin deploy |
| **C3** CORS | Sacar `CORS_ORIGINS` del `.env` y vuelve el default | 30 s |
| **C4** frontend | Restaurar el `assets/` e `index.html` anteriores (respaldarlos antes del `cp -r dist/assets/*`) | 1 min |

**Nada de esto tocó la base de datos**, así que no hay nada que revertir ahí.

---

## 7. Verificación manual — 6 minutos

### Backend
1. `pm2 logs machparts-api` al reiniciar: **no** debe aparecer
   `[startup] AVISO: uploads/bodega se sirve SIN autenticacion`.
   Sí debe desaparecer `[config] AVISO CORS: ...` una vez puesto `CORS_ORIGINS` en el `.env`.
2. `curl -i https://machparts.bigcode.cl/uploads/bodega/cualquier-cosa.jpg` → **404**
   (antes servía el archivo si acertabas el nombre).
3. `curl -i https://machparts.bigcode.cl/api/bodega/fotos/1` sin token → **401**.
4. Login con clave mala 6 veces seguidas → la sexta responde **429** con `Retry-After`.
   Después, entrar con la clave buena: funciona (el contador se limpia solo al entrar bien,
   o con `pm2 restart`).

### Frontend
5. **Bodega:** abrir una recepción con un ítem dañado. Las fotos **existentes** deben
   verse — antes no se veían (bug B1). Subir una nueva: aparece sin recargar. Borrarla:
   desaparece y no vuelve al colapsar y expandir.
6. **El caso que más importa probar:** en otra pestaña, DevTools →
   `localStorage.removeItem('machparts-auth')`. Volver y hacer clic en cualquier menú.
   Debe llevar **al login**, no dejar avisos infinitos.
7. Con una cuenta **automotriz**, escribir a mano `/tesoreria` en la barra de direcciones.
   Debe salir **un** aviso y dejarte en `/monzaparts`, sin parpadeo ni bucle. Recargar ahí:
   estable.
8. **Regresión de flujo:** guardar un ítem de bodega marcado como dañado y **sin** foto.
   Debe seguir saliendo el aviso "Debes subir al menos 1 foto" y **no** debe navegar a
   ninguna parte. Esto confirma que ningún guard de negocio quedó atrapado por el
   interceptor nuevo.

---

## 8. Pruebas automáticas

```bash
cd backend
./venv/bin/python -m pytest routers/tests/test_login_rate_limit.py -q
./venv/bin/python -m pytest routers/tests/test_bodega_fotos_auth.py -q
./venv/bin/python -m pytest routers/tests/test_bodega_fotos_candado.py -q         # 2026-10-06
./venv/bin/python -m pytest routers/tests/test_fotos_bodega_espejo_frontend.py -q  # 2026-10-06; necesita node + npm install en frontend-src
./venv/bin/python -m pytest routers/tests/test_auth_errors_espejo_frontend.py -q   # 2026-10-06; ídem (sin node: el cinturón corre y el espejo se salta con motivo)
./venv/bin/python -m pytest -q          # el gate completo
```

```bash
cd frontend-src
npx tsc --noEmit                        # sin errores
```

Cada prueba nueva documenta en su encabezado **su sonda de poder discriminante**: qué
línea del arreglo hay que quitar para que se ponga roja. Una prueba que pasa igual con y
sin el arreglo no prueba nada.

---

## 9. Lo que esta fase NO hizo

- No agregó ni modificó columnas, tablas ni índices.
- No tocó el candado de empresa de `routers/bodega.py` (sigue pendiente la decisión del
  dueño; ponerlo hoy dejaría a bodega en 403 si algún usuario tiene `empresa` en blanco).
- No cambió el comportamiento de autenticación: el limitador se agrega **encima**, la
  consulta a la base y el `verify_password` quedan idénticos.
- No unificó los dos clientes axios: comparten la política, cada uno conserva su
  `baseURL` y sus interceptores.

---

## 10. Revisión independiente y correcciones

Antes de dar esto por terminado se sometió a **tres revisores independientes** (seguridad,
Python, TypeScript/React) con el encargo explícito de **refutar**, no de aprobar. Dos de los
tres lo rechazaron en la primera vuelta. Esto es lo que encontraron y cómo quedó.

### Defectos que encontraron en MI código y se corrigieron

| # | Defecto | Cómo quedó |
|---|---|---|
| 1 | **`serve_foto` no verificaba pertenencia.** Consultaba la recepción y no usaba el resultado: una consulta muerta justo donde iba el control. Como `foto_id` es correlativo, cualquier sesión bajaba todas las fotos con un bucle | Lleva `require_empresa("mineria")` a nivel de ruta + verificación de que la foto cuelgue de una recepción viva |
| 2 | **`delete_foto` tenía el mismo agujero, y es preexistente.** Recibía `recepcion_id` y `ri_id` en la URL y los **ignoraba**: filtraba solo por `foto_id`. Cualquier sesión borraba la evidencia de daño de cualquier recepción | Ahora la foto tiene que pertenecer al ítem y a la recepción de la URL. Además el archivo se borra por la misma ruta confinada |
| 3 | **Carrera en el limitador.** Comprobar y anotar eran dos operaciones con un bcrypt en medio: 40 peticiones simultáneas pasaban el control antes de que ninguna anotara. El tope real era ~45, no 5 | `reservar_intento()` hace las dos cosas dentro del mismo lock |
| 4 | **El diagnóstico mentía.** `_segundos_restantes` usaba el fallo más antiguo; con más de 5 marcas respondía "no bloqueado" mientras el login seguía dando 429 — **exactamente el síntoma que el propio módulo enseña a leer como "hay varios workers"**. Diagnóstico equivocado garantizado | Usa la marca que ocupa la posición del tope contando desde el final |
| 5 | **10 de 13 pruebas eran verdes incondicionales.** `check()` acumula en vez de lanzar, y yo había expuesto las etapas internas como funciones `test_*`, así que pytest las recolectaba y pasaban con cualquier código | Las etapas se llaman `_paso_*`; solo la función que revisa `_fails` es recolectable |
| 6 | **Una sonda no probaba nada.** El test del confinamiento usaba `.../../../../etc/passwd`, pero `basename` lo reduce a `passwd` **antes** de la comprobación de prefijo: el test pasaba igual con y sin el arreglo | Se agregó el payload `/uploads/bodega/..`, que sí sale del directorio, y se exige **400 exacto** |
| 7 | **Inyección en el log y memoria sin acotar.** El email viaja sin validar: uno con salto de línea fabrica entradas falsas en `pm2 logs`, y como es la llave del diccionario, su tamaño lo elegía el atacante | Se trunca a 254 caracteres (RFC) y se imprime con `repr()` |
| 8 | **`estado()` decía "no muta nada" y purgaba**, pudiendo descartar contadores vivos | Purga solo lo vencido, sin aplicar el techo de llaves, y el docstring lo dice |
| 9 | **El diagnóstico devolvía la IP del operador**, no la de la persona bloqueada | La IP se pasa por parámetro; se agregó el PID |
| 10 | **`.env.example` descomentaba `CORS_ORIGINS`**, así que quien copiara la plantilla perdía `localhost:5173` y depuraba un error de CORS opaco | Vuelve a quedar comentado, con la advertencia |
| 11 | **El aviso del 403 moría en la navegación.** `toast.error()` seguido de recarga: el usuario era teletransportado sin explicación | El aviso viaja por `sessionStorage` y se emite al cargar en el destino |
| 12 | **Se navegaba también en escrituras.** Un `PUT` denegado arrastraba al usuario fuera de un formulario a medio llenar | Solo se navega en lecturas (`GET`); las escrituras solo avisan |
| 13 | **Una foto borrada podía resucitar.** `invalidarFoto` no cancelaba la petición en vuelo: al resolver, la volvía a meter al caché como blob huérfano | Contador de generación por foto; el resultado tardío se descarta. Desde 2026-10-06 también se descarta lo que venía viajando al cambiar la sesión (`limpiarCacheFotos`) |
| 14 | **`podar()` podía revocar un blob en uso** y el `<img>` quedaba roto para siempre, sin que nadie se enterara | `onError` con **un** reintento |
| 15 | **El "toque" del LRU era inalcanzable**: el efecto cortaba antes de llamar a `cargar`, así que el caché era FIFO y podía descartar la foto más usada | Todos los lectores pasan por `cargar` |
| 16 | **Comentario falso:** decía "son miniaturas, pocos MB". El backend **no** redimensiona: son fotos de celular de 3-8 MB pintadas a 64×64 | Comentario corregido y techo del caché bajado de 60 a 20 |

### Lo que los revisores aprobaron sin cambios

`auth_errors.py`, el manejador de excepciones de `main.py` (incluida la delegación en
`http_exception_handler`, que preserva los códigos sin cuerpo y las cabeceras), los flags de
`config.py`, y la decisión de contar **emails distintos** por IP en vez de fallos.

### Deuda registrada, NO incluida en esta fase

| Tema | Por qué se difiere |
|---|---|
| `upload_foto` valida solo la extensión, no los bytes; sin tope de tamaño | Es preexistente y toca el flujo de subida, que nadie pidió cambiar. (2026-10-06: el derivado de imagen se construyó por otro camino —`backend/portal/fotos.py`, tabla `foto_articulo`— y no toca `upload_foto`; la deuda sigue abierta y sin fase) |
| Verificar la configuración de nginx (`location /uploads/`, y que el 8002 no sea alcanzable directo) | **No es código: es del servidor.** Si nginx sirve `/uploads/` por su cuenta, el cerrojo C1 es decorativo. **Hay que confirmarlo en el deploy** |
| El lockout de una cuenta ajena es renovable indefinidamente | Bloquea el login, no la cuenta. La mitigación (llavear por email+ip) queda anotada en el módulo |
| Rotación de logs de pm2 | Es configuración de operación (`pm2-logrotate`) |

---

## 11. Segunda revisión independiente (comité de 36 agentes) y correcciones

Tras corregir los 16 defectos de la primera ronda, el trabajo se sometió a un **segundo
comité independiente** de tres revisores (seguridad, Python, React), cada uno con un
escéptico que intentaba refutar sus hallazgos, más un crítico de completitud.
**Verificaron empíricamente cada corrección** —con hilos, con mutaciones y contra la base
real— en vez de creer en la palabra de nadie. Confirmaron 14 hallazgos nuevos.

### Los tres graves, todos reproducidos en vivo

| # | Defecto | Cómo quedó |
|---|---|---|
| **A** | **El tope de login se evadía con tildes.** La columna `users.email` es `utf8mb4_unicode_ci`, o sea insensible a acentos: para MySQL `ádmin@x.cl` y `admin@x.cl` son la MISMA fila, pero el limitador las contaba como dos llaves distintas. Reproducido: agotados los 5 intentos, cinco variantes acentuadas recibieron cupo nuevo, y con la clave correcta la variante acentuada **entró**. El presupuesto contra una cuenta pasaba de 5 a ~50 por ventana. Y era **invisible**: el diagnóstico del email real seguía diciendo 5 intentos | `normalizar_email` pliega acentos (NFKD) y usa `casefold()`. La llave usa ahora la MISMA equivalencia que la base |
| **B** | **Puse el candado de marca en UNA sola de las tres puertas.** `serve_foto` lo llevaba; `upload_foto` y `delete_foto`, en el mismo archivo y tocados en el mismo cambio, no. Reproducido: una sesión de MonzaParts recibía 403 al PEDIR la foto, pero el DELETE devolvía 200 y borraba fila y archivo, y el POST devolvía 201 y adjuntaba una foto a una recepción de Grupo AM | Las tres puertas llevan `require_empresa("mineria")`, y el test lo prueba en las tres |
| **C** | **REGRESIÓN QUE YO INTRODUJE.** Al agregarle el filtro de pertenencia a `delete_foto`, dejé de aceptar la segunda forma del `ri_id`: para un ítem no empezado la pantalla manda el id del *EmbarqueItem*, no el del ítem de recepción. Resultado: borrar una foto recién subida respondía `{"ok": true}` **sin borrar nada**. El operador veía "Foto eliminada", la foto desaparecía de la lista y al recargar volvía. El disco acumulaba huérfanas y el guard de "los ítems dañados requieren foto" quedaba satisfecho por una foto que el operador creía borrada | Se extrajo `_resolver_ri()`, **un helper compartido por subir y borrar**, para que la regla de resolución viva en un solo lugar. Y ahora responde **404** en vez de `{"ok": true}`: el silencio fue lo que convirtió un desajuste de ids en un borrado fantasma con mensaje de éxito |

> **El patrón que se repitió:** arreglé una puerta y olvidé las gemelas — dos veces en el
> mismo archivo. Lo que lo corta no es otro parche, sino la pieza compartida: el candado
> en las tres rutas y `_resolver_ri` como única fuente de la regla.

### Pruebas que no probaban nada

| # | Qué pasaba | Cómo quedó |
|---|---|---|
| D | **La corrección de la carrera no tenía NINGUNA prueba.** El comité reconstruyó la versión con el defecto y la suite seguía verde: el defecto más caro de la primera ronda podía volver mañana sin que el gate se enterara | `_paso_6` lanza 40 hilos con barrera y exige que pasen **exactamente 5**. Verificado por mutación: con el defecto puesto pasan 40 de 40 |
| E | **La sonda del borrado no discriminaba.** El caso cruzado cambiaba las DOS dimensiones a la vez (otra recepción Y otro ítem), así que cualquiera de las dos condiciones que sobreviviera bastaba para bloquearlo: quitando una sola, el test seguía verde | Se partió en `_paso_6a` (misma recepción, otro ítem) y `_paso_6b` (mismo ítem, otra recepción). Cada uno varía **una sola** dimensión |
| F | **La corrección de los verdes incondicionales quedó a medias**: la apliqué en dos de los tres archivos. El tercero seguía aportando 5 pruebas que pasaban con cualquier código | Los tres archivos usan `_paso_*`. De 8 pruebas recolectadas quedan **3 reales** |
| G | El docstring de sondas nombraba `revisar()`, una función que ya no existe | Corregido, y ahora cada sonda dice qué línea romper |

**Todas las sondas se verificaron por mutación en una copia**, no de palabra: 8 mutaciones
distintas, las 8 ponen la prueba en rojo.

### Otros hallazgos corregidos

- **La foto se cacheaba 5 minutos en el navegador.** `private` solo se lo prohíbe a los
  proxies; la caché del navegador se indexa por URL, no por el header `Authorization`. En
  el computador compartido de bodega, el siguiente usuario podía ver la imagen sin pasar
  por el candado. Ahora es `private, no-store`.
- **El 403 de ámbito ya no navega, solo avisa.** Navegar causaba tres daños: sacaba al
  usuario de un formulario a medio llenar, el aviso moría en la recarga, y se adelantaba a
  los mensajes que las pantallas ya tenían escritos —`MonzaLeadsPage` dice "Avisa al
  administrador para que revise la marca asignada a tu cuenta", bastante mejor que un aviso
  genérico. De paso desaparecieron tres piezas: la bandera anti-bucle, el paso de mensajes
  por `sessionStorage` y la distinción lectura/escritura.

### Deuda registrada, NO incluida

| Tema | Por qué |
|---|---|
| **Verificar en el servidor que nginx no sirva `/uploads/` por su cuenta, y que el puerto 8002 no sea alcanzable directo** | **No es código, es del servidor. Es el punto más importante de esta lista:** si nginx sirve esa carpeta, el cerrojo C1 es decorativo, y si el 8002 es alcanzable sin proxy, la regla de IP del limitador se evade con una cabecera |
| `upload_foto` valida la extensión, no los bytes; sin tope de tamaño | Preexistente. (2026-10-06: el derivado de la Fase 5 se construyó por otro camino y no cubre esta subida; sigue abierta y sin fase) |
| `serve_foto` no responde 304: cada miniatura descarga la foto original completa | (2026-10-06: el derivado del servidor existe para las fotos del portal, no para las de recepción; sigue abierta) |
| El purgado recorre todo el diccionario en cada intento (4,1 ms medidos con 10.000 llaves) | Solo pesa bajo ataque activo, y ahí es preferible el costo al agujero |
| El lockout de una cuenta ajena es renovable | Bloquea el login, no la cuenta. La mitigación queda anotada en el módulo |

---

## 12. Tercera revisión (comité de 40 agentes) y correcciones

El trabajo se sometió a un **tercer comité independiente**, enfocado en lo de mayor riesgo:
regresiones introducidas por la ronda anterior de arreglos. Verificaron contra la
**aplicación real** y la base real, no contra un FastAPI de juguete. Un revisor RECHAZÓ.

### Los dos graves, ambos reproducidos en vivo

| # | Defecto | Cómo quedó |
|---|---|---|
| **A** | **Mi arreglo del `ri_id` seguía adivinando.** El campo `id` que manda la pantalla vive en DOS espacios de numeración: `RecepcionEmbarqueItem.id` para un ítem empezado y `EmbarqueItem.id` para uno que no. Mi resolvedor probaba uno y después el otro, "el primero que aparezca". Los dos contadores van casi parejos —al escribir esto, a 3 unidades—, así que un mismo entero puede designar dos filas distintas de la misma recepción. **El comité demostró que el gate se iba a poner rojo solo**: igualando los contadores, la prueba que yo había escrito justamente para ese camino fallaba sin que nadie tocara una línea | Ya no se adivina. `_candidatos_ri` devuelve TODOS los candidatos y **quien llama decide con el dato que sí tiene**: el borrado se ancla en la foto (que sabe de qué ítem cuelga, dato autoritativo) y la subida responde **409 pidiendo recargar** si hay ambigüedad. Y el frontend guarda el id real que devuelve la subida, así que deja de usar el ambiguo |
| **B** | **El plegado de acentos cerró media puerta.** MySQL da peso cero a los acentos **y a los caracteres invisibles**. Mi arreglo quitaba los combinantes y dejaba pasar los de formato: con un ancho-cero metido en el email, la base encuentra la misma fila y el limitador ve una llave nueva. Medido: 77 de 117 variantes de un solo carácter evadían el contador, y **con la clave correcta la variante invisible entraba como el mismo usuario**. Segunda familia: `casefold()` corría ANTES de NFKD, así que las descomposiciones que producen mayúsculas (№→No, ™→TM) se escapaban | Se descartan también las categorías `Cf` y `Cc`, y `casefold()` corre **después** de NFKD |

> **El patrón, otra vez:** los dos son arreglos míos que cerraron el caso que tenía a la
> vista y dejaron abierto el de al lado. Lo que lo corta no es más cuidado, es quitar la
> adivinanza: donde hay un dato autoritativo se usa ese, y donde no lo hay se responde
> 409 en vez de elegir al azar.

> **Precisión (2026-10-06):** la fila A describe un diseño que la sección 13 reemplazó.
> Hoy `upload_foto` no responde 409 ante la ambigüedad: sus únicos 4xx son 400 (recepción
> cerrada o inexistente; formato) y 404 (ítem de otra recepción). Y el id real que devuelve
> la subida (`riId` en `BodegaPage.tsx`) viaja solo en la URL de las fotos: guardar el ítem
> sigue mandando `item.id` y el backend resuelve por `embarque_item_id`.

### Y algo peor: seis de mis sondas no probaban nada

El comité mutó el código en una copia y encontró **seis lugares donde se podía revertir un
arreglo de seguridad y la suite seguía verde**. Los seis están cubiertos ahora, y **cada
sonda nueva se verificó por mutación**: se rompe el arreglo y la prueba tiene que ponerse
roja. Si no se pone roja, la prueba no vale nada.

| Sonda que no discriminaba | Por qué | Cómo quedó |
|---|---|---|
| El confinamiento del **borrado** | Mi payload `"/uploads/bodega/.."` apunta a un DIRECTORIO: `isfile` da False y no se borra nada ni con el defecto puesto | Ahora apunta a un **archivo real** fuera de la carpeta |
| El **`Cache-Control`** | Solo exigía `private`, así que `private, max-age=86400` —justo el caso del computador compartido— pasaba | Exige `no-store` y prohíbe `max-age` |
| El **mount público** | Pedía un archivo inventado, y `StaticFiles` responde 404 igual **con el mount puesto** | Pide un archivo que **existe de verdad** |
| La **traducción 403→401** | No existía prueba: el cerrojo C4 entero se podía revertir sin que nada fallara | Archivo nuevo `test_main_cerrojos.py` contra la app **real** |
| La **sobre-reclasificación** de 403 | Usaba un token basura, que da 401 antes de llegar al guard de empresa | Usa una **sesión real de la otra marca** |
| El aislamiento de las **dos condiciones** del borrado | El caso cruzado variaba las dos dimensiones a la vez | Partido en dos etapas, cada una con **una sola** dimensión |

### Deuda que queda abierta, y es importante

| Tema | Estado |
|---|---|
| **`require_empresa` separa MARCAS, no separa personal de clientes** | `'mineria'` es el valor por omisión de `users.empresa`. Ese eje sirve para Grupo AM vs. MonzaParts, **no** para "empleado vs. cliente del portal". La Fase 1 del plan (rol + tabla puente) es la que resuelve eso, y **tiene que estar antes del primer usuario externo** |
| **El resto de `bodega.py` sigue sin candado** | Una sesión de la otra marca leyó `GET /api/bodega/recepciones/{id}` con el nombre del archivo de la foto y la observación del daño, listó reclamos y embarques, y **cerró una recepción de Grupo AM**. Es **preexistente** y está fuera del alcance declarado de la Fase 0 (el candado del router completo es una decisión que el dueño difirió), pero es lo más grave que queda en pie |
| **Verificar nginx en el servidor** | Sigue siendo el punto que no se puede cerrar desde el código |

---

## 13. Cuarta revisión (comité de 25 agentes) y correcciones

Dos de los tres revisores RECHAZARON. Encontraron que **mi arreglo de la tercera ronda era
peor que el bug** en un caso, y que el otro seguía abierto con una familia de caracteres
distinta. Los dos reproducidos en vivo contra la aplicación real.

### El peor error de toda la fase: el 409 bloqueaba al bodeguero

La tercera ronda hizo que `upload_foto` respondiera **409** cuando el identificador del
ítem era ambiguo. Suena a "fallar seguro". No lo era:

- El 409 solo dispara cuando **ambas** interpretaciones del id resuelven — que es justo el
  caso en que el id era **correcto**.
- La pantalla **no deja guardar un ítem dañado sin foto**. Así que el bodeguero quedaba sin
  poder subir la foto **ni** guardar.
- El remedio que anunciaba el mensaje ("recarga la recepción") era **inerte**: para un ítem
  ya empezado los dos identificadores son el mismo número, así que el bloqueo era permanente.

> **La lección:** bloquear no es fallar seguro cuando deja al operador sin salida.

**Cómo quedó:** el llamador desambigua con el dato que ya tiene (`embarque_item_id`), que es
**la convención que este archivo ya usaba** — `marcar_item` lo recibe en el cuerpo desde
siempre. Yo no la seguí. Ahora el frontend lo manda al subir la foto, el backend lo respeta,
y sin él se cae al comportamiento histórico dejando rastro en el log. **Sin bloqueos.**

De paso apareció **la tercera puerta del mismo contador**: `marcar_item` seguía eligiendo
"el primero que aparezca" y podía escribir la cantidad recibida en la fila de **otro ítem**
— sin atacante, en el flujo normal. Ahora usa el mismo resolvedor.

### El plegado del correo: la familia equivocada

El filtro usaba `unicodedata.combining(c)`, que **no responde "¿es una marca?"**: devuelve la
clase canónica de combinación, un **número**, y vale **0** para muchísimas marcas reales.

| Carácter | Categoría | `combining()` | El filtro viejo |
|---|---|---|---|
| U+0301 acento agudo | Mn | 230 | lo quitaba |
| U+0902 anusvara | Mn | **0** | **lo dejaba pasar** |
| U+034F juntador | Mn | **0** | **lo dejaba pasar** |
| U+FE0F selector de variación | Mn | **0** | **lo dejaba pasar** |
| U+0488 marca envolvente | Me | **0** | **lo dejaba pasar** |

El comité barrió los codepoints preguntándole **a la base** cuáles considera iguales: ~76-100
variantes de un carácter seguían evadiendo el tope, y con la clave correcta la variante con
U+FE0F **entraba** como el mismo usuario.

**Cómo quedó:** se filtra por **categoría** (`Mn`, `Mc`, `Me`, `Cf`, `Cc`), no por la clase.
Preguntar por la categoría cierra la familia completa, no un caso a la vez. Verificado
barriendo codepoints contra la columna real: **0 evasiones**.

### Todas las sondas, verificadas por mutación

El comité también encontró cuatro arreglos que se podían revertir con la suite verde. Ahora
cada uno tiene su sonda, y **cada sonda se verificó rompiendo el arreglo a propósito**: van
**18 mutaciones** comprobadas en total, y todas ponen la prueba en rojo. Una que no lo hacía
(el confinamiento del borrado) se rehizo con un payload que sí ejercita la línea.

---

> **Constancia (2026-10-06):** el comentario del caso ambiguo de `_resolver_ri` ya no remite a
> «la historia, 4ª versión»; el porqué completo vive en su docstring.

## 14. Lo que se agregó por pedido del dueño (2026-08-20)

Estas tres cosas **no** son de seguridad: son funcionalidad que el dueño pidió durante la
revisión, y todas se apoyan en datos que ya existían.

| # | Pedido | Qué faltaba realmente |
|---|---|---|
| **1** | Poder subir fotos en la recepción **aunque el ítem llegue completo**, y varias por ítem | El backend siempre lo permitió; era **la pantalla** la que escondía el bloque de fotos salvo para ítems dañados. Motivo del dueño: los ítems sobre $3.000.000 de venta se fotografían aunque lleguen perfectos. La foto sigue siendo **obligatoria solo para los dañados**: la exige `marcar_item` al guardar y, desde 2026-10-06, `delete_foto` no deja borrar la última foto de un ítem ya guardado como dañado (409). `cerrar_recepcion` no revisa fotos |
| **2** | Poder **acceder después** a esas fotos | La pestaña Historial era una tarjeta muerta: el endpoint no devolvía `recepcion_id`, así que no había forma de abrir una recepción cerrada. Las fotos estaban en la base y el detalle ya las servía — faltaba el camino. Ahora la tarjeta abre la recepción en **modo consulta**: se ven ítems, observaciones y fotos; no se edita ni se borra nada. Desde 2026-10-06 lo sostiene también el backend: subir o borrar una foto de una recepción cerrada responde 400 (`_recepcion_abierta_con_lock`); y una subida o un borrado puede esperar unos instantes si otro operador está guardando o cerrando la misma recepción. (De paso, el conteo de ítems que pintaba esa pestaña venía vacío) |
| **3** | Ver las fotos **por ítem desde Despachos**, sin poder borrarlas | Endpoint nuevo `GET /api/bodega/items/{item_cotizacion_id}/fotos`, que recorre la cadena de llaves que ya existía. **Solo lectura por construcción**: no hay borrado en esa ruta. En cada ítem de Despachos hay un botón con dos pestañas: **Foto recepción** (existen) y **Foto despacho** (todavía no — es la Fase 5, y la pestaña lo dice en vez de esconderse). 2026-10-06: la pestaña «Foto despacho» ya existe (trabajo del portal, fuera del paquete de fotos de recepción), y la fecha de cada foto de recepción se pinta en día de Chile con `fmtFechaServidor`, igual que la fecha del despacho |

> **Sobre la foto que verá el cliente:** es la del **despacho**, y **no existe todavía**. Va
> en una tabla aparte (`foto_articulo`), con derivado sin EXIF, en la Fase 5. Mantenerla
> separada de la evidencia de recepción es deliberado: con dos tablas distintas,
> publicarle al cliente una foto de daño **no es un error posible**.

### Nota sobre un fallo transitorio

Durante estas correcciones una corrida del gate marcó `test_paridad_inversa_bodega` en rojo.
**No se reprodujo**: esa prueba pasa sola y el gate completo volvió a dar verde dos veces.
La explicación más probable es que esa corrida se solapó con los agentes del comité, que
estaban ejecutando pruebas contra la misma base de datos. Queda anotado en vez de darse por
arreglado — si vuelve a aparecer, el sospechoso es la concurrencia contra MySQL, no el código.

## 15. Quinta revisión (2026-08-30): revisión multienjambre y correcciones

Una barrida completa con expertos sobre TODO lo agregado en la fila de pedidos del dueño
(sección 14). Tres hallazgos técnicos, los tres **reproducidos y verificados por mutación**
(se rompe el arreglo a propósito y se confirma que la prueba se pone roja):

| Gravedad | Hallazgo | Arreglo |
|---|---|---|
| **CRÍTICO** | El nombre del archivo de foto era `recepcion_{id}_{segundo}.jpg`. Subir **dos fotos al mismo ítem dentro del mismo segundo** (justo el caso del ítem caro que se fotografía varias veces) daba el **mismo nombre**: el segundo archivo pisaba al primero en disco, quedaban **3 filas apuntando a 1 archivo**, y borrar una dejaba a las otras dos con un 404. | El nombre lleva ahora un `uuid4`: `recepcion_{id}_{uuid.uuid4().hex}.jpg` (en `upload_foto` de `bodega.py`). Es el mismo `uuid4` que ya usa `monza_router_documentos.py`, no un invento nuevo |
| **ALTO** | En la galería, `podar()` (el que recorta el caché de miniaturas cuando pasa de 20) podía **revocar el blob de una foto que todavía se estaba mostrando**. Con más de 20 miniaturas montadas a la vez, algunas quedaban rotas sin que nadie se enterara. | Conteo de referencias: `montadas` (un `Map` fotoId→cuántos `<FotoBodega>` la muestran AHORA) y `podar()` **solo** revoca las que no están montadas en ninguna parte. `limpiarCacheFotos()` vacía el caché al cambiar la sesión; desde 2026-10-06 la dispara una suscripción al store de sesión en `FotoBodega.tsx` (cualquier cambio de token, incluido el que llega desde otra pestaña), no los botones de salir |
| **ALTO** | El tope de intentos del login se llaveaba por el correo **plegado** (`normalizar_email`), y ese plegado por categorías Unicode no cubre los dígitos de otros alfabetos (árabe, devanagari): la base los pliega y la función no, así que `admin1` y su gemelo en árabe caían en **cubetas distintas** y el atacante duplicaba su cupo de intentos contra una cuenta real. | Cuando la cuenta **existe**, el contador se llavea por `uid:{user.id}` — la identidad la define la base, no el texto del correo (`auth.py:68`). El correo plegado queda solo de respaldo para cuentas que no existen, donde atacar no sirve de nada |

### El fallo transitorio, ahora con causa raíz

En la corrida final del gate aparecieron **dos** pruebas en rojo, las dos de MonzaParts y
ninguna tocada por esta fase (el trabajo de la Fase 0 vive entero en `bodega.py` y en el
login de Grupo AM, y no escribe una sola fila en las tablas de Monza):

- `monza_contabilidad/tests/test_integration.py::test_monza_contabilidad_integration`
- `monza_tests/test_alertas_diarias.py::test_alertas_diarias_monza`

Corridas **aisladas**, seguían rojas — así que no era el azar de la concurrencia del momento,
sino datos que la concurrencia había dejado **enterrados** en la base. La causa raíz resultó
única para las dos:

> Una corrida anterior de `test_integration` **murió a medio camino** (se solapó con los
> agentes del comité peleando por la misma base de MySQL) y su `cleanup()` no alcanzó a
> correr. Ese `cleanup()` solo sabe borrar lo que **ese** proceso sembró, porque guarda los
> ids en un diccionario en memoria (`_seed`). Al morir el proceso, sus filas
> `__TEST_MC__-COT`, `__TEST_MC__-ADEL`, `__TEST_MC__-RET` y el despacho huérfano
> `__TEST_MC__-ORPH` quedaron colgadas.

De ahí salían **los dos** fallos:

| Prueba | Cómo la rompía el huérfano |
|---|---|
| `test_integration` | Su `seed()` vuelve a insertar `__TEST_MC__-COT`, pero `numero` es **ÚNICO** → `Duplicate entry '__TEST_MC__-ORPH' for key 'uq_monza_despachos_numero'`. Rojo en cada corrida futura hasta limpiar la base a mano |
| `test_alertas_diarias` | La venta huérfana `__TEST_MC__-RET` tenía un ítem en `en_bodega`. El barrido diario evalúa **toda** la base (lo dice su propio encabezado), así que le generaba un aviso "venta lista para despacho" de más: la prueba esperaba 5 avisos y contaba 6 |

**El arreglo no es borrar filas a mano** (eso cura el síntoma de hoy y vuelve mañana). Es
estructural: `seed()` ahora llama a un `_barrer_mark(db)` que limpia **por marca**
(nombre/número/email, no por los ids de `_seed`) **antes** de sembrar. Una corrida que muere
ya no puede romper la siguiente: la próxima se auto-cura. Es el mismo criterio que
`monza_tests/*::_cleanup`, que ya limpiaba por marca (`like(f"OCP-{SUF}-%")`) en vez de por
ids en memoria — `test_integration` era el que quedaba fuera de esa norma.

Verificado con **sonda de poder discriminante**: se plantó a mano un huérfano en colisión
(`__TEST_MC__-COT` + `__TEST_MC__-ORPH`) y se corrió la prueba. **Antes** del arreglo moría
con `Duplicate entry`; **después** pasa en verde, curándose sola. Con eso, el gate pelado
(`pytest` sin lista de carpetas a mano) da **359 en verde, 0 en rojo**.

> Nota de alcance: `test_integration.py` es un archivo de **pruebas** de Monza, no código de
> producción; el único cambio es endurecer su preparación de datos. No se tocó ninguna regla
> de negocio de MonzaParts. Queda anotado acá porque un archivo de Monza cambió en una rama
> de la Fase 0 de Grupo AM, y el porqué no debe ser un misterio para quien lo revise.
