# DISEÑO CONSOLIDADO — COMPRA NACIONAL, COSTO POR ÍTEM Y RENTABILIDAD (Grupo AM)
Síntesis del panel (Dev Contable + Arquitecto de Flujo + Crítico). Documento base para el plan que verá Aldo.

---
## 0. RESUMEN PARA EL DUEÑO (en simple)

Hoy Grupo AM sabe seguir una compra que viene del extranjero (embarque, flete, aduana, costo "puesto en bodega"). Lo que pides es el otro caso: **un proveedor chileno que llega con su camión, su guía de despacho y su factura, y se paga al contado o a 30/60 días — sin embarque de por medio.** Y además quieres saber, al final, **cuánto ganaste en cada venta y en cada repuesto**, sumando tanto lo importado como lo nacional.

La buena noticia: **casi todo ya existe.** El "número propio" que crees que falta **ya lo emite el sistema** (OCP-2026-001); lo que falta es que se VEA bien en pantalla y en el PDF. El pago, el movimiento contable y la conciliación con el banco **ya funcionan** para cualquier compra. Lo que hay que agregar es acotado y se hace en módulos aparte, sin tocar lo que ya opera:

1. Un **interruptor Nacional / Internacional** al crear la orden al proveedor.
2. Una **recepción nacional simple** en Bodega (guía del proveedor + cuánto llegó), que impide despachar/facturar más de lo que el proveedor entregó.
3. Que la **factura de compra nacional** quede **ligada a los repuestos** (no solo al proveedor), para que sea el costo de esos ítems.
4. Un **informe de rentabilidad por orden de cliente y por ítem**, que resta venta menos costo, sumando origen nacional + internacional.

**Crítica honesta a tus ideas** (lo pediste explícito): tu idea del "número propio nuevo" **no hay que construirla — ya está**; construir otro sería crear dos numeraciones peleando por la misma orden. Y tu idea de "que genere movimiento contable y conciliación" **no necesita plomería nueva**: el circuito compra → por pagar → egreso → conciliación bancaria ya está y sirve para esto tal cual. El trabajo real y valioso está en **el costo por ítem** y **la rentabilidad**, que hoy no existen.

---
## 1. DISENSOS RESUELTOS (dónde el panel no coincidió y qué gana)

### DISENSO CENTRAL — D1: ¿cómo entra físicamente el repuesto nacional a bodega?
Tres posturas sobre la mesa:
- **Arquitecto de Flujo:** crear un "**embarque nacional sintético**" que reusa la tabla `Embarque`/`EmbarqueItem`/`RecepcionEmbarque` → hereda gratis el tope físico y la recepción particionada (0 código nuevo en Bodega/Despachos).
- **Crítico:** **rechaza** el embarque sintético (contamina pantallas internacionales y produce números falsos) **y** rechaza el camino directo naïve (rompe el tope). Propone una **tabla de recepción nacional propia + un UNION aditivo al tope de Despachos**.
- **Dev Contable:** neutral (el costo se congela con la factura, no depende del camino físico).

**GANA la postura del CRÍTICO. Se DESCARTA el embarque sintético del Arquitecto.** Razones verificadas hoy contra el código real:

1. **El embarque sintético SÍ contamina código del programador y genera cifras falsas.** Verifiqué el "abanico" de la tabla `Embarque`: se consulta en `routers/compras.py:494-495` (contadores de embarques abiertos/cerrados), `:1431` (board de Logística `listar_embarques`), y en `embarques_pricing/router.py:64,377`. Peor: al abrir el pricing se llama `ensure_pricing_for_embarque` (router.py:54), y en `embarques_pricing/integration.py:108-111` **una moneda que no sea EUR cae al `else` que siembra el tipo de cambio USD**. Es decir, un embarque nacional en CLP recibiría el TC USD (~950) y calcularía un "costo puesto en bodega" en USD sobre montos en pesos = **basura**. Para evitarlo habría que **editar compras.py (código del programador) en 4-6 lugares** con filtros `tipo_origen != 'nacional'` — justo lo que pediste NO hacer ("no refactorizar código del programador"), y con riesgo de olvidar uno y dejar embarques fantasma o costos falsos en pantalla.

2. **El camino directo naïve (poner el ítem `en_bodega` a mano) rompe el tope físico G6.** Verifiqué `_tope_fisico` (`routers/despachos.py:207-209`): si el ítem **no** tiene recepción registrada, devuelve `item.cantidad` = **la cantidad VENDIDA completa, sin tope**. Un ítem nacional sin recepción sería despachable y facturable al 100% aunque el proveedor no entregara una sola unidad. Inaceptable.

3. **La propuesta del Crítico es la única que respeta las dos reglas de oro:** su único toque a código compartido es un **UNION aditivo en `_qty_recibida_utilizable` (despachos.py)** — y despachos.py/bodega.py son **nuestro** código endurecido (G6 fue nuestro), no el del programador. El UNION es **direccionalmente seguro por construcción**: para un ítem nacional solo puede **bajar** el tope (de "todo lo vendido" a "min(vendido, recibido)"), y no toca los ítems internacionales (fuente distinta).

**Rescato lo válido del Arquitecto (su preocupación legítima):** "no re-implementes la lógica de reclamo/faltante particionada de G6, que costó un enjambre entero y una copia diverge". **Respuesta de síntesis:** la recepción nacional **no necesita** ese baile de clonado-partición-reclamo. Ese mecanismo existe porque un embarque **consolidado** se cierra y hay que decidir qué hacer con lo que no llegó de una carga aérea/marítima. Lo nacional solo necesita un **libro de "cuánto recibí realmente" acumulado por ítem** — que es exactamente lo que el tope YA hace (suma filas). Entregas sucesivas simplemente agregan filas; el tope suma. No hace falta clonar la línea ni forzar reclamos. Así evitamos **tanto** la contaminación (Crítico) **como** la divergencia de lógica (Arquitecto). Eso es la síntesis: **arquitectura del Crítico + disciplina del Arquitecto** (reusar el vocabulario de estados y el patrón exacto de suma del tope, sin duplicar la máquina de reclamos).

### Disensos menores
- **`tipo_origen` en `Embarque`:** el Arquitecto lo quería para filtrar los sintéticos. Como **descartamos el embarque sintético, NO se agrega** `tipo_origen` a `Embarque`. Solo va en `OcProveedor`.
- **Costo por ítem — tabla vs overlay:** Dev Contable y Crítico coinciden (tabla de detalle `cont_compra_item` dentro de `compras_contab`, **no** un módulo overlay). El overlay estilo `embarques_pricing` existe porque el landed internacional PRORRATEA flete-por-peso y gastos-por-CIF; lo nacional no tiene nada de eso ("**la factura ES el costo**"). Un overlay sería sobre-ingeniería (YAGNI). **Coinciden los tres de facto.**
- **Lectura unificada:** Dev Contable propone `costo_por_item` con UNION de las dos fuentes; el Crítico propone `COALESCE`. Es la misma idea. Se adopta **una función `costo_por_item` que elige la única fuente poblada por ítem** y marca el origen.

---
## 2. RECOMENDACIÓN FINAL POR DECISIÓN (D1–D7), una frase de porqué

- **D1 — Camino físico:** **Recepción nacional propia (tabla nueva, módulo aislado) + UNION aditivo y direccionalmente seguro al tope de `despachos.py`.** *Porque es lo único que protege el tope G6 sin tocar código del programador ni contaminar las pantallas internacionales, y el nacional no necesita la máquina de reclamos del embarque.*
- **D2 — Nacional/Internacional:** **Columna explícita `OcProveedor.tipo_origen` (String(20), server_default `'internacional'`).** *Porque `pais` es texto libre y `moneda` no distingue (un nacional podría facturar USD); un flag explícito es determinista y deja todo lo histórico correcto sin migrar datos.*
- **D3 — Costo por ítem:** **Tabla de detalle `cont_compra_item` dentro de `compras_contab` (factura → ítems), NO un overlay.** *Porque en lo nacional la factura ES el costo (Σ de sus líneas netas); un módulo paralelo duplicaría CxP y sería sobre-ingeniería.*
- **D4 — Contable NIIF:** **El costo se ALMACENA al registrar la factura (imputada a Existencias, activo); el COGS es un REPORTE al vender, no un asiento; el costo usa NETO (el IVA es crédito fiscal recuperable).** *Porque PartsControl no lleva libro diario formal y el flujo internacional ya trata la compra como inventario; basta el dato listo por ítem.*
- **D5 — Vínculo factura↔ítems:** **factura → ÍTEMS directamente (`cont_compra_item` con cantidad y costo), con puntero suave a la OC en cabecera y en la línea.** *Porque solo el grano ítem×cantidad expresa 1 factura sobre varias OC, N facturas por 1 OC y cobertura parcial.*
- **D6 — Mezcla nacional+internacional en la misma OC cliente:** **Se resuelve por diseño usando `item_cotizacion_id` como clave común en ambas fuentes snapshot; la rentabilidad suma los dos orígenes sin ramas especiales.** *Porque los ítems nacionales también generan `OcProveedorItem` (con su `oc_cliente_id`), igual que los internacionales.*
- **D7 — Alcance UI de Abastecimiento:** **Marcar nacional al CREAR la OC-Proveedor; en Seguimiento los nacionales saltan preparado/pre-embarque/embarque y muestran "Nacional — por recibir"; en Bodega una recepción nacional simple (guía + cantidad).** *Porque la ceremonia internacional (consolidar carga) no aplica a un camión que llega, y el operador solo necesita confirmar qué entregó el proveedor.*

---
## 3. ARQUITECTURA FINAL (aislada, aditiva, estilo `compras_contab`/`tesoreria`)

### 3.1 Columna nueva (1)
- **`OcProveedor.tipo_origen`** — `String(20)`, `server_default='internacional'`, index. Única fuente de verdad del origen; gobierna la UI y el camino físico. Todo lo histórico queda 'internacional' sin migración de datos.
- (Opcional Fase 2) espejar `tipo_origen` en el maestro `Proveedor` como default que pre-llena la OC. La verdad sigue siendo el flag a nivel OC.

### 3.2 Módulo nuevo `recepcion_nacional/` (camino físico — aislado)
Tablas nuevas (bootstrap por su propio `init_db.py`, sin Alembic, igual que el resto):
- **`recepcion_nacional`**: `id`, `oc_proveedor_id` (FK→oc_proveedor, SET NULL, index), `numero_guia_proveedor` (String — la guía de despacho del proveedor), `fecha`, `estado` ('abierta'|'cerrada'), `documento` (foto/PDF de la guía), `created_at`.
- **`recepcion_nacional_item`**: `id`, `recepcion_id` (FK CASCADE, index), `item_cotizacion_id` (FK→items_cotizacion, SET NULL, **index**), `oc_proveedor_item_id` (SET NULL), `qty_recibida` (Numeric 12,4), `estado_recepcion` (mismo vocabulario **verbatim** que `_RECEPCION_UTILIZABLE`: `completo`/`faltante`/`danado_utilizable`/`sobrante` suman; `no_llego`/`danado_no_utilizable` aportan 0), `created_at`.
- Un endpoint "**Registrar entrega nacional**" que toma ítems `comprado` de una OC nacional, crea la recepción y sus líneas, y al cerrar pone `estado_item='en_bodega'`. Entregas sucesivas = nuevas recepciones sobre el remanente (el tope acumula). **No clona la línea ni fuerza reclamos** (a diferencia del embarque consolidado): el reclamo por faltante es una acción opcional, no automática.

### 3.3 Punto de contacto aditivo #1 (nuestro código endurecido, no del programador)
- **`routers/despachos.py` → `_qty_recibida_utilizable`**: agregar una **segunda consulta (UNION en Python)** que sume `Σ recepcion_nacional_item.qty_recibida` (recepción `cerrada`, estado utilizable) en el **mismo dict** por `item_cotizacion_id`. `_tope_fisico` queda **sin cambios** (ya hace `min(vendido, recibido)`). Efecto direccionalmente seguro (solo baja el tope de los nacionales; no toca internacionales). **Test de regresión obligatorio:** ítem internacional no cambia; ítem nacional sin recepción NO es despachable.

### 3.4 Lado contable — cambios en `compras_contab/` (aditivos)
- **`ContCompra` (3 cambios que no rompen nada):**
  - `origen`: agregar valor **`'NACIONAL'`** al enum existente (`MANUAL|EMBARQUE`).
  - Entrada en `CUENTA_DEFAULT_CODIGO` para `('NACIONAL','cogs')` → **Existencias (activo)**, no gasto.
  - `oc_proveedor_id`: **FK suave** nueva → `oc_proveedor.id` (SET NULL, index), solo pista/filtro de cabecera. **No** tocar `numero_documento` (sigue siendo el FOLIO del proveedor) ni la unicidad (empresa, proveedor_rut, numero_documento_activo).
- **Tabla nueva `cont_compra_item`** (el costo por ítem = espejo conceptual de `emb_pricing_item`):
  `id`; `compra_id` (FK→cont_compra, CASCADE, index); `item_cotizacion_id` (FK, SET NULL, **index** — clave de costeo); `oc_proveedor_id`, `oc_proveedor_item_id` (SET NULL, trazabilidad); `numero_parte`, `descripcion` (snapshot); `cantidad` (Numeric 12,4); `precio_unit` (moneda factura, auditoría); `costo_unit_clp`, `costo_total_clp` (= neto de línea × `compra.tc`; **BASE de rentabilidad**); `created_at`. Se llena al registrar la factura. **Costo = NETO** (IVA recuperable ≠ costo).
- **`compras_contab/init_db.py`**: agregar `cont_compra_item` a las tablas + `create_all(checkfirst=True)` + la columna `tipo_origen`/enum.

### 3.5 Lectura unificada del costo (un solo lugar — D3/D6)
- **`costo_por_item(item_ids)`** — función service **solo lectura**, carga por lotes (sin N+1). Para cada `item_cotizacion_id` elige la **única fuente poblada**:
  - internacional → `emb_pricing_item.costo_total_clp` (landed congelado al 'cerrar'),
  - nacional → `Σ cont_compra_item.costo_total_clp`.
  Devuelve `costo_unit_clp`, `costo_total_clp`, `cantidad_costeada`, y **`origen_costo` ∈ {internacional, nacional, mixto, sin_costo}**. `sin_costo` se muestra VISIBLE (nunca se asume 0 como real).

### 3.6 Módulo nuevo `rentabilidad/` (aislado, SOLO LECTURA, sin tablas, candado `require_empresa('mineria')`)
- `GET /rentabilidad/oc-cliente/{oc_id}`: por cada ítem, **venta_neta** (factura cliente → despacho_item → item_cotizacion, precio congelado — el dato de venta ya existe) vs **costo** (`costo_por_item`) → margen $ y %. Suma internacional + nacional en la misma OC. Si `origen_costo='sin_costo'`, marca **"costo pendiente"** y **no fabrica margen**.
- `GET /rentabilidad/item/{item_cotizacion_id}`. Carga por lotes.

### 3.7 Circuito contable nacional de punta a punta (ya existe, solo se confirma)
Factura nacional → `cont_compra` `origen='NACIONAL'` imputada a Existencias, IVA → crédito fiscal (automático) + `cont_compra_item`. Condición contado/30/60 → `fecha_vencimiento` (ya existe). Pago: Tesorería `/por-pagar` → `/pagos` reusa `_crear_egreso` (locks anti doble-pago). Conciliación: cargo del banco ↔ `cont_egreso` (ya existe). Nacional CLP → `tc=1`, sin NIC 21. **Ninguna plomería nueva.** **No** reusar `FacturaProveedor` (es USD-only, sin empresa, sin NIIF).

---
## 4. FLUJO OPERACIONAL — PANTALLA POR PANTALLA

- **Panel Compras (crear OC-Proveedor):** interruptor **Nacional / Internacional** (default Internacional). En Nacional: se ocultan país/moneda-USD/plazo-en-USD; aparecen **condición contado/30/60 + moneda CLP** por defecto; el `numero` propio (OCP-2026-00N) se genera igual y se **muestra etiquetado "N° interno (Grupo AM)"** junto al "N° del proveedor". La asignación de ítems no cambia (crea `OcProveedorItem`, estado `comprado`).
- **Seguimiento:** los ítems nacionales `comprado` muestran **"Registrar entrega nacional"** en vez de "Preparar (embarque)". **No** pasan por Preparados/PreEmbarques/Embarques. Estado visible: "**Nacional — por recibir**".
- **PreEmbarques / Embarques / Embarques Pricing:** **no aparece nada nuevo** — quedan solo-internacionales, **sin editar** (ganancia clave de descartar el embarque sintético: cero filtros en código del programador).
- **Bodega:** pantalla de **recepción nacional simple** (N° de guía del proveedor + cantidad recibida por ítem + adjuntar la guía + marcar faltante opcional). Alimenta el tope vía la tabla nueva. Entregas parciales = recepciones sucesivas.
- **Despachos:** **sin cambios visibles.** El ítem nacional recibido es despachable/facturable como cualquier otro, **capado por lo recibido** (tope G6 intacto). OC mixtas (nacional + internacional) funcionan porque el despacho opera por `item_cotizacion`, agnóstico al origen.
- **Contabilidad → Compras y Pagos:** la factura nacional se registra con detalle por ítem; el pago y la conciliación operan como hoy.
- **Nuevo → Rentabilidad:** informe por OC de cliente y por ítem (venta − costo), con "costo pendiente" donde aún no llegó la factura.

---
## 5. FASES DE IMPLEMENTACIÓN (con el criterio del Crítico: "sin estragos")

### FASE 1 (incluir)
1. Columna `OcProveedor.tipo_origen` + UI del interruptor y etiquetas de N° interno/proveedor (idea #2 resuelta como UI, no como esquema).
2. Módulo `recepcion_nacional/` (2 tablas + endpoint "Registrar entrega nacional") + `init_db`.
3. UNION aditivo direccionalmente seguro en `despachos.py` + **test de regresión** del tope.
4. `cont_compra` (`origen='NACIONAL'`, cuenta Existencias, `oc_proveedor_id`) + tabla `cont_compra_item` + `init_db`.
5. `costo_por_item` (lectura unificada) con `origen_costo` incluido `sin_costo`.
6. Módulo `rentabilidad/` (solo lectura, candado minería) por OC cliente y por ítem.
7. Guards: doble-costeo, Σlíneas ≤ neto, Σ costeado ≤ recibido, "costo pendiente" visible.

### DIFERIDO DELIBERADAMENTE (para no generar estragos)
- **Embarque nacional sintético** — descartado por diseño (contamina Logística/pricing).
- **Devoluciones / notas de crédito del proveedor** que reduzcan costo.
- **Costo estimado/provisional** antes de la factura (requiere campo nuevo en `OcProveedorItem`, que hoy no tiene precio) → deja la rentabilidad "provisional" para Fase 2.
- **Compra nacional en moneda ≠ CLP** (asumir CLP; `tc`/`costo_total_clp` lo resolverían si aparece).
- **Prorrateo sofisticado de flete local** (Fase 1: una línea plana opcional de `cont_compra` cogs; si se exige, prorratear por **valor** de línea, nunca por peso).
- **Asientos formales de COGS-al-venta** (no hay libro diario; la utilidad es reporte).
- **Port a MonzaParts** (seguir el patrón de módulos aislados cuando toque).

### NOTA DE DEPLOY
Fase 1 agrega **1 columna + 3 tablas nuevas** → correr los `init_db` correspondientes (`recepcion_nacional`, `compras_contab`) **ANTES** de reiniciar en producción, igual que el patrón ya establecido para `tesoreria`/`embarques_pricing`.

---
## 6. RIESGOS PRINCIPALES Y MITIGACIÓN

1. **(CRÍTICO) Romper el tope físico G6.** Si un ítem nacional llega a `en_bodega` sin fila de recepción, `_tope_fisico` (despachos.py:209) devuelve la cantidad vendida completa → se despacha/factura lo no recibido. **Mitigación:** recepción nacional **obligatoria** (nunca un atajo que ponga `en_bodega` a mano) + UNION aditivo + test que confirme (a) ítem internacional inalterado y (b) nacional sin recepción NO despachable.
2. **(ALTO) Doble costeo del ítem.** Un ítem con landed internacional Y `cont_compra_item` nacional restaría el costo dos veces → margen inflado. **Mitigación:** origen único por `OcProveedor.tipo_origen` + **guard en el alta de `cont_compra_item`** que rechaza el ítem si ya tiene `emb_pricing_item` (y viceversa) + `costo_por_item` elige **una sola** fuente.
3. **(ALTO) Contaminación de pantallas internacionales** — SOLO si alguien reintroduce el embarque sintético. Verificado: caería en `compras.py:494-495/1431` y `embarques_pricing/router.py`, y sembraría **TC USD sobre CLP = costo falso**. **Mitigación:** mantener descartado el embarque sintético; el camino nacional no crea filas en `Embarque`.
4. **(ALTO) Venta sin costo por crédito 30/60.** La factura del proveedor (que ES el costo) llega DESPUÉS de facturar la venta; además `OcProveedorItem` no guarda costo esperado. Entre medio la rentabilidad mostraría margen ≈ 100% falso. **Mitigación Fase 1:** `origen_costo='sin_costo'` visible como "**costo pendiente**"; la rentabilidad de esa OC se completa cuando entra la factura (recálculo en vivo, no congelado). Costo estimado = Fase 2.
5. **(MEDIO) Sobre-facturación / cuadre factura↔líneas.** N facturas por OC podrían sumar más cantidad que lo recibido; y Σ`cont_compra_item` puede diferir del neto de la factura (líneas no-inventario). **Mitigación:** guard **Σ cantidad costeada por ítem ≤ cantidad recibida** + **Σlíneas ≤ neto_clp**, mostrando la diferencia; permitir cobertura parcial, nunca que las líneas superen el neto.
6. **(MEDIO) Reversa/anulación de una `cont_compra` ya costeada** cambia el costo del ítem retroactivamente. Como la rentabilidad es reporte **en vivo**, recalcula solo y es correcto — pero hay que comunicarlo (la utilidad de ayer puede diferir hoy). **Mitigación:** no congelar la rentabilidad; nota en el informe.
7. **(MEDIO) Divergencia de la lógica de recepción.** Re-implementar la máquina de reclamos de G6 en el módulo nacional la haría divergir. **Mitigación:** el nacional **NO** copia el clonado-partición-reclamo; usa un simple **acumulado de recibido** por ítem (que el tope ya suma) + el **vocabulario de estados verbatim** de `_RECEPCION_UTILIZABLE`. El reclamo por faltante es acción opcional, no automática.
8. **(BAJO) Deploy sin `init_db`.** Tablas/columna nuevas ausentes en prod → 500. **Mitigación:** correr `init_db` de `recepcion_nacional` y `compras_contab` ANTES de reiniciar; checklist de deploy.

---
## 7. CIERRE
El diseño respeta tus tres condiciones: **aditivo**, en **módulos aislados** (`recepcion_nacional/`, `rentabilidad/`, más detalle dentro de `compras_contab/`), y con **un solo toque a código compartido** — y ese toque es a **nuestro** `despachos.py` endurecido, no al del programador, y es direccionalmente seguro. Reusa tal cual el pago, la conciliación y el tope físico que ya funcionan; entrega lo que hoy falta de verdad: **el costo por ítem** y **la rentabilidad por OC de cliente y por repuesto**, sumando origen nacional e internacional en un solo lugar.
