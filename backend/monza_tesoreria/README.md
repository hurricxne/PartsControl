# Tesorería — MonzaParts

Módulo **aislado** y **solo para MonzaParts** (`empresa = "automotriz"`). Concentra el
manejo del dinero real del banco en 4 sub-áreas: **Aprobaciones** (la orden de los
adelantos 50%), **Por pagar** (la orden de los pagos a Compras/CxP), **Conciliación
bancaria** (espejo de la Tesorería de Grupo AM) y **Flujo de caja** (proyección NIC 7).

## 1. Aprobaciones — la orden la da Tesorería

Flujo del adelanto (ej. 50%):

```
Comercial cierra venta con % adelanto     (MonzaCotizacionesPage → pct_adelanto)
        │
        ▼
TESORERÍA · pestaña Aprobaciones          ←── el cliente deposita el 50%
  ve la cola "por aprobar" con el monto sugerido (total × pct) y, si la cartola ya
  está cargada, el ABONO del banco que calza (sugerencia automática)
        │  POST /aprobaciones/{cot_id}/aprobar  (monto/fecha/banco/N° operación)
        ▼
adelanto_verificado = 1  +  registro monza_cont_adelanto
        │
        ├──► ABASTECIMIENTO queda destrabado (cortafuego en monza_router_abastecimiento
        │    — NO se modificó: sigue leyendo adelanto_verificado)
        └──► al FACTURAR la venta, el adelanto se aplica solo como cobranza
             (lógica existente de monza_contabilidad)
```

- Ventas-Contab muestra el estado **solo lectura** ("se aprueba en Tesorería").
- La operación es la MISMA regla de negocio que el endpoint histórico
  `POST /api/monza/contabilidad/ventas/{id}/adelanto/verificar` (mantener en sync;
  ambos escriben `monza_cont_adelanto` con lock de la cotización y tope por el total).

## 2. Por pagar — la orden del pago la da Tesorería

`GET /por-pagar` lista las compras ACTIVAS con saldo (registradas en Compras/CxP con
pago futuro o parcial), ordenadas por vencimiento (NULLS LAST) con buckets y estado
EN VIVO. `POST /pagos` crea el Comprobante de Egreso que paga 1..N compras con la
MISMA regla de negocio que Compras (reusa `_crear_egreso` de `monza_compras_contab`:
locks anti doble-pago, tope por saldo, recálculo de estados).

## 3. Conciliación bancaria

Espejo de `backend/tesoreria/` (Grupo AM) sobre tablas `monza_tes_*`:

- **Cuentas bancarias** (catálogo) → **Cartolas** (importa CSV/XLSX con parser flexible:
  detecta encabezados por sinónimos y montos en formato chileno Y anglosajón, incl.
  espacio duro 0xA0 como separador de miles; **anti-duplicados**: reimportar la misma
  cartola omite los movimientos idénticos ya existentes — 409 si TODOS son duplicados)
  → **Movimientos** (cargo|abono).
- Cruce 1:1 exacto (±TOL = $1) con sugerencias por monto y cercanía de fecha:
  - **cargo ↔ Comprobante de Egreso** de Compras (`monza_cont_egreso`): marca ambos
    conciliados y copia fecha/referencia bancaria al egreso (igual que Grupo AM;
    desconciliar limpia esa fecha/ref SOLO si vinieron de ese cruce).
  - **abono ↔ Adelanto aprobado** (`monza_cont_adelanto`) — *plus de Monza* — **o**
    **abono ↔ cobranza** de Facturas y Cobranzas (`monza_cont_cobranza`, enlace en
    `monza_tes_conciliacion_ingreso`): en ambos el "conciliado" se DERIVA de la
    existencia del enlace (no se agregaron columnas a tablas de monza_contabilidad).
- Protecciones: no borrar movimiento/cartola conciliados ni cuenta con movimientos;
  desconciliar libera el destino; locks `with_for_update` en importar/conciliar/
  desconciliar; conciliación automática solo en cuentas CLP (guard de moneda);
  la cobranza conciliada no se borra en Facturas (409) y el adelanto conciliado no
  se re-aprueba (409) sin desconciliar primero.

## 4. Flujo de caja (NIC 7, solo lectura)

`GET /flujo-caja`: saldos de **Compras por pagar** (salidas) y **facturas por cobrar**
(entradas) clasificados por vencimiento en buckets `vencido / 0-7 / 8-30 / 31-60 /
61+ / sin_fecha`, con el neto por bucket y los **adelantos por aprobar** informados
aparte (aún no son plata "segura"). No escribe nada: se deriva en vivo de
`monza_cont_compra` y `monza_cont_factura_cliente`.

## Datos (5 tablas nuevas, aditivas)

| Tabla | Rol |
|---|---|
| `monza_tes_cuenta_bancaria` | Cuentas bancarias de MonzaParts. |
| `monza_tes_cartola` | Cada carga de cartola (lote), auditable y borrable en bloque. |
| `monza_tes_movimiento` | Un movimiento del banco (cargo/abono, monto siempre positivo). |
| `monza_tes_conciliacion` | Enlace movimiento ↔ egreso **o** ↔ adelanto (exactamente uno). |
| `monza_tes_conciliacion_ingreso` | Enlace abono ↔ cobranza (UNIQUE cobranza_id; estado derivado). |

Dependencias documentadas (lectura/escritura acotada): `monza_cont_adelanto` y
`monza_cotizaciones.adelanto_verificado` (aprobar), `monza_cont_egreso.conciliado/
fecha_mov_bancario/referencia_bancaria` (conciliar), `monza_cont_cobranza` (solo
lectura + enlace), `_crear_egreso` de Compras (aprobar pagos), lecturas de
compras/facturas (flujo de caja). No se alteró ningún esquema existente.

## API — `prefix /api/monza/tesoreria` (candado `automotriz`)

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/aprobaciones` | Cola por aprobar (con monto sugerido + abono de cartola que calza) y aprobadas (con estado de conciliación). |
| POST | `/aprobaciones/{cot_id}/aprobar` | LA ORDEN: registra el adelanto y destraba Abastecimiento. 400 sobre-tope, 409 si ya se aplicó a factura o ya está conciliado con el banco. |
| GET | `/por-pagar` | Cola de pagos: compras activas con saldo, por vencimiento, con buckets y estado en vivo. |
| POST | `/pagos` | LA ORDEN del pago: Comprobante de Egreso que paga 1..N compras (reusa `_crear_egreso`). |
| GET/POST/PUT/DELETE | `/cuentas…` | Catálogo de cuentas bancarias (DELETE solo sin movimientos). |
| POST | `/cartolas/importar` | Importa cartola CSV/XLSX (multipart: cuenta_id, nombre, file). Anti-duplicados: informa `n_duplicados`, 409 si todos ya existían. |
| GET/DELETE | `/cartolas…` | Lista / borra lote (rechaza si tiene conciliados; solo borra los NO conciliados y re-chequea). |
| GET/POST/DELETE | `/movimientos…` | Lista (filtros cuenta/estado/tipo/q, paginado) / alta manual (fecha inválida → 400; mantiene n_movimientos) / borrar (no conciliado). |
| GET | `/movimientos/{id}/sugerencias` | Candidatos por monto ≈ y fecha: egresos (cargo) o adelantos + cobranzas (abono). Solo cuentas CLP. |
| POST | `/movimientos/{id}/conciliar` | `{egreso_id}`, `{adelanto_id}` o `{cobranza_id}` (exactamente uno; montos ±TOL). |
| POST | `/movimientos/{id}/desconciliar` | Libera movimiento y destino(s), incluidos los enlaces de ingreso. |
| GET | `/egresos-pendientes` · `/adelantos-pendientes` · `/cobranzas-pendientes` | Pendientes de conciliar (para emparejar a mano). |
| GET | `/flujo-caja` | Proyección por buckets + neto + adelantos por aprobar. |
| GET | `/resumen` | KPIs: aprobaciones y pagos por aprobar, movimientos/egresos/cobranzas sin conciliar, por pagar vencido. |

## Puesta en marcha (una vez por entorno)

```bash
cd backend
python -m monza_tesoreria.init_db   # crea las 5 tablas (idempotente)
```

`main.py` ya monta el router (create_all también crea las tablas al levantar).

## Tests

```bash
cd backend
./venv/bin/python monza_tesoreria/tests/test_service.py       # parser cartolas + buckets (sin BD)
./venv/bin/python monza_tesoreria/tests/test_integration.py   # flujo completo + candado
```

La integración ejerce: aprobación (aparece→validaciones→aprobar→cortafuego
destrabado→historial), cartola CSV importada + anti-duplicados (total 409 y parcial),
sugerencias y cruce cargo↔egreso, abono↔adelanto y abono↔cobranza, desconciliar
(egreso/cobranza liberados), borrados protegidos (409, incluida cobranza conciliada y
re-aprobación de adelanto conciliado), por pagar + aprobación de pago (bucket vencido,
sobre-pago 400), guard de moneda (cuenta USD → 400), flujo de caja, resumen y candado
mineria→403. Limpia todo al terminar.

## Diferencias con Grupo AM (tesoreria)

| | Grupo AM | MonzaParts (este módulo) |
|---|---|---|
| Tablas | `conc_*` con columna `empresa` | `monza_tes_*` propias (separación POR TABLA) |
| Cruce | cargos ↔ `cont_egreso` y abonos ↔ `cont_cobranza` | igual (tablas `monza_cont_*`) **más** abonos ↔ `monza_cont_adelanto` (plus Monza) |
| Aprobación de adelantos | (no existe: es de MonzaParts) | pestaña Aprobaciones — la orden que destraba a Abastecimiento |
| Por pagar / aprobar pagos | `GET /por-pagar` + `POST /pagos` | igual (reusa `_crear_egreso` de `monza_compras_contab`) |
| Flujo de caja | NIC 7, excluye factorizadas e informa la retención del factor aparte | NIC 7, incluye facturas con saldo + adelantos por aprobar aparte |

El parser de cartolas (bilingüe CL/US) y la mecánica de conciliación 1:1 (±TOL,
sugerencias, locks, anti-duplicados, protecciones de borrado) son idénticos.
