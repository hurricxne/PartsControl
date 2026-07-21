# N° AWB / BL escribible, visible y buscable en TODOS los embarques

Fecha: 2026-07-17 · Rama: `feature/adelantos-clientes`

## Problema (negocio)

La columna `embarques.awb` NO guarda el número de la guía aérea: guarda el
**nombre del archivo** del documento AWB/BL adjunto (lo sube Logística al cerrar el
pre-embarque; el `DocUploadField` es obligatorio). Consecuencias:

- Bodega mostraba `AWB: {nombre_de_archivo}` en vez del número real.
- El buscador de Embarques Pricing matcheaba contra ese filename.
- Embarques (Logística) no tenía buscador de texto.
- **No existía ningún campo donde escribir el número** de la guía.

## Solución

Se separa el identificador de documento del número. Columna nueva
`embarques.awb_numero` (`VARCHAR(100)`, índice `ix_embarques_awb_numero`) = el
**número universal**, escrito a mano, editable y buscable. La columna `awb` queda
**intacta** (sigue siendo el archivo adjunto, con su obligatoriedad al cerrar).

Semántica final:

| Campo | Qué es | Obligatorio | Editable | Buscable |
|-------|--------|-------------|----------|----------|
| `awb` | Nombre del archivo adjunto AWB/BL | Sí (al cerrar por el modal con adjunto) | vía docs | (histórico) |
| `awb_numero` | N° de la guía aérea / BL escrito a mano | No | Sí (ex-post) | Sí |

## Qué cambia por archivo (aditivo y mínimo)

Backend:
- `models/models.py` — `Embarque.awb_numero = Column(String(100), index=True)`.
- `migrate_awb_numero.py` (NUEVO, idempotente) — `ALTER TABLE embarques ADD COLUMN awb_numero` + `CREATE INDEX ix_embarques_awb_numero` si faltan.
- `routers/compras.py` — `CerrarPreEmbarqueBody.awb_numero` (opcional, `max_length=100`); `cerrar_pre_embarque` persiste `awb_numero`; `EmbarqueUpdate.awb_numero` (opcional, `max_length=100`; el PUT genérico ya lo persiste solo); `listar_embarques` y `get_embarque` exponen `awb_numero`. El `max_length=100` empareja la columna `VARCHAR(100)`: un texto más largo rebota como **422** de validación y NO como **500** (`DataError 1406`).
- `routers/bodega.py` — 3 serializers de embarque exponen `awb_numero`.
- `embarques_pricing/router.py` — detalle y lista exponen `awb_numero`; el haystack de búsqueda ahora incluye `awb_numero`.

Frontend:
- `pages/PreEmbarquesPage.tsx` — `GenEmbarqueModal` (cierre con adjuntos): campo de texto "N° AWB / BL" (opcional) que viaja como `awb_numero`; el adjunto obligatorio sigue igual. `CerrarEmbarqueModal` (cierre de texto): espeja su campo AWB también a `awb_numero` para que esos embarques también queden buscables.
- `pages/EmbarquesPage.tsx` — badge `AWB {n°}` en la tarjeta; `InlineEdit` "N° AWB / BL" (edición ex-post); **buscador de texto nuevo** (N° de embarque, N° AWB, forwarder) client-side.
- `pages/BodegaPage.tsx` — muestra el `N° AWB` real cuando existe (activos e historial); el fallback `AWB: {filename}` se conserva.
- `embarques-pricing/types.ts` + `EmbarquesPricingPage.tsx` — tipos y badges; el buscador ya pega al backend (que ahora matchea `awb_numero`).

## Borde conocido

Vaciar por completo `awb_numero` desde el `InlineEdit` es **no-op** (el envío de `""`
se convierte en `null` y `exclude_none=True` lo descarta), idéntico al comportamiento
de Forwarder/Notas. Para corregir el número se tipea el nuevo (sobreescribe).

## Deploy (ORDEN CRÍTICO)

1. `git pull`
2. `cd backend && ./venv/bin/python migrate_awb_numero.py`  ← **ANTES de reiniciar el backend** (el modelo ya declara la columna).
3. Reiniciar el backend.
4. `cd frontend-src && npm run build` y publicar.

La migración es **idempotente**: correrla 2 veces imprime "ya existe" y no falla.

## Verificación

- `backend`: `./venv/bin/python routers/tests/test_awb_numero.py` (14 checks, incluye N° >100 → 422) + suite completa `pytest tests_contabilidad wasabil_dte/tests compras_contab/tests tesoreria/tests embarques_pricing/tests routers/tests` → 61 passed.
- `frontend-src`: `npx tsc --noEmit` → 0 errores.
