# Plan espejo Grupo AM → MonzaParts — roadmap por fases

**Fecha:** 2026-07-23 · **Mapeo:** enjambre de 7 revisores por área + orquestador · **Gaps totales:** 73 (53 ausentes, 20 parciales)

> Fase 1 (blindaje de concurrencia del flujo de plata de Monza) HECHA — commit c4bd1b4.
> Este documento es la fuente de verdad del plan; actualizar el estado de cada fase al completarla.

## Resumen ejecutivo

Dónde estamos: Monza ya tiene el esqueleto de todos los módulos y, con la Fase 1 ya terminada, el flujo de plata (cobranzas y adelantos) quedó blindado contra choques cuando varias personas trabajan a la vez. Frente a Grupo AM faltan 73 piezas (53 no existen y 20 están a medias); pero varias son detalles y otras son decisiones de negocio, no errores.

El hueco más grande y de mayor valor: Monza NO emite NINGÚN documento electrónico al SII — hoy las guías de despacho y las facturas se teclean a mano. Replicar la emisión al SII (Wasabil) es el premio grande, pero antes hay que asegurar dos cosas: (a) que la mercadería que sale nunca supere la que realmente llegó a bodega, y (b) que los datos del cliente estén bien (RUT válido, y N° + fecha de la orden de compra), porque el SII rechaza documentos con datos incompletos.

Qué falta, en orden lógico: 1) blindar Despachos y Bodega para que jamás se entregue ni facture más de lo recibido, y para que una llegada parcial deje despachable lo que sí llegó (hoy manda toda la línea a reclamo) — esto es lo más urgente y no depende de nadie externo; 2) dejar los datos del cliente listos para facturar (además arregla un error de hoy: se pueden emitir facturas con RUT vacío); 3) mostrar la plata de cada venta (cuánto se cobró, cuánto está facturado sin cobrar y cuánto falta por facturar); 4) emitir GUÍAS al SII; 5) emitir FACTURAS al SII; 6) factura de anticipo con descuento automático (solo si sus clientes la piden); 7) compras a proveedores chilenos (solo si Monza compra en Chile); 8) afinar detalles de costos de importación y tesorería.

Recomendación: empezar YA por la Fase 2 (integridad de despachos), y EN PARALELO iniciar el trámite para abrir la cuenta Wasabil de MonzaParts SpA (RUT 78.121.316-0), porque ese trámite externo es el que más demora y es indispensable para poder emitir al SII en las Fases 5 y 6.

> **2026-07-28 — Auditoría de paridad de lo construido:** 27 divergencias reales Monza<GA
> reparadas a 0 (tope Σ brutos, retroactivo del adelanto, locks factoring, folio obligatorio,
> _hoy_chile, half-up, snapshot desconciliar, des-cierre, suites visibles a pytest, etc.) +
> e2e 'viaje de la plata' espejo. 33 puntos de paridad INVERSA (Monza más robusto que GA)
> anotados en la Fase 9.

## Fases

| Fase | Nombre | Impacto | Esfuerzo | Depende de | Estado |
|---|---|---|---|---|---|
| 1 | Blindaje concurrencia flujo de plata | CRITICO | MEDIO | — | **HECHA** (c4bd1b4) |
| 2 | Integridad física de Despachos y Bodega (lo que sale ≤ lo que llegó) | CRITICO | ALTO | ninguna (Fase 1 ya está hecha) | **HECHA** 2026-07-23 (ver docs/monza-flujo-bodega-despachos.md; doble enjambre a 0) |
| 3 | Datos maestros para facturar bien (OC del cliente + RUT/receptor) | ALTO | MEDIO | ninguna dura; se ordena antes de las Fases 5 y 6 (emisión SII) | **HECHA** 2026-07-28 (docs/monza-cierre-venta-datos-maestros.md; incluye fix del pct_adelanto perdido) |
| 4 | Visibilidad G15 de Ventas–Contabilidad + tableros de Despachos | ALTO | MEDIO | Fase 2 (reutiliza los estados de despacho ya confiables y el mismo cálculo de avance) | **HECHA** 2026-07-28 (docs/monza-avance-plata-despachos.md; regla de oro base física) |
| 5 | Emisión electrónica al SII: GUÍAS de despacho 52 (Wasabil) | CRITICO | ALTO | Fase 2 (despacho confiable) + Fase 3 (OC con N°+fecha para la ref 801 y receptor válido) + cuenta/token Wasabil de Monza | **HECHA** 2026-07-29 (docs/monza-guias-sii.md; paquete monza_wasabil_dte/; multienjambre a 0) |
| 6 | Emisión electrónica al SII: FACTURAS 33 (Wasabil) | ALTO | ALTO | Fase 5 (las guías se emiten primero; la factura 33 las referencia) + Fase 4 (preview/fuente única) + Fase 3 (receptor válido) | **HECHA** 2026-07-29 (`76ecd93` + `a1fbee6`; docs/monza-facturas-sii.md). Incluye la auditoría integral de las Fases 1→6: 20 hallazgos, entre ellos un CRÍTICO (Tesorería tenía copia propia de adelantos sin guard SII) y un deadlock Conciliación↔Cobranzas |
| 7 | Adelantos Vía B: factura de anticipo + descuento automático | MEDIO | ALTO | Fase 6 (necesita la maquinaria de factura 33 y un folio SII referenciable para el anticipo y su descuento) | **HECHA** 2026-07-29 (`f28234e`; docs/monza-factura-anticipo.md). Adaptación al modelo de Monza: vínculo DERIVADO en vez de columna, porque acá el adelanto es UNO por venta y lo crea Tesorería. Multienjambre de 6 auditores → 21 hallazgos reparados, incluido un CRÍTICO (se podían emitir N facturas de anticipo REALES por el mismo adelanto) |
| 8 | Compras nacionales (condicional al negocio) | MEDIO | ALTO | Fase 2 (el tope físico de despacho debe existir para engancharle el UNION de lo recibido nacional) | **HECHA** 2026-07-28 (docs/monza-compras-nacionales.md; decisión del dueño: Monza SÍ compra en Chile) |
| 9 | Paridad fina de Embarques Pricing y Tesorería | MEDIO | MEDIO | ninguna dura (piezas independientes que se pueden intercalar como relleno) | **HECHA** 2026-07-30 — 8 commits, ver la sección de la Fase 9 más abajo. OJO: el plan tenía DOS FALSAS ALARMAS y el reconocimiento empírico las descartó (snapshot bancario ya estaba hecho; el AWB no necesitaba columna nueva). Aparecieron además 2 huecos que el plan NO listaba, uno de ellos costoso (Monza no tenía NINGUNA alerta automática) |

### Fase 2: Integridad física de Despachos y Bodega (lo que sale ≤ lo que llegó)

**Impacto:** CRITICO · **Esfuerzo:** ALTO · **Depende de:** ninguna (Fase 1 ya está hecha)

**Objetivo:** Garantizar que Monza nunca despache ni facture más unidades de las que Bodega recibió, que las llegadas parciales dejen despachable lo que sí llegó (y solo el faltante real vaya a reclamo), y que un despacho errado se pueda corregir o anular. Es corrección de integridad, no cosmética.

**Incluye:**
- Tope físico de despacho = min(vendido, recibido) − ya_despachado, consumiendo qty_recibida de recepciones cerradas (hoy qty_recibida es dato muerto; monza_router_despachos.py:188 solo filtra estado_linea)
- Cierre de recepción PARTICIONADO: lo recibido queda en bodega despachable; solo el faltante REAL va a reclamo (hoy la línea entera cae a reclamo, monza_router_bodega.py:184-195)
- Reclamo del faltante real acumulando recepciones previas (una línea en 2 embarques o una reposición no genera reclamo fantasma)
- 'completo' con menos unidades que lo vendido → reclamo del faltante (hoy desaparece sin traza)
- Despacho PARCIAL por cantidad con remanente en bodega (hoy cada ítem sale por su cantidad completa, monza_router_despachos.py:199-201)
- Ciclo de vida del despacho: en_preparacion (borrador) → cerrar → anular con reversa de línea y de embarque (hoy nace 'despachado' e irreversible)
- Flujo guía-primero: crear sin N° de guía y completar transportista/N° guía/N° expedición DESPUÉS vía PUT de cabecera (prerequisito para que el SII pise el folio en Fase 5)
- Blindaje de concurrencia del despacho: FOR UPDATE sobre ítems + retry del correlativo DSP + populate_existing (la Fase 1 NO tocó despachos; se vuelve obligatorio al introducir el despacho parcial)
- Anti-sobredespacho, rechazo de líneas de ítem duplicadas y validación de pertenencia del ítem a la OC
- Guards de integridad en recepción: rechazar marcar ítems sobre una recepción cerrada y rechazar qty negativa (monza_router_bodega.py:143-162 no lo valida)
- Cierre de recepción forzado (forzar=true) con reclamo trazable 'no_llegó' en vez de bloquear por completo

**Por qué este orden:** Es el único gap de severidad CRÍTICA puramente interno, sin bloqueo externo, y es la base física sobre la que después se emiten guías 52 con folio legal: emitir documentos tributarios sobre un flujo que puede sobredespachar sería peor que no emitir. Los tres ítems críticos (tope físico + cierre particionado + despacho parcial) están acoplados y deben portarse en el mismo lote junto con el blindaje de concurrencia; el flujo guía-primero se incluye aquí porque es prerequisito de la emisión SII.

### Fase 3: Datos maestros para facturar bien (OC del cliente + RUT/receptor)

**Impacto:** ALTO · **Esfuerzo:** MEDIO · **Depende de:** ninguna dura; se ordena antes de las Fases 5 y 6 (emisión SII)

**Objetivo:** Dejar los datos de venta y de cliente completos y validados para facturar sin errores hoy y para poder emitir al SII mañana. Arregla además un bug real actual: Monza puede emitir facturas con RUT nulo o inválido en silencio.

**Incluye:**
- N° de OC del cliente OBLIGATORIO al cerrar la venta (backend rechaza + campo marcado en el modal); hoy el cierre PATCH estado='vendida' no lo pide (monza_router_cotizaciones.py:319)
- Fecha de la OC del cliente (columna nueva): la referencia 801 del SII exige N° Y fecha; hoy oc_cliente es solo un String sin fecha (monza_models.py:260)
- Cierre de venta idempotente / anti re-venta (evitar reejecutar el cierre y duplicar efectos; la Fase 1 no tocó este path)
- Candado de rol/empresa en la edición ex-post de la OC (hoy el PATCH genérico está abierto a cualquier usuario autenticado, sin require_empresa('automotriz'))
- Validación de RUT con dígito verificador + razón social obligatoria al emitir factura (portar los 3 helpers de RUT de Grupo AM + override en el schema y el modal)
- Receptor SII completo: RUT obligatorio para clientes facturables + alta en la cuenta Wasabil de Monza (o cargar giro/dirección/comuna en la ficha; hoy MonzaCliente.rut es nullable y sin giro/dirección/comuna)

**Por qué este orden:** Doble valor: corrige un error de facturación de hoy (RUT nulo) y es el prerequisito de DATOS de toda emisión SII — la guía 52 necesita N°+fecha de OC para la referencia 801, y el receptor de la factura 33 necesita RUT válido y ficha completa o el SII lo rechaza. Se hace antes de Wasabil para no descubrir estos huecos recién al emitir. No tiene bloqueo externo, así que puede avanzar mientras el dueño gestiona la cuenta Wasabil.

### Fase 4: Visibilidad G15 de Ventas–Contabilidad + tableros de Despachos

**Impacto:** ALTO · **Esfuerzo:** MEDIO · **Depende de:** Fase 2 (reutiliza los estados de despacho ya confiables y el mismo cálculo de avance)

**Objetivo:** Mostrar, para cada venta, cuánta plata ya se cobró, cuánta está facturada sin cobrar y cuánta falta por facturar, con base física real; y dar a Despachos la misma visión de avance de la OC, reutilizando un único cálculo para no duplicarlo.

**Incluye:**
- por_facturar_clp con BASE FÍSICA: Σ(cantidad − facturada) × precio congelado del ítem − anticipo por descontar (en Monza es más simple que en GA porque los precios ya están congelados por el TC congelado)
- Barra de avance de la plata de la OC: cobrado · facturado sin cobrar · por facturar, con nota del anticipo
- mercaderia_pendiente_clp como cifra autoritativa del backend (evita que el frontend la reconstruya y sobredeclare)
- 'Por facturar' agrupado por estado logístico (en tránsito / en bodega / etc.), reusando item.estado_linea que ya viaja
- Tabla de ítems plegada sobre umbral (>10) con buscador por N° parte / descripción / marca
- Bloque 'Facturas de la OC' expandible (el backend ya serializa las facturas en monza_contabilidad/router.py:366; falta la UI)
- Preview de factura antes de emitir con fuente ÚNICA de verdad (extraer un _construir_factura compartido desde crear_factura) — enganche de UX para la emisión SII de la Fase 6
- Vista OC en Despachos con barra de avance/buckets + alerta de plazo crítico y notificación 'OC lista para despacho' al cerrar la recepción (usa MonzaCotizacion.fecha_entrega_est); coordinar con el cálculo de avance de G15 para no duplicarlo
- Historial de embarques ya recepcionados/despachados (hoy Monza solo expone los pendientes)

**Por qué este orden:** Valor operativo inmediato y sin bloqueo externo: le da al dueño la foto de la plata por venta. Se hace después de la Fase 2 para que el avance se calcule una sola vez sobre datos de despacho ya correctos y se reutilice en Ventas-Contab y en Despachos (evita el trabajo duplicado que los revisores advirtieron). El preview de factura que se construye aquí queda listo para la emisión SII posterior. Puede avanzar en paralelo mientras el dueño tramita la cuenta Wasabil.

### Fase 5: Emisión electrónica al SII: GUÍAS de despacho 52 (Wasabil)

**Impacto:** CRITICO · **Esfuerzo:** ALTO · **Depende de:** Fase 2 (despacho confiable) + Fase 3 (OC con N°+fecha para la ref 801 y receptor válido) + cuenta/token Wasabil de Monza (bloqueo externo del dueño)

**Objetivo:** Que Monza emita guías de despacho electrónicas 52 al SII con folio legal real, en vez de teclear el N° de guía a mano. Es el hueco más grande del roadmap y el objetivo de mayor valor operativo.

**Incluye:**
- Cuenta y token Wasabil de MonzaParts SpA + config MULTI-TENANT (hoy config.py es single-tenant con un solo WASABIL_API_TOKEN; el client HTTP debe elegir el token por empresa)
- Tabla nueva monza_wasabil_dte con FKs a monza_despachos/monza_cont_factura_cliente y candado único anti-doble-emisión + claim 'en vuelo' (la tabla de GA no se reutiliza: sus FKs apuntan a tablas de Grupo AM)
- Cliente HTTP del API Wasabil (la pieza más reutilizable: crear/estado/obtener documento, buscar cliente por RUT, manejo de errores), parametrizado por token de empresa
- Service de armado de guía 52 sobre modelos Monza (MonzaDespachoItem.qty_despachada + MonzaCotizacionItem.precio_unitario_clp como neto; IVA 19% half-up, tope 25 chars, referencia 801 con N°+fecha de OC)
- Router de emisión de guías: preview/emitir/estado/reintentar/estado-batch, con candado require_empresa('automotriz') y protocolo issue=False→confirmación→issue=True
- Candado anti-huérfano en monza_router_despachos.py: una guía 52 viva (emitida/procesando/pendiente) bloquea anular el despacho o pisar el N° de guía a mano
- init_db del módulo (crea monza_wasabil_dte) + cableado en main.py (import + include_router, idealmente bajo MONZA_CONTAB_ENABLED)
- Frontend: botón 'Emitir guía SII' + modal de 2 pasos (previsualizar problemas bloqueantes → confirmar) + badge de estado por despacho + reintentar + monzaWasabilAPI en el services/api de Monza

**Por qué este orden:** Es el premio de mayor valor pero tiene prerequisitos ineludibles: no se puede emitir una guía legal sobre un despacho que sobredespacha (Fase 2) ni sin los datos que el SII exige (Fase 3), y no hay emisión sin la cuenta Wasabil de MonzaParts SpA — por eso ese trámite debe iniciarse desde ya, en paralelo a las Fases 2-4. Las guías van ANTES que las facturas porque la factura 33 referencia la guía 52 emitida.

### Fase 6: Emisión electrónica al SII: FACTURAS 33 (Wasabil)

**Impacto:** ALTO · **Esfuerzo:** ALTO · **Depende de:** Fase 5 (las guías se emiten primero; la factura 33 las referencia) + Fase 4 (preview/fuente única) + Fase 3 (receptor válido)

**Objetivo:** Que Monza emita facturas electrónicas 33 al SII con folio real que devuelve Wasabil, reemplazando el folio SII que hoy se teclea a mano al crear la factura (monza_contabilidad/schemas.py:28).

**Incluye:**
- Service de armado de factura 33 sobre MonzaContFacturaClienteItem (líneas persistidas, neto/IVA/bruto)
- Router de emisión de facturas: preview/emitir/estado/reintentar
- Resolución del receptor por RUT (reusa buscar_cliente_por_rut del client Wasabil) + validación de completitud de datos
- Referencia de la factura a la guía 52 emitida + bloqueo si hay guía electrónica en vuelo
- Badge/estado DTE por factura + acceso al PDF en el listado (relación 1:1 factura↔DTE)
- Precios congelados de la guía 52 al construir la factura para que cuadre con lo enviado al SII
- Frontend de emisión desde Facturas/Ventas-Contab reutilizando el preview de la Fase 4

**Por qué este orden:** Cierra el ciclo tributario de la venta. Va después de las guías porque la factura las referencia como documento de respaldo, y reutiliza toda la infraestructura Wasabil (config multi-tenant, tabla DTE, client HTTP, candado) ya montada en la Fase 5, más el preview construido en la Fase 4.

### Fase 7: Adelantos Vía B: factura de anticipo + descuento automático

**Impacto:** MEDIO · **Esfuerzo:** ALTO · **Depende de:** Fase 6 (necesita la maquinaria de factura 33 y un folio SII referenciable para el anticipo y su descuento)

**Objetivo:** Permitir emitir una factura de ANTICIPO (doc 33 sin guía) que respalde el 50% ante el SII y se descuente automáticamente en la factura del despacho real, para no cobrarle dos veces al cliente. Hoy Monza solo tiene la Vía A (cobranza automática medio='adelanto'), que ya funciona.

**Incluye:**
- Columna es_anticipo en monza_cont_factura_cliente + rama de construcción de factura de anticipo (hoy inexistente: grep de es_anticipo en Monza = vacío, confirmado)
- Descuento automático (línea negativa) que referencia el folio de la factura de anticipo, para que Σ facturas de la venta == total; incluye el guard 'anticipo sin folio SII bloquea la factura del despacho'
- Flujo del excedente del adelanto ligado (adelanto > bruto del anticipo → el sobrante rebaja las facturas del despacho real)
- Endurecimientos de la aplicación del adelanto: guard de factoring vigente (no aplicar contra un saldo que es retención del factor) y cap por SALDO actual de la factura en vez del bruto
- Aplicación retroactiva del adelanto a facturas ya existentes al aprobarlo (hoy solo se aplica en crear_factura)
- DECISIÓN DE NEGOCIO: máquina de estados del adelanto (informado→aprobado→anulado) y múltiples adelantos por venta — el modelo actual de Monza es 1 adelanto por venta y la orden la da Tesorería; confirmar con el dueño si quiere el modelo multi-adelanto de GA o conserva el suyo

**Por qué este orden:** Solo hace falta si algún cliente Monza exige factura/boleta de anticipo; para el caso 50% típico la Vía A ya resuelve el cobro del adelanto. Depende de que exista la emisión de facturas 33 (Fase 6), porque el descuento automático referencia el folio SII de la factura de anticipo. Toca modelos, schema y el builder de factura, por eso es alto esfuerzo y se deja para cuando el ciclo SII básico ya esté operativo.

### Fase 8: Compras nacionales (condicional al negocio)

**Impacto:** MEDIO · **Esfuerzo:** ALTO · **Depende de:** Fase 2 (el tope físico de despacho debe existir para engancharle el UNION de lo recibido nacional). Pregunta de negocio RESPONDIDA 2026-07-28: Monza SÍ compra a proveedores chilenos — fase construida (docs/monza-compras-nacionales.md)

**Objetivo:** Permitir comprar repuestos a proveedores chilenos en Monza: OC nacional + recepción física SIN embarque + costo por ítem → CxP/Tesorería, alimentando el tope físico de despacho. Hoy Monza asume 100% importación (MonzaRecepcion.embarque_id es NOT NULL, línea 471) y una compra nacional literalmente no tiene dónde recibirse.

**Incluye:**
- Columna tipo_origen ('nacional'|'internacional') en la OC de proveedor (enabler foundacional; hoy toda OC nace 'internacional' implícitamente, moneda EUR)
- Libro de recepción SIN embarque (tablas + API: registrar entrega, cerrar, pendientes por recibir = vendido − Σ recibido, listar, detalle) en espejo de recepcion_nacional/
- Anulación segura de recepción con reversa direccional + locks FOR UPDATE en orden canónico + retry ante deadlock
- UNION de lo recibido nacional al TOPE FÍSICO de despachos (se engancha al tope construido en la Fase 2)
- Transición de estado comprado→en_bodega al cerrar (el ítem nacional nunca entra al pipeline de embarque) + guard anti-embarque (rechaza meter un ítem nacional a preparar/embarcar)
- Costo por ítem de compra nacional (tabla cont_compra_item + enlace compra↔OC nacional + catálogo de OC costeables) — DIFERIBLE, es la base de la rentabilidad futura que el dueño ya difirió en GA
- Frontend: modo nacional en la página de Compras (toggle, selector de OC nacional, líneas de costo con tope 'disponible a costear')

**Por qué este orden:** Es trabajo net-new grande y solo se justifica si Monza compra repuestos en Chile — si es 100% importación EUR, el gap es tolerable y se puede diferir. Debe confirmarse la necesidad con el dueño antes de dimensionar. Va después de la Fase 2 porque el aporte nacional al tope físico solo tiene dónde engancharse una vez que el tope existe; la parte de costo por ítem es diferible igual que en Grupo AM.

### Fase 9: Paridad fina de Embarques Pricing y Tesorería

> **HECHA 2026-07-30 — 8 commits.** Resultado ítem por ítem, porque el plan de abajo (escrito
> el 23-07, ANTES de las fases 2-8) resultó desactualizado en varios puntos:
>
> | Ítem del plan | Resultado |
> |---|---|
> | Peso editable por ítem | **HECHO** `24e2d52`. De paso reparó una trampa: `fob_manual` era `bool = False`, no tri-estado, así que editar solo el peso **revertía en silencio un FOB manual** |
> | N° AWB/BL buscable | **FALSA ALARMA PARCIAL** → `a69e60d`. Monza **NO necesita** la columna `awb_numero` que sí necesitó GA: acá `awb` ya es texto libre y los adjuntos viven aparte en `monza_documentos`. Solo faltaba cablear `tracking` al buscador del pricing |
> | FOB real del proveedor | **HECHO** `68419d6`, y salió CHICO en vez de grande. El dato **NO EXISTE** en Monza (no hay factura de proveedor por ítem) y los dos caminos "baratos" eran trampas: `monza_cont_compra_item` exigía abrir los 3 candados nacionales de la F8 → **doble capitalización**; y escribir sobre `MonzaCotizacionItem.costo` **movería el precio de una venta ya cerrada**. El dueño eligió cargarlo en Pricing, donde el input ya existía → sin migración, `fob_origen` solo gana el valor `'factura'` |
> | Snapshot de fecha/ref bancaria | **FALSA ALARMA — ya estaba hecho.** Verificado en `monza_tesoreria/` (conciliar guarda, desconciliar restaura). No era un pendiente |
> | Candado de empresa | **DIFERIDO por el dueño** con la evidencia a la vista. Ver `docs/deudas-diferidas-decision-dueno.md`, que incluye las DOS TRAMPAS para quien lo tome (el webhook de Nexor NO se canda a nivel de router; empezar por `monza_router_config.py`, que es el único sin candado que toca el SII) |
> | Paridad INVERSA (Monza → GA) | **HECHA en dos tandas.** `8bdc28e` cierra los TRES que este plan lista: guard del faltante (era **un clic** desde la pantalla y emitía guía + factura por mercadería inexistente), guard de pertenencia y retry 1213/1205 en crear/anular, más el lock de la recepción. Y `25cdef3` cierra dos CRÍTICOS que el plan NO listaba y que un auditor descubrió: el **doble conteo del depósito** (el cliente pone $59.500 y el sistema decía "cobrado $119.000") y la **2ª guía 52 real al SII** por la misma carga |
>
> **DOS huecos que este plan NO listaba** y que el barrido encontró:
> - **Alertas automáticas** `85aad36` — el barrido diario de las 06:00 **solo consultaba tablas de
>   GA**: un proveedor Monza atrasado **no avisaba nunca**, aunque `plazo_dias` estuviera cargado.
>   Era el hueco más costoso en silencio que quedaba.
> - **Envíos parciales** `f86880a` — el dueño confirmó que le pasan seguido. Se acabó el reclamo
>   fantasma. Sin migración. Ver `docs/monza-envios-parciales.md`.
>
> **Método que conviene repetir:** SONDAS DE PODER DISCRIMINANTE. Cada pieza riesgosa se probó
> quitándole el arreglo, para ver si el test detectaba la regresión. Sin el lock del split, dos
> preparaciones simultáneas **inventaban unidades en 6/6 rondas**; sin los guards de bodega el
> backend responde `{"ok":true}` al marcar Faltante por la cantidad completa. Un test que pasa no
> prueba nada si no se comprobó que falla cuando debe.
>
> **Lección de proceso:** este plan tenía 2 falsas alarmas de 6 ítems. **Nunca ejecutar un plan
> viejo sin verificarlo empíricamente primero.**
>
> Gate al cierre: **107 Monza + 91 Grupo AM**. Migraciones del deploy:
> `monza_embarques_pricing.init_db` y `migrations.monza_notif_alertas` (esta última falla en
> SILENCIO si no se corre: el backend arranca y la campana de Monza queda vacía).

**Impacto:** MEDIO · **Esfuerzo:** MEDIO · **Depende de:** ninguna dura (piezas independientes que se pueden intercalar como relleno)

**Objetivo:** Cerrar las mejoras menores que Grupo AM recibió DESPUÉS del port inicial de Monza y los endurecimientos pendientes, para dejar los dos módulos exactamente a la par. Tesorería ya está casi línea por línea con GA; estos son los últimos deltas.

**Incluye:**
- Peso editable por ítem en Embarques Pricing (override tri-estado que gobierna el prorrateo del flete; hoy el peso siempre sale de cot.peso_kg y no hay forma de corregir un peso mal cargado — tarea G10 de GA sin replicar)
- N° AWB/BL escribible, visible y buscable, separado del archivo adjunto (Monza tiene 'tracking' pero no está cableado al buscador del pricing — tarea G11 de GA sin replicar)
- FOB real por ítem desde la factura del proveedor (hoy Monza usa solo el costo estimado de la cotización); esfuerzo alto: depende de cablear monza_compras_contab al pricing y verificar que exista el equivalente a unit_price_usd por (ítem, OC)
- Snapshot de fecha/ref bancaria previa del egreso al conciliar, para restaurarla EXACTA al desconciliar (hoy Monza usa una heurística que puede borrar un dato cargado a mano si coincidía con el banco)
- DECISIÓN DE NEGOCIO: candado de empresa 'automotriz' en monza_router_despachos.py y demás routers operativos (GA sí candó despachos/compras; Monza dejó los 15 routers operativos sin candado) — el dueño históricamente difirió candar routers operativos; confirmar antes
- **Paridad INVERSA (Monza → GA), detectada por el enjambre de la Fase 2 (2026-07-23):** tres endurecimientos que Monza ya tiene y a GA le faltan: (a) guard de coherencia del 'faltante' (recibido ≥ vendido → 400) en `routers/bodega.py:marcar_item`; (b) retry de deadlock 1213/1205 en `create_despacho` y `anular_despacho` de `routers/despachos.py` (hoy solo `cerrar` lo tiene); (c) guard de pertenencia del ítem al embarque de la recepción en `routers/bodega.py:marcar_item`/`crear_recepcion_item` (una fila espuria con ítem ajeno infla el tope físico de otra OC — misma clase de hueco que se cerró en Monza)

**Por qué este orden:** Son mejoras de precisión de costos y de trazabilidad que no bloquean nada y no tienen dependencias técnicas, por eso van al final o intercaladas como relleno entre fases mayores. La única de peso es el FOB real (afecta el costo landed); las demás son bajas. El candado de empresa en routers operativos requiere confirmación del dueño porque él eligió diferir esa postura en el código operativo.

## Recomendación

Tomar AHORA la Fase 2 (Integridad física de Despachos y Bodega). Es el único gap de severidad CRÍTICA que es puramente interno —sin ningún bloqueo externo—, corrige un riesgo de plata real (hoy Monza puede entregar y facturar más de lo que Bodega recibió, y una llegada parcial manda toda la línea a reclamo dejando indespachable lo que sí llegó), y es la base física indispensable antes de emitir guías legales al SII. EN PARALELO, y desde ya, el dueño debe iniciar el trámite para abrir la cuenta Wasabil de MonzaParts SpA (RUT 78.121.316-0): es el bloqueo externo de mayor plazo y sin él las Fases 5 y 6 (el objetivo de mayor valor: emitir al SII) no pueden arrancar. Así, cuando terminen las Fases 2-4, la cuenta Wasabil ya estará lista para enganchar la emisión.
