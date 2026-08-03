# Módulo Wasabil DTE MonzaParts — Guías (52) y facturas (33) electrónicas al SII

Emisión de documentos tributarios electrónicos **directo al SII vía Wasabil** con
la cuenta propia de MonzaParts (**LOPEZ HERNANDEZ INVERSIONES SPA, RUT
78.121.316-0**) — desde los Despachos y las Facturas de Monza, sin portal MiPyme,
sin firma manual. Espejo del módulo batalla-probado `wasabil_dte/` de Grupo AM
(folios reales 136/137 en guías, 116 en facturas), en paquete aislado propio
(patrón de la casa: **cero imports cruzados** `monza_*` ↔ GA).

- **Fase 5 — guías de despacho (52)**: ver "Qué hace" más abajo.
- **Fase 6 (la "Fase B" de GA) — facturas electrónicas (33)**: ver la sección
  [Fase B](#fase-b--facturas-electrónicas-33).

## Qué hace

En la página **Despachos Monza**, un despacho **en preparación** gana el botón
**"Emitir guía SII"** (flujo guía-primero de F2: la guía se emite ANTES de
cerrar el despacho):

1. **Previsualizar** (no toca el SII): receptor (ficha real del cliente en
   Wasabil, por RUT), líneas (cantidades del despacho × **precio CONGELADO**
   `MonzaCotizacionItem.precio_unitario_clp` — el mismo que honra la factura
   Monza, jamás recálculo vivo), NETO/IVA/TOTAL con la **tasa de IVA de la
   venta** (`iva_pct` congelado; fallback config → 0.19) y la referencia a la
   OC del cliente (tipo 801: `MonzaCotizacion.oc_cliente` + `oc_fecha`).
   Si falta algo (RUT, OC, fecha OC, precio, cliente no existe en Wasabil),
   lo dice y bloquea.
2. **Confirmar y emitir** (con el OK explícito del usuario): la guía viaja al
   SII. Al quedar **Emitida**, el folio real se graba en
   `MonzaDespacho.numero_guia` (única escritura del módulo a una tabla
   existente) y quedan los links al PDF y XML. Si el SII la rechaza, se
   muestra el motivo y hay **reintento seguro**.

## Diseño (patrón de los módulos aislados monza_*)

```
monza_wasabil_dte/
├── client.py    ← ÚNICO punto que habla con api.wasabil.com; lee
│                  settings.WASABIL_API_TOKEN_MONZA (jamás el token de GA)
├── service.py   ← lógica pura: guía 52 y factura 33 formato v2/v3 de nacimiento,
│                  nombres ≤25, refs 801/52 sin reason, IVA half-up con tasa POR
│                  VENTA (parámetro)
├── models.py    ← tabla NUEVA `monza_wasabil_dte` (ancla anti doble emisión;
│                  despacho_id para la 52 · factura_id para la 33)
├── init_db.py   ← creación/upgrade idempotente de la tabla (correr en deploy)
├── router.py    ← /api/monza/wasabil/... (candado empresa 'automotriz',
│                  montado bajo el gate MONZA_CONTAB_ENABLED)
└── tests/       ← 100% fakes por monkeypatch del client MONZA — jamás el API real
```

- **Tabla ancla propia** `monza_wasabil_dte` con FK a `monza_despachos.id` +
  UNIQUE por despacho y FK a `monza_cont_factura_cliente.id` + UNIQUE por factura
  (la `wasabil_dte` de GA apunta a las tablas de GA — reutilizarla anclaría a las
  equivocadas). Guía y factura conviven en la MISMA tabla, discriminadas por
  `tipo_dte` (52|33) y por cuál origen viene poblado (`despacho_id` XOR
  `factura_id`); los dos UNIQUE conviven porque en MySQL los NULL no colisionan.
  InnoDB explícito (los `SELECT ... FOR UPDATE` del protocolo lo requieren).
- `models.py` **importa `monza_contabilidad.models`** a propósito: este módulo
  también se carga con `MONZA_CONTAB_ENABLED` apagado (el guard de Despachos hace
  un import local para no dejar anular una guía SII viva) y sin ese registro el
  primer `configure_mappers()` tumbaría toda query ORM del proceso. Se importa el
  módulo de TABLAS, nunca el router: no hay ciclo.
- **Candado de marca DOBLE**: `require_empresa('automotriz')` a nivel de router
  **y** `empresa server_default='automotriz'` en la tabla. El riesgo más grave
  del espejo es emitir documentos de Monza con el RUT de Grupo AM: por eso el
  client es propio y lee SOLO el token Monza.
- **Adaptaciones vs GA** (documentadas en la decisión de arquitectura): formato
  v2/v3 **de nacimiento** (name = descripción limpia [:25], parte en `code`;
  `invoiceReference` = SOLO el N° de despacho `DSP-AAAA-####`; referencia 801
  SIN `reason`); precios congelados (no `_precios_de_cotizacion`); cuadratura
  con `iva_rate` como **parámetro** (no constante 0.19); `oc_fecha` es columna
  `Date` (F3) — sin `parse_fecha_oc`; no existe OcCliente: la cotización ES la
  venta; reintento por referencia con match EXACTO puro (Monza no tiene
  documentos legados v1).

## Protocolo de seguridad (NUNCA saltarse — violarlo = doble emisión REAL)

1. Emitir al SII es **IRREVERSIBLE**. `issue=false` por defecto en TODO;
   `issue=true` existe SOLO en el camino de emisión, después de que el usuario
   vio la previsualización y confirmó ESE documento.
2. **Claim commiteado ANTES de cualquier HTTP**: `_reclamar_emision` toma
   FOR UPDATE del despacho **con `populate_existing()`** (sin eso SQLAlchemy
   sirve el identity map viejo y la re-validación no ve el claim del request
   gemelo), re-chequea `en_preparacion` bajo lock, marca `en_vuelo_desde` y
   **commitea** — claim visible y locks liberados antes del HTTP. `rollback()`
   antes de tomar el lock (el snapshot REPEATABLE READ debe nacer con él).
   Locks cortos: JAMÁS red adentro de un lock.
3. **La taxonomía `ambiguo` decide el claim**: error NO ambiguo (ConnectError /
   401 / 4xx: seguro no se creó) → libera claim, reintento inmediato; error
   AMBIGUO (timeout / 5xx / JSON ilegible) → el claim QUEDA y expira solo a los
   180 s — nadie duplica en la ventana. Cambiar UN solo False↔True al copiar
   habilita doble emisión o bloquea reintentos legítimos.
4. **Máquina de estados default-deny**: emitido (3), claim vigente, uuid con
   status 2/6 o desconocido → bloquean; solo fallido (4) o sin-uuid-con-error
   habilitan reintentar.
5. **El folio se registra SOLO con status 3 (Emitido)** → recién ahí se copia a
   `MonzaDespacho.numero_guia`. El uuid se persiste apenas responde Wasabil y
   libera el claim (el uuid pasa a ser el candado).
6. **El reintento nunca re-crea a ciegas**: con uuid consulta el estado real
   (falla → 502 aborta); sin uuid busca por referencia (`MonzaDespacho.numero`,
   match exacto) y aborta con 502 si la búsqueda falla o vino truncada
   (`busqueda_completa=False`) — "no lo encontré" en lista truncada NO prueba
   inexistencia.
7. **Guards inversos en Despachos**: anular con guía viva (emitida | claim
   vigente | uuid en proceso) → 409; pisar `numero_guia` a mano con guía viva →
   409 (guía FALLIDA no bloquea).
8. **Reloj del claim UTC-naive** (`utcnow()` en ambos extremos) — inmune al
   cambio de hora chileno. Fecha del DTE = `hoy_chile()` (America/Santiago),
   jamás `date.today()` (el VPS en UTC emitiría con fecha de mañana en la noche).
9. Estados Wasabil: 6 Pendiente · 2 Procesando · 3 **Emitido** (folio + PDF/XML)
   · 4 Fallido (`display_error`; reintento permitido).
10. **DESARROLLO Y TESTS: PROHIBIDO llamar al API real** — todo con fakes por
    monkeypatch de `monza_wasabil_dte.client` (el client propio da superficie
    de monkeypatch independiente de las suites GA).

## Fase B — facturas electrónicas (33)

La Fase 6 de Monza (equivalente a la "Fase B" de GA) agrega la emisión de la
**factura electrónica DTE 33** sobre la MISMA maquinaria: el módulo NO
reimplementa ninguna regla de facturación, es un envoltorio de emisión sobre las
piezas de `monza_contabilidad` (`_construir_factura` / `_persistir_factura`), así
que los topes por ítem, por guía y el tope Σ brutos ≤ total de la venta corren
IGUAL que en el registro manual.

### El flujo, y por qué la factura nace SIN folio

1. **Previsualizar** (`POST /monza/wasabil/facturas/preview`): no persiste nada y
   no toca el SII. Muestra receptor, líneas, NETO/IVA/TOTAL y las referencias, y
   lista los **problemas bloqueantes** antes de que exista nada.
2. **Emitir** (`POST /monza/wasabil/facturas/emitir`): crea la factura LOCAL con
   `numero_factura = NULL` y, **en la misma transacción**, la fila
   `monza_wasabil_dte` (tipo 33, `factura_id`, claim `en_vuelo_desde`); commitea
   AMBAS y recién entonces llama a Wasabil. El folio **lo asigna el SII** y se
   copia a `numero_factura` sólo al confirmarse status 3 (Emitido).
3. **Sondeo** (`GET .../estado`) y **reintento seguro** (`POST .../reintentar`),
   con las mismas reglas de la guía 52 (nunca re-crea a ciegas).

Por eso el folio obligatorio de facturas vive en el **endpoint manual** de
`monza_contabilidad`, jamás dentro del persistidor compartido: la vía SII persiste
sin folio a propósito, y la UNIQUE `uq_monza_cont_factura_folio` admite N filas con
NULL en MySQL (los borradores en vuelo conviven sin colisionar). Simétrico: la vía
SII **rechaza** un payload que traiga folio digitado.

### Referencias del DTE 33 (formato v3, sin `reason`)

| Tipo | Contenido | Regla |
|------|-----------|-------|
| `801` | N° y fecha de la **OC del cliente** (`MonzaCotizacion.oc_cliente` / `oc_fecha`) | SIEMPRE. Bloquea si falta, si excede 18 caracteres o si la fecha no está |
| `52` | **Folio SII de la guía**, resuelto en vivo desde `monza_wasabil_dte` por `despacho_id` | Sólo si la factura viene de un despacho |

- El folio de la 52 **jamás sale del snapshot `factura.numero_guia`**: ese campo
  puede conservar el N° tecleado a mano, porque el módulo lo pisa con el folio real
  recién al confirmarse la emisión de la guía. Referenciar el viejo produce un DTE
  33 real apuntando a una guía que el SII no conoce — **irreversible**.
- **Retiro en oficina** (`sin_guia`, exclusivo de Monza): no hay guía, así que la
  factura 33 lleva **sólo la 801**. Es una rama legítima y explícita, no un error.
- `invoiceReference` = `FACT-<id de la factura local>`: el ancla de recuperación
  con la que el reintento sin uuid busca el documento en Wasabil (match EXACTO).
- `paymentMethod` es **obligatorio** en el esquema del 33 (`contado`|`credito`,
  default `credito`) — se deriva de `condicion_pago` / `plazo_dias`.
- **Sin descuento de anticipo ni referencia 33**: Monza no tiene facturas de
  anticipo (eso es la Fase 7). Las líneas de la 33 nunca son negativas.
- **Los precios cuadran por construcción**: guía y factura leen el MISMO
  `MonzaCotizacionItem.precio_unitario_clp` congelado, así que no hace falta el
  puente `_precios_congelados_guia` de GA. El IVA usa la **tasa de la venta**
  (`iva_rate_de`), no el 0.19 fijo de GA: con una tasa distinta, copiar la
  constante descuadraría la 33 contra la 52.

### Protocolo específico de la 33 (además del general de más arriba)

1. **`db.rollback()` ANTES del `with_for_update()`** de la cotización. No es
   código muerto: la preparación ya abrió un snapshot REPEATABLE READ (con una
   llamada HTTP adentro), y sin el rollback la re-validación bajo lock no ve la
   factura que un request gemelo acaba de commitear → **dos facturas reales al
   SII** (bug reproducido 4/4 rondas en GA).
2. **Candado de intención por venta**: el UNIQUE `uq_monza_wasabil_dte_factura`
   protege una factura YA creada, pero en "emitir factura nueva" cada request
   crearía una factura con id DISTINTO. Por eso hay además un chequeo de
   emisión 33 en vuelo por cotización → 409.
3. **Guía 52 en proceso bloquea la 33**: si el DTE de la guía no está Emitido y
   llegó a Wasabil (uuid o claim vigente), emitir la factura se rechaza con
   mensaje explícito. Una guía FALLIDA sin uuid NO bloquea (ahí no hay guía
   electrónica). En Monza este guard es más necesario que en GA: cerrar el
   despacho lo deja facturable en el mismo segundo (GA intercala la firma).
4. **Adelantos DIFERIDOS** (invariante de plata): una factura que el SII rechaza
   NO debe haber movido plata. La vía SII persiste con `aplicar_adelanto=False` y
   el adelanto se aplica **recién** al confirmarse el folio. Blindado desde el
   otro lado: si la factura no tiene folio y su DTE no está Emitido, la aplicación
   de adelantos retorna 0 — sin eso, Tesorería aprobando un adelanto en esa
   ventana dejaría "pagada" una factura fantasma. Efecto visible: la cobranza
   `medio='adelanto'` aparece MÁS TARDE que en la vía manual.
5. **Folio duplicado no pierde el folio**: si el folio del SII choca con uno ya
   digitado a mano en otra factura, se anota el error y el DTE queda Emitido — se
   resuelve a mano. Perder el folio de un documento ya emitido sería peor.
6. **Borrar una factura con DTE ambiguo está PROHIBIDO** (`uuid IS NULL` y
   `en_vuelo_desde IS NOT NULL`): la respuesta se perdió y el documento PUDO nacer
   con folio real; borrar el ancla `FACT-<id>` lo volvería inadoptable y liberaría
   el cupo para una SEGUNDA factura. Parece un bug y no lo es — la salida es
   **Reintentar**, que consulta a Wasabil. Emitida → anular en Wasabil (nota de
   crédito). Fallo CONFIRMADO no enviado → sí se borra junto con la factura.
   El guard vive en `_bloqueo_dte_factura` (`monza_contabilidad/router.py`), que
   además BORRA la fila DTE en el único caso permitido: la FK `factura_id` es
   RESTRICT y sin ese `db.delete` el DELETE de la factura revienta con 1451.
   Monza es un punto MÁS ESTRICTO que GA a propósito: con `uuid` presente bloquea
   sea cual sea el estado (no solo 2|6), porque `status_id` local es una FOTO de la
   última sincronización — un rechazado (4) pudo corregirse y emitirse en Wasabil
   desde entonces. El frontend NO pre-bloquea el caso "SII fallida": bajo ese único
   badge conviven el borrable y el imborrable, y quien los separa es este guard.
7. **Factura zombi**: si el armado del payload falla DESPUÉS de persistir, la
   factura y su DTE se borran antes de responder 409. Una factura sin folio ya
   consume cupo de mercadería y tope de la venta desde el instante en que existe.

## Configuración

En `backend/.env` (NUNCA en git; el valor JAMÁS se lee/imprime en código ni logs):

```
WASABIL_API_TOKEN_MONZA=<token de la cuenta Wasabil de MonzaParts>
# compartido con GA (mismo host, solo difiere el token; default oficial):
WASABIL_API_BASE=https://api.wasabil.com/api
```

`GET /api/monza/wasabil/config` expone solo `{configurado: bool}`. Sin token:
la previsualización funciona con datos locales y avisa; emitir se bloquea con
mensaje claro. Antes de habilitar producción, hacer una **primera emisión real
controlada** (despacho de prueba, tipo de traslado interno).

## Deploy (orden OBLIGATORIO — regla de la casa)

1. `cd backend && ./venv/bin/python -m monza_contabilidad.init_db`
   **PRIMERO**: crea `monza_cont_factura_cliente`, destino de la FK
   `factura_id` de la Fase 6. Sin ella el paso 2 aborta con un mensaje claro
   (y, si se saltara el chequeo, MySQL fallaría con el críptico errno 150).
2. `cd backend && ./venv/bin/python monza_wasabil_dte/init_db.py`
   (crea `monza_wasabil_dte` y, si venía de la Fase 5, agrega `factura_id` +
   su índice + su FK + el UNIQUE `uq_monza_wasabil_dte_factura`; idempotente,
   correr las veces que sea — detecta por `information_schema`, no por nombre
   de índice autogenerado)
3. Verificar que `backend/.env` tenga `WASABIL_API_TOKEN_MONZA` (sin él el
   módulo queda en modo solo-preview) y que `config.py` ya lo declara
   (sin la declaración, `Settings()` revienta y el backend NO arranca).
4. Recién entonces reiniciar el backend. El montaje va DENTRO del gate
   `MONZA_CONTAB_ENABLED`: apagado, el módulo ni se importa.

**Saltarse el paso 2 con el backend ya reiniciado no es benigno**: a diferencia
del guard de Despachos (que se apaga solo si la tabla no existe, MySQL 1146),
las lecturas por `factura_id` chocarían con un error 1054 *unknown column* sobre
una tabla que SÍ puede tener guías emitidas. Preferimos fallar ruidoso.

## Limitación conocida (heredada de GA, aceptada y documentada)

El API de Wasabil no expone clave de idempotencia. El diseño la compensa con el
claim + la búsqueda por referencia antes de re-crear, pero queda una ventana
teórica: si tras un timeout el documento tarda MÁS que el TTL del claim (180 s)
en hacerse visible en Wasabil, un reintento podría no encontrarlo y re-crear.
En la práctica la creación es de segundos; si alguna vez ocurre, el documento
extra queda visible en Wasabil (misma referencia interna, única por despacho) y
se anula allá (`full_annulment`). NO intentar "arreglarla" en el port.

Otra limitación heredada: `GET /documents` responde 405 en el API real — el
reintento por-referencia solo puede abortar seguro (502), no reencontrar
documentos perdidos. El flujo normal usa el uuid y no depende de esto.

## Cómo revertir

Quitar el import + `include_router` de `monza_wasabil_dte` en `main.py`, quitar
`monzaWasabilAPI` de `services/monzaApi.ts` y el botón/modal de
`MonzaDespachosPage.tsx` (guías 52) y de `MonzaFacturasPage.tsx` (facturas 33).
La tabla `monza_wasabil_dte` puede quedar (no molesta) o eliminarse a mano.

Revertir SOLO la Fase B (dejando vivas las guías 52): basta con quitar el modal
de facturas del frontend. La columna `factura_id` puede quedarse: es nullable y
las filas de guía la llevan en NULL. **No borrar filas de DTE 33 ya emitidas** —
son el vínculo con documentos tributarios reales ante el SII.
