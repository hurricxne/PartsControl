# TC congelado en la cotización (freeze-forward) — Grupo AM

**Fecha:** 2026-07-23 · **Alcance:** solo Grupo AM (minería). MonzaParts ya congela por su cuenta.

## El problema

El precio de venta de una cotización se calcula con **8 parámetros globales** que viven en
una única fila de configuración (`configuracion_cotizador`, id=1):

| Parámetro | Efecto en el precio |
|---|---|
| `tipo_cambio_usd` | convierte exwork USD → CLP y escala shipping y AWB |
| `costo_shipping_usd_kg` | flete por kilo |
| `adicionales_shipping_usd` | AWB fijo, prorrateado por peso |
| `costo_agencia_pct` | % de agencia sobre CIF |
| `costo_agencia_minimo_clp` | piso de agencia |
| `desconsolidado_clp` | gasto fijo |
| `bodegaje_clp` | gasto fijo |
| `margen_venta_pct` | margen de venta |

La cotización **no guardaba** con qué parámetros se calculó. Resultado: cada vez que se
actualizaba el dólar (o cualquier parámetro), **todas** las cotizaciones históricas —incluidas
las ya vendidas— cambiaban de total. Ese era el origen de los montos "fantasma" en el
*por facturar* de Ventas—Contabilidad y hacía poco confiables los márgenes históricos.

## La solución: congelar una "foto" al Cierre de Venta

- La tabla `cotizaciones` gana dos columnas (nullable):
  - `pricing_snapshot` — JSON con los 8 parámetros, tomados al cerrar la venta.
  - `pricing_snapshot_at` — cuándo se tomó la foto.
- Al **Cierre de Venta** (`POST /api/compras/oc-cliente`, cuando nace la OC del cliente) se
  estampa la foto con el config global de ese instante. Idempotente: solo se estampa si aún
  no hay foto (un reintento del cierre no la re-pisa).
- El motor de precios usa la foto **si existe**; si no (borrador o venta antigua), usa el
  config global vivo. Un borrador **sí** debe reflejar el dólar de hoy: eso es correcto.

```
Cotización en borrador ─────────► usa dólar global de HOY (se re-precia, deseable)
         │
   Cierre de Venta (nace la OC) ─► se saca la FOTO (pricing_snapshot)
         │
Cotización vendida ─────────────► usa la FOTO para siempre (no se mueve nunca más)
```

### Decisión de negocio: congelar solo de aquí en adelante

Las ventas **ya cerradas antes** de esta función quedan con `pricing_snapshot = NULL` y
**siguen comportándose exactamente como hoy** (usan el config global). La migración **no**
hace backfill. Se descartó reconstruir el dólar histórico (el cálculo es de dos pasadas y no
lineal → deducción frágil).

Los documentos tributarios ya emitidos (guías 136/137, factura 116) **no se ven afectados**:
la factura guarda sus propios montos congelados en su tabla, aparte del motor de precios.

## Piezas del cambio

| Archivo | Qué |
|---|---|
| `backend/models/models.py` | `Cotizacion.pricing_snapshot` + `pricing_snapshot_at` |
| `backend/services/pricing_service.py` | `CLAVES_PRICING`, `snapshot_desde_config()`, `config_efectivo()` |
| `backend/routers/compras.py` | estampa la foto en `crear_oc_cliente` (rama de OC nueva) |
| `backend/routers/{ventas,contabilidad,cotizador,compras}.py` | los 10 llamadores del motor + 3 generadores de documento formal usan `config_efectivo` |
| `backend/routers/cotizador.py` (`get_editor`) | devuelve el config **efectivo** (no el global) para que el editor del navegador —que recalcula la tabla en el cliente— muestre en pantalla lo mismo que el PDF/Excel congelado |
| `backend/migrations/cotizacion_pricing_snapshot.py` | ALTER TABLE idempotente (sin backfill) |

`config_efectivo(snapshot_json, config_global)` es a prueba de datos malos: JSON corrupto,
no-dict o vacío → cae al config global (nunca revienta el cálculo de un precio). Mergea la
foto **sobre** el global, así una llave futura que aún no se congelaba cae al global.

## Pruebas

- `backend/tests_contabilidad/test_tc_congelado.py` — unidad de los helpers + prueba
  **empírica** con el motor real: una venta congelada a 940 no se mueve aunque el dólar
  global suba a 1020; sin congelar, sí se mueve (el bug). Cubre los 8 parámetros.
- `backend/tests_contabilidad/test_cierre_congela_snapshot.py` — integración contra la BD
  real (dato MARCADO + limpieza total): el Cierre de Venta estampa la foto = config global
  del momento, es idempotente, y **no re-pisa** una foto ya existente (guard).
- `backend/tests_contabilidad/test_editor_config_congelado.py` — el editor
  (`GET /api/cotizador/{id}`) devuelve el config congelado para una venta cerrada y el
  global para un borrador (blinda la coherencia pantalla ↔ documento).

```bash
cd backend && ./venv/bin/python -m pytest tests_contabilidad/test_tc_congelado.py \
    tests_contabilidad/test_cierre_congela_snapshot.py \
    tests_contabilidad/test_editor_config_congelado.py -q
```

## Despliegue

**Correr la migración ANTES de reiniciar el backend** (agrega las 2 columnas; idempotente):

```bash
cd backend && ./venv/bin/python -m migrations.cotizacion_pricing_snapshot
```

Sin datos que migrar: las cotizaciones existentes quedan con foto NULL y siguen igual.
El congelado empieza a aplicar en la **próxima** venta que se cierre.
