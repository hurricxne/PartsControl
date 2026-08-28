# Firma PARCIAL de la guía de despacho — MachParts (2026-08-22)

**Commit principal:** `72b66d7` (+ afinado de la revisión multienjambre posterior).
**Migración:** `python -m migrations.despacho_qty_firmada` **ANTES de reiniciar**
(checklist §1.a, CRÍTICA — sin ella las bandejas sobreviven y el sistema parece sano,
pero cerrar un despacho, facturar o emitir revienta con 1054).

## El problema que resuelve

A veces la guía de despacho se emitió por ítems que **no llegaron** (perdidos en la
entrega). Antes, al marcar «guía firmada», TODO el despacho pasaba a facturación — el
ítem perdido se facturaba igual. Y los despachos en estado `despachado` no tenían
desplegable para ver sus ítems.

## Cómo funciona

1. **El desplegable**: cada despacho se abre y muestra sus ítems (parte, descripción,
   despachada, firmada, facturado, y el motivo del faltante si lo hay).
2. **Firmar con cantidades**: en el modal de «Marcar guía firmada», cada ítem tiene su
   cantidad (pre-completa — el caso común no cambia). Destickear o bajar la cantidad
   declara un **faltante de entrega**, con **motivo obligatorio** (5–300 caracteres).
3. **Lo firmado va a facturación; el faltante NO** — jamás por esa guía. La guía
   desaparece del selector cuando lo firmado quedó facturado.
4. **«Por facturar» descuenta el faltante declarado** y lo dice aparte («2 por
   facturar + 1 un. faltante declarado») — en el detalle Y en el listado de Ventas.
5. **Re-firma** («Editar firma», disponible para toda guía firmada): el courier
   encontró la caja → subir la cantidad recupera la unidad en por facturar. Sin
   re-subir la foto (se conserva la actual). Bajar de lo ya facturado → 409.

## Las reglas que NO se negocian (con sonda cada una)

- **La reposición va SIEMPRE por cotización nueva** (decisión del dueño). Declarar un
  faltante NO libera cupo de despacho, NO toca `estado_item`, NO toca bodega. El
  diseño alternativo («liberar cupo») fue descartado por el verificador adversarial
  del plan: creaba despachos fantasma (la unidad perdida salió físicamente de bodega
  y el tope es aritmético, no inventario).
- **`qty_despachada` es intocable**: es la verdad física y el espejo de la guía SII.
  La firma vive en `despacho_items.qty_firmada` (NULL = firmada completa — el
  histórico no se migra).
- **El respaldo tributario**: facturar solo lo firmado es lo correcto (la guía
  documenta el TRASLADO, la factura la VENTA) y evita la nota de crédito que el
  sistema no tiene. Un faltante SIN respaldo se presume venta y paga IVA (art. 8
  letra d, DL 825) — por eso el motivo es obligatorio y queda con usuario y fecha.
- **Facturar 2 contra una guía que declaró 3 es válido ante el SII** (verificado en
  el código del DTE): la referencia 52 no lleva cantidades y los precios congelados
  de la guía electrónica son por unidad.

## Dónde vive cada pieza

| Pieza | Archivo |
|---|---|
| Columnas `qty_firmada` / `faltante_motivo` | `backend/models/models.py` (DespachoItem / Despacho) |
| Migración idempotente | `backend/migrations/despacho_qty_firmada.py` |
| Firmar (lock OC→despacho, guards, 2 capas de re-firma) | `backend/routers/despachos.py` (`firmar_despacho`) |
| Gate de facturación (`_firmada_efectiva`, 4 puntos) | `backend/routers/contabilidad.py` |
| Suite (§A–§G, con sondas de mutación) | `backend/routers/tests/test_firma_parcial.py` |
| Modal + desplegable + badge | `frontend-src/src/pages/DespachosPage.tsx` |
| Faltante en Ventas—Contab (fila, barra, sección, chip de guía) | `frontend-src/src/pages/VentasContabPage.tsx` |

## El guard de re-firma tiene DOS capas

La 1ª compara contra lo facturado **por línea de guía** (`despacho_item_id`); la 2ª,
por **ítem físico** contra TODO lo facturado de la venta — es la que ve las líneas de
factura sin guía declarada (facturación por ítems sueltos, alcanzable hoy). Sin la 2ª,
una re-firma a la baja podía declarar «faltante» sobre unidades ya facturadas. La
sonda §G del suite crea exactamente ese caso.

## Pendientes (registrados en `docs/deudas-diferidas-decision-dueno.md` §3)

Espejo MonzaParts · alerta de guías sin firmar a N días (pérdida total) · castigo
contable de la pérdida definitiva.
