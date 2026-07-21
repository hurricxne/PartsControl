# Flujo Bodega → Despachos → Guía/Factura (reglas y casos borde)

Rama `feature/adelantos-clientes`. Documenta el endurecimiento del 2026-07-16, nacido
del enjambre de pruebas empíricas (42 agentes ejecutando casos reales contra MySQL)
tras el reporte del dueño: *"la guía sale con toda la OC, no con lo que está en bodega"*.

## El pipeline de estados (por línea de cotización)

```
ingresado → cerrado (cierre de venta) → comprado → preparado → pre_embarcado
   → embarcado → [RECEPCIÓN EN BODEGA] → en_bodega | reclamo_proveedor → despachado
```

La recepción (`routers/bodega.py`) marca cada ítem del embarque con un
`estado_recepcion` y una `qty_recibida`, y al **cerrar la recepción** decide el
estado de la línea.

## La regla nueva: el TOPE FÍSICO por lo recibido

**Antes**: `qty_recibida` se registraba pero nadie la usaba — el disponible para
despachar era siempre la cantidad VENDIDA. Llegaban 8 de 10 y el sistema dejaba
despachar, emitir guía SII 52 y facturar por 10.

**Ahora** (`routers/despachos.py: _qty_recibida_utilizable / _tope_fisico`):

> **Disponible para despachar = min(cantidad vendida, Σ recibido utilizable) − ya despachado**

- "Recibido utilizable" = Σ `qty_recibida` de recepciones **cerradas** con estado
  `completo`, `danado_utilizable`, `sobrante` o `faltante` (en `faltante`,
  `qty_recibida` son las unidades que SÍ llegaron buenas).
- `sobrante` (llegó más de lo vendido) queda topeado a la cantidad vendida.
- Un ítem `en_bodega` **sin recepción registrada** (flujo antiguo / carga manual)
  no se acota: disponible = cantidad, como siempre (compatibilidad histórica).
- El tope se aplica en el detalle de la OC (`qty_disponible`), y en
  `create_despacho` bajo el mismo `SELECT … FOR UPDATE` anti-sobredespacho, con
  mensaje explícito: *"Cantidad excede lo RECIBIDO en bodega (recibido: X)"*.
- Como la guía SII y la factura derivan del despacho, **ya no pueden salir por
  más de lo físicamente recibido**.

## Reglas de la recepción (cerrar_recepcion, bodega.py)

| Marca | qty_recibida | Estado de la línea | Reclamo creado |
|---|---|---|---|
| `completo` (= cantidad) | 10/10 | `en_bodega` | — |
| `completo` (parcial) | 8/10 | `en_bodega` (despachable **8**) | `faltante` por **2** |
| `faltante` (llegó algo) | 8/10 | `en_bodega` (despachable **8**) | `faltante` por **2** |
| `faltante` (nada) / `no_llego` | 0/10 | `reclamo_proveedor` | por **10** (lo esperado) |
| `danado_utilizable` | n | `en_bodega` (despachable n) | — |
| `danado_no_utilizable` | n | `reclamo_proveedor` | por **n** (lo dañado) |
| `sobrante` | 12/10 | `en_bodega` (despachable **10**) | — |
| **sin marcar + forzar=true** | — | `reclamo_proveedor` | `no_llego` por lo esperado |

**Reposición y líneas repartidas en varios embarques**: el faltante a reclamar
se calcula contra lo que AÚN no llega **acumulando todas las recepciones** de la
línea — una reposición que completa la línea (o la 2ª mitad de un split 5+5) no
genera reclamos fantasma; el reclamo original se resuelve a mano en el panel de
Reclamos cuando llega la reposición.

Cambios clave respecto del comportamiento anterior:
1. **Llegada parcial ya no es todo-o-nada**: lo que llegó queda despachable; solo
   la diferencia va a reclamo. (Antes: `faltante` bloqueaba la línea entera, y
   `completo` parcial dejaba despachar de más sin dejar traza del faltante.)
2. **`qty_afectada` del reclamo ahora es la cantidad realmente afectada**:
   faltante → lo que falta; no_llego → todo lo esperado; dañado → lo dañado.
   (Antes registraba lo que LLEGÓ, y `no_llego` reclamaba 0.)
3. **Cierre forzado trazable**: los ítems sin marcar van a reclamo `no_llego` en
   vez de quedar atascados en `embarcado`, invisibles para Bodega y Despachos.
4. `qty_recibida` negativa se rechaza (400) al marcar.

## Semántica de tandas (sin cambios, verificada por el enjambre)

- Despachos **parciales en tandas**: la línea pasa a `despachado` solo cuando la
  cobertura alcanza la cantidad; un cierre parcial deja el remanente en bodega,
  despachable y facturable. **La cobertura cuenta SOLO despachos CERRADOS**
  (mesa redonda G16, 2026-07-19): un despacho abierto es un borrador anulable, no
  mercadería salida — contarlo marcaba la línea y el embarque `despachado`
  prematuro con tandas abiertas. El tope anti-sobredespacho de crear sí cuenta
  abiertos+cerrados (consumen cupo igual). La auto-transición del embarque a
  `despachado` ahora tiene reversa defensiva en anular (vuelve a `en_bodega` si
  alguna línea deja de estar despachada — auto-sana estados atascados legados).
- **Anular** un despacho en preparación devuelve la cobertura; las líneas que
  quedan sin cobertura completa vuelven a `en_bodega`. Un despacho **cerrado no
  se anula** (400). Con **guía SII viva**, anular/pisar folio → 409.
- Una línea con recepción parcial queda "parcial" (disponible 0 tras despachar lo
  recibido) hasta que el reclamo se resuelva y llegue el saldo: **no** se marca
  `despachado`, para dejar abierta la reposición del proveedor.

## El orden en la pantalla de Despachos (UI, 2026-07-18)

El despacho se arma en **cuatro pasos visibles**, en este orden (`DespachosPage.tsx`);
el modal del paso 1 lo rotula como "Paso 1 de 4":

1. **Crear despacho** (botón *Crear Despacho* → modal "Paso 1 de 4"): se
   elige **qué** se despacha (ítems + cantidades, con contacto/dirección de
   referencia). **No** se pide transportista ni N° de guía aquí — el modal es solo
   el "qué". El despacho nace `en_preparacion`.
2. **Emitir guía SII** (botón en la fila del despacho): arma y emite la guía 52.
   El **folio lo asigna el SII** y se graba en `despacho.numero_guia`. El
   transportista **no** viaja al SII, por eso no hace falta antes de emitir.
3. **Agregar transportista** (botón en la fila, junto a *Emitir guía SII*): abre el
   modal de edición (transportista + N° de expedición). Reusa el `PUT /despachos/{id}`,
   que ya **blinda el folio del SII** para que no se pise a mano (con guía viva,
   `numero_guia` no es editable). Se puede completar antes o después de confirmar.
4. **Confirmar** el despacho (cierra la preparación; aplica el pipeline de estados).

Por qué este orden: **la guía 52 se arma con los ítems del despacho** (líneas =
cantidades × precios de la cotización), así que el "qué" (paso 1) es requisito para
emitir (paso 2). El N° de guía manual se quitó del paso 1 porque el SII lo reemplaza
al emitir; sigue disponible en el modal de edición para el caso de **guía en papel**
(sin emisión electrónica). El flujo es idéntico para OC de origen **internacional** y
**nacional** — lo único que cambia por origen es el tope físico (ver más arriba).

## Verificación

```bash
cd backend && ./venv/bin/python routers/tests/test_bodega_despachos_flujo.py
# o con todo:  ./venv/bin/python -m pytest routers/tests -q
```
La suite cubre los 8 escenarios de la tabla + el ciclo en tandas (4+3+3, anular,
re-despachar) + compatibilidad histórica (ítems sin recepción).
