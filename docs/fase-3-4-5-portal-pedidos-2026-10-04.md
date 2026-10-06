# Fases 3, 4 y 5 del Portal de Clientes — pedidos, documentos y fotos

**Fecha:** 2026-10-05 · **Marca:** Grupo AM (MachParts) · **Plan:** `docs/plan-portal-clientes-2026-08-20.md` §6 a §12

Este documento existe para el programador: qué se construyó, por qué está hecho así, cómo
verificarlo y cómo revertirlo. Las fases anteriores tienen el suyo
(`fase-0-portal-cambios-2026-08-20.md`, `fase-2-portal-fecha-promesa-2026-08-30.md`,
`fase-1-portal-identidad-2026-10-02.md`). El detalle del módulo está en
`backend/portal/README.md` y el de las pantallas en `frontend-src/src/portal/README.md`.

---

## 1. Qué entrega

Hasta la Fase 1 un cliente podía entrar y veía su nombre. Ahora ve lo que vino a ver:

1. **Sus pedidos**: cada orden de compra con sus ítems, y de cada ítem el precio de venta,
   la **fecha comprometida** y el **estado** (dónde va), por unidades cuando el ítem va
   repartido («6 despachadas · 4 en nuestra bodega»).
2. **Un tablero**: cuánto compró, cuánto falta por entregar y dónde viene lo que falta.
3. **Un buscador** por N° de orden, N° de parte, descripción, marca o folio, tolerante al
   guion (`1R-0716` = `1R0716` = `1r 0716`).
4. **Sus documentos**: guías de despacho (con el PDF electrónico, la copia firmada y cómo
   las recibió) y facturas (como documento: folio, fecha y montos; sin saldos ni pagos).
5. **Las fotos de sus repuestos**, que el personal sube desde Despachos.

Todo es de **solo lectura** para el cliente. El personal gana una sola cosa nueva: en
Despachos, la pestaña «Foto despacho» de cada ítem ahora sube fotos.

## 2. Qué se tocó

### Código nuevo (aislado, dentro de `backend/portal/`)

| Archivo | Qué |
|---|---|
| `candado.py` | **El** filtro "de quién es una orden". Único archivo que lo escribe |
| `visibles.py` | Qué despacho, qué factura y qué fotos *existen* para el cliente (una sola definición, en SQL) |
| `ordenes.py` | Las órdenes: la consulta base (una por venta), la cabecera, el listado y el detalle |
| `renglones.py` | Los ítems: junta las filas de un split y desglosa por unidades (estado, fechas) |
| `valor.py` | Cuánto vale un ítem: por tramos (facturado, en guía emitida, calculado) |
| `precios.py` | El precio CALCULADO de venta, nunca costo; cuándo hay precio congelado |
| `estados.py` | Traducción de los estados internos a los del cliente y la línea de tiempo. Funciones puras |
| `tiempo.py` | El día de Chile, los días tecleados a mano y las fechas en texto libre |
| `busqueda.py` | El buscador |
| `documentos.py` | Guías y facturas como documentos del pedido |
| `dtes.py` | Lo que el portal lee de un DTE: emitido, fecha, a quién se emitió, precios de la guía |
| `descargas.py` | La entrega de archivos: el PDF lo baja el servidor; bitácora; esperas acotadas |
| `seguimiento.py` | El seguimiento de un ítem: hitos con fecha, documentos, fotos |
| `resumen.py` | El tablero |
| `fotos.py` | Cómo se guarda una foto: solo un derivado sin metadatos |
| `cupo.py` | Tope de frecuencia por cuenta (429) |
| `router_ordenes.py` | Rutas `/api/portal/*` de datos (cliente) |
| `router_admin_fotos.py` | Rutas `/api/portal-admin/*` de fotos (personal) |
| `seed_demo_pedidos.py` | Pedidos de demostración con fotos de prueba. **No es un paso de producción** |
| `tests/test_estados.py`, `test_tiempo.py`, `test_renglones.py`, `test_valor.py`, `test_ordenes.py`, `test_busqueda.py`, `test_documentos.py`, `test_fotos.py` | Las suites de esta entrega |
| `tests/mutaciones/` | Las 218 mutaciones con que se verificó que cada suite prueba lo que dice (§7) |

### Código existente que se modificó

| Archivo | Cambio |
|---|---|
| `backend/auth.py` | `huella_clave`, `token_para` y la comparación de la huella en `get_usuario_autenticado` (32 líneas agregadas; el archivo sigue en CRLF) |
| `backend/routers/auth.py` | El login emite el token con `token_para` |
| `backend/config.py` | `PORTAL_CUPO_ENABLED` |
| `backend/portal/models.py` | Tablas `foto_articulo` y `portal_descarga_log`; constantes `DESCARGA_*` |
| `backend/portal/init_db.py` | Las crea (mismo comando de siempre) |
| `backend/portal/deps.py` | `require_portal_listo` (clave temporal), `PortalScope.ve_montos`, `RutaDelPortal` (503 si falta una tabla) |
| `backend/portal/router.py` | `/portal/yo` toma `debe_cambiar_clave` del scope; el cambio de clave devuelve un token nuevo |
| `backend/portal/tests/_datos.py` | Fábricas de ventas, compras, embarques, recepciones, reclamos, despachos, facturas, DTE y fotos; carpetas temporales |
| `backend/portal/tests/test_aislamiento.py` | Candado estructural: toda ruta de datos exige clave propia y cobra el cupo |
| `backend/main.py` | `include_router` de los dos routers nuevos |
| `backend/requirements.txt` | `pillow` (procesa las fotos) |
| `frontend-src/src/portal/*` | Las pantallas del cliente (ver su README) |
| `frontend-src/src/components/FotosItemModal.tsx` | La pestaña «Foto despacho» ahora sube fotos |
| `frontend-src/src/App.tsx` | Rutas anidadas bajo `/portal` |

**No se tocó ningún router del ERP** salvo `routers/auth.py` (una línea: el token). El
portal lee las tablas del ERP; no escribe en ellas (salvo las dos tablas propias).

## 3. Cómo se arma lo que ve el cliente

### 3.1 De quién es una orden (`candado.py`)

Una orden es de un cliente si su cotización está anclada a una de sus fichas:
`Cotizacion.cliente_id IN scope.cliente_ids`. Esa condición se escribe **solo** en
`candado.de_sus_ordenes`; órdenes, guías, facturas y fotos se consultan pasando por ahí.
`test_ordenes.py` lee el código y falla si otro archivo del portal nombra
`Cotizacion.cliente_id`.

Lo ajeno responde **404**, nunca 403: los ids son correlativos y un 403 confirmaría que
el id existe.

**La unidad es la venta.** Si una venta tiene más de una fila `oc_cliente` (pasó antes
de que el Cierre de Venta fuera idempotente), el portal muestra una sola orden (la de
menor id) y carga sus ítems, guías y facturas por cotización.

### 3.2 Qué es «un ítem» (`renglones.py`)

Para el cliente, un ítem es un renglón de su orden de compra. En la base ese renglón puede
estar partido en varias filas de `items_cotizacion` (la compra parcial, la preparación
parcial y el cierre de pre-embarque clonan la fila con otra cantidad). El portal las vuelve
a juntar (`_clave_de_linea`): si pidió 14 renglones, ve 14.

Cuando las partes van en estados distintos, el ítem lo dice por unidades. La fila
`en_bodega`, que el ERP mantiene así mientras no salga completa, se desglosa en lo que ya
salió, lo que está en bodega y lo que no llegó (`_partes_de_fila`), con las mismas reglas
del tope físico de Despachos (`routers/despachos.py::_qty_recibida_utilizable`).

Tres cantidades, que no hay que confundir (`types.ts::ItemDeOrden`):

| Campo | Qué es |
|---|---|
| `cantidad` | lo que el cliente pidió |
| `cantidad_entregada` | lo que salió en un despacho cerrado y no declaró como no recibido |
| `cantidad_por_entregar` | lo que todavía se espera bajo esta orden |

La diferencia entre la primera y las otras dos es lo «no recibido en la entrega» (3.3).

### 3.3 El estado (`estados.py`)

Son dos máquinas de estado que corren en paralelo y el cliente ve su cruce:

| El ítem está… | …y su embarque está… | El cliente ve |
|---|---|---|
| `ingresado`, `cerrado` | — | Pedido confirmado |
| `comprado` | — | En proceso de compra |
| `preparado`, `pre_embarcado` | — | Listo en origen |
| `embarcado` | `en_bodega_proveedor` | Listo en origen |
| `embarcado` | `en_transito` (o desconocido) | En tránsito |
| `embarcado` | `en_aduana` | En internación aduanera |
| `embarcado` | `en_recepcion`, `en_bodega` | Llegó a Chile, en recepción |
| `en_bodega` | — | En nuestra bodega |
| `despachado` | — | Despachado (o «Despachado y facturado», por unidades) |
| `reclamo_proveedor` | — | En gestión de reposición |

Y dos **incidencias**, fuera del recorrido:

- **En gestión de reposición** (`en_gestion`): el ítem tiene un reclamo al proveedor (no
  vino o llegó dañado). Se sigue esperando bajo la misma orden. Lo que una recepción
  cerrada no trajo se muestra así **solo si hay reclamo**; en la compra nacional, que
  llega por tandas, es «En proceso de compra».
- **No recibido en la entrega** (`no_recibido`): unidades que salieron en una guía y que
  el cliente, al firmarla, declaró no haber recibido. El ERP no las repone por esa venta
  (`routers/despachos.py::firmar_despacho`: la reposición va en una cotización nueva), así
  que el portal dice el hecho y no promete nada: no cuentan como entregadas ni como «por
  entregar», no marcan atraso y no suman en «por entregar» del tablero. La orden sigue
  abierta mientras el dueño no decida otra cosa (§8).

Ante un valor desconocido se responde el estado **menos** avanzado: el portal puede decir
de menos, nunca prometer un avance que no ocurrió. Ninguna etiqueta nombra al proveedor,
un país ni una fecha estimada.

### 3.4 Las fechas (`renglones.py`, `tiempo.py`)

Una sola fecha por ítem: la **comprometida**, congelada al Cierre de Venta
(`items_cotizacion.fecha_promesa_cliente`, Fase 2). Para ventas anteriores a ese
congelado, o para un ítem cuyo plazo no sirve para calcular, la fecha de entrega de la
orden. Nunca un cálculo en vivo. «Vencida» la
calcula el backend con el día de Chile (`tiempo.hoy_chile`) y solo aplica a lo que
todavía se espera.

La fecha de una orden (`proxima_fecha`) es la **más antigua** entre lo que se espera: si
hay atrasados, ya pasó, y las pantallas la rotulan «atrasado N días». No es «la próxima
que todavía no vence».

Tres clases de fecha en la base, cada una con su función en `tiempo.py`: un instante del
reloj del servidor (UTC → `dia_chile`), un día tecleado guardado a medianoche
(`despachos.fecha_firma` → `dia_tecleado`; pasarlo por `dia_chile` lo corre al día
anterior) y un texto libre (`oc_cliente.fecha_oc` → `fecha_de_texto`, que solo acepta un
año de dos dígitos si cae cerca del día de ingreso de la orden).

Los hitos de la línea de tiempo llevan la fecha en que **ocurrieron** (tomada de la fila
que los registra; ver la tabla en la cabecera de `seguimiento.py`). Un hito futuro no
lleva fecha: el portal no estima. Los pasos de embarque (listo en origen, tránsito,
aduana, recepción) se muestran solo si el ítem pasó por un embarque, o si todavía no
llega y no se compró en Chile; lo que ya está en bodega sin embarque registrado no los
afirma.

### 3.5 Los montos (`valor.py`, `precios.py`)

- El precio CALCULADO es `precio_venta_clp` de `pricing_service.calcular_cotizacion`, el
  mismo que usa Contabilidad. `precio_unit_cotizacion` **no** es un precio de venta (en una
  cotización de origen `costo` es el precio del proveedor en USD).
- Solo hay precio calculado cuando ya no se mueve: cotización de origen `venta_clp`, o con
  la foto de precios congelada al cierre (`pricing_snapshot`) **completa** (las 8
  `CLAVES_PRICING`, numéricas). Una venta de origen `costo` anterior al congelado se
  recalcularía con el dólar de hoy: no muestra montos, **tampoco en los ítems que ya
  tienen factura** (lo que falta por facturar se valorizaría con el dólar del día de su
  guía y el total se movería). Sus facturas se ven igual, como documentos.
- El valor de un ítem se arma **por tramos de unidades**: lo facturado vale lo que dicen
  sus líneas de factura; lo que viajó en una guía electrónica emitida y aún no se factura
  vale el precio de esa guía (`wasabil_dte.payload_json`, igual que
  `contabilidad._precios_congelados_guia`); el resto, el precio calculado. El documento
  **reemplaza** al cálculo, no lo crea. Si las unidades tienen más de un precio, el
  unitario va `null` y la pantalla dice «Varios precios».
- El redondeo es el de la factura (unitario a 2 decimales; total de línea e IVA a peso,
  half-up). `test_ordenes.py` §C compara contra `routers/contabilidad.py`.
- El tablero suma comprado, por entregar y facturado sobre **el mismo conjunto**: las
  órdenes con precio en todos sus ítems; las demás se cuentan en `ordenes_sin_monto`.
- Perfil `bodega`: ningún monto viaja (`PortalScope.ve_montos`).

### 3.6 Los documentos (`documentos.py`, `dtes.py`, `visibles.py`)

- Guía visible = despacho **cerrado**. Factura visible = **emitida** (con folio) y de
  Grupo AM. La misma definición la usan el listado, el buscador, el estado «facturado» y
  el seguimiento. Mientras una guía electrónica está a medio emitir no se muestra ni se
  busca el número tecleado en el despacho (lo va a reemplazar el folio del SII).
- De cada guía: qué viajó, y **cómo la recibió el cliente** (`recepcion`: pendiente,
  conforme, con faltante) con el día de la firma; `cantidad_recibida` es `null` mientras
  nadie firma.
- **A quién se emitió** (`dtes.py`): si consta que un DTE fue a un RUT que no es el de la
  venta, la factura no se muestra y la guía no entrega archivos, esté en el estado que
  esté. Y los archivos de un documento que el SII ya aceptó solo se entregan si consta que
  fue al RUT de la venta: sin poder leer a quién (p. ej. un folio registrado a mano, que
  reemplaza la respuesta guardada), no se ofrecen. Falla cerrado. La evidencia es
  `wasabil_dte.respuesta_json["receiver_rut"]`; la respuesta de una emisión normal la
  trae (comprobado sobre los documentos reales de la base).
- Perfil `bodega`: sin facturas y sin archivos (el PDF de la guía trae precios).
- La guía firmada se sirve solo si es PDF/JPG/PNG con el nombre que genera la subida del
  ERP; `uploads/docs` es compartida con el AWB y la factura del proveedor, y por eso el
  portal nunca recibe ni devuelve un nombre de archivo.

### 3.7 Las descargas (`descargas.py`)

- **El PDF no se entrega como enlace.** `wasabil_dte.pdf_url` es público y permanente.
  El portal baja el PDF en el servidor (con el token de la empresa, armando la dirección
  desde el uuid validado), guarda una copia en `uploads/portal_docs/` y lo entrega como
  bytes con la sesión del cliente. `pdf_url`, `xml_url` y `uuid` no salen.
- Cada descarga se anota en `portal_descarga_log` (con la IP) **antes** de entregar, y la
  ruta **cierra la sesión de la base** antes de hablar con Wasabil: una bajada lenta no
  tiene tomada una conexión del pool, que es de todo el ERP.
- Nada espera sin tope: la copia local se entrega sin candado; a lo más 3 bajadas desde
  Wasabil a la vez (la que no cabe recibe «no disponible» de inmediato); dos peticiones
  del mismo documento no lo bajan dos veces, y la segunda desiste a los 2 segundos; un
  fallo se recuerda 60 segundos. Wasabil tiene tiempo máximo por fase y total (30 s) y
  tope de peso (15 MB). El cliente recibe siempre el mismo texto (502); el motivo real
  queda en el log.

### 3.8 Las fotos (`fotos.py`, `router_admin_fotos.py`)

- Son las que el personal sube **al despachar**, en una tabla aparte (`foto_articulo`).
  Las fotos de recepción en bodega son evidencia interna y **no** se publican.
- **Subir es publicar**: no hay paso de aprobación. La pantalla lo avisa antes de subir.
- Se guarda solo un **derivado**: reducido a 1600 px, enderezado y sin metadatos (el
  original trae el GPS de la bodega). El original no se guarda. Topes: 15 MB de subida,
  60 MP para un JPEG y 25 MP para PNG/WebP (que se decodifican enteros), a lo más dos
  derivados a la vez.
- Un ítem en reclamo al proveedor no admite fotos ni muestra las que tenía. Máximo 6 por
  ítem, también si varias subidas llegan juntas (el recuento se hace con la fila
  bloqueada).

### 3.9 El buscador (`busqueda.py`)

Mismo contrato que el buscador de Despachos (AND entre palabras, OR entre campos,
comodines escapados), con tres diferencias deliberadas explicadas en su cabecera: solo
busca en lo que la pantalla muestra, compara sin guiones siempre (también el N° de orden)
y tolera las costumbres de quien escribe desde un correo («OC 4500123», la raya larga).

### 3.10 Tres defensas que no se ven

- **Tope de frecuencia por cuenta** (`cupo.py`): 300 peticiones por minuto a las rutas de
  datos, 30 al tablero (la ruta más cara) y 30 descargas. Pasado el tope, 429 con
  `Retry-After` y un texto para el cliente. Vive en memoria (un worker) y se apaga con
  `PORTAL_CUPO_ENABLED=false`.
- **El token va atado a la clave** (`auth.token_para`): el de una cuenta portal lleva la
  huella de su clave; al cambiarla, todo token anterior deja de valer (también el de
  quien hubiera interceptado la clave temporal) y el cambio devuelve uno nuevo. Los tokens
  del personal no cambian.
- **503 en vez de 500 mudo** (`deps.RutaDelPortal`): si falta una tabla del módulo (deploy
  sin `init_db`), las rutas del portal responden 503 con el comando exacto.

## 4. Rutas

**Cliente** (`/api/portal`, cuenta portal que ya reemplazó su clave temporal):

| Ruta | Devuelve |
|---|---|
| `GET /resumen?anio=` | El tablero |
| `GET /ordenes?q=&estado=&anio=&page=&page_size=` | Mis pedidos |
| `GET /ordenes/{id}` | Una orden: ítems, guías, facturas, ejecutivo |
| `GET /ordenes/{id}/items/{item_id}` | Seguimiento de un ítem |
| `GET /fotos/{id}` | La imagen |
| `GET /guias/{despacho_id}/pdf` · `/firmada` | El PDF de la guía · la copia firmada |
| `GET /facturas/{id}/pdf` | El PDF de la factura |

Respuestas que la pantalla traduce: 403 (clave temporal, cuenta suspendida), 404 (no
existe o no es tuyo), 429 (demasiadas solicitudes), 502 (documento no disponible ahora),
503 (módulo sin instalar).

**Personal** (`/api/portal-admin`, cuentas internas de `mineria`):

| Ruta | Qué hace |
|---|---|
| `GET /items/{item_id}/fotos` | Fotos publicadas de un ítem |
| `POST /items/{item_id}/fotos` | Sube una foto (queda publicada) |
| `GET /fotos/{id}` · `DELETE /fotos/{id}` | Vista previa · borrar |

Los tipos de cada respuesta están en `frontend-src/src/portal/types.ts`.

## 5. Deploy

```bash
pip install -r requirements.txt     # entra pillow: sin él el backend NO levanta
python -c "import PIL; print(PIL.__version__)"   # debe responder 12.2 o más
python -m portal.init_db            # crea foto_articulo y portal_descarga_log (idempotente)
```

- Carpetas nuevas bajo `backend/uploads/`: `portal_fotos/` y `portal_docs/`. Las crea el
  código la primera vez que escribe. **No deben quedar servidas por nginx**: verificar
  igual que `uploads/bodega` (ver el checklist).
- `client_max_body_size` de nginx es el tope real de lo que se puede subir como foto (el
  backend rechaza sobre 15 MB, pero el cuerpo ya llegó). Dejarlo en 16m o más.
- Los PDF de guías y facturas necesitan `WASABIL_API_TOKEN` (el mismo que ya usa la
  emisión). Sin token, el botón de descarga responde «no pudimos obtener el documento».
- `.env`: `PORTAL_CUPO_ENABLED` (opcional; `true` por defecto). Con dos workers de uvicorn
  el tope efectivo se duplica (cada proceso cuenta aparte).
- **Antes de publicar, solo lectura:** cuántos documentos emitidos no traen el RUT del
  receptor en la respuesta guardada (esos no ofrecerán archivos en el portal, §3.6):

  ```sql
  SELECT tipo_dte, COUNT(*) AS sin_receptor
  FROM wasabil_dte
  WHERE status_id = 3 AND folio IS NOT NULL AND folio <> ''
    AND (respuesta_json IS NULL OR respuesta_json NOT LIKE '%receiver_rut%')
  GROUP BY tipo_dte;
  ```

  Si da cero (lo esperado: la emisión normal lo trae), no hay nada que hacer. Si no, son
  folios registrados a mano u otra forma de respuesta: avisar antes de publicar, porque
  esos PDF se van a mandar por correo mientras el módulo de emisión no guarde el receptor.
- **Antes de abrir el portal, solo lectura:** ventas con más de una orden de cliente
  (podía pasar cuando dos personas cerraban la misma venta al mismo tiempo; hoy el
  cierre lee la cotización bajo lock y `portal/ordenes.py` muestra una sola orden por
  venta, pero lo histórico hay que mirarlo):

  ```sql
  SELECT cotizacion_id, COUNT(*) FROM oc_cliente
  WHERE cotizacion_id IS NOT NULL GROUP BY cotizacion_id HAVING COUNT(*) > 1;
  ```

  Si da filas, avisar: no existe endpoint para borrar una `oc_cliente` y Ventas y
  Contabilidad las cuentan todas.
- `users.tipo_acceso` y las tablas de la Fase 1 ya tienen que existir (`init_db` lo
  comprueba).

### Rollback

El mismo de la Fase 1, **con orden**: primero `python -m portal.desactivar_cuentas
--aplicar` (desactiva toda cuenta que no sea del personal: las portal y las de tipo
desconocido, que no se reactivan desde la pantalla), después el código. Para apagar
solo esta entrega sin tocar las cuentas:
quitar los dos `include_router` de `main.py` (`portal_ordenes_router`,
`portal_admin_fotos_router`); el cliente vuelve a ver solo su página de cuenta. Las dos
tablas nuevas pueden quedarse. Al revertir `auth.py`, los tokens de las cuentas portal
emitidos con huella siguen valiendo (el claim extra se ignora).

## 6. El piloto

```bash
python -m portal.seed_piloto            # 2 fichas demo + 2 cuentas (Fase 1)
python -m portal.seed_demo_pedidos      # pedidos demo con fotos de prueba
python -m portal.seed_demo_pedidos --limpiar
```

`seed_demo_pedidos` es para la base local o de QA: escribe ventas, despachos y facturas
ficticias directamente en las tablas (aparecen en las pantallas internas, marcadas
`DEMOPORTAL-…`). Para probar la cadena real, cotizar y cerrar una venta normal con el RUT
de la ficha demo.

Una cuenta (demo o real) que ya descargó algún documento **no se elimina**: la FK de
`portal_descarga_log` conserva su bitácora (Cuentas del portal responde 409 «desactívala»
y el log del proceso dice qué tabla lo bloqueó). Por lo mismo, `seed_piloto --limpiar`
termina con el error de esa FK y sin borrar nada si una cuenta demo descargó algo, hasta
que se decida qué hacer con esa bitácora.

## 7. Pruebas

```bash
cd backend && ./venv/bin/python -m pytest portal/tests -q     # las suites
./venv/bin/python -m portal.tests.mutaciones                   # las 218 mutaciones (tarda)
./venv/bin/python -m portal.tests.mutaciones documentos        # solo una suite
```

| Suite | Qué protege | Mutaciones |
|---|---|---|
| `test_estados.py` | La tabla de traducción de estados, las incidencias y la línea de tiempo | 15 |
| `test_tiempo.py` | El día de Chile, el día tecleado y las fechas en texto | 5 |
| `test_renglones.py` | Un renglón es un renglón; unidades en su lugar; lo no recibido; facturado por unidades; fechas; recorrido | 39 |
| `test_valor.py` | El valor por tramos; sin precio congelado no hay montos; por entregar; el tablero suma lo mismo | 21 |
| `test_ordenes.py` | Cada cliente ve lo suyo; lista blanca de campos; paridad de precios con Contabilidad; perfiles; listado; clave temporal y token; tablero; cupo | 38 |
| `test_busqueda.py` | Cómo escribe la gente; comodines; que la búsqueda no abra el candado; que no se busque por lo que no se muestra | 19 |
| `test_documentos.py` | Qué documentos existen; el enlace público no sale; el PDF por el servidor; a quién se emitió; esperas acotadas; Wasabil | 55 |
| `test_fotos.py` | El derivado sin metadatos; topes reales; quién sube y quién ve; subidas simultáneas; archivos sueltos | 19 |
| `test_aislamiento.py` | Cada ruta tiene su puerta; toda ruta de datos exige clave propia y cobra el cupo | 7 |

Corren contra la base local, con la app completa y sesiones reales; los archivos van a
carpetas temporales. A Wasabil no se le llama: `test_documentos.py` reemplaza la descarga
por una falsa. `portal/tests/mutaciones/` guarda, por suite, qué se rompió a propósito
para confirmar que la suite lo detecta; su cabecera explica cómo corre (sobre una copia
del backend, nunca sobre el árbol de trabajo).

## 8. Decisiones que quedan para el dueño

Mientras no se decidan, se aplica lo que dice cada punto.

1. **Lo que el cliente firmó como no recibido** (firma parcial de la guía). Hoy: la orden
   sigue **abierta**, el ítem muestra «No recibido en la entrega» con la fecha de la
   firma y la frase «Según la guía firmada, estas unidades no se recibieron. Para
   reponerlas, contacta a tu ejecutivo de MachParts», sin marca de atraso y fuera de
   «por entregar». Por decidir: el texto final; si la orden se cierra mostrando el
   faltante o sigue abierta; si se marca atrasado.
2. **Un ítem en reclamo al proveedor** se muestra «En gestión de reposición» hasta que el
   reclamo se resuelva en el ERP. No hay fecha nueva: la reposición se cotiza aparte. Un
   reclamo resuelto sin reposición deja la orden abierta.
3. **Las ventas cerradas antes del congelado de precios** (origen `costo`, sin
   `pricing_snapshot` completo) no muestran montos, tampoco en sus ítems facturados.
   Opción: mostrar en esos pedidos el precio de sus facturas solo en los ítems facturados
   por completo.
4. **Un documento cuyo folio se registró a mano** no ofrece PDF ni guía firmada en el
   portal, y hoy nada lo repone: se le manda al cliente por correo. El arreglo de fondo
   (guardar el receptor al registrar el folio) es del módulo `wasabil_dte`.
5. **Una factura anulada con nota de crédito** sigue visible como emitida: el ERP no
   registra las notas de crédito (se emiten directo en Wasabil).
6. **Quién puede operar Cuentas del portal y subir fotos**: hoy, cualquier cuenta interna
   de la marca `mineria`.
7. **Los topes de frecuencia** (300 / 30 / 30 por minuto por cuenta) y su interruptor
   (`PORTAL_CUPO_ENABLED`).
8. **El cliente del portal descarga el ERP entero antes de ver su pantalla**: 2,45 MB
   (≈597 KB comprimido). Hay un diseño listo y medido que lo deja en 384 KB (≈120 KB):
   requiere visto bueno porque toca `main.tsx` y `App.tsx`. Riesgo de no hacerlo: bajo,
   solo demora la primera carga con mala señal. Mientras no se decida, no se aplica
   (`docs/paquete-frontend-general-2026-10-06.md`, C15).
9. **El portal nace en tema claro y el cliente puede pasarlo a oscuro con un botón** (en
   la cabecera, `CabeceraPortal.tsx`; la primera vez que una cuenta portal entra desde un
   navegador, `PortalLayout` lo pasa a claro y desde ahí manda el botón). Detalle
   conocido: en la PRIMERA visita el login se ve oscuro (`ThemeProvider` nace en `dark`)
   y el portal aparece claro al entrar; desde la segunda visita el login ya sale claro.
   El tema se guarda en la misma llave que usa el ERP (`machparts-theme`): probar una
   cuenta de cliente en el navegador de trabajo deja también el ERP en claro esa primera
   vez (se devuelve con el botón del ERP; en una ventana de incógnito no pasa).
