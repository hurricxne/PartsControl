# Checklist de deploy — rama `feature/adelantos-clientes` (corte 2026-07-21)

Para el programador: esta rama acumula los módulos y endurecimientos listados abajo.
**Orden del deploy: primero los scripts de BD, DESPUÉS reiniciar el backend** (todos
los scripts son idempotentes: correr de nuevo no rompe nada).

## 1. Scripts de base de datos (desde `backend/`, con el venv del servidor)

```bash
python -m wasabil_dte.init_db          # tabla wasabil_dte (guías 52 + facturas 33) + índice único por factura
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

## 3.b ⚠️ REQUISITO NUEVO — nivel de aislamiento de MySQL

Esta rama pone el engine en **READ COMMITTED** (`backend/database.py`), lo que cierra una
clase de bug que costó cinco fugas de plata reproducibles. Antes de reiniciar:

```sql
SELECT @@global.binlog_format;   -- debe ser ROW o MIXED
```

- `ROW` / `MIXED` → seguir normal.
- `STATEMENT` → **NO desplegar todavía**: MySQL rechaza las escrituras bajo READ COMMITTED
  con ese binlog. Pedir el cambio a ROW al hosting y, mientras tanto, revertir SOLO el
  commit del aislamiento (los arreglos a mano ya protegen los caminos críticos por sí solos).

Detalle y fundamento: `docs/regla-lecturas-de-plata.md`.

## 4. Reiniciar el backend

Recién después de 1-3. **Reinicio COMPLETO del servicio, no `--reload`**: el nivel de
aislamiento se fija al abrir cada conexión, así que un reload deja workers viejos con el
nivel anterior. En el log de arranque debe aparecer `[startup] isolation=READ-COMMITTED`
(si dice otra cosa, algo pisó `backend/database.py`).

Verificación rápida post-deploy: login → Despachos
(botones ① Emitir guía SII ② Agregar transportista ③ Confirmar en un despacho
en preparación) → Contabilidad → Ventas (barra de avance + "Por facturar") →
Facturas y Cobranzas ("Emitir factura" debe abrir en modo SII con el folio
"Lo asigna el SII al emitir" y el enlace al registro manual).

## Qué trae esta rama (resumen para orientarse)

| Área | Qué cambió | Doc |
|---|---|---|
| Guías SII (Wasabil) | Módulo `wasabil_dte/` completo; PRIMERA EMISIÓN REAL OK (folio 136); formato v2: OC referenciada una sola vez, nombre de línea = descripción | `backend/wasabil_dte/README.md`, `docs/integracion-wasabil-guias.md` |
| Facturas SII (Wasabil, Fase B) | Emisión de la factura 33 desde Facturas y Cobranzas (normal y anticipo): folio lo asigna el SII; refs 801+52+33; descuento de anticipo como % por línea; adelantos diferidos hasta Emitida; modo manual conservado. **PRIMERA FACTURA REAL OK: folio 116** (2026-07-21, desde la guía 136, con el adelanto de $17.885.300 aplicado solo al confirmarse el folio) | `backend/wasabil_dte/README.md` (sección Fase B) |
| Referencias DTE — formato v3 | El motivo (RazonRef) ya no repite lo que el tipo y el folio imprimen: la OC salía escrita DOS veces en la guía 137. Verificado contra el API real con un borrador y contra PDFs del portal SII | `backend/wasabil_dte/README.md` (sección Formato v3) |
| Guard guía↔factura | No se puede facturar una guía cuyo folio del SII todavía viene en camino (referenciaría un folio inexistente, irreversible) | commit `a0d4671` |
| Flujo de plata — concurrencia | Lecturas BLOQUEANTES donde se decide un tope (cobranzas, adelantos, factoring, pago a proveedores) + engine en READ COMMITTED. Cierra 5 fugas de dinero reproducibles | `docs/regla-lecturas-de-plata.md` |
| Despachos | Flujo guía-primero (crear sin N° guía manual; transportista después); cobertura de estados por despachos CERRADOS + reversa del embarque | `docs/flujo-bodega-despachos.md` |
| Bodega→Despachos | Tope físico por lo RECIBIDO; recepción parcial con reclamos trazables | `docs/flujo-bodega-despachos.md` |
| Compras NACIONALES | OC nacional/internacional, recepción sin embarque, costo por ítem, CxP→Tesorería | `docs/plan-compras-nacionales-2026-07-18.md`, READMEs de `recepcion_nacional/` y `compras_contab/` |
| Adelantos de clientes | Vía A (cobranza) y vía B (factura de anticipo con descuento automático); Tesorería aprueba; % ↔ CLP espejo en Cierre de Venta | `docs/adelantos-clientes-grupo-am-2026-07-16.md` |
| Ventas—Contabilidad | Desglose por factura + avance real de la OC ("por facturar" con base física) + anti-muro para OCs grandes | commit `76bc331` |
| OC cliente | N° obligatorio al cerrar venta, edición ex-post controlada | `docs/integracion-oc-cliente.md` |
| Embarques | N° AWB escribible/buscable; peso editable en Embarques Pricing | `docs/awb-numero-embarques-2026-07-17.md`, `docs/peso-editable-embarques-pricing-2026-07-17.md` |

Suites: `cd backend && python -m pytest -q` (**74 verdes** al corte) — necesitan MySQL.

## Emisiones REALES ya hechas con esta rama (no tocar, son documentos tributarios)

| Documento | Folio SII | Cuándo | Nota |
|---|---|---|---|
| Guía de despacho 52 | **136** | 2026-07-20 | primera emisión real; formato v1 (de ahí salieron los arreglos v2) |
| Guía de despacho 52 | **137** | 2026-07-21 | formato v2; reveló la tercera duplicación → formato v3 |
| **Factura 33** | **116** | 2026-07-21 | primera factura real, desde la guía 136; refs 801+52 limpias; adelanto aplicado solo |

## Lo que un desarrollador debe saber antes de tocar este código

1. **Toda decisión sobre plata se relee BAJO LOCK** — nunca desde una relación perezosa
   ni desde un `selectinload`. Ver `docs/regla-lecturas-de-plata.md`; ahí está el porqué
   y lo que costó cada fuga.
2. **Los tests usan un login que CONSULTA la base** a propósito. No volver al `lambda`
   seco: con el atajo, las carreras de concurrencia se vuelven invisibles.
3. **El motivo de una referencia DTE nunca repite el tipo ni el folio** — el render los
   imprime solo. Hay un test que lo protege.
4. **Emitir al SII es irreversible.** El módulo solo manda `issue=true` tras la
   confirmación explícita del usuario, y el claim anti doble emisión se commitea ANTES
   del HTTP. No simplificar ese orden.
