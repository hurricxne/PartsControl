# Embarques Pricing (Costo Landed) — Backend

Módulo **aislado** de Contabilidad que calcula el costo landed (CIF + gastos
locales) por ítem de cada embarque que crea Logística. Réplica de la lógica del
Sheet de GRUPO AM (creadores de embarque).

## Qué hace

Por cada ítem de un embarque calcula:

```
FOB Total (ME)  = cantidad × FOB unit            (moneda extranjera: USD/EUR)
FOB CLP         = FOB Total × TC
Shipping CLP    = shipping_total × (peso_i / Σ peso)     ← prorrateo por PESO
CIF CLP         = FOB CLP + Shipping CLP
Gastos Loc CLP  = total_gastos × (CIF_i / Σ CIF)         ← prorrateo por CIF
Costo Total CLP = CIF CLP + Gastos Loc CLP
Costo Unit CLP  = Costo Total / cantidad
```

Los gastos locales son **6 líneas predeterminadas fijas**: Desconsolidación,
Almacenaje, Agencia de Aduana, Arancel/Derechos, Otros, IVA Importación. Cada
línea lleva Monto Neto, IVA, Total Bruto, N° Factura, **Fecha de factura** y
**Banco**. `total_gastos` capitaliza SOLO los netos de Desconsolidación,
Almacenaje, Agencia, Arancel y Otros. **Excluye** el IVA y el IVA Importación
(recuperables = crédito fiscal, no son costo). El **Arancel** capitaliza pero
**sin IVA** (igual que el IVA Importación).

El **FOB** por ítem sale, en orden: factura del proveedor (`FacturaProveedorItem.
unit_price_usd`, por par ítem+OC) → precio de la cotización → 0; y se puede
sobrescribir a mano.

El **peso** por ítem sale de la cotización (`ItemCotizacion.peso_unit_lbs`) y se
puede **sobrescribir a mano** (mismo patrón que el FOB): si el peso vino mal,
Contabilidad lo corrige y el flete se re-prorratea (el shipping se reparte por
peso). El snapshot congela el peso al cerrar. La columna `emb_pricing_item.
peso_origen` (`auto`|`manual`) guarda el origen. FOB y peso comparten la MISMA
fila de override y son **independientes**: el payload usa flags tri-estado
(`fob_manual`/`peso_manual` como `true`/`false`/ausente), así editar solo el peso
no revierte un FOB manual y viceversa.

## Flete por tipo de embarque

`flete_en_me` = el flete viene prepagado por el proveedor en moneda extranjera
(`shipping_total = shipping_me × TC`); si es CLP, se usa `shipping_clp` directo.
Default en `integration.FLETE_EN_ME_DEFAULT` (editable en la UI):

| Tipo | Flete | Default `flete_en_me` |
|---|---|---|
| normal (LATAM) | CLP en Chile **o** prepagado por proveedor | `False` (elegible) |
| courier (DHL)  | CLP en Chile **o** prepagado por proveedor | `False` (elegible) |
| baukat (Europa)| siempre prepagado por proveedor (EUR)      | `True` |
| fastmark       | siempre CLP local                          | `False` |

## Integración con Logística (NO invasiva)

**No se toca `routers/compras.py` ni el código de Logística.** El módulo solo LEE
la tabla `embarques`: la lista (`GET /api/embarques-pricing`) muestra TODOS los
embarques (estado `sin_pricing` hasta abrirlos), así que cada embarque "aparece"
en Contabilidad apenas Logística lo crea. El registro de pricing + sus 6 gastos
se crean **de forma diferida** la primera vez que Contabilidad abre el embarque
(`GET /{id}` → `_get_or_create_pricing` → `ensure_pricing_for_embarque`).

> Se probó un hook en `cerrar_pre_embarque` para crear el pricing al embarcar,
> pero se **revirtió** a pedido del dueño (preferencia: no modificar el código
> de Logística). La creación diferida cumple el mismo objetivo sin tocar Compras.

Las FKs del módulo usan ON DELETE CASCADE/SET NULL para no bloquear borrados de
Logística. El detalle expone los **documentos** del embarque (AWB, factura
comercial, packing list, certificado de origen, otros) para trazabilidad, y un
**correlativo** (= `emb_pricing.id`, parte de 1).

> El buscador (`GET ?q=`) matchea contra N° de embarque, forwarder, el nombre del
> archivo AWB adjunto (`awb`) y, desde 2026-07-17, el **N° AWB/BL escrito a mano**
> (`awb_numero`, columna nueva independiente del adjunto).

## Archivos

| Archivo | Rol |
|---|---|
| `service.py` | Cálculo landed (función pura, sin DB) |
| `integration.py` | Creación/seed del pricing (creación diferida desde el router) |
| `models.py` | 3 tablas nuevas: `emb_pricing`, `emb_pricing_gasto`, `emb_pricing_item` |
| `init_db.py` | Migración idempotente: crea tablas faltantes + agrega `emb_pricing_item.peso_origen` |
| `router.py` | API REST `/api/embarques-pricing` |
| `tests/test_service.py` | Tests del cálculo + detect_tipo + flete defaults (8) |
| `tests/test_integration.py` | Flujo completo contra la DB (incluye paso 12: peso editable — re-prorrateo, quitar override, manual ≤ 0, FOB+peso independientes, cerrado congela, fallback por FOB; siembra y limpia) |

## Endpoints

- `GET /api/embarques-pricing` — lista todos los embarques + estado de pricing
- `GET /api/embarques-pricing/{embarque_id}` — detalle (crea el pricing si no existe)
- `PUT /api/embarques-pricing/{embarque_id}` — guarda TC/flete/gastos/overrides y recalcula
- `POST /api/embarques-pricing/{embarque_id}/cerrar` — congela el costo
- `POST /api/embarques-pricing/{embarque_id}/reabrir` — vuelve a editable

## Correr los tests

```bash
cd backend
./venv/bin/python embarques_pricing/tests/test_service.py
./venv/bin/python embarques_pricing/tests/test_integration.py   # requiere MySQL local
```

## Deploy (una vez por entorno)

```bash
cd backend && python -m embarques_pricing.init_db
```

Agrega la columna `emb_pricing_item.peso_origen` de forma **idempotente** (crea
también las 3 tablas si faltan). Correrlo **ANTES** de reiniciar el backend con el
código nuevo: el modelo ya declara la columna, así que un backend nuevo la
necesita al leer/escribir el snapshot; sin la migración, un backend viejo no la
ve pero uno nuevo falla. Es seguro correrlo varias veces (la 2ª imprime "ya
existe").

## Cómo deshacer (revertir 100%)

1. En `backend/main.py` quitar las 2 líneas marcadas con `Embarques Pricing`:
   - el `import ... embarques_pricing_router`
   - el `app.include_router(embarques_pricing_router, ...)`
2. Borrar la carpeta `backend/embarques_pricing/`.
3. (Opcional) Borrar las 3 tablas: `DROP TABLE emb_pricing_item, emb_pricing_gasto, emb_pricing;`
   Son aditivas: dejarlas no afecta nada.
4. En el frontend, revertir `src/App.tsx` al import del mockup y borrar `src/embarques-pricing/`.

> `routers/compras.py` NO se toca (la creación del pricing es diferida, desde el
> router de este módulo). No hay nada que revertir ahí.

## Pendiente / mejora futura

- **TC por orden (FastMark multi-OC):** hoy el embarque usa un TC único (cubre
  Normal/Courier/Baukat). El TC distinto por orden de FastMark se puede agregar
  guardando un override de TC por ítem.
