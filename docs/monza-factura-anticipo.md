# MonzaParts · Factura de ANTICIPO (vía B) — Fase 7 del espejo Grupo AM

**Fecha:** 2026-07-29 · **Referencia:** `docs/adelantos-clientes-grupo-am-2026-07-16.md` y
`backend/tests_contabilidad/test_factura_anticipo.py` (el contrato vivo de Grupo AM),
`docs/plan-espejo-monza-2026-07-23.md` (Fase 7).

## El problema que resuelve

El cliente paga un adelanto **antes** de que llegue la mercadería y pide una **factura** por
ese pago (no le sirve un comprobante interno: la necesita para su contabilidad y su IVA).

Hasta la Fase 6, Monza solo tenía la **vía A**: el adelanto se aplicaba como *cobranza*
`medio='adelanto'` sobre las facturas reales. Funciona para la plata, pero no emite ningún
documento tributario del anticipo.

La **vía B** agrega ese documento — y el problema de fondo es **no cobrar dos veces**:
si se factura el anticipo Y después se factura toda la mercadería, el cliente recibe
facturas por más que el total de la venta.

## La regla de oro

> **Σ brutos de las facturas de la venta == total de la venta.** Siempre.

Se consigue así: cuando se factura el despacho real, el sistema **descuenta solo** el
anticipo con una **línea negativa** que cita el folio de la factura de anticipo.

```
Venta: neto 200.000 + IVA 19% = 238.000

1) Factura de ANTICIPO (sin guía)        neto  50.000 + IVA  9.500 =  59.500
2) Factura del despacho real             neto 200.000
   − línea "DESCUENTO" (cita el folio)        −50.000
                                         ───────────
                                         neto 150.000 + IVA 28.500 = 178.500
                                                       Σ brutos  =  238.000  ✔
```

El IVA de la factura final se calcula **sobre el neto ya descontado** — si se calculara sobre
el neto completo, el cliente pagaría dos veces el IVA del anticipo.

## Un anticipo por venta (y la puerta explícita)

Emitir al SII es **irreversible**. Como en Monza el adelanto es **uno por venta**, una
segunda factura de anticipo es casi siempre un error de operación — dos documentos
tributarios reales por la misma plata. Por eso el sistema **bloquea** el segundo y nombra
al primero:

> *Esta venta ya tiene una factura de anticipo (N° 1187, $59.500). En Monza el adelanto es
> uno por venta: si de verdad necesitas un segundo anticipo, márcalo explícitamente.*

La puerta explícita existe (`confirmar_segundo_anticipo`, la casilla del modal), porque
arrinconar al operador tampoco sirve. Grupo AM no tiene este guard: allá una OC admite
**N adelantos**, así que varios anticipos son legítimos.

El candado anti doble emisión del módulo SII **no bastaba**: solo dura mientras la llamada
HTTP está en vuelo. Apenas responde la primera emisión, la segunda pasaba libre.

## La única excepción a la regla rectora

En todo el sistema **una factura nace de una guía de despacho firmada**. La factura de
anticipo es la **única excepción**: no hay mercadería todavía. Por eso:

- `despacho_id` se fuerza a `None` aunque venga en el payload,
- lleva **una sola línea** `ANTICIPO`, **sin** `item_cotizacion_id` ni `despacho_item_id`
  (así no consume los topes físicos por ítem ni por guía),
- ante el SII va como **DTE 33 normal** con una sola referencia: la **801** (OC/venta).
  Nunca lleva referencia 52 (guía) ni 33 (no descuenta nada).

**La excepción es la GUÍA, no el receptor.** Si falta el RUT o la razón social del cliente,
bloquea igual que una factura normal — el SII rechaza igual. En Monza el receptor se
completa **en la venta** (la Fase 3 dejó el RUT obligatorio en el Cierre de Venta), no en el
modal.

## Diferencia de diseño con Grupo AM (deliberada)

Grupo AM tiene **N adelantos por OC** y por eso lleva la columna
`cont_adelanto.factura_anticipo_id` para saber qué adelanto respalda a qué factura de
anticipo.

**Monza tiene UN adelanto por venta** (`UNIQUE` sobre `monza_cont_adelanto.cotizacion_id`) y
la fila del adelanto **la crea Tesorería al aprobar** — no existe un adelanto "informado"
como fila; ese estado vive en `MonzaCotizacion.pct_adelanto`.

Por eso en Monza **no se agregó esa columna**: con un solo adelanto por venta, el ruteo
correcto del dinero se obtiene **ordenando las facturas con el anticipo primero** al aplicar
(ver abajo), y el vínculo adelanto ↔ factura de anticipo se **DERIVA** (las facturas
`es_anticipo=1` de la venta). Cero estado que pueda quedar obsoleto.

El comportamiento observable es idéntico al de Grupo AM, **incluido el excedente**.

## El flujo completo

```
Comercial cierra la venta e informa el % de adelanto   → MonzaCotizacion.pct_adelanto
        ▼
Contabilidad → Facturas → "Factura de anticipo"
        │  emite el DTE 33 sin guía (o registra uno ya emitido, vía manual)
        ▼
El cliente paga → TESORERÍA aprueba el adelanto
        │  la plata cae PRIMERO en la factura de anticipo (queda pagada)
        │  el EXCEDENTE (si el adelanto es mayor) fluye a las facturas del despacho real
        ▼
Llega la mercadería → guía firmada → factura del despacho real
        │  el sistema le descuenta el anticipo SOLO (línea negativa + referencia 33)
        ▼
Σ brutos de la venta == total de la venta · el cliente no pagó dos veces
```

**En la factura de anticipo el adelanto entra como COBRANZA. En la factura final entra como
DESCUENTO.** Son cosas distintas: la primera es plata recibida, la segunda es un ajuste de
neto. Por eso la factura final **no** lleva cobranza `medio='adelanto'` cuando el anticipo ya
absorbió todo el adelanto.

### El orden no importa (re-encauce del adelanto)

El flujo de arriba supone que el anticipo se emite antes de que Tesorería apruebe. En la
práctica el orden se invierte todo el tiempo: Tesorería aprueba el lunes y Contabilidad
emite el anticipo el martes. Cuando eso pasa, la plata ya cayó en la factura del despacho.

El sistema la **re-encauza sola**: al nacer la factura de anticipo, libera de las facturas
normales lo justo para saldarla y la vuelve a aplicar donde corresponde. El excedente se
queda donde ya estaba bien. No es plata que el operador movió — la cobranza
`medio='adelanto'` la genera el sistema (registrarla a mano está prohibido), así que el
sistema puede re-encauzarla.

Si algún candado lo impide (la otra factura está en factoring, o su cobranza ya está
conciliada con el banco), **no falla en silencio**: emite igual y devuelve una advertencia
que dice exactamente qué hacer. Esa advertencia viaja por las dos vías, la manual y la del
SII.

**Una cobranza manual sobre una factura de anticipo se rechaza** (409): su única forma
legítima de saldarse es el adelanto que aprueba Tesorería. Sin ese guard, un administrativo
saldaba el anticipo con la transferencia del cliente, Tesorería veía la factura sin saldo y
mandaba el adelanto a otra factura — el mismo depósito contado dos veces y la venta dada por
cobrada con la mitad de la plata.

## Qué cambió

### Datos (2 columnas nuevas)

| Tabla | Columna | Para qué |
|---|---|---|
| `monza_cont_factura_cliente` | `es_anticipo INT DEFAULT 0` | marca la factura de anticipo |
| `monza_cont_factura_cliente_item` | `anticipo_factura_id INT NULL` | la línea de descuento apunta al anticipo que descuenta |

La FK de `anticipo_factura_id` va **sin `ON DELETE`** a propósito: la base de datos bloquea
borrar una factura de anticipo ya descontada, como segundo cinturón del 409 explícito.

**El "anticipo pendiente de descontar" no se guarda: se DERIVA** —
`monto_neto del anticipo − Σ(−total_neto) de las líneas que lo referencian`. Por eso borrar
la factura final restaura el descuento **solo**, por cascade, sin código de reversión.

### Contabilidad (`backend/monza_contabilidad/`)

- `_anticipos_pendientes_de_descuento` (`router.py:169`) — el pendiente derivado, FIFO por id.
- `_construir_factura_anticipo` (`router.py:1333`) — valida y arma la factura de anticipo.
- Bloque de **descuento automático** en `_construir_factura` (`router.py:1195-1258`) —
  aplica a los tres modos de facturación (ítems explícitos, retiro en oficina, despacho).
- `crear_factura`: anticipo como **boleta → 400**; ruteo al constructor de anticipo.
- `set_factoring`: factoring sobre una factura de anticipo → **409** (respalda plata ya recibida).
- `eliminar_factura`: anticipo **ya descontado → 409**; ahora toma el lock de la cotización
  (orden global cotización → factura), si no la carrera salía como error 500 en vez de un 409
  explicado.
- Agregados de la venta (detalle **y** listado): `anticipo_por_descontar_clp` real (en bruto),
  `mercaderia_pendiente_clp` autoritativo y
  **`por_facturar = mercadería pendiente − anticipo por descontar`** (clamp ≥ 0).
  La base sigue siendo **FÍSICA** (regla de oro G15): jamás `total vivo − Σ brutos`.

### Tesorería (`backend/monza_tesoreria/router.py:328`)

`_aplicar_adelanto_a_facturas` recorre ahora las facturas con
`order_by(es_anticipo.desc(), id.asc())`.

**No es cosmético.** Al saldarse el anticipo en la misma pasada, el excedente del adelanto
queda libre para las facturas del despacho real dentro de la misma transacción. Con el orden
por id a secas, un anticipo emitido *después* de una factura normal se saldaba último y el
excedente quedaba atrapado: la factura de anticipo impaga y la deuda del cliente
sobrestimada. Verificado con experimento de control (con el cambio pasa, sin él falla).

Ojo para quien escriba pruebas: el invariante
`adel.monto_aplicado == Σ cobranzas medio='adelanto'` **se cumple igual con el ruteo malo**.
Hay que assertear el **saldo de cada factura** por separado.

### SII / Wasabil (`backend/monza_wasabil_dte/`)

La línea negativa **no viaja al SII**: el API rechaza `price < 0` y `quantity < 0`, y no tiene
descuento a nivel de documento. Viaja como **`discount` porcentual por línea**, repartido de
mayor a menor total, con la **precisión completa del float** (Wasabil calcula con la
precisión enviada; redondear el porcentaje rompería la cuadratura neto DTE == neto local).

- `aplicar_descuento_lineas` (`service.py:541`) — el reparto y sus dos bloqueos: descuento
  mayor que las líneas, y **piso de $1** (bajo un peso el neto llega al SII como $0 y lo
  rechaza). El piso se evalúa sobre el total **ya descontado**.
- `armar_referencias_factura(..., anticipos=)` — **referencia 33** con folio, fecha y motivo
  `"Descuento anticipo"`. Es la única de las tres referencias que conserva motivo: el tipo y
  el folio no explican *por qué* se referencia esa factura (formato v3).
- **Un anticipo sin folio SII bloquea en dos capas**: Contabilidad ni siquiera lo mete en los
  descuentos, y el guard del placeholder `#<id>` ataja el reintento de una factura ya
  persistida. Ni se descuenta (citaría un folio inexistente) ni se ignora (cobraría dos veces).
- **El folio de un anticipo tiene que ser numérico.** El SII exige `FolioRef` numérico al
  referenciar un DTE, y el folio se teclea a mano al registrar una factura de anticipo ya
  emitida. Se valida **al registrarla**, no al facturar el despacho — si no, el error
  aparecía tarde y lejos, con el folio propio ya consumido.
- **El reparto del descuento se mide en PESOS half-up**, el mismo dominio en que Contabilidad
  calculó el neto local. Medirlo en centavos perdía medio peso al consumir una línea
  terminada en `,5` y el documento salía al SII **$1 por debajo del libro de ventas**.
- El **candado anti doble emisión por venta** (`_emision_33_en_vuelo_de_cot`) ya existía y
  cubre el anticipo — es la única defensa aquí, porque cada request de anticipo crearía una
  factura nueva y no hay tope de mercadería que lo frene.

### Frontend

- **Facturas**: botón y modal "Factura de anticipo" (dos modos: emitir al SII o registrar una
  ya emitida), badge de anticipo en la lista, y el bloque de descuento en el modal de emisión.
- **Ventas — Contabilidad**: la barra de avance ya descuenta el anticipo de "por facturar",
  badge de anticipo, `↩` en las líneas de descuento y el trazo "respaldo Factura N° …" junto
  al adelanto.

## Pruebas

```bash
cd backend
./venv/bin/python -m pytest monza_contabilidad/tests/test_factura_anticipo.py -q
./venv/bin/python -m pytest monza_contabilidad/tests/test_regresiones_bloque_a.py -q
./venv/bin/python -m pytest monza_wasabil_dte/tests/test_factura_anticipo_sii.py -q
```

La primera cubre los 17 puntos del contrato de Grupo AM traducidos a Monza (emisión sin guía,
topes, descuento automático, reversiones, guards, factura final en $0 y el excedente) contra
la base de datos real, con datos marcados y limpieza total. La segunda fija las reparaciones
del multienjambre (un anticipo por venta, cobranza manual rechazada, tolerancia plana,
re-encauce del adelanto, boleta bloqueada, folio numérico). La tercera cubre el armado del
DTE con **fakes** (`issue=False`, nunca toca el SII): reparto porcentual, cuadratura exacta
neto DTE == neto local, piso de $1, bloqueos por folio faltante o no numérico, y las
referencias.

Una advertencia sobre los invariantes: `adel.monto_aplicado == Σ cobranzas 'adelanto'`
**se cumple igual cuando la plata está en la factura equivocada**. Los tests del ruteo
assertean el **saldo de cada factura por separado**; el invariante de la suma no detecta esa
regresión.

## Despliegue

**Antes de reiniciar el backend** (idempotente):

```bash
cd backend && python -m monza_contabilidad.init_db
```

Agrega `es_anticipo` (NOT NULL DEFAULT 0), `anticipo_factura_id`, su índice y su FK. Correr
**aunque `MONZA_CONTAB_ENABLED` vaya apagado**. Requiere `npm run build` del frontend
(cambian dos pantallas).

> **Si se reinicia el backend sin correr esto**, Contabilidad Monza cae entera con
> `Unknown column 'es_anticipo'` — pero el usuario solo ve *"Internal Server Error"* en
> Facturas, Ventas y KPIs. El `create_all` del arranque **no agrega columnas a una tabla que
> ya existe**. Es el error más caro de este despliegue y el más fácil de evitar.

## Deuda declarada (verificada, no silenciada)

- **Grupo AM tiene el mismo hueco de la cobranza manual** sobre una factura de anticipo.
  Aquí quedó cerrado; allá no lo toqué (no se pidió tocar GA en esta fase).
- **La suite de anticipos de Grupo AM no detectaría la regresión del ruteo**: en sus dos
  escenarios de excedente el anticipo siempre se emite primero, así que el orden viejo daba
  el mismo resultado. La suite de Monza quedó estrictamente más fuerte en ese punto.
- **Líneas con `discount: 100%`** (el anticipo cubre entera la línea más grande): el piso de
  $1 protege el total del documento, no la línea. Grupo AM se comporta idéntico y ya emitió
  real sin problemas — pero conviene confirmarlo con un borrador `issue:false` antes de la
  primera emisión real de una factura con descuento.
- **Anticipo con centavos**: se acepta (paridad con GA) y deja un desfase sub-peso en el
  reparto. El total de la venta sigue cuadrando.
- **Venta anulada con un anticipo vivo**: la venta desaparece del listado y la factura queda
  huérfana en pantalla. Es preexistente; la vía B lo hace más probable porque el anticipo se
  emite antes de que haya despachos.
