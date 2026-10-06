# Paquete «login-y-limitador» — 2026-10-06

Endurecimiento del inicio de sesión (`POST /api/auth/login`), del limitador de intentos
(`backend/rate_limit_login.py`), del cambio de clave del Portal de Clientes y del
fail-fast de `SECRET_KEY`. Nada de esto requiere migración ni `init_db`. Dos precauciones
nuevas en el deploy, al final de este documento.

## Qué se arregló, por ítem

| Id | Qué pasaba | Qué hace ahora |
|---|---|---|
| **C28, P22** | La llave del contador la armaban a mano `login`, `estado_rate_limit` y `cambiar_clave`, y la llave de una cuenta (`uid:N`) compartía espacio con la de un correo sin cuenta: el texto «uid:5» tecleado como correo gastaba el cupo de la cuenta 5 (también «UID:5» y la variante de ancho completo). | `rate_limit_login.py` es el único que arma la llave: `llave_de_cuenta(user_id)` → `uid:N`; `llave_de_login(user_id, email)` → `uid:N` si hay cuenta, si no `mail:` + el correo plegado. Los dos espacios quedan separados por el prefijo; login, diagnóstico y cambio de clave llaman a esas funciones. |
| **P23** | `limpiar_exito` borraba TODAS las marcas de la llave, también las reservas de peticiones que seguían dentro del bcrypt: quien sabe la clave (un cliente, o una clave robada) podía iniciar sesión en bucle sin ningún tope (medido: 339 admitidos y 40 simultáneos en 1,5 s). | Tres reglas en ventanas de 15 min: **5 fallos** por llave (el éxito los borra), **30 intentos** por llave hayan salido bien o mal (el éxito NO los borra: `MAX_INTENTOS_POR_LLAVE`, contador `_INTENTOS`) y **10 llaves distintas** por IP. El 429 dice «Demasiados intentos. Reintenta en N segundos.» y el diagnóstico trae `intentos_totales` y `motivo` (`fallos` / `intentos` / `ip`). |
| **C22** | Si nginx no reenvía `X-Forwarded-For`, todos los clientes llegan como `127.0.0.1` y la regla por IP se vuelve un cerrojo global que cualquiera activa con 10 peticiones. | `_ip_identifica_cliente(ip)`: con loopback o IP ausente la regla por IP se OMITE (no se anota en `_SPRAY_IP`), cada intento queda en el log con `regla_ip=omitida` y se imprime UN `[login] AVISO` por proceso con la directiva que falta. Las reglas por llave no dependen de la IP y siguen igual. |
| **C6** | Para una cuenta real el limitador recibía `uid:N` pero lo llamaba `email` en parámetros, diccionarios, constantes y log: `pm2 logs \| grep juan@grupoam.cl` no encontraba nada justo para las cuentas que importan. | Todo el módulo habla de «llaves» (`MAX_FALLOS_POR_LLAVE`, `MAX_LLAVES_POR_IP`, `_FALLOS`, parámetro `llave`). `reservar_intento(llave, ip, correo='')` recibe aparte el correo tecleado, SOLO para el log: `[login] intento llave='uid:42' correo='juan@grupoam.cl' ip='200.1.2.3' fallos=1/5 intentos=1/30 llaves_ip=1/10`. El diagnóstico devuelve `email` (el consultado) y `llave` por separado; `emails_distintos_desde_ip` / `max_emails_por_ip` pasan a `llaves_distintas_desde_ip` / `max_llaves_por_ip`. |
| **C7, C26, C27** | Docstrings que citaban una `revisar()` que no existe, `registrar_fallo` muerta (y peligrosa: anotaba sin comprobar el tope), narraciones de versiones que nunca estuvieron en git, y un `int()` en `_segundos_restantes` que admitía un intento hasta 1 s antes de que venciera la marca crítica. | `registrar_fallo` eliminada; `_segundos_restantes` redondea hacia ARRIBA (`math.ceil`); docstrings y encabezado en presente (módulo, `cambiar_clave`, comentario de `LOGIN_RATE_LIMIT_ENABLED` en `config.py`, suite). |
| **P18** | Un surrogate suelto en el JSON (`"\ud800"`) pasa pydantic pero el driver de MySQL no puede codificarlo: el SELECT del login terminaba en 500 (anónimo, antes del limitador); lo mismo el `encode` de la clave nueva en `cambiar_clave`. Un correo sin tope de largo. | `rl.correo_imposible(email)` (más de 254 caracteres, o no es Unicode válido) se pregunta ANTES del SELECT: 401 de siempre, sin tocar la base ni el contador. En `cambiar_clave`, el `encode` de la clave nueva va en try/except → 400 «La clave nueva tiene caracteres no válidos». |
| **C24, C33** | El login decía qué correos tienen cuenta: por tiempo (sin cuenta no corría el bcrypt: milisegundos contra 0,2-0,4 s) y por estado (una clave con surrogate daba 500 solo si la cuenta existía). | `_clave_correcta(user, clave)` gasta un bcrypt haya cuenta o no (sin cuenta, contra `_HASH_SIN_CUENTA`, generado al importar con la misma `get_password_hash`). Ese bcrypt de relleno tiene techo propio, global y que no depende del flag: `rl.cupo_de_relleno()`, 300 por ventana; agotado, el 401 sale sin bcrypt (se pierde el disimulo, no la CPU). `auth.verify_password` responde False ante un surrogate en vez de lanzar. |
| **C5** (solo documentar) | El docstring de `login` afirmaba que la reserva va «ANTES de tocar la BD» (falso: el SELECT va primero) y anunciaba «tres líneas de `rl`». | Docstring reescrito con el ORDEN real: correo imposible → SELECT → reserva → bcrypt (haya cuenta o no) → `limpiar_exito`. |
| **C9** | La lógica del portal en el login (`_entrada_portal`) vivía dentro de `routers/auth.py` y no traducía el esquema incompleto: sin `portal_acceso` (deploy sin `portal.init_db`) toda cuenta portal recibía un 500 mudo en el login (el login no pasa por `RutaDelPortal`). | Módulo nuevo `backend/portal/entrada.py` con `entrada_por_login(db, user)`: corte por inactividad, corte por clave temporal vencida, 503 con el comando de `init_db` si falta una tabla o columna (solo `esquema_incompleto`; cualquier otro error de la base sigue siendo 500) y anota el último acceso. `login` queda con una llamada e import local; `_entrada_portal` eliminada. |
| **C30** (solo documentar) | El frontend clasifica el 403 de ámbito por el COMIENZO del `detail` (`DETALLES_AMBITO` en `frontend-src/src/services/authErrors.ts`) y el backend no lo avisaba en ninguno de los tres orígenes. | Un comentario sobre cada origen: `auth.DETALLE_CUENTA_PORTAL`, `empresa_guard.require_empresa`, `role_guard.require_rol`. |
| **P24** | El fail-fast de `SECRET_KEY` solo conocía `changeme-in-production`; la plantilla trae otro placeholder (`CAMBIAR-POR-UNO-ALEATORIO-LARGO`) y la clave vacía también pasaba (python-jose firma HS256 con clave vacía). Con cualquiera de esos valores, quien tenga el repo firma un JWT de cualquier usuario. | `_SECRET_KEY_SIN_CONFIGURAR = {'', default, placeholder}` comparado con `.strip()` → `RuntimeError` con el comando para generar una clave. Con menos de 32 caracteres arranca pero AVISA en el log (`[config] AVISO SECRET_KEY`): el largo de las claves ya desplegadas no se conoce desde el repo y un `raise` tumbaría el backend en el siguiente restart. La prueba nueva lee el placeholder de la propia plantilla. |

## Archivos tocados

Código (por símbolo):

- `backend/rate_limit_login.py` — módulo reescrito de una sola vez: encabezado («LA LLAVE DEL CONTADOR» sube a segunda sección; tres reglas; «EL BCRYPT DE RELLENO»; modos de falla con la cabecera `X-Forwarded-For` y el supuesto de un solo proceso); `llave_de_cuenta`, `llave_de_login`, `correo_imposible`, `_ip_identifica_cliente`, `_vencer`, `_esperas`, `cupo_de_relleno` (nuevas); `MAX_FALLOS_POR_LLAVE`, `MAX_INTENTOS_POR_LLAVE`, `MAX_LLAVES_POR_IP`, `MAX_RELLENOS_POR_VENTANA`, `MOTIVO_*`, `_FALLOS`, `_INTENTOS`, `_RELLENOS`, `_IP_DESCONOCIDA`; `reservar_intento(llave, ip, correo='')`, `limpiar_exito`, `estado`, `reset`, `_purgar`, `_segundos_restantes` (`math.ceil`); `registrar_fallo` eliminada.
- `backend/routers/auth.py` — `_HASH_SIN_CUENTA`, `_credenciales_incorrectas`, `_clave_correcta` (nuevos); `login` (docstring y cuerpo); `_entrada_portal` eliminada; `estado_rate_limit` (comentario, llave, `datos["email"]`, párrafo del docstring).
- `backend/auth.py` (CRLF conservado) — `verify_password`; comentario sobre `DETALLE_CUENTA_PORTAL`.
- `backend/portal/entrada.py` (NUEVO) — `entrada_por_login`.
- `backend/portal/router.py` — `cambiar_clave` (docstring, try/except del encode, `rl.llave_de_cuenta`). El `return` con `access_token` y el `route_class=RutaDelPortal` no se tocaron.
- `backend/portal/__init__.py` — `entrada.py` en la línea «identidad» del docstring.
- `backend/config.py` — comentario de `LOGIN_RATE_LIMIT_ENABLED`; bloque fail-fast (`_SECRET_KEY_SIN_CONFIGURAR`, `_LARGO_MINIMO_SECRET_KEY`).
- `backend/.env.example` — bloques `SECRET_KEY` y `LOGIN_RATE_LIMIT_ENABLED`.
- `backend/empresa_guard.py`, `backend/role_guard.py` — solo un comentario sobre el `raise` del 403.

Pruebas:

- `backend/routers/tests/test_login_rate_limit.py` — renombres (`MAX_FALLOS_POR_LLAVE`, `MAX_LLAVES_POR_IP`, `_FALLOS`), sondas nuevas en `_paso_3`, `_paso_6`, `_paso_8`, `_paso_9`; pasos nuevos `_paso_10` (IP que no identifica), `_paso_11` (los éxitos topan) y `_paso_12` (el 401 no delata ni revienta); helpers `_crear_cuenta_real` / `_borrar_cuentas`; encabezado en presente.
- `backend/portal/tests/test_cuentas.py` — `rl.reset()` al entrar y al salir de `run()`; d3b, d5b, d6c nuevos; §J «esquema a medias» (j1, j2, j3) con un gancho `before_cursor_execute` sobre `database.engine`; encabezado e imports.
- `backend/routers/tests/test_config_secret_key.py` (NUEVO) — arranque real en subproceso con la variable `SECRET_KEY`.
- `backend/portal/tests/mutaciones/login.py` (NUEVO) — las 36 mutaciones del paquete, para `python -m portal.tests.mutaciones`.
- `backend/portal/tests/mutaciones/pedidos.py` — la entrada «el login no corta por inactividad» de `test_aislamiento` apunta ahora a `portal/entrada.py`.

Documentación: `backend/portal/README.md` (fila `entrada.py`), `deploy/README.md` (párrafo del fail-fast de `SECRET_KEY`) y este archivo.

## Cómo verificar

Desde `backend/`, con uvicorn DETENIDO (con el servidor arriba pytest se cuelga) y una
suite a la vez:

```bash
./venv/bin/python -m pytest routers/tests/test_login_rate_limit.py -q    # 1 passed (12 pasos, ~30 s)
./venv/bin/python -m pytest portal/tests/test_cuentas.py -q               # 1 passed (§A-§J)
./venv/bin/python -m pytest portal/tests/test_aislamiento.py -q           # 1 passed (§5 sigue pasando por entrada_por_login)
./venv/bin/python -m pytest routers/tests/test_config_secret_key.py -q    # 1 passed (8 arranques en subproceso)
```

Resultado obtenido el 2026-10-06: las cuatro verdes (1 passed cada una; antes del arreglo,
con el código de ese día, las tres primeras nacían rojas y la cuarta tenía 5 checks rojos).

Mutaciones (el arnés copia `backend/` a una carpeta temporal, corre la suite sin mutar,
y por cada entrada rompe UNA regla y exige que la suite se ponga roja; nunca toca el árbol
de trabajo):

```bash
./venv/bin/python -m portal.tests.mutaciones login        # routers/tests/test_login_rate_limit.py
./venv/bin/python -m portal.tests.mutaciones cuentas      # portal/tests/test_cuentas.py
./venv/bin/python -m portal.tests.mutaciones secret_key   # routers/tests/test_config_secret_key.py
./venv/bin/python -m portal.tests.mutaciones aislamiento  # la entrada re-apuntada debe salir «roja», no ERROR
```

Resultado obtenido el 2026-10-06 (todas «roja», ninguna «VERDE (NO DISCRIMINA)» ni ERROR):

| Lista | Suite | Resultado |
|---|---|---|
| `login` | `routers/tests/test_login_rate_limit.py` (121 checks) | 24 de 24 mutaciones detectadas |
| `cuentas` | `portal/tests/test_cuentas.py` (54 checks) | 6 de 6 |
| `secret_key` | `routers/tests/test_config_secret_key.py` (8 checks) | 6 de 6 |
| `aislamiento` | `portal/tests/test_aislamiento.py` (33 checks; lista de `pedidos.py`) | 7 de 7, con «el login no corta por inactividad» ya apuntando a `portal/entrada.py` |

Greps que deben dar cero (nombres viejos y la función movida):

```bash
grep -rnE 'revisar\(|registrar_fallo|MAX_FALLOS_POR_EMAIL|MAX_EMAILS_POR_IP|_FALLOS_EMAIL|_entrada_portal' backend --include='*.py' --exclude-dir=venv
grep -n portal backend/routers/auth.py   # solo el import local de entrada_por_login, su comentario y los textos de /me, /users y register
```

Finales de línea: `backend/auth.py` sigue en CRLF (`git ls-files --eol -- backend/auth.py`
→ `i/crlf w/crlf`), y `git diff --stat` y `git diff --ignore-cr-at-eol --stat` sobre ese
archivo coinciden.

## Cómo revertir

- Todo el paquete: `git revert` del commit (no hay migración ni tabla nueva; los
  contadores viven en memoria, así que el formato nuevo de la llave no deja estado viejo
  tras el restart). `portal/entrada.py` se va con el revert; `_entrada_portal` vuelve a
  `routers/auth.py`.
- Solo el limitador (sin deploy): `LOGIN_RATE_LIMIT_ENABLED=false` en `backend/.env` y
  `pm2 restart machparts-api`. Apaga las tres reglas (fallos, intentos totales e IP); el
  techo del bcrypt de relleno (`cupo_de_relleno`) sigue vigente, a propósito.
- Solo el tope de 30 intentos: subir `MAX_INTENTOS_POR_LLAVE` en `rate_limit_login.py`.
- El fail-fast de `SECRET_KEY` no tiene interruptor: un `.env` con el placeholder o la
  clave vacía deja de arrancar. La salida es generar una clave, no revertir el código.

## Dos precauciones en el deploy (antes del restart)

1. **`SECRET_KEY` del servidor** (PROD y QA). Imprime solo un número; 0 = bien:
   `grep -c -E '^SECRET_KEY=("?)(CAMBIAR-POR-UNO-ALEATORIO-LARGO|changeme-in-production)?("?)[[:space:]]*$' backend/.env`.
   Si da 1, generar una clave nueva ANTES de reiniciar
   (`python -c "import secrets; print(secrets.token_urlsafe(48))"`); cambiarla cierra las
   sesiones abiertas. Largo sin mostrar la clave:
   `awk -F= '/^SECRET_KEY=/{print length($2)}' backend/.env` (con menos de 32 arranca y avisa).
2. **Cabecera `X-Forwarded-For` en nginx.** `grep -rn 'X-Forwarded-For' /etc/nginx/sites-enabled/`
   debe mostrar `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` en el
   `location` que hace proxy al 8002. Tras iniciar sesión una vez desde un navegador,
   `pm2 logs machparts-api --lines 200 --nostream | grep '\[login\] intento'` debe mostrar
   `ip=` con la IP pública del navegador. Si dice `127.0.0.1` y la línea termina en
   `regla_ip=omitida` (con un `[login] AVISO` más arriba), falta la directiva: las reglas
   por cuenta siguen vivas, la regla por IP no. Si la IP que aparece es la de un CDN o
   balanceador, avisar antes de seguir: todos los clientes compartirían esa IP.

## Decisiones que quedan para el dueño

- Dirección de internet de quien inicia sesión: pedir al programador que confirme en PROD y en QA (dos minutos) que nginx le pasa al sistema la dirección real de cada persona (la línea `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`) y que, tras un inicio de sesión, el registro muestra esa dirección y no 127.0.0.1. Mientras nadie lo confirme se aplica lo conservador: si la dirección no llega, el sistema NO usa la regla de «10 cuentas distintas desde una misma dirección» (así nadie puede dejar a toda la empresa sin poder entrar con diez intentos) y lo avisa en el registro; el tope de 5 intentos fallidos por cuenta sigue funcionando igual.
- Clave que firma las sesiones (SECRET_KEY): antes del próximo reinicio en PROD y en QA, pedir al programador dos comprobaciones que no muestran la clave: que no sea el texto de la plantilla ni esté vacía (si lo fuera, el sistema ya no arranca: hay que generar una nueva antes de reiniciar) y cuántos caracteres mide (`awk -F= '/^SECRET_KEY=/{print length($2)}' backend/.env`). Decisión pendiente: si se exige un mínimo de 32 caracteres. Conservador mientras tanto: una clave corta solo produce un aviso en el registro, no impide arrancar. Cambiar la clave cierra la sesión de todos una vez (vuelven a entrar).
- Tope nuevo: 30 inicios de sesión (o cambios de clave) por cuenta cada 15 minutos, contando los correctos. Hoy quien sabe su clave no tiene ningún tope, y una sola cuenta, por ejemplo la de un cliente del portal o una clave robada, puede poner lento todo el sistema. ¿Existe alguna cuenta compartida por mucha gente, o algún programa automático, que entre con la misma cuenta más de 30 veces en 15 minutos? Mientras no se sepa queda en 30, muy por encima del uso de una persona; si molestara, se sube ese número o se apaga todo el límite con LOGIN_RATE_LIMIT_ENABLED=false y un reinicio.
- Confirmar con el programador que el backend corre como UN solo proceso (uvicorn sin `--workers`). El límite de intentos se cuenta en la memoria de ese proceso; con varios, cada uno llevaría su propia cuenta y el tope real sería varias veces mayor. Conservador mientras tanto: el sistema lo supone, y el diagnóstico (GET /api/auth/rate-limit/estado) muestra el número de proceso para detectarlo.

## Notas para la pasada de documentación (fuera de este paquete)

El plan deja apuntados estos ajustes en documentos que este paquete no toca:

- `docs/fase-0-portal-cambios-2026-08-20.md`: la llave del correo sin cuenta lleva `mail:` (fila ALTO de la última tabla; cambiar la cita `auth.py:68` por `rl.llave_de_login` / `rl.llave_de_cuenta`); la fila C2 y la frase «un login correcto borra el rastro» quedan falsas (el éxito borra los fallos, no los 30 intentos totales); la tabla «Dos llaves que miden cosas distintas» pasa a tres reglas; «Para depurar sin leer logs» (el diagnóstico trae `llave`, `motivo`, `intentos_totales`; `email` es el consultado; en `pm2 logs` una cuenta real aparece como `llave='uid:N'` con el correo en `correo=`); la columna «Llave» de la tabla C2; el modo de falla de la cabecera ausente; el recuadro `pm2 describe` no prueba un solo proceso (lo delatan una sola línea «[scheduler] APScheduler started» y el mismo `pid` en dos llamadas al diagnóstico); la frase «antes de tocar la base» de «Cómo cuenta» (va después del SELECT); el 401 «no sirve para enumerar cuentas» se acota a «ni por el texto ni por el tiempo»; un correo de más de 254 caracteres o con texto inválido recibe 401 sin tocar la base; la fila G («nombraba `revisar()`») ahora también es cierta en el módulo de producción.
- `docs/CHECKLIST-DEPLOY-2026-07-20.md`: viñeta `LOGIN_RATE_LIMIT_ENABLED` (agregar «y 30 intentos por cuenta, éxitos incluidos» y los campos nuevos del diagnóstico); tercera comprobación de nginx (`X-Forwarded-For`, ver arriba); sección 2: en un entorno NUEVO además hay que poner `DATABASE_URL` y generar `SECRET_KEY`, con la comprobación del grep antes de reiniciar.
- `docs/fase-1-portal-identidad-2026-10-02.md`: sección de claves y fila MEDIO del cambio de clave (el tope de 5 frena a quien NO sabe la clave; la palanca de CPU de quien SÍ la sabe la acota la regla de 30); fila de `backend/routers/auth.py` en la tabla de archivos (el login delega en `portal/entrada.py`).
- `docs/plan-portal-clientes-2026-08-20.md`, fila S2: el fail-fast cubre default, plantilla y vacío (sin números de línea).
- `backend/portal/deps.py`, docstring de `require_portal` (archivo con trabajo sin commit de otro frente, por eso no se tocó): una línea de referencia cruzada, «La clave temporal VENCIDA no se corta acá sino en el login (`portal/entrada.py`): quien entró el último día tiene que poder terminar de cambiarla».
- `deploy/README.md`: si se quiere dejar documentado que el backend corre con un solo proceso, anotarlo SOLO tras verificarlo en el servidor, con el comando de arranque (uvicorn sin `--workers`).
