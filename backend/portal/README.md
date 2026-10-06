# Portal de Clientes (Grupo AM / MachParts)

Módulo aislado y aditivo. Los contactos de un cliente entran por **el mismo login** que el
personal, con una cuenta `tipo_acceso='portal'`, y ven solo los pedidos de su(s) cliente(s).
Para el cliente es **solo lectura**. Plan completo: `docs/plan-portal-clientes-2026-08-20.md`.
Qué se hizo en cada fase y por qué: `docs/fase-0-…`, `docs/fase-2-…`,
`docs/fase-1-portal-identidad-2026-10-02.md` (identidad) y
`docs/fase-3-4-5-portal-pedidos-2026-10-04.md` (pedidos, documentos y fotos).

## Archivos

| Archivo | Qué es |
|---|---|
| `__init__.py` | Deja visible la bitácora del módulo: configura el logger `portal` en INFO al importarse. Sin eso, los `logger.info` (quién creó, restableció, reactivó, desactivó o eliminó una cuenta) no llegan a `pm2 logs` |
| `models.py` | Tablas propias: `usuario_cliente` (puente usuario ↔ cliente ↔ perfil), `portal_acceso` (vigencia y último uso), `foto_articulo` (fotos para el cliente) y `portal_descarga_log` (bitácora de descargas) |
| `deps.py` | `require_portal()` → `PortalScope` (**único** lugar que resuelve a qué clientes ve una cuenta; falla cerrado); `require_portal_listo()` (además, ya reemplazó la clave temporal); `RutaDelPortal` + `esquema_incompleto` (503 con el comando de `init_db` **solo** cuando MySQL responde 1146/1054; cualquier otro error de la base se relanza y queda en el log). La clase de ruta la usan los cuatro routers del portal |
| `entrada.py` | La entrada por el login (`entrada_por_login`): corte por inactividad, corte por clave temporal vencida, 503 con el comando de `init_db` si falta una tabla, anota el último acceso. `require_portal` no corta por clave temporal vencida, a propósito |
| `service.py` | Vigencia de la cuenta (corte a los 60 días sin uso), clave temporal (vence a los 7 días: `vencimiento_clave_temporal`), alta de cuentas (que además pone al día el vínculo de todas las ventas) y vínculo venta ↔ cliente (la ficha se confirma con candado antes de anclar: `cliente_para_rut_confirmado`) |
| `rut.py` | Helpers de RUT: sanear (tolera la tipografía de un RUT pegado desde Word o un PDF), canonizar, validar y el generador para datos de prueba (`rut_con_dv`). Sin imports del backend |
| `sincronia.py` | Gancho de sesión que mantiene `cotizaciones.cliente_id` al día cada vez que cambia el RUT de una venta |
| `router.py` | `/api/portal/yo` y el cambio de clave — la identidad del **cliente** |
| `router_ordenes.py` | `/api/portal/*` de datos — pedidos, seguimiento, documentos, fotos (cliente) |
| `router_admin.py` | `/api/portal-admin/*` — lo que opera el **personal** (pantalla Admin → Cuentas del portal) |
| `router_admin_fotos.py` | `/api/portal-admin/*` de fotos — lo que el personal publica desde Despachos |
| `candado.py` | **El** filtro «de quién es una orden». Único archivo que lo escribe |
| `visibles.py` | Qué despacho, factura y fotos *existen* para el cliente (condiciones SQL, una sola definición) |
| `ordenes.py` | Las órdenes: consulta base (una por venta), cabecera, listado, detalle |
| `renglones.py` | Los ítems: junta las filas de un split, desglosa por unidades, fechas |
| `valor.py` | El valor de un ítem por tramos (facturado, en guía emitida, calculado) |
| `precios.py` | El precio calculado de venta (nunca costo) y cuándo hay precio congelado |
| `estados.py` | Estados del cliente y línea de tiempo. Funciones puras |
| `tiempo.py` | El día de Chile, el día tecleado y las fechas en texto libre |
| `busqueda.py` | El buscador |
| `documentos.py` | Guías y facturas como documentos del pedido |
| `dtes.py` | Lo que el portal lee de un DTE (emitido, fecha, a quién se emitió, precios de la guía) |
| `descargas.py` | La entrega de archivos: el PDF lo baja el servidor, bitácora, esperas acotadas |
| `seguimiento.py` | El seguimiento de un ítem: hitos con fecha, documentos, fotos |
| `resumen.py` | El tablero |
| `fotos.py` | Cómo se guarda una foto: solo un derivado sin metadatos |
| `cupo.py` | Tope de frecuencia por cuenta (429) |
| `init_db.py` | Crea las tablas propias + las 2 columnas aditivas del núcleo |
| `vincular_clientes.py` | Backfill histórico de `cotizaciones.cliente_id` + reporte de huérfanas |
| `seed_piloto.py` | 2 clientes DEMO con sus cuentas, para probar sin un cliente real. **No es un paso de producción** |
| `seed_demo_pedidos.py` | Pedidos demo con fotos de prueba para esas cuentas. **Tampoco** es un paso de producción |
| `desactivar_cuentas.py` | **Rollback:** desactiva toda cuenta que no sea del personal (las portal y las de tipo desconocido; estas últimas no se reactivan desde la pantalla). Se corre ANTES de revertir el código |
| `tests/` | Las suites (`test_*.py`), las fábricas compartidas (`_datos.py`) y `mutaciones/` (lo que se rompió a propósito para verificar cada suite) |

## Crear el esquema

```bash
cd backend && pip install -r requirements.txt  # pillow: sin él el backend no levanta
python -m portal.init_db                      # ANTES de reiniciar el backend
python -m portal.vincular_clientes            # ensayo: qué ventas se vincularían
python -m portal.vincular_clientes --aplicar  # escribe
```

Carpetas que escribe el módulo bajo `backend/uploads/`: `portal_fotos/` y `portal_docs/`.
No deben quedar servidas por nginx. `.env`: `PORTAL_CUPO_ENABLED` (opcional) y el
`WASABIL_API_TOKEN` de siempre para los PDF.

`init_db` es **la migración más crítica del checklist**: los modelos ya declaran
`users.tipo_acceso`, y sin la columna el login responde 1054 — nadie entra, en ninguna
de las dos marcas.

## Endpoints

**Cliente** (`/api/portal`, solo cuentas portal):

| Ruta | Devuelve |
|---|---|
| `GET /portal/yo` | Quién soy, mi perfil y mi(s) cliente(s) |
| `POST /portal/yo/clave` | Cambiar mi clave (devuelve un token nuevo: el anterior deja de valer) |
| `GET /portal/resumen` | El tablero |
| `GET /portal/ordenes` · `/ordenes/{id}` · `/ordenes/{id}/items/{item_id}` | Mis pedidos, una orden, el seguimiento de un ítem |
| `GET /portal/fotos/{id}` · `/guias/{id}/pdf` · `/guias/{id}/firmada` · `/facturas/{id}/pdf` | Archivos, como bytes con la sesión |

Todo lo que no es `/yo` exige haber reemplazado la clave temporal (`require_portal_listo`)
y cobra el tope de frecuencia de la cuenta (`cupo.py`): `test_aislamiento.py` lo comprueba
ruta por ruta.

**Personal** (`/api/portal-admin`, solo cuentas internas de `mineria`):

| Ruta | Qué hace |
|---|---|
| `GET /clientes` | Fichas con cuántas ventas vinculadas y cuentas tienen |
| `GET /cuentas` | Cuentas portal con estado (`activa` / `vencida` / `desactivada`), último acceso y el plazo de la clave temporal (`clave_temporal_vence_at`, `clave_temporal_vencida`: una cuenta activa con la temporal vencida NO entra) |
| `POST /cuentas` | Crea la cuenta y devuelve la **clave temporal una sola vez** |
| `POST /cuentas/{id}/restablecer-clave` · `/reactivar` · `/desactivar` | Ciclo de vida |
| `DELETE /cuentas/{id}` | Borra la cuenta (destraba borrar la ficha o corregir su RUT) |
| `GET /vinculacion` · `POST /vinculacion/aplicar` | Ensayo y aplicación del vínculo venta ↔ cliente |
| `GET /items/{item_id}/fotos` · `POST …/fotos` · `GET /fotos/{id}` · `DELETE /fotos/{id}` | Fotos publicadas para el cliente (subir es publicar) |

## Cómo funciona el aislamiento (leer antes de tocar nada)

Dos mitades, las dos obligatorias:

1. **`auth.get_current_user` niega a toda cuenta que no sea `'interno'`.** Es la puerta
   por la que pasan todos los endpoints del ERP (también `require_empresa` y
   `require_rol`). Un router propio para el portal aísla lo que el portal *sirve*; este
   corte aísla lo que el token *alcanza*.
2. **El portal entra por `auth.get_usuario_autenticado` + `require_portal`**, y cada
   consulta filtra además por `scope.cliente_ids` (doble candado).

`get_usuario_autenticado` deja pasar a cualquier tipo de cuenta: solo pueden usarla
`portal/deps.py` y `GET /auth/me`. `tests/test_aislamiento.py` falla si aparece en otro
archivo, si una ruta nueva queda sin puerta o si una ruta `/api/portal/*` no pasa por
`require_portal`.

## Reglas para lo que se agregue

- Toda consulta de órdenes nace de `ordenes.base` (pasa por `candado.de_sus_ordenes`).
  Filtrar **solo** por `cliente_id` entero. Nunca por `cotizaciones.cliente` (texto) ni por
  `rut_cliente`. `test_ordenes.py` falla si otro archivo nombra `Cotizacion.cliente_id`.
- Recurso de otro cliente → **404**, no 403 (un 403 confirma que el id existe).
- Respuestas armadas a mano, campo por campo (`Renglon.publico`, `ordenes.cabecera`,
  `documentos.guias`…). `test_ordenes.py` §B compara cada respuesta contra una lista
  blanca de campos: un campo nuevo se agrega ahí y en `frontend-src/src/portal/types.ts`.
- Lo que **nunca** sale al cliente: margen, costos, proveedor, país, forwarder, AWB,
  transportista, N° interno de cotización, RUT, enlaces públicos de Wasabil, nombres de
  archivo. Un monto sale solo por `valor.py` / `precios.py`.
- Qué documento o foto *existe* para el cliente se escribe una vez, en `visibles.py`.
- Las rutas de DATOS llevan `require_portal_listo` y el cupo en el **router** (no en cada
  endpoint): una ruta nueva en otro router los declara igual, o `test_aislamiento.py` la
  marca.
- Las descargas siguen el orden de `descargas.py`: resolver contra las órdenes del cliente
  → copiar a variables → anotar (cierra la sesión) → entregar. Nada después de anotar lee
  un objeto de la base.
- Fechas: un instante del servidor pasa por `tiempo.dia_chile`; un día tecleado, por
  `tiempo.dia_tecleado`; un texto, por `tiempo.fecha_de_texto`. Nunca `date.today()`.
- Máximo 400 líneas por archivo; un test por cada 403/404; toda regla nueva con su
  mutación en `tests/mutaciones/`.

## Tests

```bash
cd backend && ./venv/bin/python -m pytest portal/tests -q
```

Corren contra la base local, con la app completa, cuentas reales y sesiones reales
(un stub de `get_current_user` no ejerce el corte); los archivos van a carpetas
temporales y a Wasabil no se le llama. Cada suite dice en su encabezado qué protege; las
mutaciones con que se verificó (romper la regla → la suite se pone roja) están en
`tests/mutaciones/` y se repiten con `python -m portal.tests.mutaciones`.

## Cómo deshacer

- **Todo el módulo — con ORDEN:** primero `python -m portal.desactivar_cuentas --aplicar`
  (desactiva toda cuenta que no sea del personal: las portal y las de tipo desconocido,
  que no se reactivan desde la pantalla), después `git revert` del commit de la Fase 1.
  Al revés es peligroso: con el código anterior, una cuenta activa que no sea del
  personal es un usuario interno con acceso a todo el ERP (el corte vive en
  `auth.get_current_user`, que es código de esta fase). Las columnas
  `users.tipo_acceso` y `cotizaciones.cliente_id` pueden quedarse.
- **Una cuenta:** Admin → Cuentas del portal → Desactivar (corta también la sesión abierta).
  Una cuenta (demo o real) que ya descargó documentos **no se elimina** (409: su
  bitácora en `portal_descarga_log` la conserva): se desactiva.
- **El piloto:** `python -m portal.seed_piloto --limpiar`. Si una cuenta demo descargó
  algún documento, el script termina con el error de la FK de `portal_descarga_log` y
  sin borrar nada, hasta que se decida qué hacer con esa bitácora.
