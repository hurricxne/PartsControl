# MonzaParts · Compras a proveedores chilenos (Fase 8 del espejo Grupo AM)

**Fecha:** 2026-07-28 · **Decisión del dueño:** MonzaParts SÍ compra a proveedores
chilenos. · **Espejo de:** `backend/recepcion_nacional/` + `docs/plan-compras-nacionales-2026-07-18.md`
(Grupo AM), especificado por grupo de expertos con archivo:línea antes de construir.

## La idea en una frase

Una compra **nacional** llega en camión con la guía del proveedor — **sin embarque, sin
aduana**: la OC se marca `nacional`, la mercadería se registra en un libro de recepción
propio, al cerrar pasa a bodega **despachable** (capada por lo recibido), y el costo se
anota por ítem en Compras/CxP para pagarse por Tesorería como cualquier compra.

## Las piezas

### 1. OC de proveedor con `tipo_origen` (`monza_models.py` + abastecimiento)

- `monza_oc_proveedor.tipo_origen` = `'internacional'` (default histórico) | `'nacional'`.
  Fuente **única** del camino físico — no se deriva de país ni moneda. Toggle al comprar.
- **Guard anti-embarque en las DOS entradas del pipeline**: `preparar` y `crear_embarque`
  rechazan ítems de OC nacional (400) — un ítem nacional jamás entra al flujo de embarques.

### 2. Libro de recepción nacional (`backend/monza_recepcion_nacional/`)

- `POST /api/monza/recepcion-nacional` — registrar la entrega (guía del proveedor, fecha,
  documento, líneas con cantidad y estado; `cerrar=true` cierra en la misma transacción).
- Al **cerrar**: ítems utilizables con cantidad > 0 pasan `comprado → en_bodega` bajo el
  lock canónico (id ASC, `FOR UPDATE`); jamás retrocede un `despachado`.
- **Anular con reversa direccional**: abierta se borra; cerrada exige que nada cuelgue —
  **409 si hay despachos** que consumen esas unidades o **409 si hay costeo** en CxP
  (se anulan primero); la reversa devuelve `en_bodega → comprado` solo si corresponde.
- Retry de deadlock (1213/1205), candado `require_empresa("automotriz")`, pendientes por
  recibir = vendido − Σ recibido.
- **Adaptación estructural** (documentada en el código): Monza no tiene tabla de
  asignación ítem↔OC — el vínculo es directo (`MonzaCotizacionItem.oc_proveedor_id`), así
  que pertenencia y guards validan por esa columna y la tabla de líneas no arrastra FK inexistente.

### 3. Tope físico de despachos (enganche a la Fase 2)

`_qty_recibida_utilizable` de despachos suma ahora **dos fuentes**: recepciones de
embarque + recepciones **nacionales** cerradas (mismo vocabulario de estados utilizables,
`float()` en ambos operandos). Lo recibido nacional queda despachable con el mismo tope
`min(vendido, recibido) − despachado` de siempre.

### 4. Costeo por ítem en Compras/CxP (`monza_compras_contab`)

- Tabla `monza_cont_compra_item`: una compra nacional se costea **por ítem de venta**, con
  tope **"disponible a costear"** = recibido nacional cerrado − ya costeado, decidido
  **bajo lock**. Catálogo `GET /compras-contab/oc-nacionales` (OCs nacionales con sus
  ítems costeables y el disponible ya calculado por el backend).
- Convive con el costeo overlay de embarques sin duplicar; cuenta de Existencias del plan
  del módulo; el pago sigue el flujo CxP → egreso → Tesorería de siempre.

### 5. Pantallas (idioma visual Monza)

- **Abastecimiento**: toggle Nacional/Internacional al crear la OC + badges.
- **Seguimiento**: CTA "Registrar entrega" en ítems de OC nacional → modal con líneas,
  cantidades, estado de recepción, guía del proveedor y cierre inmediato.
- **Compras/CxP**: modo de compra nacional por ítem con el disponible a costear visible.

## Pruebas (todas en el gate de pytest)

- `monza_recepcion_nacional/tests/test_integration.py` — 37 checks (flujo completo,
  guards, anulación con 409 por despacho/costeo, scope de empresa invertido).
- `monza_tests/test_recepcion_nacional_tope.py` — 32 checks del tope físico con
  escenarios solo-nacional, **mixtos embarque+nacional**, reversa y re-recepción.
- `monza_compras_contab/tests/test_nacional.py` — 38 checks del costeo (tope bajo lock,
  anulaciones en ambos sentidos, convivencia con overlay embarques).
- `monza_tests/test_oc_tipo_origen.py` — saneo del tipo y guard anti-embarque en las 2 entradas.

## Despliegue (orden OBLIGATORIO, antes de reiniciar)

```bash
cd backend && python -m monza_recepcion_nacional.init_db   # 1° tablas + columna tipo_origen
python -m monza_compras_contab.init_db                      # 2° tabla monza_cont_compra_item
```

Ambos idempotentes. Requiere `npm run build` del frontend.
