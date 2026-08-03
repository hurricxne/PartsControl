# MonzaParts · Flujo Bodega → Despachos (Fase 2 del espejo Grupo AM)

**Fecha:** 2026-07-23 · **Referencia:** `docs/flujo-bodega-despachos.md` (Grupo AM) y
`docs/plan-espejo-monza-2026-07-23.md` (Fase 2).

## La regla de oro (igual que en Grupo AM)

**Lo que sale de bodega nunca puede superar lo que llegó.** La cantidad RECIBIDA
(registrada al cerrar la recepción) gobierna cuánto se puede despachar:

```
despachable = min(vendido, recibido en recepciones CERRADAS) − ya reservado/despachado
```

Antes de esta fase, Monza registraba `qty_recibida` pero **nadie la usaba**: el despacho
salía siempre por la cantidad vendida completa, aunque hubiera llegado menos.

## Qué cambió

### Bodega (`monza_router_bodega.py`)

- **Cierre PARTICIONADO**: una llegada parcial (`faltante` con cantidad > 0, o `completo`
  con menos unidades que lo vendido) deja **lo recibido en bodega, despachable**, y
  reclama **solo el faltante real**. Antes la línea entera caía a reclamo y lo que sí
  llegó quedaba indespachable.
- **Anti reclamo fantasma**: el faltante se calcula contra lo recibido ACUMULADO en
  todas las recepciones cerradas (una línea repartida en 2 embarques, o una reposición
  que completa la línea, no genera reclamos duplicados).
- **Cierre forzado** (`forzar=true`): permite cerrar con ítems sin marcar; quedan como
  reclamo `no_llego` **trazable** (antes quedaban atascados en `embarcado` para siempre).
- **Guards**: no se marca sobre una recepción cerrada; cantidades negativas se rechazan;
  `faltante` con recibido ≥ vendido se rechaza (si llegó todo, es `completo`); el ítem
  debe pertenecer al embarque de la recepción; y un embarque con recepción CERRADA no se
  re-recepciona (sumaría sus cantidades de nuevo al tope — una reposición llega en un
  embarque nuevo). Locks `FOR UPDATE` en la recepción y el embarque contra dobles clics.
- Compatibilidad con datos legados: `qty_recibida` NULL se interpreta por el estado
  (completo/sobrante = todo; dañado utilizable = cantidad − dañada; faltante = 0).

### Despachos (`monza_router_despachos.py`)

- **Ciclo de vida** (espejo GA): `en_preparacion` (borrador) → **Confirmar** (cerrar) →
  la mercadería queda registrada como salida. **Anular** solo en borrador, y devuelve el
  cupo. Los despachos legados (nacidos `despachado`) no se tocan.
- **Despacho PARCIAL**: cantidad por ítem, topeada al disponible físico. La línea voltea
  a `despachado` **solo cuando los despachos CERRADOS cubren la cantidad vendida** (un
  borrador no cuenta); la venta voltea solo cuando todas sus líneas lo hacen.
- **Flujo guía-primero**: el despacho se crea SIN N° de guía; transportista, N° de guía
  y N° de expedición se completan después con el PUT de cabecera (enganche listo para la
  guía electrónica SII de la Fase 5).
- **Concurrencia** (regla de la casa): `FOR UPDATE + populate_existing` en ítems y
  despacho, orden de lock canónico (despacho → ítems id ASC), retry del correlativo
  `DSP-AAAA-####` ante choque del índice único, retry 1213/1205 al cerrar.

### Endpoints nuevos

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/monza/despachos/entidades/{id}` | detalle con ítems |
| PUT | `/api/monza/despachos/entidades/{id}` | editar cabecera (guía/transportista/expedición…) |
| POST | `/api/monza/despachos/entidades/{id}/cerrar` | confirmar el borrador (la salida real) |
| DELETE | `/api/monza/despachos/entidades/{id}` | anular el borrador (devuelve cupo) |

`POST /crear` acepta ahora `items: [{item_id, qty}]` (parcial); la vía legada
`item_ids: [...]` sigue funcionando y despacha el **disponible** (no lo vendido).
`GET /listos` expone `qty_disponible` por ítem y omite ítems sin cupo.

### Frontend

- **Despachos**: modal de crear con cantidad editable por ítem (topeada al disponible),
  guía opcional, y panel **"Despachos en curso"** con Confirmar / Anular / Editar.
- **Bodega**: campo "Recibido" por ítem (llegadas parciales) y cierre con pendientes
  (confirmación + forzado).

## Pruebas

`backend/monza_tests/test_bodega_despachos_flujo.py` — 35 checks e2e contra la BD real
(datos MARCADOS + limpieza total, verificación con sesión nueva): partición, tope físico,
ciclo de vida completo, cierre forzado y reposición sin reclamo fantasma.

```bash
cd backend && ./venv/bin/python -m pytest monza_tests/test_bodega_despachos_flujo.py -q
```

## Despliegue

**Antes de reiniciar el backend** (idempotente):

```bash
cd backend && python -m migrations.monza_despachos_ciclo_vida
```

Agrega `fecha_despacho` y `numero_expedicion` a `monza_despachos` y el índice único del
correlativo (si detecta números duplicados preexistentes, avisa y no lo crea).
Requiere `npm run build` del frontend (cambian 3 archivos de UI).
