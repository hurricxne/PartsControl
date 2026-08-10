# Módulo Contabilidad MonzaParts — Ventas + Facturas/Cobranzas/Factoring

Aislado y **SOLO para MonzaParts** (`empresa = "automotriz"`). Es el espejo del módulo
de Grupo AM (`backend/routers/contabilidad.py`) apuntando a las tablas `monza_*`.

## Qué hace
- **Ventas**: lista las cotizaciones vendidas/despachadas (`MonzaCotizacion`) con su
  resumen de cobranza. La venta se agrupa por **cotización** (MonzaParts no tiene "OC
  cliente" como tabla; `oc_cliente` es solo un campo de la cotización).
- **Facturas (cuentas por cobrar)**: emite facturas desde una **guía de despacho
  'despachado'** (`MonzaDespacho`), con **doble tope** por ítem y por guía contra lo ya
  facturado. Los precios netos vienen de la cotización (no se recalcula).
- **Retiro en oficina (sin guía)**: cuando el cliente retira en oficina no hay guía; con
  `sin_guia=true` se factura el **saldo pendiente de la venta** (tope por lo VENDIDO −
  ya facturado), sin requerir despacho. Es **excluyente** con `despacho_id`/`items`. El
  mismo `fact_qty_item` cuenta todas las facturas → retiro y guía nunca se solapan.
- **Cobranzas**: pagos reales del cliente, con control de sobre-pago.
- **Factoring**: cesión de la factura a un factor (adelanto/retención), 1 por factura.
- **KPIs** y **antigüedad de cartera** (0-30 / 31-60 / 61-90 / 91+).

## Reglas de negocio (igual que Grupo AM)
- Solo se factura una guía en estado **'despachado'**; nunca más de lo despachado ni dos
  veces (doble tope por ÍTEM y por GUÍA).
- La **firma de la guía es OPCIONAL/registrable** (campo `guia_firmada` en
  `monza_despachos`): se puede marcar/subir, queda visible, pero **NO bloquea** facturar.
- El dinero se congela como `Numeric` (decimal exacto). La factura guarda **snapshots**
  (cliente, RUT, N° cotización, guía, líneas) para ser un documento inmutable.
- Borrado de factura **seguro**: se rechaza si tiene cobranzas reales o factoring.
- Concurrencia: `SELECT ... FOR UPDATE` sobre cotización/factura para serializar.

## Adelanto (ej. 50% personas naturales)
Flujo: **Comercial** cierra la venta indicando `pct_adelanto` (columna en `monza_cotizaciones`)
→ la venta queda **"por verificar"** → **Contabilidad** la verifica con
`POST /ventas/{cot_id}/adelanto/verificar` (guarda `monza_cont_adelanto` con monto/fecha/banco
y marca `adelanto_verificado=1`) → al **emitir la factura**, el adelanto verificado se aplica
**automáticamente** como cobranza `medio='adelanto'` (descuenta el saldo). `Abastecimiento`
muestra "pago no verificado / verificado".
- `monto_aplicado` evita aplicar el adelanto dos veces y soporta facturación parcial; si se
  **revierte** esa cobranza, el monto vuelve a `monto_aplicado` (re-aplicable).
- No se puede cambiar `pct_adelanto` después de verificado (409); el monto del adelanto no
  puede exceder el total de la venta.

## Tablas (nuevas, aditivas)
`monza_cont_factura_cliente`, `monza_cont_factura_cliente_item`, `monza_cont_cobranza`,
`monza_cont_factoring`, `monza_cont_adelanto`. Además agrega columnas aditivas a tablas
existentes: `monza_despachos` (`guia_firmada`, `guia_firmada_archivo`; `fecha_firma` y
`usuario_firma_id` los agrega `migrations/monza_despachos_fecha_firma.py`) y `monza_cotizaciones`
(`pct_adelanto`, `adelanto_verificado`).

## Endpoints — prefijo `/api/monza/contabilidad`
| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/ventas` | listado de ventas + resumen cobranza |
| GET | `/ventas/{cot_id}` | detalle (ítems, guías, facturas) |
| GET | `/ventas/{cot_id}/despachos-facturables` | guías facturables (las sin firmar viajan con `guia_firmada=false`; el selector las deshabilita) |
| POST | `/ventas/{cot_id}/adelanto/verificar` | Contabilidad verifica el adelanto (monto/fecha/banco) |
| GET | `/facturas` | listado + antigüedad de cartera |
| POST | `/facturas` | emitir factura |
| DELETE | `/facturas/{id}` | borrado seguro |
| POST | `/facturas/{id}/cobranzas` | registrar pago |
| DELETE | `/facturas/{id}/cobranzas/{id}` | revertir pago |
| POST | `/facturas/{id}/factoring` | ceder a factor |
| POST | `/facturas/{id}/factoring/liquidar` | liquidar factoring |
| GET | `/kpis` | indicadores de cobranza |

## Activación
- `main.py`: importa `monza_contabilidad.router` y lo monta sin prefix
  (`app.include_router(monza_contabilidad_router)`); el router ya trae
  `prefix=/api/monza/contabilidad` y el candado `require_empresa("automotriz")`.
- Frontend: `services/monzaApi.ts → monzaContabilidadAPI`; páginas
  `MonzaVentasContabPage.tsx` y `MonzaFacturasPage.tsx`; menú en `MonzaLayout.tsx` y
  rutas en `App.tsx` (bajo `/monzaparts`).

## Inicializar la BD (una vez por entorno)
```bash
cd backend && python -m monza_contabilidad.init_db
```
Crea las tablas `monza_cont_*` y agrega las columnas de firma a `monza_despachos`.
(Las tablas también se autocrean al iniciar el backend vía `create_all`; el `init_db` es
necesario para las **columnas nuevas** de la tabla existente `monza_despachos`.)

## Cómo deshacer
Quitar el import + `include_router` en `main.py`, la ruta/menú en el frontend, y
`DROP TABLE monza_cont_factoring, monza_cont_cobranza, monza_cont_factura_cliente_item,
monza_cont_factura_cliente;`. Las columnas en `monza_despachos` son inertes si el módulo
se retira.
