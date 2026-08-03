# MonzaParts · Fase 4 del espejo: la plata por venta + tableros de Despachos (G15)

**Fecha:** 2026-07-28 · **Espejo de:** el feature G15 de Grupo AM (`docs/ventas-contab` /
`routers/contabilidad.py` + `routers/despachos.py`), especificado empíricamente por un
grupo de expertos antes de construir (nada asumido de memoria).

## G15a — El desglose de la plata por venta (Contabilidad)

### La regla de oro (innegociable)

```
por_facturar = Σ (cantidad − facturada) × precio CONGELADO del ítem × (1 + IVA)
```

**Base FÍSICA, jamás** `total_vivo − Σ brutos de facturas`: esa resta deja $1–3 fantasma
de polvo de redondeo por tandas (hallazgo HIGH del enjambre G15 de GA). Con base física,
una venta 100% facturada da **0 por construcción**. En Monza la fórmula es más simple que
en GA: los precios ya están congelados por ítem (`precio_unitario_clp`) — sin pricing
vivo ni prorrateo. El IVA sale de `iva_rate_de(cot, cfg)` (nunca 1.19 fijo: cubre tasa
congelada por venta).

### Qué expone el backend (`monza_contabilidad`)

- **Listado** `GET /ventas`: cada fila gana `por_facturar_clp` — calculado **en lote**
  (una query de cantidades facturadas para todo el listado, anti-muro).
- **Detalle** `GET /ventas/{id}`: `resumen` gana `por_facturar_clp`,
  `anticipo_por_descontar_clp` (0.0 hasta la Fase 7 — el campo existe por compatibilidad
  con la UI y la vía B futura) y `mercaderia_pendiente_clp` (cifra **autoritativa**: el
  frontend no debe reconstruirla).
- Helper puro `mercaderia_pendiente_bruto(items, qty_fact, iva_rate)` en `service.py`
  (testeable sin BD); línea sin precio congelado pesa $0 **con warning** en el log.

### Pantalla (MonzaVentasContabPage)

Barra de avance segmentada (cobrado / facturado sin cobrar / por facturar), sección
"Facturas de la venta" expandible con semáforo de vencimiento, "Por facturar" agrupado
por estado logístico real (despachable ya / en tránsito / por comprar / reclamo / otro),
y tabla de ítems plegada sobre 10 con buscador — todo en el idioma visual Monza.

## G15b — Tableros de avance de Despachos

- `GET /api/monza/despachos/avance?tab=listas|en_curso|historial&q=` — tarjeta por venta:
  buckets del pipeline (6, siempre presentes), % de avance, cascada de estado
  (pendiente→listo→parcial→completado) y **días hábiles restantes** (feriados chilenos,
  helper propio `monza_fechas.py` — copiado de GA, no importado, para no acoplar
  empresas). `historial` = ventas con despacho **cerrado** (una venta parcial aparece
  aunque siga "vendida" — lección G16).
- `GET /avance/{venta}` — detalle: ítems (bucket, despachado por cierres, disponible
  físico), despachos y embarques que trajeron cada ítem.
- `GET /counts` — contadores en una query.
- **Notificaciones**: al cerrar la recepción, "Venta lista para despacho" (todos los
  ítems en bodega) o "Plazo crítico" (parcial a ≤3 días hábiles del compromiso), con
  anti-duplicado (no repite una notificación igual sin leer).
- `GET /api/monza/bodega/embarques/historial` — embarques ya recepcionados (criterio:
  recepción CERRADA, no el estado del embarque), con quién y cuándo cerró.

## Pruebas

- `monza_tests/test_por_facturar_fisico.py` — la regla de oro e2e con números:
  35.700 → factura parcial → 28.560 → factura del saldo → **0 exacto**.
- `monza_tests/test_avance_despachos.py` — tabs, buckets, counts, notificación con
  anti-duplicado, historial de embarques, detalle de avance.

## Despliegue

Sin migraciones nuevas de esta fase (usa columnas existentes). Requiere `npm run build`.
Las migraciones de las fases previas del espejo (F2/F3/paridad) están en el checklist.
