# FOB real del proveedor en Embarques Pricing — MonzaParts (2026-07-30)

Último ítem de la **Fase 9** (paridad MonzaParts ↔ Grupo AM). Cierra la brecha "el costo
landed de Monza se calcula con el precio **estimado**, no con el que realmente se pagó".

---

## 1. El problema, en una frase

El costo landed de un repuesto importado (lo que cuesta **puesto en bodega**) se arma con
tres cosas: **FOB** (lo que le pagas al proveedor) + **flete** + **gastos de internación**.
En MonzaParts el FOB salía del **costo estimado** que se puso al cotizar, y no había forma
de decir "este número ya no es un estimado, es lo que dice la factura del proveedor".

En Grupo AM eso se resuelve solo porque el sistema **tiene** las facturas de proveedor
cargadas (`FacturaProveedorItem`) y el FOB se lee de ahí. **MonzaParts no tiene esa tabla**:
no hay de dónde leer el precio real. Así que el dato tiene que entrar a mano.

## 2. La decisión del dueño

> El FOB real se carga **en Embarques Pricing**, al costear el embarque.

Eso es lo natural: es el momento en que ya llegó la mercadería, ya está la factura del
proveedor sobre la mesa y ya se están cargando el flete y los gastos de aduana. La pantalla
**ya tenía** el campo para teclear el FOB; lo que faltaba era **distinguir de dónde salió
el número**.

## 3. Qué se ve en pantalla

En **Contabilidad → Embarques Pricing**, dentro de la tabla "Costo landed por ítem", bajo el
campo **FOB unit**:

- Una casilla **`de factura`**. Márcala si el FOB que cargaste es el precio de la factura
  real del proveedor.
- Al lado, la etiqueta de estado cuando **no** está marcada: `de cotización` (estimado),
  `manual` (corrección a mano) o `sin dato`.
- Un atajo arriba de la tabla: **"Marcar los FOB cargados como «de factura»"**, para el caso
  real de "llegó la factura y todos estos números son los suyos".
- La nota que explica para qué sirve:

  > El **FOB** parte del costo **estimado** de la cotización. Cuando tengas la factura del
  > proveedor, carga acá el precio real y marca **"de factura"**: así sabes de un vistazo si
  > el costo landed está calculado con lo que pagaste de verdad o todavía con el estimado.

Con el pricing **cerrado** todo queda solo de lectura y el origen aparece **congelado**
junto al costo (auditable).

## 4. Los cuatro estados del FOB

| Etiqueta | `fob_origen` | Significa |
|---|---|---|
| `de factura` | `factura` | Precio **real** de la factura del proveedor |
| `manual` | `manual` | Corrección a mano, sin factura que la respalde |
| `de cotización` | `cotizacion` | Costo **estimado** puesto al cotizar (default) |
| `sin dato` | `auto` | La cotización no trae costo y nadie cargó el FOB |

**`factura` y `manual` pesan igual en el cálculo**: los dos son "una persona tecleó un
número" y los dos pisan al estimado de la cotización. La diferencia es **informativa** — es
la que responde "¿este costo es real o es todavía una estimación?".

## 5. Qué NO se tocó (y por qué importa)

| No se tocó | Por qué |
|---|---|
| `MonzaCotizacionItem.costo` | Es la **base del precio de venta** (`costo × markup → precio_unitario_clp`). Pisarlo con el FOB real movería el precio de una venta **ya cerrada** y rompería el TC congelado. |
| `monza_cont_compra_item` | Es **solo nacional**, protegido por tres candados. Abrirlo para meter el FOB internacional capitalizaría el mismo repuesto **dos veces** (el bug que cerró el multienjambre de compras nacionales). Verificado: el candado de doble costeo internacional → `409` sigue en pie. |
| Base de datos | **Sin migración.** `monza_emb_pricing_item.fob_origen` ya existía (`VARCHAR(20)`) y ya se guardaba: solo gana el valor `'factura'`. Ni columnas, ni tablas, ni endpoints nuevos. |
| Grupo AM | Nada. El cambio es 100 % dentro del módulo de Monza. |

## 6. Alcance del cambio (chico a propósito)

Cambiar el FOB mueve **dos cifras**: la fila del ítem en el pricing y el total del listado de
embarques. El costo landed de Monza **no** alimenta rentabilidad, **no** alimenta
contabilidad (que toma solo los **gastos** del pricing, nunca los ítems) y **no** alimenta el
precio de venta. Por eso el ítem se pudo cerrar sin tocar el modelo de datos.

## 7. Aviso de moneda mezclada (defensivo)

El dueño confirmó que **un embarque siempre viaja en una sola moneda**, aunque consolide
varios proveedores (Fastmark). El pricing asume eso: usa **una moneda y un TC** para todo el
embarque, tomados del **primer** ítem.

Se agregó un **aviso visible** para el día que eso deje de ser cierto: si los ítems traen
más de una moneda, el detalle del pricing muestra una banda ámbar explicando que los ítems de
la otra moneda van a quedar mal convertidos, y qué hacer (cargar todo en la moneda del
pricing, o pedir separar el embarque).

**Es aviso, no bloqueo**, y la razón es de negocio: el embarque **ya llegó físicamente** y
este módulo es el único lugar donde se registra su costo. Un `409` dejaría mercadería
recibida **sin costear**, que es peor que costearla avisando. El aviso se mantiene después de
guardar y con el pricing cerrado.

### Deuda documentada (afecta a las DOS marcas)

El defecto de raíz **no se arregló**: la moneda del embarque se toma del primer ítem y se
aplica a todos (`monza_embarques_pricing/integration.py`, `moneda_de_embarque`), y **Grupo AM
tiene el mismo patrón** (un único `tc_header` para todos los ítems en
`embarques_pricing/router.py`). El arreglo de raíz es **moneda + TC por ítem**, que cambia el
modelo de datos y el cálculo en ambas marcas. Queda documentado y, ahora, **visible**.

---

## 8. Archivos

| Archivo | Qué cambió |
|---|---|
| `backend/monza_embarques_pricing/router.py` | `fob_es_factura` en el payload, `fob_origen='factura'`, precedencia con `FOB_ORIGEN_TECLEADO`, aviso de moneda mezclada |
| `backend/monza_embarques_pricing/tests/test_integration.py` | pasos 12 (origen del FOB) y 13 (moneda mezclada) → **94 checks** |
| `backend/monza_embarques_pricing/README.md` | documentación del módulo + deuda de moneda |
| `frontend-src/src/monza-embarques-pricing/types.ts` | `FobOrigen` gana `'factura'`, `advertencias`, `fob_es_factura` |
| `frontend-src/src/pages/MonzaEmbarquesPricingPage.tsx` | casilla "de factura", atajo masivo, banda de aviso, textos y `title` corregidos |

## 9. Puesta en marcha

**Nada que correr.** Sin migración, sin `init_db` nuevo, sin variables de entorno. Basta
desplegar el código y reiniciar el backend.
