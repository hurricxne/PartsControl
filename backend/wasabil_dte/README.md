# Módulo Wasabil DTE — Guías (52) y Facturas (33) electrónicas al SII

Emisión de la guía de despacho electrónica **y de la factura electrónica**
**directo al SII vía Wasabil** (facturador electrónico de GRUPO AM SPA
77.977.813-4) — guías desde Despachos de Logística (Fase A) y facturas desde
Facturas y Cobranzas de Contabilidad (Fase B) — sin portal MiPyme, sin firma
manual.

## Qué hace

En la página **Despachos**, un despacho **en preparación** gana el botón
**"Emitir guía SII"**:

1. **Previsualizar** (no toca el SII): muestra el receptor (ficha real del
   cliente en Wasabil, por RUT), las líneas (cantidades del despacho × precios
   de la cotización — el MISMO cálculo que usa Contabilidad al facturar),
   NETO/IVA/TOTAL y la referencia a la OC del cliente (tipo 801: N° + fecha).
   Si falta algo (RUT, precio, fecha de OC, cliente no existe en Wasabil),
   lo dice y bloquea.
2. **Confirmar y emitir** (con el OK explícito del usuario): la guía viaja al
   SII. Al quedar **Emitida**, el folio real se graba en `despacho.numero_guia`
   y quedan los links al PDF y XML. Si el SII la rechaza, se muestra el motivo
   y hay **reintento seguro**.

El resto del flujo del programador NO cambia: cerrar el despacho, firmar la
guía y facturar siguen igual (y la futura factura referencia este folio).

## Diseño (patrón de los módulos aislados: compras_contab / tesoreria)

```
wasabil_dte/
├── client.py    ← ÚNICO punto que habla con api.wasabil.com (token en .env)
├── service.py   ← lógica pura: armado guía 52, nombres ≤25, ref 801, IVA half-up
├── models.py    ← tabla NUEVA `wasabil_dte` (ancla anti doble emisión)
├── router.py    ← /api/wasabil/... (candado empresa 'mineria')
└── tests/       ← test_service.py (pytest) + test_integration.py (Wasabil simulado)
```

- **Cero ALTER**: la tabla se crea sola con `create_all`. La única escritura a
  una tabla existente es `despacho.numero_guia = folio` al quedar Emitido.
- **Candado minería**: Wasabil emite con el RUT de GRUPO AM; usuarios de otra
  empresa reciben 403 (mismo `empresa_guard` de los módulos de contabilidad).

## Protocolo de seguridad (NUNCA saltarse)

1. Emitir al SII es **IRREVERSIBLE**. `issue=true` se manda SOLO después de que
   el usuario vio la previsualización y confirmó ESE documento.
2. **Anti doble emisión**: la fila `wasabil_dte` se crea y commitea ANTES de
   llamar a Wasabil; el `uuid` se persiste apenas responde; índice único por
   despacho; el reintento consulta el estado real (por uuid o por la referencia
   interna `OC … · DSP-…`) antes de re-crear nada.
3. El **folio** se registra SOLO cuando el documento queda **Emitido (status 3)**.
4. Estados Wasabil: 6 Pendiente · 2 Procesando · 3 **Emitido** (folio + PDF/XML)
   · 4 Fallido (`display_error`; reintento permitido).

## Configuración

En `backend/.env` (NUNCA en git):

```
WASABIL_API_TOKEN=<token de https://app.wasabil.com/api-tokens>
# opcional (default oficial):
WASABIL_API_BASE=https://api.wasabil.com/api
```

Sin token: la previsualización funciona con datos locales y avisa; emitir se
bloquea con mensaje claro. Antes de habilitar producción, hacer una **primera
emisión real controlada** (despacho de prueba, tipo de traslado interno) — ver
la sección "Verificado contra el API real" más abajo.

## Limitación conocida (aceptada y documentada)

El API de Wasabil no expone clave de idempotencia. El diseño la compensa con el
claim + la búsqueda por referencia interna antes de re-crear, pero queda una
ventana teórica: si tras un timeout el documento tarda MÁS que el TTL del claim
(180 s) en hacerse visible en Wasabil, un reintento podría no encontrarlo y
re-crear. En la práctica la creación es de segundos; si alguna vez ocurre, el
documento extra queda visible en Wasabil (misma referencia interna) y se anula
allá (`full_annulment`).

## Formato v3 — el motivo de la referencia (folio 137, 2026-07-21)

Tras la SEGUNDA emisión real el dueño reportó que la orden de compra **seguía
saliendo impresa dos veces**, pese a los dos arreglos del v2. Era una tercera
fuente, distinta de las anteriores.

Una referencia viaja con tres datos: **tipo** (801), **folio** (1788) y **motivo**
(`reason`). Wasabil imprime la etiqueta legible del tipo — "ORDEN DE COMPRA",
derivada del código del SII — junto al folio. Nuestro `reason` decía además
"Orden de compra 1788", así que el papel mostraba:

```
ORDEN DE COMPRA  1788   Orden de compra 1788
└─ lo pone Wasabil ─┘   └─ lo mandábamos nosotros ─┘
```

Regla que quedó: **el `reason` nunca repite lo que el tipo o el folio ya imprimen.**

| Referencia | Antes | v3 |
|---|---|---|
| 801 — OC (guías y facturas) | "Orden de compra 1788" | *(sin motivo)* |
| 52 — guía facturada | "Guía de despacho 137" | *(sin motivo)* |
| 33 — anticipo descontado | "Descuento anticipo Factura 901" | "Descuento anticipo" |

En la 33 se conserva porque explica algo que el tipo no dice (que esa factura se
está descontando), pero sin repetir la palabra "Factura" ni el folio.

**VERIFICADO CONTRA EL API REAL** (borrador `issue:false`, documento 20260700200782,
2026-07-21): Wasabil **acepta** la referencia sin `reason` y la devuelve con
`"reason": null`. El campo es opcional (RazonRef del SII) y `payload_a_rest` ya lo
omitía cuando viene vacío. El mismo borrador confirmó que el camino de la app
produce un documento idéntico al aprobado (misma cuadratura, mismas referencias);
la única diferencia es `externalId` en las líneas, un id interno de trazabilidad que
Wasabil ya aceptó en las guías reales 136 y 137.

Protección de regresión: `test_referencias_sin_texto_redundante_v3` exige que
**ningún motivo contenga el folio de su propia referencia**, en los tres tipos.

## Aprendizajes de la PRIMERA EMISIÓN REAL (folio 136, 2026-07-20)

La guía salió válida, con dos defectos cosméticos corregidos en el formato **v2**:

1. **La OC salía referenciada DOS veces**: la referencia formal 801 (correcta) +
   el campo `invoice_reference`, que Wasabil TAMBIÉN imprime en el documento y
   llevaba "OC <n> · <N° despacho>". Desde v2, `invoice_reference` = **solo el
   N° de despacho interno** (sigue siendo el ancla anti doble emisión, única por
   despacho); la OC queda referenciada una sola vez (la 801 legal).
   La recuperación por referencia (reintento sin uuid) acepta AMBOS formatos:
   match exacto v2 y sufijo "· <N° despacho>" para documentos v1 ya emitidos.
2. **El nombre de línea salía feo**: "<N° parte> <descripción>" cortado a 25
   ("ROD-INF-PV351 RODILLO INF"). Desde v2, `name` = la **descripción limpia**
   (truncada a 25 si excede); el N° de parte viaja en `code`, que la guía
   imprime como código — sin duplicar ni cortar a media palabra.

## Fase B — Facturas electrónicas (DTE 33) desde Facturas y Cobranzas

En **Facturas y Cobranzas**, los dos modales ("Emitir factura" desde guía
firmada y "Factura de anticipo") parten en modo **Emitir al SII** (el folio lo
asigna el SII); un enlace permite cambiar a **registrar una factura ya emitida
a mano** con su folio (el modal clásico). El flujo SII es el mismo patrón de 2
pasos de las guías: previsualizar (receptor real de la ficha Wasabil,
referencias, descuento de anticipo, totales — sin tocar el SII) → confirmar →
sondeo hasta Emitida (folio + PDF) o Fallida (reintento seguro).

Diseño (espejo endurecido de la Fase A, con una diferencia estructural):

1. **La factura local se crea PRIMERO sin folio** (`numero_factura` NULL) +
   claim `wasabil_dte` (índice único `uq_wasabil_dte_factura`) **commiteados
   ANTES del HTTP**. El folio del SII se graba en `_finalizar_factura_emitida`
   (idempotente, locks en orden OC→factura).
2. **Adelantos DIFERIDOS**: `_persistir_factura(aplicar_adelantos=False)` — una
   factura que el SII rechaza NO movió plata; los adelantos se aplican recién
   al quedar Emitida. La vía manual (folio digitado) sigue aplicando al tiro.
3. **Referencias** (≤5, folio ≤18): 801 a la OC SIEMPRE; 52 a la guía si la
   factura viene de un despacho (folio SII de la guía electrónica si existe,
   si no el manual); 33 por cada factura de anticipo descontada (un anticipo
   con folio placeholder bloquea la emisión).
4. **Descuento del anticipo como `discount` % por línea** (greedy mayor-primero,
   precisión float completa): el API real RECHAZA líneas con precio<0 o
   cantidad<0, y no existe descuento a nivel documento. Verificado contra el
   API real en borrador: cuadratura EXACTA. Un descuento que deja el documento
   en $0 se bloquea (el SII no acepta doc sin montos).
5. **Receptor más estricto que en guías**: ficha Wasabil inexistente o sin
   giro/dirección/comuna BLOQUEA (emitir un 33 incompleto termina en rechazo).
6. `paymentMethod` (contado|credito) es OBLIGATORIO en el 33 — se deriva del
   plazo/condición de pago de la factura.
7. `invoice_reference` = `FACT-<id local>` (formato v2: es el ancla interna de
   recuperación, única por factura; Wasabil lo imprime, por eso NO lleva la OC).
8. **Eliminar factura**: con DTE emitido → 409 (se anula en Wasabil con nota de
   crédito); con emisión en curso → 409; con DTE fallido se borra junto.

Endpoints: `POST /api/wasabil/facturas/preview` · `POST /api/wasabil/facturas/emitir`
· `GET /api/wasabil/facturas/{id}/estado` · `POST /api/wasabil/facturas/{id}/reintentar`.
Tests: `tests/test_facturas_integration.py` (TestClient + MySQL + Wasabil
simulado que valida como el API real) + casos Fase B en `tests/test_service.py`.

### Qué debe cumplir un despacho para ser facturable

El selector de "Emitir factura" solo ofrece despachos con `estado='despachado'`
(cerrado) **y** `guia_firmada=1`. Una guía electrónica recién emitida NO basta:
primero viaja con la carga, el cliente la firma, se sube la foto y se cierra el
despacho. Recién ahí aparece para facturar (`_despacho_items_de_oc`).

### Endurecimiento anti doble emisión (auditoría 2026-07-21)

Un enjambre de revisión probó empíricamente (4 de 4 rondas) que **dos clics
simultáneos en Emitir creaban DOS facturas reales ante el SII**. Causa raíz y
defensas que quedaron:

1. **Snapshot viejo bajo el lock.** `_preparar_emision_factura` abre la
   transacción (SELECTs + HTTP a Wasabil) *antes* del `FOR UPDATE` de la OC, y en
   REPEATABLE READ todas las lecturas no bloqueantes siguientes servían ese
   snapshot: la re-validación no veía la factura que el request gemelo acababa de
   commitear. Ahora `emitir_factura_sii` hace `db.rollback()` **antes** de tomar el
   lock, así el snapshot nace con él (la vía manual `crear_factura` ya era inmune:
   toma el lock como primera sentencia).
2. **Candado de intención por OC** (`_emision_33_en_vuelo_de_oc`): el índice único
   `uq_wasabil_dte_factura` protege una factura YA creada, pero en este flujo cada
   request crearía una factura nueva. Con una emisión 33 de claim vigente en la
   misma OC, la segunda recibe 409. Es la única defensa en **anticipos**, que no
   tienen tope de mercadería que los frene.
3. **Puerta de una sola dirección en la UI**: tras disparar el POST, el modal ya no
   vuelve al formulario (la respuesta pudo perderse con el documento ya creado).

Otros arreglos de la misma auditoría: el botón Reintentar reventaba con 500
(`factura.cotizacion` no existe en el modelo → se usa `factura.oc_cliente.cotizacion`);
una factura cuya emisión nunca llegó a Wasabil quedaba **imborrable para siempre**
(el guard pedía esperar un resultado que ya nunca llegaría) y secuestraba el tope
facturable; Tesorería podía aplicar un adelanto a una factura en vuelo o rechazada
por el SII (rompía el diferimiento); y un descuento repartido podía dejar el
documento en centavos sin activar el bloqueo de "$0".

## Verificado contra el API real (2026-07-17) y lo que queda pendiente

CONFIRMADO con el token real (creación de un borrador `issue:false`, sin tocar
el SII): el API envuelve toda respuesta OK en `{success, status, data}`; los
listados vienen como `{items, total, lastPage}`; la ficha del cliente trae giro
y dirección ANIDADOS en `giros[]`/`addresses[]` (client.py los desenvuelve y
aplana); `dispatch_guide {dispatch_type_code}` y `references` son aceptados;
el folio de una referencia (N° OC) tiene tope de 18 caracteres (validado en el
preview). PENDIENTE de la primera emisión real: la forma exacta de la respuesta
al EMITIR (`status_id`/folio/PDF) y el endpoint de LISTADO de documentos
(`GET /documents` responde 405 — solo afecta el reintento por-referencia, que
aborta seguro). Cualquier ajuste queda contenido en `service.payload_a_rest()`
y `client.py`.

## Cómo revertir

Quitar el import + `include_router` de `wasabil_dte` en `main.py`, quitar
`wasabilAPI` de `services/api.ts` y el botón/modal de `DespachosPage.tsx`.
La tabla `wasabil_dte` puede quedar (no molesta) o eliminarse a mano.
