# MonzaParts · Facturas electrónicas (DTE 33) al SII (Fase 6 del espejo)

**Fecha:** 2026-07-29 · **Espejo de:** la "Fase B" de Grupo AM (`backend/wasabil_dte/` +
`routers/contabilidad.py`), que emitió la factura **real folio 116** desde la guía 136 ·
**Requiere:** Fase 5 (guías 52) ya construida.

## Qué hace

Desde **Facturas** de Monza, una factura se puede **emitir electrónicamente al SII**: el
modal abre en **modo SII** por defecto (con un enlace para volver al registro manual, que
se conserva como respaldo), el campo de folio dice **"Lo asigna el SII al emitir"**, se
previsualiza lo que se enviará (con los problemas bloqueantes a la vista) y, tras tu
confirmación, se emite. El folio del SII queda en la factura y la referencia a la
**guía 52** usa el **folio real** de esa guía.

## Las decisiones que protegen la plata

**1. Adelantos DIFERIDOS.** En la vía SII el adelanto del cliente se aplica **recién cuando
el SII confirma el folio** — nunca antes. Si el SII rechaza el documento, la plata no se
movió. Además hay un guard por el otro lado: si Tesorería aprueba un adelanto mientras la
emisión está en curso, no se aplica a una factura que aún no existe ante el SII. La vía
manual (folio tecleado) sigue aplicando de inmediato, como siempre.

**2. Anti doble emisión** (espejo exacto de GA, incluido el arreglo del bug que allá se
reprodujo 4 de 4 veces): `rollback` antes de tomar el candado, el claim y la factura local
se guardan **en la misma transacción antes de cualquier llamada a Wasabil**, candado de
intención por venta (dos clics simultáneos no generan dos documentos), y el folio se
escribe una sola vez, solo cuando Wasabil confirma Emitido.

**3. La referencia a la guía usa el folio REAL**, resuelto desde el registro del DTE 52 —
no el número guardado en la factura, que podría ser el tecleado antes de emitir.

**4. Borrar una factura con emisión electrónica está bloqueado** (409 con mensaje claro:
se anula primero en Wasabil). Sin este guard, borrarla además reventaba con error 500 por
la clave foránea.

## Refactor previo (necesario y sin cambios de comportamiento)

`crear_factura` era un monolito; se partió en tres piezas reutilizables
(`_construir_factura` / `_persistir_factura` / `_aplicar_adelantos_pendientes`), igual que
GA, para que la emisión SII use **la misma maquinaria** de la vía manual. El guard de folio
obligatorio quedó en el endpoint (la vía SII persiste sin folio a propósito).

## Fuera de alcance (Fase 7)

El **descuento de anticipo** (factura de anticipo con líneas negativas y referencia 33) no
entra aquí: Monza no tiene facturas de anticipo todavía. Se documenta para que la Fase 7 lo
agregue sin sorpresas.

## Pruebas

`backend/monza_wasabil_dte/tests/test_factura_*.py` — 6 suites **100% con dobles**: service
(referencias, cuadratura, piso de $1), integración (emisión feliz, folio, adelanto diferido),
concurrencia (doble clic), guard de borrado, y aislamiento de los simuladores entre marcas.

## Despliegue

`python -m monza_wasabil_dte.init_db` antes de reiniciar (agrega `factura_id` + índice único;
idempotente) + `WASABIL_API_TOKEN_MONZA` en `.env` + build del frontend.

## Primera emisión real

Igual que la factura 116 de Grupo AM: puedo dejarte un **borrador** para revisar y la
**emisión real la haces tú** con tu clic.
