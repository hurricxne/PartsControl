# Paquete «cuentas-y-vinculo» — Portal de Clientes (2026-10-06)

Para el programador. Qué se arregló en las cuentas del portal y en el vínculo venta ↔
cliente, archivo por archivo, cómo se verifica y cómo se deshace. Sin commit: lo que
sigue describe el árbol de trabajo.

Convenciones de este documento: las rutas son relativas a la raíz del repo; «suite» es
`./venv/bin/python -m pytest <ruta> -q` desde `backend/`; «arnés» es
`./venv/bin/python -m portal.tests.mutaciones <parte-del-nombre>` desde `backend/`
(copia `backend/` a una carpeta temporal, rompe el código a propósito y exige que la
suite se ponga roja; nunca toca el árbol de trabajo).

## 1. Qué se arregló, por ítem

### C1, C13 — La bitácora del módulo no llegaba al log

El backend no configura `logging`; el logger `portal` heredaba WARNING y todos sus
`logger.info` (quién creó, restableció, reactivó, desactivó o eliminó una cuenta) se
descartaban sin aviso.

- `backend/portal/__init__.py` · `_hacer_visible_la_bitacora` (nuevo): le pone al logger
  `portal` un `StreamHandler` a stderr con hora UTC y nivel INFO al importar el paquete.
  Solo ese logger (subir la raíz encendería el INFO de httpx y apscheduler). Si algún
  día el backend configura logging para todos, este manejador sobra (cada línea del
  portal saldría dos veces): lo dice su docstring.
- `backend/portal/router_admin.py` · `crear`: el mensaje trae el correo (`%r`) y a qué
  clientes se dio acceso. `eliminar`: copia `actor_id, correo` al entrar (después del
  commit no queda fila) y el mensaje trae el correo. La clave temporal jamás entra a un
  mensaje.
- `backend/portal/tests/_datos.py` · `log_del_portal` (nuevo): captura lo que el logger
  escribe desviando la salida del MISMO manejador que instala `__init__.py`.

### C2 — Puerta gemela del 503 de esquema

`RutaDelPortal` (`deps.py`) ya traducía solo 1146/1054 a 503 con el comando de
`init_db`, pero `router_admin.py` era el único router del portal sin `route_class`:
«Cuentas del portal» respondía un 500 mudo justo donde el comando le llega a quien puede
correrlo.

- `backend/portal/router_admin.py` · `router`: `route_class=RutaDelPortal`.
- Nada más cambia en `deps.py`; `DETALLE_ESQUEMA` conserva su nombre y su comando.

### C3, C23 — Las dos carreras del alta daban 500

El `except IntegrityError` que envolvía el commit en `router_admin.crear` era
inalcanzable: el INSERT en `users` sale en el primer `flush` de `crear_cuenta`.

- `backend/portal/service.py` · `crear_cuenta`: cada `flush` traduce su propio
  `IntegrityError` a `CuentaError(409)`: el primero es el UNIQUE de `users.email` (otra
  alta del mismo correo en vuelo), el segundo es la FK de `usuario_cliente` (la ficha
  eliminada entre el chequeo y el INSERT). El error real de la base (`exc.orig`) queda
  en el log con `logger.warning`. Sin rollback dentro del servicio: lo hace el router.
- `backend/portal/router_admin.py` · `crear`: se borró el `try/except` muerto; queda
  `db.commit()`.

### C4 — Lo que faltaba en el log

- `backend/rate_limit_login.py` · `reservar_intento(llave, ip, correo="", origen="login")`:
  las líneas `[login] intento`, `TOPE ALCANZADO` y `RECHAZADO por limite` llevan
  ` origen=cambio-clave` cuando el intento viene del cambio de clave; las del login
  quedan idénticas. Documentado en «CÓMO SE DEPURA» del encabezado.
- `backend/portal/router.py` · `cambiar_clave`: `rl.reservar_intento(llave, ip, origen="cambio-clave")`.
- `backend/portal/router_admin.py` · `eliminar`: el `except IntegrityError` deja en el
  log, con `exc.orig`, qué tabla bloqueó el borrado (hoy, `portal_descarga_log`: una
  cuenta que descargó un documento no se puede borrar). El 409 no cambia de texto.

### C12, C34 — El listado prometía que una cuenta con la clave temporal vencida entraba

Había dos definiciones de «puede entrar»: el login miraba inactividad Y clave temporal;
el listado, solo inactividad.

- `backend/portal/service.py` · `vencimiento_clave_temporal` (nuevo): cuándo deja de
  servir la clave temporal (`clave_temporal_desde + 7 días`), o None. `clave_temporal_vencida`
  se apoya en ella: una sola fuente del plazo.
- `backend/portal/router_admin.py` · `_plazo_de_la_clave` (nuevo): `{clave_temporal_vence_at,
  clave_temporal_vencida}` con el MISMO cálculo que hace el login. Se suma a cada cuenta
  de `listar_cuentas` y a las respuestas de `crear`, `restablecer_clave` y `reactivar`.
  Todo aditivo; `EstadoCuenta` no gana valores.
- `frontend-src/src/portal/types.ts` · `PlazoDeClave` (nuevo); `CuentaPortal`,
  `CuentaCreada` y `ClaveRestablecida` lo extienden; `CuentaReactivada` (nuevo).
- `frontend-src/src/portal/api.ts` · `portalAdminAPI.reactivar` devuelve `CuentaReactivada`.
- `frontend-src/src/portal/CuentasPortalPage.tsx` · `ESTADO_CLAVE_VENCIDA` (nuevo),
  `textosDe('reactivar')` (ya no promete «con la clave que ya tiene»; avisa si la
  temporal venció), `ejecutar` (rama propia de reactivar: toast neutro de 9 s si la
  temporal venció), celda Estado (insignia «Clave temporal vencida» y, debajo, hasta
  cuándo sirve o «No puede entrar hasta que restablezcas su clave»), `alCrear` y la rama
  de restablecer pasan `vence` al cuadro de la clave.
- `frontend-src/src/portal/modalesAdmin.tsx` · `ClaveEntregada.vence` (nuevo) y la frase
  «Vence el … (hora de Chile) si el contacto no la cambia antes» en el aviso ámbar.
- Reactivar NO renueva la temporal (decisión del dueño, ver §4).

### C25 — Los helpers de RUT vivían en `service.py` y las suites importaban un privado del piloto

- `backend/portal/rut.py` (NUEVO): `digito_verificador` (el módulo 11, UNA vez),
  `rut_saneado`, `rut_canonico`, `rut_valido` (mudados tal cual) y `rut_con_dv` (el
  generador, antes `seed_piloto._con_dv`). Sin imports del backend.
- `backend/portal/service.py`: importa los tres helpers desde `.rut` y los sigue
  ofreciendo; `routers/clientes.py`, `portal/dtes.py` y las suites no cambian.
- `backend/portal/seed_piloto.py` · `sembrar`: usa `rut_con_dv`; `_con_dv` desaparece.
- `backend/portal/tests/_datos.py` · `rut_de_prueba`: usa `portal.rut.rut_con_dv`.
- `service.py` queda en 381 líneas (tope del README: 400).

### C31 — Reactivar una cuenta sin registro de vigencia fallaba abierto

- `backend/portal/router_admin.py` · `reactivar`: en la rama `acceso is None` el registro
  se recrea con `clave_temporal_desde=ahora`: la clave que tenga se trata como temporal
  (hay que cambiarla y, si no, vence). Una sola línea más `ahora = datetime.utcnow()`.

### C32 — El rollback apagaba «las que dicen portal», no lo que el corte deja fuera

- `backend/portal/desactivar_cuentas.py` · `_FUERA_DEL_ERP = "COALESCE(tipo_acceso, '') <> 'interno'"`,
  `resumen(conn)` (una fila por tipo), `desactivar(conn)`, y `main` imprime una línea
  por tipo más un «ATENCIÓN» para todo tipo que no sea `portal` (el ERP ya les niega la
  entrada, también se desactivan, y no se reactivan desde la pantalla). Ensayo sin
  `--aplicar` e idempotencia no cambian. En la base local el ensayo da el mismo total de
  antes (2 cuentas portal, ningún tipo desconocido).

### C39 — Combinantes crudos en la clase de la búsqueda

Ya estaba resuelto en el árbol: `frontend-src/src/portal/NuevaCuentaForm.tsx::llave`
lleva los escapes ASCII `̀-ͯ` (verificado por bytes con `xxd`, y
`grep -rnP '[\x{0300}-\x{036f}]' frontend-src/src` sale vacío). No se cambió nada.
Comprobación manual pendiente en la pantalla: «mineria» encuentra «Minería …» y «76123»
encuentra «76.123.456-7».

### C43 — Un RUT pegado desde Word dejaba la venta fuera del portal en silencio

- `backend/portal/rut.py` · `_GUIONES_TIPOGRAFICOS`, `_A_GUION` (nuevos) y `rut_saneado`:
  traduce los guiones tipográficos (U+2010…U+2015, U+2212) al del teclado, quita todo
  espacio Unicode y todo carácter invisible de formato (categoría `Cf`). Es el TERCER
  desvío deliberado respecto de Contabilidad (docstring del módulo). El módulo 11 se
  sigue exigiendo. Efecto lateral favorable: `portal/dtes.py` compara el receptor del
  DTE con estos helpers.

### P11 — Sondas sin cambio de producción

- `backend/portal/tests/test_cuentas.py` · d7b («un cambio de clave correcto no deja
  marca en el contador»). d6c ya existía (la puso el paquete login-y-limitador): no se
  duplicó.

### P13, P15 — Las tres puertas del vínculo decidían con un mapa sin candado

- `backend/portal/service.py` · `_rut_de_ficha_con_candado` (nuevo): relee el RUT de la
  ficha con `with_for_update(read=True)` (`LOCK IN SHARE MODE`, el mismo candado que
  toma la FK al escribir). `cliente_para_rut_confirmado` (nuevo): la ficha del mapa,
  confirmada contra ese RUT; si ya no calza, None. `sincronizar_cliente` (el Cierre)
  confirma SIEMPRE que haya ficha; `vincular_historico` confirma solo lo que va a anclar
  de nuevo (el ensayo no toma candados) y deja un `logger.info` con cuántas vinculó y
  desvinculó; `crear_cuenta` llama a `vincular_historico(db, aplicar=True)` en la misma
  transacción (segunda capa: una ficha con cuentas ya no puede cambiar de RUT).
- `backend/portal/sincronia.py` · `_sincronizar_cliente_de_cotizaciones`: usa
  `cliente_para_rut_confirmado`, SIN try (si la base falla al confirmar, el flush se
  aborta y no se escribe nada). Cabecera: distingue las dos fallas.
- `backend/routers/clientes.py` · `_rechazar_cambio_de_rut_con_cuentas` (cruce con el
  paquete cierre-de-venta-y-fechas, aplicado sobre lo que hay): antes de mirar
  `usuario_cliente`, `db.query(Cliente.id).filter(Cliente.id == ficha.id).with_for_update().first()`.
  Con la ficha tomada se espera a un alta en vuelo y recién entonces se mira si tiene
  cuentas. Son 3 líneas (comentario de 2 + la consulta).

### P16 — El histórico hidrataba 27 columnas para usar 5

- `backend/portal/service.py` · `vincular_historico`: consulta
  `Cotizacion.id, rut_cliente, cliente_id, numero, cliente`; `_motivo_huerfana` recibe el
  RUT y no la entidad.
- `backend/portal/tests/test_ordenes.py` · `permitidos`: `service.py` entra a la lista
  (es el archivo que MANTIENE el vínculo). Ese paso de la suite sigue verde.

### P21 — El alta aceptaba correos que el login no deja escribir

- `backend/portal/service.py` · `_CORREO` (nuevo): la regla WHATWG del
  `<input type="email">` más un punto obligatorio en el dominio, con `fullmatch`.
  `crear_cuenta` responde 400 «escribe solo la dirección, sin nombre ni signos».
- `frontend-src/src/portal/NuevaCuentaForm.tsx` · `enviar`: le pregunta al navegador si
  el campo (que ya es `type=email`) es una dirección (`validity.typeMismatch`); no se
  copia la expresión. Lo que el navegador acepte y el backend no vuelve como el 400, que
  el formulario ya muestra fijo.

## 2. Cómo verificar

Desde `backend/`, con MySQL local arriba y el uvicorn local detenido:

```bash
./venv/bin/python -m pytest portal/tests/test_cuentas.py -q        # 1 passed (~10 s)
./venv/bin/python -m pytest portal/tests/test_vinculacion.py -q    # 1 passed (~4 s)
./venv/bin/python -m pytest portal/tests/test_aislamiento.py -q    # 1 passed (~7 s)
./venv/bin/python -m pytest routers/tests/test_login_rate_limit.py -q   # 1 passed (el limitador)
./venv/bin/python -m portal.tests.mutaciones cuentas        # 25 de 25 mutaciones detectadas
./venv/bin/python -m portal.tests.mutaciones vinculacion    # 10 de 10
./venv/bin/python -m portal.tests.mutaciones aislamiento    # 13 de 13
```

Las mutaciones nuevas están en `backend/portal/tests/mutaciones/cuentas.py` (35: 19 de
`test_cuentas`, 10 de `test_vinculacion`, 6 de `test_aislamiento`). El arnés junta las
listas de todos los módulos de esa carpeta, así que en esas cifras entran además las 6
de `mutaciones/login.py` para `test_cuentas` y las 7 de `mutaciones/pedidos.py` para
`test_aislamiento`. Cada suite describe en
su cabecera qué sonda cubre cada regla (§K, §L, b7–b9, d2b2–d2b5, g2b, g3b/g3c, d6b2,
d7b en `test_cuentas`; §A ampliada, d10, §J, §K en `test_vinculacion`; §S, §R en
`test_aislamiento`). Cada sonda nació roja con el código anterior y quedó verde con el
arreglo.

Frontend: `cd frontend-src && npx tsc -b` sale limpio. No hay arnés de frontend; la
mitad de pantalla de C12/C34 y P21 se revisa a mano con una cuenta demo: envejecer
`clave_temporal_desde` en la base y mirar la fila (insignia «Clave temporal vencida»),
reactivar (toast con la instrucción) y pegar «Nombre <correo>» en el alta (aviso
inmediato).

Comprobaciones adicionales en el servidor, después del deploy:

- `pm2 logs machparts-api | grep portal-admin` después de restablecer una clave demo:
  tiene que salir la línea `… restableció la clave de la cuenta N`.
- `python -m portal.vincular_clientes` (ensayo) y mirar «RUT AMBIGUO» y «se
  desvincularían»: con C43, una ficha cuyo RUT tenga guion tipográfico pasa a ser válida
  y, si convive con otra del mismo RUT, ese RUT queda ambiguo (falla cerrado y visible).
- `python -m portal.desactivar_cuentas` (ensayo): el total tiene que coincidir con el de
  antes más las cuentas de tipo desconocido, si las hay.

Backend y frontend se despliegan juntos (el frontend nuevo contra un backend viejo vería
`undefined` en los campos del plazo: no rompe, pero no avisa).

## 3. Cómo revertir

No hay migraciones ni columnas nuevas: todo es código. Por ítem, lo mínimo que lo deshace:

| Ítem | Deshacer |
|---|---|
| C1, C13 | Quitar `_hacer_visible_la_bitacora()` y su llamada de `portal/__init__.py` (los mensajes de `crear`/`eliminar` pueden quedarse). Sin eso, `log_del_portal` y §K de `test_cuentas.py` se ponen rojas: quitarlas también |
| C2 | Quitar `route_class=RutaDelPortal` del `APIRouter` de `router_admin.py` (s3 de `test_aislamiento.py` se pone roja) |
| C3, C23 | Devolver los dos `flush` de `crear_cuenta` a un `db.flush()` pelado. OJO: el `try/except` del commit del router era inalcanzable; sin los flush traducidos las dos carreras vuelven a dar 500 (b7, b8) |
| C4 | `reservar_intento` sin `origen` y el llamado de `cambiar_clave` con dos argumentos (van juntos o no van: si solo se quita el parámetro, cada cambio de clave da `TypeError`). El `logger.warning` de `eliminar` se puede quitar solo |
| C12, C34 | Quitar `_plazo_de_la_clave` y sus `**` de `router_admin.py`; `vencimiento_clave_temporal` puede quedarse. En el frontend, volver `types.ts`, `api.ts`, `CuentasPortalPage.tsx` y `modalesAdmin.tsx` al estado anterior (son aditivos) |
| C25 | Volver a definir los tres helpers en `service.py`, `_con_dv` en `seed_piloto.py` y el import en `_datos.py`; borrar `rut.py`. Se pierde C43 con él |
| C31 | Quitar `clave_temporal_desde=ahora` de la rama `acceso is None` de `reactivar` (vuelve a fallar abierto: §L de la suite lo marca) |
| C32 | Volver `desactivar_cuentas.py` al predicado `tipo_acceso = 'portal'` (§R de `test_aislamiento.py` lo marca) |
| C43 | `rut_saneado` a `.replace(".", "").replace(" ", "").strip().upper()` |
| P13, P15 | En `service.py`, `sincronizar_cliente` y `vincular_historico` vuelven a `cliente_para_rut`; en `sincronia.py` el gancho también; quitar `vincular_historico(db, aplicar=True)` de `crear_cuenta`; quitar las 3 líneas del candado en `routers/clientes.py`. §J, §K y g2b lo marcan |
| P16 | Volver a `db.query(Cotizacion).all()` y `_motivo_huerfana(cot, …)`; quitar `service.py` de `permitidos` en `test_ordenes.py` |
| P21 | `crear_cuenta` a `'@' not in email`; en `NuevaCuentaForm.tsx`, quitar `refCorreo` y el `validity.typeMismatch` |

Para deshacer todo el paquete de una vez: restaurar los archivos listados en §5 desde
el commit anterior y borrar `backend/portal/rut.py`, `backend/portal/tests/mutaciones/cuentas.py`
y este documento.

## 4. Decisiones que quedan para el dueño

- Rastro de quién creó, restableció, desactivó o eliminó la cuenta de un cliente: con
  este arreglo queda en el registro del servidor (pm2 logs), que nadie respalda y que se
  pierde si se limpia. ¿Quiere además un registro permanente en la base, consultable
  desde una pantalla? Mientras tanto se aplica lo conservador: solo el registro del
  servidor, y la deuda queda declarada en el documento de la Fase 1.
- Una cuenta que ya descargó algún documento NO se puede eliminar (el sistema responde
  «tiene historial: desactívala») porque se conserva el registro de sus descargas.
  Consecuencias: la ficha de ese cliente ya no se puede borrar ni corregir su RUT
  mientras esa cuenta exista, aunque esté desactivada; y la limpieza del piloto
  (seed_piloto --limpiar) falla si una cuenta demo descargó algo. ¿Prefiere poder
  eliminar la cuenta borrando también su registro de descargas, o conservarlo siempre?
  Mientras tanto se aplica lo conservador: no se borra nada y el motivo queda escrito en
  el registro del servidor.
- Reactivar una cuenta NO renueva su clave temporal: si venció (pasaron 7 días sin que
  el contacto la cambiara), el operador tiene que apretar además «Restablecer clave» y
  entregar la nueva. La pantalla ahora lo avisa antes y después de reactivar. ¿Quiere
  que «Reactivar» genere una clave nueva en el mismo paso? Mientras tanto se aplica lo
  conservador: dos acciones separadas, para no revivir una clave que viajó por correo o
  WhatsApp.
- El correo de una cuenta del portal tiene que poder escribirse en la pantalla de
  ingreso: solo la dirección, sin tildes ni ñ en el dominio. Si algún cliente usa un
  dominio con ñ o tilde, hoy no podría tener cuenta con ese correo. Mientras tanto se
  aplica lo conservador: se rechaza y el operador usa otra dirección del contacto.
- Un RUT pegado desde Word o un PDF (con guion largo o espacios invisibles) ahora sí
  vincula la venta a su cliente en el portal, pero Facturación lo seguirá rechazando
  hasta que alguien lo corrija en la cotización. ¿Quiere pedirle al programador la misma
  tolerancia en Facturación? Mientras tanto no se toca Facturación.

## 5. Archivos tocados

Backend (código): `portal/__init__.py`, `portal/rut.py` (nuevo), `portal/service.py`,
`portal/sincronia.py`, `portal/router.py`, `portal/router_admin.py`,
`portal/seed_piloto.py`, `portal/desactivar_cuentas.py`, `rate_limit_login.py`,
`routers/clientes.py` (3 líneas).

Backend (pruebas): `portal/tests/_datos.py`, `portal/tests/test_cuentas.py`,
`portal/tests/test_vinculacion.py`, `portal/tests/test_aislamiento.py`,
`portal/tests/test_ordenes.py` (una entrada en `permitidos`),
`portal/tests/mutaciones/cuentas.py` (nuevo).

Frontend: `src/portal/types.ts`, `src/portal/api.ts`, `src/portal/CuentasPortalPage.tsx`,
`src/portal/modalesAdmin.tsx`, `src/portal/NuevaCuentaForm.tsx`.

Documentación: `backend/portal/README.md`, `docs/plan-portal-clientes-2026-08-20.md`
(§5.2, §5.3), `docs/fase-1-portal-identidad-2026-10-02.md` (§4, §6, §7, §10, §11),
`docs/fase-3-4-5-portal-pedidos-2026-10-04.md` (rollback y piloto), y este documento.

Lo que NO se tocó a propósito: `main.py`, `scheduler.py` (su frase «el backend no
configura logging» sigue siendo cierta para todo lo que no es el logger `portal`),
`portal/deps.py`, `portal/entrada.py`, `routers/auth.py`, `routers/compras.py`, la vista
del cliente del frontend y `portal.css`.
