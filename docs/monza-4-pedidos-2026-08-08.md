# MonzaParts — cuatro cambios del 2026-08-08

> Tarifa aérea por moneda · Cliente particular sin OC · Acceso a la ficha del cliente ·
> Devolver a compras (back order).
>
> Todo es **exclusivo de MonzaParts**. Grupo AM / MachParts no se tocó: los paquetes
> (`monza_*`) y las pantallas (`Monza*`) son separados por marca.

## Índice rápido

| # | Qué | Dónde vive | Migración |
|---|---|---|---|
| 1 | Dos tarifas aéreas (EUR/USD) y el selector elige cuál | `monza_router_cotizador.py` + `MonzaCotizadorModal.tsx` | `monza_tarifa_aerea_por_moneda` |
| 2 | Cliente particular: la OC es la cotización | `monza_router_cotizaciones.py` + `MonzaCotizacionesPage.tsx` | `monza_cliente_sin_oc` |
| 3 | Acceso directo a la ficha del cliente | `MonzaConfigPage.tsx` + páginas de venta | — |
| 4 | Devolver a compras (back order) | `monza_router_abastecimiento.py` + `MonzaSeguimientoPage.tsx` | — |

## Deploy (en este orden, ANTES de reiniciar el backend)

```bash
cd backend
python -m migrations.monza_tarifa_aerea_por_moneda
python -m migrations.monza_cliente_sin_oc
```

Ambas son idempotentes (se pueden correr dos veces sin daño). Van **antes** del reinicio
porque el ORM ya declara las columnas nuevas: contra la tabla vieja, cualquier consulta
revienta con *"Unknown column … in field list"*.

**Después del deploy, una tarea de una vez:** cargar en *Configuración* la tarifa aérea
de la moneda que falte (la migración solo puede llenar la que ya existía). Mientras esté
vacía, la calculadora avisa y no deja cotizar con esa moneda — a propósito.

---

## 1. Tarifa aérea por moneda

### El problema

`monza_config` tenía **una** tarifa (`tarifa_aerea_por_kg`) más la moneda en que estaba
expresada (`moneda_tarifa`). Cuando la calculadora ganó el selector de moneda del flete
(commit `87aec91`), elegir USD seguía usando **ese mismo número**: una tarifa de 4,5
EUR/kg se cotizaba como 4,5 USD/kg. Cambiaba el tipo de cambio, no el precio por kilo.

El courier cobra distinto según la moneda del contrato, así que el flete quedaba mal —
y el error viajaba **dentro del precio de venta**, sin que nadie lo notara.

### Cómo quedó

Dos columnas nuevas: `tarifa_aerea_eur_por_kg` y `tarifa_aerea_usd_por_kg`.

La tarifa efectiva se resuelve en **un solo punto por lado**, para que las dos mitades no
puedan divergir:

- Backend: `_tarifa_configurada` → `_flete_efectivo` (`monza_router_cotizador.py`)
- Frontend: `tarifaEfectiva` → `calcularPrecio` (`MonzaCotizadorModal.tsx`)

> **Por qué el frontend importa tanto acá:** el precio lo calcula el **navegador**. El
> endpoint `/cotizador/calcular` no tiene ningún llamador en la UI — solo lo usan los
> tests. El modal calcula y manda el resultado ya hecho a `/aplicar`, junto con la tarifa
> usada. Arreglar solo el backend habría sido inerte.

**Falla cerrado.** Si la moneda elegida no tiene tarifa cargada:

- el backend responde `400` con un mensaje que dice qué cargar y dónde;
- la pantalla muestra un aviso ámbar y **bloquea** el botón de aplicar.

Nunca se cotiza con flete 0: un 0 no se ve en pantalla y subvalúa la venta.

**La tarifa explícita manda.** Si el lead ya tiene su tarifa congelada, esa gana sobre la
configuración: cambiar la tarifa hoy no puede mover un precio ya ofrecido ayer.

**El legado se conserva a propósito.** `moneda_tarifa` sigue siendo la moneda
preseleccionada en la calculadora, y `tarifa_aerea_por_kg` respalda **solo a esa moneda**
mientras su tarifa nueva no se cargue. Prestársela a la otra moneda es exactamente el
defecto que este cambio corrige.

### Si algo falla

| Síntoma | Causa probable |
|---|---|
| "No hay tarifa aérea configurada en USD" | Falta cargarla en Configuración (esperado tras el deploy) |
| El precio no cambia al cambiar de moneda | Las dos tarifas tienen el mismo valor, o el lead trae tarifa congelada |
| `Unknown column 'tarifa_aerea_eur_por_kg'` | Falta correr la migración |

Suite: `monza_tests/test_tarifa_aerea_por_moneda.py` (7 checks). La sonda clave: con 4,5
EUR y 6,0 USD, la misma pieza da 45.000 y 54.000 de flete. Si alguien vuelve a la tarifa
única, ese check cae solo.

---

## 2. Cliente particular (sin orden de compra)

### El problema

Un consumidor final no emite orden de compra, pero el cierre de venta la exigía. Y el
problema de fondo no era ese `400`: **`oc_cliente` + `oc_fecha` son la referencia 801 del
SII** en todos los documentos de la venta (guía 52 y factura 33). Una venta cerrada sin
esos datos cierra… y después no se puede despachar con guía ni facturar.

Por eso la solución no podía ser "hacer la OC opcional": había que decidir **qué se graba**.

### Cómo quedó

Casilla **"Cliente particular (sin OC)"** en el modal de cierre. Marcada, el documento de
respaldo pasa a ser **nuestra cotización**:

- `oc_cliente` = el N° de la cotización (ej. `COT-2026-0001`)
- `oc_fecha` = **la fecha de emisión de esa cotización**

Folio y fecha salen del **mismo** documento. Con la fecha de hoy, la referencia 801
citaría un papel que no existe.

### El candado (esto es lo importante)

La marca se resuelve **antes** del guard W6, no después. Si se resolviera después, marcar
la casilla sería la puerta trasera para reescribir la referencia 801 de una venta ya
facturada — justo lo que W6 existe para impedir. Hay una sonda que lo fija: con un DTE
vivo, marcar la casilla responde `409` y la OC real queda intacta.

La marca se **persiste** (`cliente_sin_oc`) por dos razones: deja constancia de por qué
esa OC coincide con el N° de cotización (si no, se lee como error de digitación), y
permite reabrir el cierre con la casilla puesta.

También se valida que el N° de cotización quepa en los **18 caracteres** que el SII
permite en una referencia: mejor un `400` explicado que el rechazo de un documento
irreversible.

Suite: `monza_tests/test_cliente_sin_oc.py` (13 checks).

---

## 3. Acceso directo a la ficha del cliente

**Dónde se edita un cliente:** menú **Configuración → pestaña "Base de clientes"**
(`ClientesTab` en `MonzaConfigPage.tsx`). Ahí se busca, crea, edita y desactiva. También
se crean desde Leads al convertir un lead.

**Qué se agregó:** los errores que bloquean la emisión al SII dicen *"corrígelo en la
ficha del cliente"* (RUT inválido, sin razón social), pero eso era texto muerto. Ahora el
nombre del cliente es un enlace, en **Ventas** y en **Ventas — Contabilidad**, que abre la
ficha con el buscador ya filtrado por su RUT.

Para que se pudiera enlazar, la pestaña pasó a vivir en la URL:
`/monzaparts/configuracion?tab=clientes&cliente=<rut>`, con el patrón de `useSearchParams`
que ya usaban Bodega y Despachos.

> Contabilidad sirve el cliente como texto (no hay `cliente_id` en ese payload), así que
> el enlace se apoya en el buscador, que ya busca por RUT y por nombre. Sin cambios de
> backend. Si falta el RUT —justo el caso que hay que ir a arreglar— cae al nombre.

---

## 4. Devolver a compras (back order)

### El caso real

Baukat, proveedor de Alemania. Se emite la OC, la línea queda `comprado` y aparece en
Seguimiento, y días después el proveedor avisa que parte del pedido está en **back
order**. El pipeline era de una sola vía:

```
por_comprar → comprado → preparado → embarcado → en_bodega → despachado
```

Esa mercadería quedaba trabada en Seguimiento esperando algo que no iba a llegar.

### Cómo quedó

Botón **"← Devolver a compras"** en la fila de Seguimiento, visible **solo** en estado
`comprado`. Pide **cantidad** (el back order suele ser parcial: mandan 6 de 10) y
**motivo**.

```
comprado ──(devolver)──► por_comprar     ← vuelve al panel de compras, sin OC
        └─ el resto sigue 'comprado' con su OC intacta (si fue parcial)
```

La línea devuelta se **desliga** de su OC (`oc_proveedor_id = None`): vuelve a estar sin
comprar, que es la verdad. La traza de qué OC venía queda en el log.

> **Por qué se desliga:** si conservara el vínculo, la línea aparecería en el panel de
> compras colgando de una OC vieja, y `_rechazar_items_nacionales` —que hace JOIN por esa
> columna— la seguiría tratando como nacional.

La devolución parcial es la **misma partición** de `preparar-parcial` vista al revés, y
reusa la *regla de oro del split* (`_clonar_item_remanente`), que es lo único de este
pipeline capaz de duplicar plata. Invariante: Σ cantidad y Σ subtotal se conservan
exactos, y la cabecera de la venta no se toca.

### Los candados

| Candado | Respuesta | Por qué |
|---|---|---|
| Solo desde `comprado` | `400` | Lo preparado o embarcado ya salió del proveedor: eso es un reclamo, no un back order |
| Compra ya costeada en CxP | `409` | El costo quedaría colgado de una unidad que volvimos a pedir. Primero se corrige la compra |
| Recepción ya registrada | `409` | Mercadería que llegó no está en back order |
| Motivo obligatorio | `400` | Única transición hacia atrás; sin motivo nadie sabe después si fue back order, error o cancelación |
| Cantidad > la línea | `400` | Explícito, nunca el clamp silencioso |
| Concurrencia | retry 1213/1205 | Locks en orden id ASC, como el resto del módulo |

### Lo que NO hace, a propósito

**No cancela la OC del proveedor** aunque se quede sin líneas vivas. Nada en el módulo
cancela OCs hoy, y una OC es un documento ya enviado al proveedor: cerrarla es una
decisión comercial, no una consecuencia automática. La respuesta informa cuántas líneas
le quedan a cada OC tocada para que el operador decida.

Suite: `monza_tests/test_devolver_a_compras.py` (24 checks), con el invariante de plata y
la sonda de que la línea queda desligada.

---

## Lo que encontró la auditoría (y por qué importa para el próximo cambio)

Un multienjambre de 6 lentes revisó esta entrega y confirmó 12 defectos, verificando cada
uno adversarialmente. Los tres graves valen como lección:

**1. El arreglo de la tarifa estaba vivo en el backend y MUERTO en la pantalla.**
`tarifaEfectiva` recibía siempre el config con la moneda ya reemplazada por la elegida,
así que su comparación era una tautología y prestaba la tarifa igual que antes. El aviso
y el bloqueo que debían frenarlo eran código inalcanzable. Con el estado del día del
deploy, elegir USD cotizaba 17.280 CLP menos por unidad, sin avisar.

> **La causa de que 7 tests verdes no lo vieran:** la suite cubría solo la mitad Python,
> cuando **el precio lo calcula el navegador**. Por eso ahora existe
> `test_tarifa_espejo_frontend.py`, que compila el helper real del `.tsx` y compara su
> tabla de verdad contra el backend. Cualquier cambio futuro al cotizador debe mantener
> ese espejo: si solo se prueba Python, se está probando la mitad que no fija el precio.

**2. La devolución total no consultaba ningún documento.** El candado vivía dentro del
guard del split, que solo corre cuando algo se parte — y devolver una línea completa no
parte nada. Un ítem con factura o guía viva volvía a "por comprar".

**3. Un candado demasiado amplio atrapaba justo el caso que había que resolver.** El
guard de recepción bloqueaba por cualquier entrega, incluida una ya cerrada en la que el
proveedor no mandó nada — que es exactamente el back order de Baukat.

> Patrón que se repite en los tres: **un guard que no distingue casos es tan peligroso
> como no tener guard.** Uno deja pasar lo que debía frenar, el otro frena lo que debía
> pasar, y ambos se ven igual de verdes en los tests.

## Cómo verificar que todo está bien

```bash
cd backend && ./venv/bin/python -m pytest -q
```

```bash
cd frontend-src && npx tsc -b && npm run build
```

Al cierre de esta entrega: **261 tests verdes**, tipos limpios y build de producción OK.
