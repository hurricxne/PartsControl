# Peso editable por ítem — Embarques Pricing (Grupo AM) · 2026-07-17

## Problema

En Embarques Pricing el flete internacional se prorratea **por peso**
(`shipping_i = shipping_total × peso_i / Σ peso`). Hasta ahora el peso de cada
ítem se leía SIEMPRE de la cotización (`ItemCotizacion.peso_unit_lbs`) y la celda
de la UI era de solo lectura. Si el peso vino mal cargado, el reparto del flete
quedaba mal **sin forma de corregirlo** desde Contabilidad.

## Solución

Espejo exacto del override de FOB que el módulo ya tenía: el peso se puede
**sobrescribir a mano** por ítem. El override vive en la MISMA fila
`emb_pricing_item` que el FOB, se lee en `_build_inputs`, fluye por el service de
cálculo, se expone en el detalle vivo y en el snapshot, y sobrevive el ciclo
delete+reinsert de `_persist_snapshot`. El costo landed se re-prorratea al
instante y la Σ del flete queda intacta. El snapshot **congela** el peso al
cerrar.

`service.py` (prorrateo) y `compute.ts` (preview) NO cambiaron: el override solo
cambia el `peso_unit_lbs` de entrada; la lógica de reparto ya existía (con
fallback por FOB si Σ peso = 0).

## Contrato tri-estado (decisión central)

`ItemOverrideIn.fob_manual` y `peso_manual` son `Optional[bool]` (tri-estado):

- `True`  → fijar manual (con valor; peso exige `> 0`, un peso 0/negativo no es físico).
- `False` → volver a auto (solo si la fila estaba en manual).
- `None` (ausente) → **no tocar ese campo**.

Motivo: FOB y peso comparten la fila `emb_pricing_item`. Antes `fob_manual` era
`bool = False`, así que un override que tocaba SOLO el peso llegaba con
`fob_manual=False` por defecto y **revertía el FOB manual guardado sin aviso**
(y el caso simétrico). Con el tri-estado los dos overrides son 100%
independientes. El frontend mergea sobre el override existente
(`{ ...o[id], ... }`) para no pisar el otro campo.

## Cambios

- `backend/embarques_pricing/models.py` — columna `peso_origen String(20) default "auto"`.
- `backend/embarques_pricing/init_db.py` — **NUEVO**, migración idempotente
  (crea tablas faltantes con `create_all(checkfirst=True)` + `ALTER TABLE` aditivo
  para `peso_origen`). Patrón de `tesoreria/init_db.py`.
- `backend/embarques_pricing/router.py` — `ItemOverrideIn` (peso + tri-estado),
  `_build_inputs` (lee override de peso), `_snapshot_items` y `_compute_detail`
  (exponen `peso_default` + `peso_origen`, con `or "auto"` para filas legacy),
  `_persist_snapshot` (persiste `peso_origen`), bloque de overrides
  (guarda FOB y/o peso, ramas independientes por identidad `is True/is False`).
- `frontend-src/src/embarques-pricing/types.ts` — `PesoOrigen`, `PricingItem`
  (+`peso_default`/`peso_origen`), `ItemOverride` (peso + `fob_manual` opcional).
- `frontend-src/src/embarques-pricing/EmbarquesPricingPage.tsx` — celda de peso
  editable (espejo de la celda FOB): input + badge `manual`/`de cotización` +
  botón "Volver al peso de la cotización"; bloqueada si cerrado. Handlers que
  mergean el override; `pesoEfectivo` para el preview en vivo.
- `service.py` y `compute.ts` — **sin cambios**.

## Migración / deploy

```bash
cd backend && python -m embarques_pricing.init_db
```

Idempotente. Correrlo **antes** de reiniciar el backend con el código nuevo.
Filas viejas quedan con `peso_origen` NULL y el backend las trata como `auto`
(`s.peso_origen or "auto"`); la API nunca devuelve `null` en ese campo.

## Casos de test (paso 12 de `tests/test_integration.py`)

1. **12a** Override de peso re-prorratea el flete; **Σ shipping intacta**.
2. **12b** Quitar override (`peso_manual=False`) → vuelve al peso de la cotización.
3. **12c** Manual ≤ 0 → auto (no pisa la cotización, ni en el save ni en `_build_inputs`).
4. **12d** FOB manual + editar SOLO peso NO revierte el FOB (contrato tri-estado). **CRÍTICO**.
5. **12e** Cerrado congela el peso manual (sale del snapshot); reabrir lo mantiene; `peso_default == congelado`.
6. **12f** Todos los pesos de cotización en 0 → fallback por FOB sigue; un override de peso>0 domina el prorrateo.

Verificados además fuera de la suite: PUT solo-encabezado no pierde el peso
manual (los overrides no corren, `_build_inputs` lo relee del snapshot) y filas
legacy con `peso_origen` NULL se leen como `auto`.

## Verificación

- `python -m embarques_pricing.init_db` corrido 2× en local (idempotente OK, columna presente).
- `pytest embarques_pricing/tests` → 9 verdes.
- Suite completa (`tests_contabilidad wasabil_dte/tests compras_contab/tests tesoreria/tests embarques_pricing/tests routers/tests`) → 60 verdes.
- `npx tsc --noEmit` y `npm run build` → limpios.
