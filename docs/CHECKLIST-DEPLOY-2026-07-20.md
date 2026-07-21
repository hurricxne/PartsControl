# Checklist de deploy — rama `feature/adelantos-clientes` (corte 2026-07-20)

Para el programador: esta rama acumula los módulos y endurecimientos listados abajo.
**Orden del deploy: primero los scripts de BD, DESPUÉS reiniciar el backend** (todos
los scripts son idempotentes: correr de nuevo no rompe nada).

## 1. Scripts de base de datos (desde `backend/`, con el venv del servidor)

```bash
python -m wasabil_dte.init_db          # tabla wasabil_dte (guías SII 52)
python -m recepcion_nacional.init_db   # compras nacionales (2 tablas)
python -m compras_contab.init_db       # cont_compra_item + columnas nuevas
python -m embarques_pricing.init_db    # peso editable por ítem (override)
python -m tesoreria.init_db            # conc_* + conciliación de ingresos + adelantos
python migrate_awb_numero.py           # columna awb_numero en embarques
python -m migrations.fix_despacho_parcial_estado   # repara líneas 'despachado' de despachos parciales legados (si no se corrió antes)
```

Nota: la columna `cotizaciones.origen` ya existe en prod (vino de allá).

## 2. Variables de entorno (`backend/.env` del servidor)

- `WASABIL_API_TOKEN=...` — token del facturador (Wasabil, cuenta GRUPO AM SPA).
  Sin él la app funciona igual pero el botón "Emitir guía SII" queda bloqueado
  con aviso. **El token NUNCA va al repo.**

## 3. Frontend

```bash
cd frontend-src && npm install && npm run build
```

## 4. Reiniciar el backend

Recién después de 1-3. Verificación rápida post-deploy: login → Despachos
(botones ① Emitir guía SII ② Agregar transportista ③ Confirmar en un despacho
en preparación) → Contabilidad → Ventas (barra de avance + "Por facturar").

## Qué trae esta rama (resumen para orientarse)

| Área | Qué cambió | Doc |
|---|---|---|
| Guías SII (Wasabil) | Módulo `wasabil_dte/` completo; PRIMERA EMISIÓN REAL OK (folio 136); formato v2: OC referenciada una sola vez, nombre de línea = descripción | `backend/wasabil_dte/README.md`, `docs/integracion-wasabil-guias.md` |
| Despachos | Flujo guía-primero (crear sin N° guía manual; transportista después); cobertura de estados por despachos CERRADOS + reversa del embarque | `docs/flujo-bodega-despachos.md` |
| Bodega→Despachos | Tope físico por lo RECIBIDO; recepción parcial con reclamos trazables | `docs/flujo-bodega-despachos.md` |
| Compras NACIONALES | OC nacional/internacional, recepción sin embarque, costo por ítem, CxP→Tesorería | `docs/plan-compras-nacionales-2026-07-18.md`, READMEs de `recepcion_nacional/` y `compras_contab/` |
| Adelantos de clientes | Vía A (cobranza) y vía B (factura de anticipo con descuento automático); Tesorería aprueba; % ↔ CLP espejo en Cierre de Venta | `docs/adelantos-clientes-grupo-am-2026-07-16.md` |
| Ventas—Contabilidad | Desglose por factura + avance real de la OC ("por facturar" con base física) + anti-muro para OCs grandes | commit `76bc331` |
| OC cliente | N° obligatorio al cerrar venta, edición ex-post controlada | `docs/integracion-oc-cliente.md` |
| Embarques | N° AWB escribible/buscable; peso editable en Embarques Pricing | `docs/awb-numero-embarques-2026-07-17.md`, `docs/peso-editable-embarques-pricing-2026-07-17.md` |

Suites: `cd backend && python -m pytest -q` (66 verdes al corte) — necesitan MySQL.
