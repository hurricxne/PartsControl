# Fase 1 del Portal de Clientes — identidad, aislamiento y datos

**Fecha:** 2026-10-02 · **Marca:** Grupo AM (MachParts) · **Plan:** `docs/plan-portal-clientes-2026-08-20.md` §4, §5, §11, §13

Este documento existe para el programador: qué se cambió, por qué, cómo verificarlo y cómo
revertirlo. Las fases anteriores tienen el suyo (`fase-0-portal-cambios-2026-08-20.md`,
`fase-2-portal-fecha-promesa-2026-08-30.md`). El detalle del módulo está en
`backend/portal/README.md`.

---

## 1. Qué entrega esta fase

Los contactos de un cliente ya pueden tener **cuenta** y entrar por el mismo login. Todavía
no ven pedidos (eso es la Fase 3): ven una página propia con su nombre, el cliente que
representan y el cambio de clave. Lo que esta fase construye es lo que no se ve:

1. **Quién es cada cuenta** (`users.tipo_acceso`): personal (`interno`) o cliente (`portal`).
2. **Que una cuenta de cliente no pueda tocar el ERP** — ni por la pantalla ni por la API.
3. **De qué cliente es cada venta** (`cotizaciones.cliente_id`), mantenido al día solo.
4. **La pantalla interna** Admin → Cuentas del portal: crear cuentas, claves, vigencia.
5. **El corte automático** de una cuenta sin uso por 60 días.
6. **Un piloto**: 2 clientes demo con sus cuentas, para probar sin un cliente real.

## 2. Qué se tocó

### Código nuevo (aislado)

| Archivo | Qué |
|---|---|
| `backend/portal/` | El módulo completo — ver su `README.md` |
| `frontend-src/src/portal/` | `PortalInicioPage` (lo que ve el cliente en `/portal`), `CuentasPortalPage` (pantalla interna en `/cuentas-portal`), `api.ts`, `types.ts` |

### Código existente que se modificó

| Archivo | Qué cambió | Por qué |
|---|---|---|
| `backend/auth.py` | El cuerpo de `get_current_user` pasó a `get_usuario_autenticado`; `get_current_user` (misma firma) ahora además **niega con 403 a toda cuenta que no sea `'interno'`** | Es la puerta de todo el ERP — ver §3 |
| `backend/routers/auth.py` | El login informa `tipo_acceso`, aplica el corte por inactividad a las cuentas portal y anota su último acceso · `/me` acepta cuentas portal · `/users` (selector de asesores) solo lista personal | Sin el filtro, el contacto de un cliente aparecía como **asesor seleccionable** y, por orden alfabético, podía quedar **preseleccionado** en el Cierre de Venta |
| `backend/routers/compras.py` | `_exigir_asesor_valido`, un solo criterio para crear y editar la OC · `crear_oc_cliente` vincula la venta a su ficha | El Cierre de Venta **no validaba el asesor** (solo la edición): aceptaba cualquier id |
| `backend/routers/clientes.py` | 409 al borrar una ficha con cuentas del portal o cambiarle el RUT · resincroniza el vínculo de las ventas al crear/editar una ficha | La ficha pasó a ser el ancla del aislamiento — ver §4 |
| `frontend-src/src/pages/ClientesPage.tsx` | `deleteMutation` muestra el motivo del 409 (y el siguiente paso) durante 8 s, como ya hacían crear y editar | Antes pintaba «Error al eliminar» a secas: el guard fallaba cerrado pero el operador no sabía por qué |
| `backend/models/models.py` | `User.tipo_acceso`, `Cotizacion.cliente_id` | Las dos únicas columnas aditivas en tablas del núcleo |
| `backend/main.py` | Monta `/api/portal` y `/api/portal-admin` | — |
| `deploy/audit_schema.py` | `portal.models` en `MODULOS` | Sin eso el auditor sale rojo (módulo no registrado) |
| Frontend: `authStore`, `LoginPage`, `App.tsx`, `authErrors.ts`, `DashboardLayout` | Una sesión portal va a `/portal` y **nunca** monta el cascarón interno; ítem de menú Admin → Cuentas del portal | Ver §5 |

## 3. Tres correcciones al plan, con evidencia

El plan se ejecutó verificando cada supuesto contra el código. Tres resultaron falsos, y
los tres habrían causado daño en producción:

**(a) `users.rol` habría dejado a todo el personal fuera de Contabilidad.**
El plan pedía `users.rol ENUM('interno','portal')`. Pero `role_guard.require_rol` ya lee
`user.rol` con **otro significado** (admin, comercial, contabilidad…) y es permisivo solo
mientras ese atributo no exista. Está puesto en el **router completo de Contabilidad**, en
"Editar OC" y en el PATCH de cotizaciones de Monza; además la campana de notificaciones
filtra por `rol`. Con la columna creada y el valor `'interno'` en todos, cada usuario
interno habría recibido 403 en Contabilidad y habría dejado de ver notificaciones. La
columna se llama **`tipo_acceso`**; `rol` queda libre para los roles funcionales que el
propio `role_guard.py` anuncia.

**(b) "Router propio = aislado por construcción" era falso.**
El plan decía que con un router `/api/portal` los ~100 endpoints sin verificación de
pertenencia quedaban "fuera del alcance por construcción". No: el login es el mismo, así
que el token de un cliente es un token válido para `get_current_user`, y 12 routers del
ERP (cotizaciones, cotizador, compras, clientes, ventas, facturas, bodega…) no piden nada
más que sesión. Un cliente con su token podía leer cotizaciones con margen llamando a la
API directamente. El corte va en **`auth.get_current_user`**, la única dependencia por la
que pasan 406 de los 408 endpoints (los otros dos son el login y un webhook con API key).

**(c) El vínculo venta ↔ cliente no puede hacerse "una vez".**
El plan hablaba de un backfill por RUT. Pero el RUT de una venta lo escriben al menos
cinco caminos y **se puede corregir después**: con un vínculo que solo se llena cuando
está vacío, corregir el RUT de una venta la dejaría colgando de la ficha anterior, y el
contacto de ese cliente la seguiría viendo. `cliente_id` es un dato **derivado** del RUT
actual y se recalcula siempre, también hacia NULL (`portal/sincronia.py`).

## 4. El vínculo venta ↔ cliente

`cotizaciones.cliente_id` = la ficha de `clientes` cuyo RUT (saneado) es el de la venta.
El portal filtrará **solo** por ese entero.

| Cuándo se recalcula | Quién |
|---|---|
| Se crea una cotización o se le cambia el RUT (por cualquier camino del ORM) | Gancho de sesión `portal/sincronia.py` |
| Se cierra la venta | `crear_oc_cliente` (cubre la ficha creada entre cotizar y cerrar) |
| Se crea, edita o **borra** una ficha en la pantalla Clientes | `routers/clientes.py` → `_resincronizar_ventas`, en la **misma transacción** que guarda la ficha (o se guardan las dos cosas o ninguna) |
| Se crea una **cuenta del portal** | `service.crear_cuenta` → `vincular_historico`, en la misma transacción: una ficha con cuentas ya no puede cambiar de RUT, así que el vínculo de todas las ventas se pone al día justo antes de que alguien pueda mirarlas |
| A pedido, para lo histórico | Botón «Vincular ventas» o `python -m portal.vincular_clientes --aplicar` |

En las tres puertas que escriben `cotizaciones.cliente_id` (el gancho, el Cierre y el
histórico), la ficha destino se **confirma releyéndola con candado compartido** antes de
anclar (`service.cliente_para_rut_confirmado`): el mapa de fichas se lee sin candado y,
entre esa lectura y la escritura, otra persona puede cambiarle el RUT a la ficha o
borrarla. Si ya no calza, la venta queda sin vincular (falla cerrado).

Queda **sin vincular** (NULL — para el portal la venta no existe) cuando: no hay RUT, el
RUT está mal formado, no hay ficha, o hay **dos fichas con el mismo RUT** escrito distinto
(`clientes.rut` es único como texto: `76.874.971-K` y `76874971-K` conviven). En ese último
caso no se elige una al azar: se reporta como RUT ambiguo para que una persona unifique —
y al borrar la ficha que sobra, las ventas pasan solas a la que queda.

Los helpers de RUT del portal (`portal/rut.py`) coinciden con los de Contabilidad en todo
RUT escrito normalmente y se apartan a propósito en tres bordes: los **ceros a la
izquierda** no cambian de quién es el RUT; solo valen **dígitos ASCII** (con `'²'`, que
`str.isdigit` acepta, el helper de Contabilidad lanza `ValueError`; acá una sola ficha así
habría tumbado el cálculo del mapa completo y bloqueado todos los Cierres de Venta); y se
**tolera la tipografía** de un RUT pegado desde Word, un PDF o una web (guion largo,
espacio no separable, caracteres invisibles). Consecuencia del tercero: el portal vincula
una venta con un RUT así, que Facturación seguirá rechazando hasta que se corrija en la
cotización.

Borrar una ficha **desvincula** sus ventas (FK `ON DELETE SET NULL`), no las bloquea: la
pantalla Clientes sigue borrando como siempre. Lo que sí se bloquea (409) es borrar una
ficha con **cuentas del portal** o cambiarle el RUT: sería cambiar de quién son esas cuentas.
La pantalla muestra el motivo y el siguiente paso (eliminar esas cuentas en Admin →
Cuentas del portal) durante 8 s (`deleteMutation`, `ClientesPage.tsx`).

## 5. Qué ve cada uno

| Sesión | Entra a | Si intenta lo otro |
|---|---|---|
| Personal | El ERP de siempre. Menú nuevo: Admin → Cuentas del portal | `/portal` lo devuelve a su inicio; la API del portal le responde 403 |
| Cliente | `/portal`: su nombre, su(s) cliente(s), cambio de clave | Cualquier ruta interna lo devuelve a `/portal`; la API del ERP le responde 403 |

Las sesiones del personal ya abiertas no traen `tipo_acceso` en el navegador: el frontend
trata lo indefinido como interno (si no, el deploy habría expulsado a todos). El backend
hace lo contrario —falla cerrado— porque ahí la columna siempre tiene valor.

**El ruteo del frontend no es seguridad.** El dato vive en el navegador y se puede editar;
la barrera es el 403 del backend.

**Una sesión por navegador.** Desde esta fase la sesión se sincroniza entre pestañas:
iniciar o cerrar sesión en una cambia todas (antes una pestaña quedaba con el token viejo
en memoria y la otra con el nuevo en `localStorage`: mitad y mitad). Para probar la cuenta
de un cliente sin perder la sesión del ERP, usar una ventana de incógnito u otro navegador.
Desde 2026-10-06 el cambio de sesión que llega desde otra pestaña también vacía el caché de
fotos de bodega (`FotoBodega.tsx` se suscribe al store de sesión), igual que el botón de
salir o un inicio de sesión en la misma pestaña.

## 6. Cuentas: reglas

- **Nunca se convierte una cuenta existente.** Si el correo ya está en `users` → 409. Pasar
  la cuenta de alguien del personal a `portal` lo dejaría fuera del ERP en el acto.
- **La clave temporal se muestra una sola vez** (al crear o restablecer). No se guarda en
  claro ni se puede volver a pedir.
- **La clave temporal vence a los 7 días.** Viaja por correo o WhatsApp: no puede quedar
  como credencial definitiva. Mientras la cuenta siga con ella, la página del cliente se
  lo dice; pasado el plazo el login responde 403 con el motivo y hay que restablecerla.
  Cuando el cliente pone la suya, deja de vencer. El listado del personal muestra el
  plazo (fecha de vencimiento, o la insignia «Clave temporal vencida» en una cuenta que
  sigue activa) y el cuadro que entrega la clave dice cuándo vence. **Reactivar NO
  renueva la temporal**: si venció, además hay que restablecerla (la pantalla lo avisa
  antes y después de reactivar).
- **El correo de una cuenta tiene que poder escribirse en el login**: solo la dirección,
  sin nombre ni signos, y sin tildes ni ñ en el dominio (la misma regla del campo
  `type=email` del formulario de ingreso, más un punto obligatorio en el dominio). Lo
  que no cumple responde 400 al crear.
- **Una cuenta sin registro de vigencia** (`portal_acceso`, un estado que solo produce el
  SQL a mano) se muestra vencida y no entra; al reactivarla se recrea el registro y su
  clave se trata como temporal (hay que cambiarla).
- **El cambio de clave tiene el mismo tope de intentos que el login** (5 por cuenta cada
  15 minutos, el mismo contador). Es el único bcrypt del sistema al alcance de una cuenta
  externa: sin tope era un oráculo de la clave y una palanca para saturar la CPU. En
  `pm2 logs` sus intentos se reconocen por `origen=cambio-clave` al final de la línea
  `[login] …`; los del login no llevan marca.
- **El perfil falla cerrado:** una cuenta ve montos solo si en todos sus clientes es
  exactamente `procurement`; cualquier otro valor cae a `bodega`.
- **`/portal-admin/cuentas/{id}/…` solo trabaja sobre cuentas portal**: con el id de alguien
  del personal responde 404 y no le cambia nada.
- **Corte por inactividad:** 60 días desde el último acceso o desde la creación/reactivación,
  lo más reciente. No hay un job: se evalúa al entrar y en cada request del portal. La
  cuenta no se borra ni se desactiva; queda "vencida" hasta que alguien la reactiva.
- **Desactivar** corta también la sesión que esté abierta. Restablecer la clave, no: un
  token ya emitido vive hasta 8 horas (comportamiento de siempre del login; ver §9).

## 7. Deploy

```bash
# ANTES de reiniciar el backend — sin esto NADIE puede iniciar sesión, en las dos marcas:
cd backend && python -m portal.init_db

# Después, con el dueño mirando la lista de huérfanas:
python -m portal.vincular_clientes            # ensayo
python -m portal.vincular_clientes --aplicar
```

`portal.init_db` está en `docs/CHECKLIST-DEPLOY-2026-07-20.md` §1.a como 🔴 CRÍTICA, y
`deploy/audit_schema.py` (auditoría, `--autoprueba` y `--pasos`) quedó en verde con el
módulo registrado. No hay variables nuevas en `.env`.

**nginx:** la ruta `/portal` es de la SPA, igual que `/monzaparts`: el servidor tiene que
devolver `index.html`. Y sigue vigente la verificación de la Fase 0: con usuarios externos,
que el docroot no sirva archivos del repo (`/backend/.env`, respaldos, `uploads/`) deja de
ser teórico.

**Revertir — tiene ORDEN, no es «solo código»:**

```bash
cd backend && python -m portal.desactivar_cuentas --aplicar   # 1º: desactiva toda cuenta que NO sea del personal
# 2º: recién ahora, git revert del commit / desplegar el paquete anterior
```

Las cuentas de los clientes viven en `users`, activas y con empresa `mineria`. Lo único que
las mantiene fuera del ERP es el corte de `auth.get_current_user`, que es código de esta
fase: con el código anterior y esas cuentas activas, **cada cuenta de cliente tendría
acceso completo al ERP**. `is_active = 0` sí lo respeta el código viejo. Por eso el script
desactiva todo lo que ese corte mantiene fuera (lo que no es `'interno'`, incluidas las
cuentas con un `tipo_acceso` desconocido, que no aparecen en Cuentas del portal y no se
reactivan desde la pantalla), y no «las que dicen portal». Las columnas pueden quedarse
(el código anterior no las lee). Para sacar a UNA cuenta sin revertir nada: Admin →
Cuentas del portal → Desactivar.

## 8. El piloto (2 clientes demo)

```bash
cd backend && python -m portal.seed_piloto            # crea lo que falte
python -m portal.seed_piloto --limpiar                # lo borra
```

Crea las fichas **MINERA PILOTO NORTE (DEMO)** y **MINERA PILOTO SUR (DEMO)** y una cuenta
portal para cada una, con los correos del dueño. Las cuentas nacen **sin clave utilizable**:
se entra a Admin → Cuentas del portal → «Restablecer clave», que es el mismo camino que se
usará con un cliente real.

- Si uno de esos correos **ya es una cuenta interna** (probable en producción con el correo
  principal del dueño), el script **no la toca**, avisa y sugiere un alias.
- Si el RUT demo ya aparece en una ficha o cotización real, aborta (el vínculo por RUT le
  mostraría ventas reales a la cuenta de prueba).
- No crea ventas: para ver un pedido se cotiza y se cierra una venta normal con el RUT de
  la ficha demo, y queda vinculada sola.
- **No es un paso de producción.** Y con las fichas demo no se emite jamás un DTE.

## 9. Pruebas

`backend/portal/tests/` — app completa, cuentas reales, sesiones reales:

| Suite | Qué protege |
|---|---|
| `test_aislamiento.py` | **Candado estructural:** cada una de las ~420 rutas tiene su puerta (ERP o portal) salvo una lista corta de públicas; `get_usuario_autenticado` no se usa fuera de donde debe. Una sesión portal contra las ~194 rutas GET del ERP → 403 en todas. Cada cuenta ve solo su ficha. Fallar cerrado: sin cliente, vencida, desactivada, tipo desconocido |
| `test_cuentas.py` | Alta y clave temporal, no convertir cuentas existentes, la regla dura del personal, ciclo de vida, guards de la ficha, asesor |
| `test_vinculacion.py` | Paridad de los helpers de RUT con Contabilidad, vigencia, mapa de fichas (ambiguos, mal formados), **la fuga por vínculo obsoleto**, Cierre, pantalla Clientes, histórico, borrado |

**Verificación por mutación: 36 de 36** (quitar la protección → la suite se pone roja →
restaurar byte a byte → verde). Tres sondas resultaron no discriminar y se reemplazaron:
un RUT mal formado tampoco se vinculaba sin la validación, porque su ficha no estaba en
el mapa (ahora: el guion corrido, un RUT inválido cuyo canónico calza con una ficha real);
«el ensayo no escribe» pasaba igual porque la propia prueba hacía rollback (ahora hace
commit); y «ningún rechazo dejó una cuenta a medias» no protegía nada y se eliminó.

**Prueba manual de sesión y login (2026-10-06):** pestaña A dentro del ERP, pestaña B en
`/login` con una clave mala → B muestra «Email o contraseña incorrectos» y A sigue adentro.
El 401 del propio login no cierra la sesión vigente (`services/authErrors.ts`, vigilado
por `routers/tests/test_auth_errors_espejo_frontend.py`); cualquier otro 401 sí la cierra.

## 10. Deuda declarada (no incluida a propósito)

| Qué | Por qué queda fuera | Riesgo |
|---|---|---|
| **Quién puede operar Cuentas del portal** | Cualquier cuenta interna de `mineria`. No existen roles funcionales todavía (`require_rol` es permisivo) | Medio: un vendedor puede crear un acceso externo. Se cierra el día que exista `users.rol` |
| **`/api/docs`, `/api/redoc`, `/openapi.json` son públicos** | Preexistente. Publican el mapa completo de la API, ahora con el portal incluido | Bajo-medio. Sugerencia: apagarlos en producción con un flag |
| **Un token emitido vive 8 horas** aunque se restablezca o se cambie la clave | Comportamiento de siempre del login (no hay invalidación de tokens). Desactivar la cuenta sí corta en el acto | Bajo |
| **Dos fichas con el mismo RUT** | `routers/clientes.py` compara el RUT como texto, así que la pantalla permite crearlas. Acá se detecta y se reporta, no se impide | Bajo: falla cerrado (la venta no se vincula) |
| **El RUT de una venta sigue editable después del cierre** en el backend | Preexistente (el candado está solo en el frontend). El vínculo se recalcula, así que no hay fuga | Bajo |
| **Escrituras masivas** (`query.update`, SQL crudo) sobre `rut_cliente` no pasan por el gancho | Hoy no existe ninguna | Bajo: «Vincular ventas» lo repara |
| **Facturar con un RUT distinto al de la venta** | `_construir_factura` solo advierte y la factura no guarda el RUT del receptor. Latente hoy; **bloqueante para la Fase 3**: antes de servir documentos hay que decidir de quién es esa factura | Medio, cuando existan documentos en el portal |
| **Reactivar una cuenta desactivada revive sus tokens** emitidos antes del corte (hasta 8 h) | Misma raíz que la fila del token: no hay invalidación | Bajo |
| **Las rutas de datos de la Fase 3 deben rechazar a una cuenta con clave temporal** | Hoy la página solo avisa (no hay datos que proteger todavía) | A resolver al agregar esas rutas |
| **Carrera entre el vínculo y una edición simultánea** | El UPDATE condicionado del histórico cubre la edición simultánea del RUT de la **venta**. La edición o el borrado simultáneo de la **ficha** se cubre releyéndola con candado compartido antes de anclar (las tres puertas: gancho, Cierre e histórico) y resincronizando al crear la cuenta; tiene pruebas (`test_vinculacion.py` §J, §K). El guard de cambio de RUT de `routers/clientes.py` toma la ficha con `FOR UPDATE` antes de mirar sus cuentas, para esperar a un alta en vuelo (`test_cuentas.py` g2b). Queda declarada la desvinculación de más con un mapa viejo: falla cerrado y se cura en la próxima resincronización | Bajo |
| **Rastro de quién creó, restableció, desactivó o eliminó una cuenta** | Vive en el log del proceso (`pm2 logs machparts-api`, líneas `portal-admin: …`), no en una tabla; `portal_acceso` guarda solo `creado_por_id`. Se cierra con una bitácora en tabla si el dueño la pide | Bajo |
| **`login` y `/auth/me` rotulan como `'interno'` un `tipo_acceso` vacío** mientras `get_current_user` lo niega | Inconsistencia conocida, sin efecto de seguridad (la puerta lo niega igual) | — |
| **Espejo Monza** | El portal es de Grupo AM | — |
| **El cliente del portal descarga el ERP entero antes de ver su pantalla** | 2,45 MB (≈597 KB comprimido). Hay un diseño listo y medido que lo deja en 384 KB (≈120 KB): requiere visto bueno porque toca `main.tsx` y `App.tsx` (`docs/paquete-frontend-general-2026-10-06.md`, C15) | Bajo: solo demora la primera carga con mala señal |

## 11. Revisión adversarial

Dos pasadas independientes sobre lo construido (backend: 6 lentes con un verificador que
intenta refutar cada hallazgo; frontend: 2 lentes), después de las pruebas y antes del
commit. **Ningún hallazgo crítico.** Lo que encontraron y se corrigió:

| Gravedad | Hallazgo | Arreglo |
|---|---|---|
| **ALTO** | El rollback documentado («solo código») dejaba a las cuentas de clientes con acceso completo al ERP | Procedimiento con orden + `portal/desactivar_cuentas.py` (§7) |
| MEDIO | El cambio de clave no tenía tope de intentos (oráculo de la clave + palanca de CPU) | Mismo limitador y mismo cupo que el login, antes del bcrypt |
| MEDIO | La clave temporal no vencía ni se avisaba | Vence a los 7 días; lo dice la página del cliente y también la del personal (fecha de vencimiento, «Clave temporal vencida», aviso al reactivar) |
| MEDIO | Ficha y resincronización en dos commits: si el segundo fallaba, un cambio de RUT quedaba con las ventas viejas colgando | Una sola transacción |
| MEDIO | Borrar la ficha duplicada de un RUT ambiguo no devolvía las ventas a la que queda | Resincroniza también al borrar |
| MEDIO | Las ventas por DESVINCULAR no se veían en consola ni en pantalla | Se muestran y se pueden aplicar |
| MEDIO | `seed_piloto --limpiar`: su guarda era código muerto (dejaba ventas demo sueltas) y después no se podía volver a sembrar | La ficha con ventas se conserva |
| MEDIO | El 409 de la ficha mandaba a «eliminar la cuenta» y la pantalla no tenía ese botón | Botón Eliminar con confirmación |
| BAJO | Perfil desconocido se leía como `procurement` (ve montos) | Falla cerrado a `bodega` |
| BAJO | `rut_valido` reventaba con un dígito Unicode; los ceros a la izquierda rompían el vínculo | Solo ASCII; canónico sin ceros |
| BAJO | Dos altas simultáneas del mismo correo → 500 | 409, traducido en el flush donde la base rechaza (con prueba automática: `test_cuentas.py` b7). La ficha eliminada a mitad del alta también responde 409 (b8) |
| BAJO | Sondas que no discriminaban, contadores escritos a mano, marca 🟡 mal usada en el checklist | Corregidos |
| Frontend | Foco de los modales, estados de carga/error de las fichas, contraste en tema claro, `scope` en tablas, sesión mezclada entre pestañas | Corregidos |

Refutados por el verificador: 2 (uno de ellos, porque ya estaba resuelto cuando se revisó).
