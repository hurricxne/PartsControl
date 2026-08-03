# MonzaParts · Envíos PARCIALES del proveedor (Fase 9 del espejo Grupo AM)

**Fecha:** 2026-07-30 · **Referencia:** `backend/routers/compras.py:1010-1044` y `:1229-1249`
(la implementación de Grupo AM que sirvió de molde lógico, **no** de molde de estilo).

## El problema que resuelve

Pediste 10 filtros. El proveedor manda 6 y los otros 4 los despacha la semana siguiente.

Antes, el pipeline de Monza movía la **línea completa** entre estados: había que embarcar los
10. Cuando Bodega recibía 6, el cierre de recepción creaba un **reclamo al proveedor por 4** —
aunque el proveedor no hubiera hecho nada mal: simplemente no los había despachado todavía. Un
**reclamo fantasma**. Y las 4 unidades no tenían dónde esperar el próximo AWB.

Ahora la línea se **parte**: 6 avanzan al embarque y 4 se quedan atrás esperando. Bodega recibe
6 de 6 → `completo`, sin faltante, sin reclamo.

```
Antes:   [10] ─────────► embarque de 10 ──► llegan 6 ──► RECLAMO por 4 (falso)
                                                          y las 4 sin dónde esperar

Ahora:   [10] ──parte──► [6] ──► embarque de 6 ──► llegan 6 ──► completo, sin reclamo
                    └──► [4] queda en «preparado», espera su AWB
```

## La regla de oro: partir NO puede mover plata

`MonzaCotizacionItem` lleva la **foto de precios congelada** al crear la cotización, y los
totales de la cabecera (`total_neto` / `iva_monto` / `total_bruto`) **no se recalculan nunca
más**. Partir una línea toca justo eso, así que la regla es estricta:

| Campo | Qué se hace | Por qué |
|---|---|---|
| `precio_unitario_clp` | se **copia idéntico** | es un precio por unidad; prorratearlo lo falsearía |
| `subtotal_clp` | se **recalcula en las DOS mitades** (`cantidad × precio`) | si el clon heredara el subtotal completo, `Σ subtotal` superaría el total de la venta |
| `tc_aplicado`, `tarifa_aerea`, `markup_pct`, `costo`, `moneda`, `peso_kg` | se **copian sin dividir** | son todos **unitarios** |
| `oc_proveedor_id` | se **copia** | sin esto el remanente pierde su OC y el sistema deja de reconocerlo como compra nacional |
| cabecera de la cotización | **no se toca** | la foto es inmutable: la venta vale lo mismo antes y después de partir |

**Invariante que cuida una prueba:** `Σ cantidad de las hermanas == cantidad original`,
`Σ subtotal_clp == subtotal original`, y los tres totales de la cabecera **byte-idénticos**.

Por qué importa tanto: la pantalla de Ventas deriva el precio unitario como
`subtotal ÷ cantidad`. Si el clon se quedara con el subtotal de 10 y la cantidad de 6, ese
precio aparecería **1,67× inflado**.

Dato tranquilizador: el cálculo autoritativo del "por facturar" y el precio de la guía 52 **no**
usan `subtotal_clp`, usan `cantidad × precio_unitario_clp`. Con la partición correcta esas dos
cifras se conservan **por construcción**.

## No hay migración

Grupo AM necesita una columna `cantidad_despacho` porque tiene una etapa de *pre-embarque*
donde el operador teclea la cantidad antes de cerrar. **Monza no tiene esa etapa** (va
`comprado → preparado → embarque` directo), así que las cantidades viajan en el cuerpo de la
petición y la partición ocurre en la misma transacción.

**Cero tablas nuevas, cero columnas nuevas, ningún script de base de datos que correr.**

## Los tres candados

### 1 · Estado
Solo se parte lo que está en el estado correcto: `comprado` para preparar, `preparado` para
embarcar. Cualquier otro estado se **rechaza con un mensaje claro** (no se salta en silencio).

Esto es lo que evita el daño real: bajar la cantidad de una línea *después* de que tocó bodega
o despacho haría que un despacho parcial "cubra" la línea y la marque como despachada antes de
tiempo.

### 2 · Candado de concurrencia
Las líneas se leen bajo `FOR UPDATE` en orden de id. **No es teórico:** una sonda que quita ese
candado —dejando el código como el de Grupo AM— hizo que dos preparaciones simultáneas de la
misma línea **inventaran unidades en 6 de 6 rondas** (Σ cantidad = 14 donde había 10). Con el
candado: 0 de 6.

### 3 · Candado de documento legal
No se parte una línea que ya tiene un documento encima. Siete comprobaciones, cada una con su
mensaje:

despacho no anulado · **guía 52 viva ante el SII** · factura de cliente · recepción de embarque ·
recepción nacional · costo de embarque congelado · costo de compra asignado.

La guía 52 es la más importante: congela cantidad y precio ante el SII, y la factura hace match
1:1 con ella. Partir después desincroniza un documento tributario **irreversible**.

> **Detalle de implementación que vale saber:** el candado se aplica **después** de tomar el
> lock y **solo sobre las líneas que de verdad se parten**. La ubicación "natural" (antes de
> todo, sobre todos los ids) daba un falso rechazo a un caso que hoy funciona: volver a embarcar
> una línea **completa** que había sido sacada de un embarque anterior. El candado protege la
> partición, así que se aplica a las particiones.

## Lo que NO se copió de Grupo AM

El código de partición de Grupo AM es anterior al estándar que Monza fijó en la Fase 2. Se copió
la lógica; **no** estos seis vicios:

| Vicio en Grupo AM | Qué hace Monza |
|---|---|
| sin candado de concurrencia | `FOR UPDATE` en orden de id |
| corrige en silencio una cantidad imposible | la **rechaza** con un mensaje que dice cuál es el techo |
| `cantidad = 0` cae a "toda la línea" | la rechaza |
| un id inexistente o repetido se salta callado | los rechaza |
| acepta decimales donde la columna es entera | los rechaza |

Y una comprobación extra que Monza agregó: si la foto de precios de una línea es incoherente
(`subtotal ≠ cantidad × precio`), **no se parte** — se rechaza. Aplicar la fórmula sobre datos
incoherentes violaría el propio invariante.

## Arreglo adyacente: el badge de la venta mentía

`monza_router_ventas.py` clasificaba el avance de una venta con una lista de estados que **no
incluía `preparado` ni `embarcado`**, así que una venta con todo preparado se mostraba como
*"cotizado"*. Ya era un error; con la partición habría pasado de esporádico a permanente
(el remanente sería el estado mínimo de casi toda venta partida). Corregido en el mismo lote,
con `reclamo` como último estado y los desconocidos ignorados en vez de arrastrar la venta al
principio.

## Pruebas

```bash
cd backend
./venv/bin/python -m pytest monza_tests/test_preparar_parcial_monza.py -q
```

Cubre el invariante de plata, la herencia de la foto, la cura del reclamo fantasma de punta a
punta (contrastada contra el comportamiento viejo), los 7 candados de documento, el candado de
estado, los vicios que no se copiaron, el tope físico y la concurrencia con hilos reales.

Se reparó además una **suite invisible**: `monza_contabilidad/tests/test_concurrencia_plata.py`
tenía sus 9 verificaciones sin el envoltorio que pytest necesita para encontrarlas, así que no
corrían en el gate. Es la segunda vez que aparece esa clase de problema — conviene revisarlo
cuando se agregue una suite nueva.

## Despliegue

**Nada que correr en la base de datos.** Solo `npm run build` del frontend (cambian cuatro
pantallas: Abastecimiento, Seguimiento, Logística y Ventas).
