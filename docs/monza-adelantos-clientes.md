# MonzaParts · Adelantos de clientes (vía A)

**Espejo de:** `docs/adelantos-clientes-grupo-am-2026-07-16.md` (Grupo AM). Este doc cubre
la **vía A**: el adelanto se aplica como *cobranza* `medio='adelanto'` sobre las facturas
reales, sin emitir un documento tributario del anticipo.

> **La vía B ya existe** (Fase 7, 2026-07-29): si el cliente pide una **factura** por su
> adelanto, se emite una **factura de anticipo** (DTE 33 sin guía) y la factura del despacho
> real le descuenta ese anticipo sola, con una línea negativa. Ver
> **`docs/monza-factura-anticipo.md`**.
>
> Las dos vías conviven: si la venta tiene factura de anticipo, la plata del adelanto cae
> **primero** en ella y solo el excedente sigue a las facturas del despacho real.

## El flujo (quién hace qué)

```
Comercial cierra la venta e informa el % de adelanto (modal de Cerrar venta)
        │  → MonzaCotizacion.pct_adelanto  (≠0 ⇒ la venta "requiere adelanto")
        ▼
Abastecimiento ve "pago no verificado" y NO compra hasta que se verifique
        ▼
El cliente deposita → LA ORDEN LA DA TESORERÍA (diferencia con GA: en Monza es
Tesorería quien aprueba el adelanto; en GA lo verifica Contabilidad)
        │  → POST /api/monza/tesoreria/aprobaciones/{cot_id}/aprobar
        │     crea/actualiza monza_cont_adelanto (monto, fecha, banco, N° operación)
        ▼
APLICACIÓN AUTOMÁTICA a las facturas de la venta (cobranza medio='adelanto'):
  · al APROBAR: se aplica RETROACTIVAMENTE a las facturas ya emitidas con saldo
    (cap por SALDO actual, saltando facturas con factoring vigente) — paridad GA 2026-07-28
  · al FACTURAR después: crear_factura aplica el pendiente del adelanto a la factura nueva
        ▼
Conciliación bancaria: el abono de la cartola se concilia contra el ADELANTO
(monza_tes_conciliacion). La aplicación del adelanto a una factura NO es un depósito
nuevo: las cobranzas medio='adelanto' se EXCLUYEN del matching de abonos (port 279ee7a).
```

## Invariantes (los que protegen los tests)

1. `adelanto.monto_aplicado ≤ adelanto.monto` — el pendiente jamás es negativo.
2. La cobranza `medio='adelanto'` **solo la genera el sistema** (registrarla a mano → 400).
3. Adelanto ≤ total bruto de la venta (tope al aprobar).
4. Un adelanto **conciliado** con el banco no se re-verifica/edita sin desconciliar antes.
5. Todo lo que escribe `monto_aplicado` toma los locks en el **orden global**
   cotización → factura → adelanto (`docs/regla-lecturas-de-plata.md`).
6. Con factoring vigente la asignación de pagos está congelada: ni aplicar ni revertir.

## Dónde vive

| Pieza | Archivo |
|---|---|
| Orden/aprobación del adelanto | `backend/monza_tesoreria/router.py` (aprobar_adelanto) |
| Verificación desde Contabilidad | `backend/monza_contabilidad/router.py` (verificar_adelanto) |
| Aplicación automática | `backend/monza_contabilidad/router.py` (_aplicar_adelanto) |
| Registro | tabla `monza_cont_adelanto` (1 por venta — multi-adelanto es decisión F7) |
| Conciliación abono↔adelanto | `backend/monza_tesoreria/` (monza_tes_conciliacion) |
| Tests | `monza_contabilidad/tests/` (cortafuego, concurrencia) + `monza_tesoreria/tests/` + `monza_tests/test_viaje_de_la_plata.py` |

## Nota de historia (2026-07-28)

La auditoría de paridad encontró que `pct_adelanto` **se perdía en silencio** al cerrar la
venta (faltaba en el schema del PATCH): el flujo entero nunca se disparaba desde la UI.
Corregido y blindado con test (`monza_tests/test_cierre_venta_datos_maestros.py`).
