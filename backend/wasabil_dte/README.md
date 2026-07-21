# Módulo Wasabil DTE — Guías de despacho electrónicas (SII tipo 52)

Emisión de la guía de despacho electrónica **directo al SII vía Wasabil**
(facturador electrónico de GRUPO AM SPA 77.977.813-4) desde el flujo de
Despachos de Logística — sin portal MiPyme, sin firma manual.

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
