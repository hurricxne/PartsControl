# Adelantos de clientes — Grupo AM (2026-07-16)

> Integrado a la carpeta principal (`Parts control actual/PartsControl-main`, rama
> `feature/adelantos-clientes`) el 2026-07-16, fusionado con los endurecimientos de
> Despachos/facturas y la OC-cliente obligatoria de las ramas anteriores.

Módulo construido para cubrir los dos casos que pidió el dueño:

1. **Se cierra una OC y el cliente abona un adelanto SIN factura nuestra todavía.**
2. **Se cierra una OC y el adelanto está respaldado por una factura que emitimos**
   (factura de anticipo, sin guía de despacho).

Antes de esto no existía forma de registrar plata de un cliente sin factura: toda
cobranza exige factura y toda factura exige guía firmada. El diseño replica y adapta el
mecanismo de adelantos de MonzaParts (`monza_cont_adelanto`), con tres diferencias
pedidas por el dueño: **varios adelantos por OC**, **sin cortafuego de compras**, y la
**factura de anticipo formal con descuento automático**.

## Flujo (cómo se usa)

### Caso 1 — adelanto sin factura (vía A)
1. **Cierre de Venta**: Comercial marca "Esta venta tiene adelanto" (% o monto). También
   se puede informar después desde **Contabilidad → Ventas** (botón "Informar adelanto").
2. **Tesorería → pestaña Adelantos**: aprueba confirmando monto real, fecha, banco y N°
   de operación. **No necesita la cartola subida.**
3. **Tesorería → Conciliar**: cuando llega la cartola, el abono se concilia contra el
   adelanto (sugerencia automática por monto).
4. **Facturas y Cobranzas**: al emitir la factura de la guía firmada, el sistema aplica
   el adelanto solo (cobranza `medio='adelanto'`); la factura nace con ese monto ya
   descontado del saldo. Si el adelanto es mayor que la factura, el resto queda para la
   siguiente.

### Caso 2 — adelanto con factura de anticipo (vía B)
1. **Facturas y Cobranzas → botón "Factura de anticipo"**: OC + monto neto + folio SII,
   **sin guía de despacho** (única excepción a la regla rectora). Se liga a los
   adelantos que respalda.
2. Cuando Tesorería aprueba el adelanto, la factura de anticipo queda **pagada**
   automáticamente (si ya estaba aprobado, queda pagada al emitirla).
3. Al facturar el despacho real, la factura sale con la **línea de descuento negativa**
   "Descuento anticipo Factura N° X": Σ facturas de la OC = total de la venta, siempre.
   El saldo de esa factura final es lo que el cliente aún debe.
4. **Excedente**: si el adelanto aprobado es MAYOR que el bruto de su factura de
   anticipo (el cliente pagó 70.000 pero el anticipo se emitió por 59.500), el
   excedente se aplica a las facturas del despacho real una vez saldado el anticipo —
   misma regla que la vía A ("el resto queda para la siguiente"). La plata del cliente
   nunca queda atrapada en el vínculo al anticipo.

## Diseño técnico (resumen)

- Tabla nueva `cont_adelanto` (`models/models.py`): estados `informado → aprobado →
  anulado`; derivados `conciliado_banco` (enlace) y `pendiente` (monto − aplicado).
  `factura_anticipo_id` distingue la vía (NULL = A).
- Columnas aditivas: `cont_factura_cliente.es_anticipo`,
  `cont_factura_cliente_item.anticipo_factura_id` (línea de descuento),
  `cont_cobranza.adelanto_id` (reversión exacta),
  `conc_conciliacion_ingreso.adelanto_id` + `cobranza_id` NULLABLE + CHECK
  exactamente-uno + UNIQUE. Migración idempotente: `python -m tesoreria.init_db`
  (**correr en el deploy, ANTES de reiniciar el backend**: el código nuevo consulta
  las columnas nuevas al listar Facturas/Ventas y fallaría contra el esquema viejo).
- Regla de aplicación única: `routers/contabilidad._aplicar_adelantos_pendientes`
  (llamada desde `crear_factura` y desde la aprobación en Tesorería; cap por saldo;
  con **factoring vigente NO aplica** — igual que el guard de `registrar_cobranza`:
  el saldo de esa factura es la retención del factor, no deuda del cliente). En la
  factura normal entran los adelantos vía A y el excedente de los ligados con
  anticipo ya saldado; `aprobar_adelanto` recorre los anticipos primero para
  liberar ese excedente en la misma pasada.
- Anti-doble-conteo: cobranzas `medio='adelanto'` EXCLUIDAS de la conciliación de
  ingresos (su plata se concilia abono↔adelanto). Rechazo de `medio='adelanto'` manual.
- Descuento derivado (no acumulado): pendiente de descontar = neto de la factura de
  anticipo − Σ líneas negativas que la referencian → borrar una factura final lo
  restaura solo. Factura de anticipo descontada no se puede borrar (409 + FK).
- Guardas: factoring sobre anticipo 409; anular/re-aprobar aplicado o conciliado 409;
  Σ adelantos ≤ total venta; Σ brutos facturas ≤ total venta; factura final en $0
  permitida con advertencia (anticipo cubría todo).
- Locks `with_for_update` sobre OC + adelanto + facturas en todos los caminos de
  escritura (espejo del patrón existente).

## Endpoints nuevos

Contabilidad (`/api/contabilidad`): `POST /ventas/adelantos` (informar; acepta
`cotizacion_id`), `GET /ventas/{oc}/adelantos`, `PATCH /adelantos/{id}`,
`POST /adelantos/{id}/anular`. `POST /facturas` y `/facturas/preview` aceptan
`es_anticipo`, `monto_neto_anticipo`, `adelanto_ids`.

Tesorería (`/api/tesoreria`): `GET /aprobaciones`, `POST /adelantos/{id}/aprobar`,
`GET /adelantos-pendientes`; `conciliar` acepta `{adelanto_id}`; `sugerencias`,
`cobranzas-pendientes`, `flujo-caja` y `resumen` extendidos.

## Frontend

CierreVentaPage (sección Adelanto), TesoreriaPage (pestaña Adelantos + conciliación
clase 'adelanto' + KPI), FacturasPage (botón y modal "Factura de anticipo" + chip
Anticipo; el preview del flujo normal muestra el descuento solo), VentasContabPage
(badge + lista de adelantos + "Informar adelanto").

## Verificación

- `backend$ ./venv/bin/python -m pytest tests_contabilidad wasabil_dte/tests compras_contab/tests tesoreria/tests embarques_pricing/tests routers/tests -q` →
  **54 passed** (incluye las 2 suites nuevas: `test_adelantos.py`,
  `test_factura_anticipo.py`; cero regresiones en el resto del repo).
- `frontend-src$ npm run build` (tsc + vite) limpio.

## Pendientes / notas

- La factura de anticipo se registra con folio manual (como todas). Cuando se
  construya Wasabil Fase B (facturas 33), considerar el tipo anticipo en el armado.
- No hay cuenta contable "Anticipos de clientes" en el plan de cuentas del Excel del
  dueño (no existe libro diario en el ERP); el efecto se captura vía cobranzas y
  descuentos. Si algún día se agrega el libro diario, crear la cuenta de pasivo.
