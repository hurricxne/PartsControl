# Deudas DIFERIDAS por decisión del dueño

Cosas que se investigaron, se verificaron y **se decidió NO hacer por ahora**. No son
descuidos: son decisiones tomadas con la evidencia a la vista. Este documento existe para que
nadie las vuelva a investigar desde cero y para que, el día que cambie la condición que las
hace tolerables, se sepa exactamente qué hacer.

**Fecha de la decisión:** 2026-07-29 (Fase 9 del espejo Monza).

---

## 1 · Candado de empresa entre marcas (routers operativos)

**Decisión: NO candar todavía.**

### Qué pasa hoy

Un usuario autenticado de una marca puede llamar por API a los módulos operativos de la otra.
Verificado en vivo con `TestClient` (solo lecturas, sin escribir): un usuario con
`empresa='mineria'` recibe **200** en `/api/monza/clientes`, `/api/monza/abastecimiento/proveedores`,
`/api/monza/logs`, `/api/monza/config` y `/api/monza/notificaciones`. Y **el hueco es simétrico**:
un usuario `automotriz` recibe 200 en `/api/clientes`, `/api/ventas`, `/api/bodega/embarques` y
`/api/notificaciones` de Grupo AM.

Estado real del candado (no lo que decía el plan):

| Marca | Con candado a nivel de router | Sin candado |
|---|---|---|
| Monza | Despachos, Bodega (Fase 6) + 1 endpoint de Cotizaciones (F3) | 12 routers · 68 endpoints |
| Grupo AM | contabilidad, despachos, tesorería, wasabil | cotizaciones, ventas, facturas, **bodega**, clientes, cotizador, y 32 de 33 endpoints de compras |

### Por qué es tolerable hoy

Solo existen **2 usuarios en la base, ambos admin**: `admin@grupoam.cl` (mineria) y
`admin@monzaparts.cl` (automotriz). El dueño es el único que puede usar ambas. El registro
público ya está cerrado (`backend/routers/auth.py:44-70`: exige sesión y hereda la empresa del
creador), así que nadie de fuera puede crearse una cuenta.

### Qué lo vuelve urgente

**El día que se dé de alta un usuario operativo por marca** — un vendedor de Monza, un
bodeguero de MachParts. Desde ese momento cada uno puede leer los márgenes, los clientes con
RUT y teléfono, y la bitácora completa de la otra marca.

### Si se decide hacerlo, en este orden

1. **`monza_router_config.py`** (2 endpoints) — el único sin candado que toca el SII y el cobro.
   `PUT /api/monza/config` escribe `rut_empresa`, `razon_social`, `numero_cuenta`, `iva_pct` y
   el TC. Esa fila alimenta el emisor del DTE (`monza_wasabil_dte/router.py:121-122`), la tasa
   de IVA de facturación, el TC del costo landed y la foto de precios de cada venta nueva.
   Cambiar ahí el RUT emite documentos tributarios con el emisor equivocado; cambiar la cuenta
   bancaria manda a los clientes a pagar a otra cuenta.
2. `monza_router_cotizaciones.py` — candar el router COMPLETO (hoy va 1 de 7) y borrar el
   `Depends(require_empresa)` individual de la línea 358 para no duplicarlo.
3. Datos de personas: `monza_router_leads.py` (PII + DELETE), `monza_router_clientes.py`,
   `monza_router_documentos.py` (borra el archivo del disco con `os.remove`),
   `monza_router_logs.py`.
4. Integridad del pipeline: `monza_router_abastecimiento.py`, `monza_router_logistica.py`.
5. Bajo impacto: catalog, cotizador, ventas, notificaciones.
6. **Y el lado de Grupo AM**, empezando por `routers/bodega.py` — candar solo Monza deja la
   mitad del problema.

Molde: `backend/empresa_guard.py:17-31`, aplicado como en `backend/monza_router_bodega.py:29-33`.

### DOS TRAMPAS (leer antes de tocar)

- **`monza_router_integraciones.py` NO se canda a nivel de router.**
  `POST /api/monza/integraciones/nexor/leads` es el **único** endpoint de Monza sin
  `get_current_user`: se autentica con `X-API-Key` (`monza_router_integraciones.py:77-88`). Un
  dependency de router le inyectaría `get_current_user` y **Nexor dejaría de poder crear
  leads, en silencio**. Ahí hay que candar solo `GET /nexor/log`, endpoint por endpoint.
- Las suites que tocan esos routers deben setear `empresa='automotriz'` en su override, o
  empiezan a fallar con 403 (no es un bug, pero se ve como uno). Y agregar un test de 403 por
  router nuevo, como los que ya existen: `monza_tests/test_aud_despachos.py:234`,
  `monza_tests/test_aud_pipeline.py:344`.

Contexto histórico: el propio código lo declara. `monza_router_abastecimiento.py:24-29` dice
que el router queda a propósito sin candado *"porque el dueño difirió explícitamente el candado
de los routers del programador"*.

---

## 2 · Cotización de MonzaParts inmutable y congelada al CREAR

**Decisión: dejarlo así.**

### Qué pasa hoy

MonzaParts **congela la foto de precios al CREAR** la cotización, no al Cerrar la venta como
Grupo AM (`monza_router_cotizaciones.py:263-296`: congela `tc_usd_clp`, `tc_eur_clp`,
`moneda_tarifa`, `tarifa_aerea`, `iva_pct` en la cabecera y `tc_aplicado`, `markup_pct`,
`precio_unitario_clp`, `subtotal_clp` por ítem, con el TC resuelto en el servidor). El PDF y
toda la contabilidad leen esa foto (`monza_router_cotizaciones.py:830-833`,
`monza_contabilidad/router.py:9`), así que el congelado **funciona y es consistente**.

Dos consecuencias:

1. Una cotización en propuesta **nunca** se re-precia con el dólar de hoy. Si el cliente acepta
   tres semanas después, se vende con el dólar del día en que se creó.
2. **No se puede editar**: `CotUpdate` (`monza_router_cotizaciones.py:57-70`) solo acepta
   estado, OC, fechas, forma de pago, % de adelanto y datos de facturación. No hay forma de
   corregir un precio, una cantidad ni de agregar un ítem, y no hay historial de versiones.
   Para cambiar algo hay que rehacer la cotización.

### Por qué es tolerable

Las cotizaciones de Monza se cierran rápido, así que el dólar no alcanza a moverse lo
suficiente, y rehacer una cotización no es costoso en ese flujo de trabajo.

### Si se decide hacerlo

- **Opción acotada (recomendada si aparece la molestia):** un botón "actualizar al dólar de hoy"
  que recalcule la foto mientras la cotización siga en `propuesta`. Resuelve el dólar viejo sin
  abrir la puerta a editar precios a mano.
- **Opción completa:** el editor y el versionado de Grupo AM —
  `backend/routers/cotizador.py:222` (PUT de ítem), `:289` (`/formal`), `:713` (`/terminos`),
  `:733` y `:776` (`/versiones` y `/restaurar`).

**Ojo para quien lo tome:** NO portar el snapshot de Grupo AM encima. Monza **ya congela**, en
otro momento del flujo y con otra semántica. Portarlo duplicaría la lógica. Falta escribir
`docs/monza-tc-congelado.md` explicando esa diferencia — `docs/tc-congelado-cotizacion.md` hoy
solo dice "MonzaParts ya congela por su cuenta" sin decir dónde ni cómo.
