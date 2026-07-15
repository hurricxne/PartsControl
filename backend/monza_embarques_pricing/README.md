# Embarques Pricing — MonzaParts (costo landed)

Módulo **aislado** y **solo para MonzaParts** (`empresa = "automotriz"`). Es el espejo del
módulo de Grupo AM (`backend/embarques_pricing/`): misma metodología de **costo landed**
(shipping prorrateado por peso, gastos locales prorrateados por CIF), apuntando a las
tablas `monza_*`.

## Qué hace

Calcula, por cada ítem de un embarque, cuánto **cuesta puesto en bodega** (landed cost)
sumando FOB, flete y gastos de internación, todo llevado a CLP:

```
FOB Total (ME)  = cantidad × FOB unit                       (moneda extranjera: USD/EUR)
FOB CLP         = FOB Total × TC
Shipping CLP    = flete_total × (peso_i / Σ peso)           ← prorrateo por PESO
CIF CLP         = FOB CLP + Shipping CLP
Gastos Loc CLP  = total_gastos × (CIF_i / Σ CIF)            ← prorrateo por CIF
Costo Total CLP = CIF CLP + Gastos Loc CLP
Costo Unit CLP  = Costo Total / cantidad
```

`total_gastos` capitaliza los netos de **Desconsolidación, Almacenaje, Agencia de Aduana,
Arancel/Derechos y Otros**. **Excluye** el IVA y el **IVA Importación** (son recuperables,
no son costo).

## Integración con Logística (no invasiva)

- Lee los embarques que crea **Logística** (`monza_embarques` + `monza_embarque_items` →
  `monza_cotizacion_items`). No los modifica.
- El registro de pricing se crea **diferido** la primera vez que Contabilidad abre el
  embarque (idempotente, a prueba de carreras por el `UNIQUE(embarque_id)`).
- **FOB y peso por ítem**: el _default_ sale del ítem de cotización (`costo` / `peso_kg`).
  El dueño **sube a mano** el FOB real, el flete y los gastos (igual que en Grupo AM).

## Datos (3 tablas nuevas, aditivas)

| Tabla | Rol |
|---|---|
| `monza_emb_pricing` | 1 fila por embarque: TC, flete, estado (`borrador`/`calculado`/`cerrado`). |
| `monza_emb_pricing_gasto` | 6 líneas canónicas de **GASTOS LOCALES** (neto, IVA, factura, banco). |
| `monza_emb_pricing_item` | **Snapshot** del costo landed por ítem (se congela al calcular/cerrar → auditable). |

No tocan ninguna tabla existente. Dinero en `Numeric` (decimal exacto).
`monza_emb_pricing.embarque_id` tiene FK con **ON DELETE CASCADE** a `monza_embarques`.

## API — `prefix /api/monza/embarques-pricing` (candado `automotriz`)

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | Lista todos los embarques con estado y costo total de su pricing. `?q=` filtra por número/forwarder/AWB. |
| GET | `/{embarque_id}` | Detalle; **auto-crea** el pricing + 6 gastos si falta. Devuelve encabezado, gastos, ítems calculados y totales. |
| PUT | `/{embarque_id}` | Guarda encabezado + gastos (6 canónicas) + overrides de FOB manual; recalcula y persiste el snapshot. `409` si está cerrado. |
| POST | `/{embarque_id}/cerrar` | Congela el pricing (`cerrado`). Exige TC > 0 y costo landed > 0. |
| POST | `/{embarque_id}/reabrir` | Vuelve a `calculado`/`borrador` para volver a editar. |

Reglas tributarias: **Arancel** e **IVA Importación** quedan siempre con IVA 0 (el arancel
no lleva IVA aparte; el IVA Importación es el IVA mismo). El backend reescribe siempre las
**6 líneas canónicas** de gasto en cada guardado (es la autoridad del esquema).

Robustez del cálculo (no se "pierde" plata): si Σ pesos = 0 el flete se prorratea por FOB;
si tampoco hay FOB, en partes iguales. Si Σ CIF = 0, los gastos se reparten en partes
iguales. La suma de los prorrateos siempre da el total.

## Puesta en marcha

```bash
cd backend
python -m monza_embarques_pricing.init_db      # crea las 3 tablas (idempotente)
```

`main.py` ya importa y monta el router (antes del `create_all`), así que en local las
tablas también se crean al levantar el backend.

## Tests

```bash
cd backend
./venv/bin/python monza_embarques_pricing/tests/test_service.py        # matemática pura
./venv/bin/python monza_embarques_pricing/tests/test_integration.py    # API + BD + candado
```

`test_integration.py` siembra un embarque con 2 ítems, ejerce listado/detalle/guardar/
cerrar/reabrir, verifica el **cuadre de prorrateos**, el **override de FOB manual** y el
**candado de empresa** (mineria → 403), y limpia todo lo que creó al terminar.

## Diferencias con Grupo AM

| | Grupo AM | MonzaParts |
|---|---|---|
| Embarque origen | `Embarque` + `EmbarqueItem` (con `oc_proveedor_id`) | `MonzaEmbarque` + `MonzaEmbarqueItem` (sueltos, sin FK) |
| FOB default | Factura Proveedor → Cotización → 0 | Costo del ítem de cotización → 0 (manual) |
| Peso | libras (`peso_unit_lbs`) | kilos (`peso_kg`) |
| TC / config | `ConfiguracionCotizador` | `MonzaConfig` (`tc_usd_clp` / `tc_eur_clp`) |
| Defaults de gastos | desde Config | 0 (se cargan a mano) |

La **matemática del landed es idéntica** (función pura `calcular_landed`); solo cambian las
fuentes de datos y la unidad de peso (el prorrateo es proporcional, la unidad no altera el
resultado).
