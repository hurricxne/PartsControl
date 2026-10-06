# Portal de Clientes — frontend

Dos mundos distintos, en la misma carpeta porque comparten API y tipos:

| Mundo | Ruta | Quién lo ve |
|---|---|---|
| Portal del cliente | `/portal/*` | El **cliente** (cuenta `tipo_acceso='portal'`). No usa `DashboardLayout` ni nada del ERP |
| Cuentas del portal | `/cuentas-portal` | El **personal** de Grupo AM: menú Admin → Cuentas del portal |

Backend y reglas del módulo: `backend/portal/README.md`.
Cómo nació: `docs/fase-1-portal-identidad-2026-10-02.md` (identidad) y
`docs/fase-3-4-5-portal-pedidos-2026-10-04.md` (pedidos, documentos y fotos).

## Portal del cliente: qué archivo es qué

| Archivo | Qué es |
|---|---|
| `PortalLayout.tsx` | El cascarón: pide `GET /portal/yo` una vez, lo reparte a las pantallas, exige cambiar la clave temporal, cierra sesión. Importa `portal.css` |
| `CabeceraPortal.tsx` | Marca, secciones, buscador (navega a `/portal/pedidos?q=…`), tema y Cerrar sesión |
| `TableroPage.tsx` | `/portal` — inicio |
| `PedidosPage.tsx` | `/portal/pedidos?q=&estado=&anio=&page=` — los filtros viven en la URL |
| `OrdenPage.tsx` | `/portal/pedidos/:ordenId` |
| `ItemPage.tsx` | `/portal/pedidos/:ordenId/items/:itemId` |
| `CuentaPage.tsx` | `/portal/cuenta` — empresas que representa y cambio de clave (`CambiarClaveForm.tsx`, que guarda el token nuevo que devuelve el backend) |
| `FichaDeOrden.tsx`, `ItemsDeOrden.tsx` | La cabecera de una orden y su lista de ítems (tabla o tarjetas) |
| `PedidosFiltros.tsx`, `PedidosResultados.tsx`, `filtrosDePedidos.ts` | Los filtros de Mis pedidos (viven en la URL) y sus resultados |
| `Documentos.tsx`, `GaleriaDeFotos.tsx` | Guías/facturas de un pedido y fotos de un ítem |
| `FotosParaCliente.tsx`, `TarjetaDeFotoPublicada.tsx` | La pestaña «Foto despacho» del personal (sube y borra fotos publicadas) |
| `sesion.ts` | `usePortal()` (datos de la sesión), `claveDeConsulta()`, reintentos, título de la pestaña |
| `formato.ts` | Montos, cantidades, fechas y plazos. Puro, sin React |
| `estadosUi.ts` | El tono (forma + color) de cada estado |
| `piezas.tsx` | Componentes de presentación compartidos (insignia de estado, fecha comprometida, monto, N° de parte, miga de pan, avisos…) |
| `archivos.ts` | Fotos y PDF: se piden como Blob con el token; caché en memoria; los mensajes de 403/404/429/502 vienen del backend |
| `portal.css` | Todos los estilos del cliente (tokens `--portal-*`, clases `portal-*`), claro y oscuro |
| `api.ts`, `types.ts` | Llamadas y tipos, espejo del JSON de `backend/portal/`. `api.ts` reexporta `detalleDe` y `estadoHttp`, que viven en `services/httpErrors.ts` |

Pantalla interna: `CuentasPortalPage.tsx` (+ `NuevaCuentaForm.tsx`, `VinculacionPanel.tsx`,
`modalesAdmin.tsx`, `perfiles.ts`, `tonos.ts`). No usa `portal.css`.

## Cableado (lo que hay fuera de esta carpeta)

- `stores/authStore.ts` — `tipo_acceso` en `User`, el helper `esCuentaPortal` y la
  sincronía de la sesión entre pestañas.
- `pages/LoginPage.tsx` — la cuenta portal va a `/portal` antes del chequeo de marca; el
  403 del login (cuenta suspendida) se muestra fijo, no en un toast.
- `App.tsx` — desvío en `PrivateRoute`, guard `PortalRoute`, comodín `RutaDesconocida`,
  el bloque de rutas `/portal` y la ruta `cuentas-portal`.
- `services/authErrors.ts` — el 401 cierra la sesión (las pantallas no lo tratan), menos
  el del login (lo muestra `LoginPage`) y el de una petición que viajó con un token ya
  renovado; el 403 de cuenta portal clasifica como `ambito`. Sus textos son gemelos de los
  del backend: los vigila `backend/routers/tests/test_auth_errors_espejo_frontend.py`.
- `services/httpErrors.ts` — `detalleDe` y `estadoHttp`; los usan el portal (vía `api.ts`)
  y los `catch` de fotos de `BodegaPage`.
- `pages/DashboardLayout.tsx` — ítem de menú y título de la pantalla interna.
- `components/FotosItemModal.tsx` — la pestaña «Foto despacho» de Despachos monta
  `FotosParaCliente` (las fotos que el cliente va a ver).

## Reglas que no se ven en el código y que hay que conservar

1. **`undefined` es interno.** Las sesiones del personal guardadas antes del portal no
   traen `tipo_acceso`. La única comparación válida es `=== 'portal'` (por eso existe
   `esCuentaPortal`). Comparar con `!== 'interno'` expulsa a todo el personal.
2. **El ruteo no es seguridad.** `user` vive en `localStorage` y se puede editar. La
   barrera es el backend, que responde 403 a un token portal en todo el ERP y filtra cada
   respuesta del portal por los clientes de la cuenta.
3. **La ruta interna no empieza con `/portal`.** `DashboardLayout` resuelve títulos por
   prefijo y `/portal` es del cliente.
4. **Las fechas son texto.** Llegan como `'AAAA-MM-DD'` (día de Chile) y se formatean con
   `formato.ts`. `new Date('2026-08-12')` las pinta un día antes.
5. **«Hoy», «vencida» y los estados los calcula el backend.** Cada respuesta trae `hoy`;
   la pantalla no compara fechas con el reloj del dispositivo.
6. **`null` no es `$0`.** Un monto `null` se pinta «—» (la cuenta no ve montos, o la orden
   no tiene precio congelado). Un `unitario_clp` null con total es «Varios precios»
   (`piezas.tsx::PrecioUnitario`).
6b. **Tres cantidades por ítem** (`cantidad`, `cantidad_entregada`,
   `cantidad_por_entregar`): lo que falta para cuadrarlas es «no recibido en la entrega».
   La fecha comprometida solo lleva plazo o atraso si todavía se espera algo
   (`FechaComprometida`, prop `seEspera`); una orden puede estar abierta sin que se espere
   nada (`orden.items.por_entregar === 0`).
6c. **Al cambiar la clave, el token anterior muere.** `CambiarClaveForm` guarda el que
   devuelve `POST /portal/yo/clave` antes de avisar a la pantalla; si no, la siguiente
   consulta responde 401 y el portal expulsa a la persona.
7. **Toda consulta de react-query empieza con `['portal', <id del usuario>, …]`**
   (`sesion.ts::claveDeConsulta`) y cerrar sesión las borra: los datos de una cuenta no
   se le pintan a otra.
8. **Los archivos no son enlaces.** Fotos y PDF se piden con `portalAPI` (llevan el
   token) y se muestran desde memoria (`archivos.ts`). Nunca `<img src="/api/…">`.
9. **Ningún texto usa el naranjo de marca** (`--portal-marca`, `#ea6c0d`): no alcanza el
   contraste. El color de texto de acento es `--portal-acento`.

## El tema

La primera vez que una cuenta portal entra desde un navegador, el portal pasa a tema
CLARO (`PortalLayout::useTemaClaroLaPrimeraVez`, marca `machparts-portal-tema-elegido`);
desde ahí manda el botón de la cabecera. El tema vive en la misma llave que usa el ERP
(`machparts-theme`): probar una cuenta de cliente en el navegador de trabajo deja también
el ERP en claro esa primera vez — otro motivo para probar en incógnito.

## Una sesión por navegador

Iniciar sesión en una pestaña cambia la sesión de **todas** las pestañas de ese
navegador. Para probar la cuenta de un cliente sin perder la sesión del ERP, usar una
ventana de incógnito u otro navegador.

## Cómo deshacer

Quitar de `App.tsx` los imports del portal, los dos guards y las rutas; el ítem del menú
y su título en `DashboardLayout.tsx`; el bloque portal de `LoginPage.tsx`; la pestaña
«Foto despacho» de `components/FotosItemModal.tsx` (importa `FotosParaCliente`); y esta
carpeta. `authStore` y `authErrors` pueden quedar como están: sin cuentas portal no hacen
nada. `services/httpErrors.ts` se queda (`BodegaPage` lo importa).
