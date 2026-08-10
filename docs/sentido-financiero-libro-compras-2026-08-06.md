# El sentido financiero del Libro de Compras — Grupo AM SpA / MachParts

**Fecha:** 2026-08-06 · **Rol:** Controller financiero (no técnico: este documento no propone código, propone criterio)
**Empresa:** GRUPO AM SPA, RUT 77.977.813-4 · Wasabil id 2757
**Fuentes:** docs/plan-libro-compras-sii-2026-08-03.md (incluido el Addendum 2026-08-05 con las decisiones del dueño), libro real de 12 meses (389 documentos, $1.267.460.692), plan de cuentas NIIF del dueño (Excel «Grupo AM SpA Oficial repuestos», hoja Plan de cuentas, importado por backend/compras_contab/import_plan_cuentas.py), y el modelo contable vivo del sistema (backend/compras_contab/models.py, backend/embarques_pricing/models.py, backend/tesoreria/models.py).

---

## 0. Resumen para Aldo — qué dice de verdad el libro

El libro de compras del SII de los últimos 12 meses suma **$1.267 millones en 389 documentos**. La tentación natural es leer ese número como "lo que gastamos". **Es falso, y por mucho:**

| Qué es | Monto (12 meses) | % del libro | Veredicto del controller |
|---|---|---|---|
| VECTOR CAPITAL — compra de moneda (23 docs) | $550,9 M | 43,5% | **No es gasto.** Es plata cambiada de CLP a USD para pagar a los proveedores extranjeros. El gasto real es solo la comisión/spread. Ver Objeción 1. |
| BERLIAM (7 docs) | $408 M | 32,2% | **Sin clasificar en ningún plan.** Es el segundo monto del libro y nadie lo nombra. Hipótesis: mercadería nacional → costo por ítem. Ver Objeción 2. |
| TRANSLOG (2 docs) | $59 M | 4,7% | Por clasificar: $29,5 M por factura es atípico para transporte local. |
| LOPEZ HERNANDEZ INVERSIONES = MonzaParts (12 docs) | $45,0 M | 3,6% | **Intercompañía**: plata que el grupo se factura a sí mismo. Nunca al costo sin revisión. |
| BODEGAS MAQUIRENT (12 docs, $4 M fijos/mes) | $43 M | 3,4% | Gasto del período (bodegaje post-recepción). Decisión del dueño 2026-08-05, correcta. |
| STEEL INGENIERIA (1 doc) | $28 M | 2,2% | Una sola factura de una ingeniería: huele a **activo fijo**, no a gasto. Ver Objeción 3. |
| RAMAQ (11), transportes locales (~$17 M), FastAir, SAMEX, honorarios, seguros, comisiones, otros 120+ RUT | ~$130 M | ~10% | Aquí vive el gasto operativo real del período, y la fracción chica que capitaliza. |

**La conclusión que ordena todo lo demás:** del libro completo, lo que de verdad capitaliza al costo landed de los embarques (agencia, desconsolidación, flete de importación, almacenaje aeroportuario) anda **en torno al 1% de la plata** — FastAir $3 M/18 docs, SAMEX $2,8 M/9, agencia Cancino, fletes exentos DACHSER/FASTMARK/LATAM/SKY. El mapa de clasificación **no existe para repartir plata grande al costo: existe para IMPEDIR que plata grande entre al costo por error**. Un solo documento de Vector Capital vinculado a un embarque se prorratea por CIF a todos los ítems y multiplica el costo unitario que alimenta el precio de venta (docs/plan-libro-compras-sii-2026-08-03.md, C8).

---

## 1. El sentido: el triángulo que se cierra

Toda contabilidad sana es el mismo triángulo, y cada compra tiene que existir en sus tres vértices con el mismo monto:

```
        DOCUMENTO TRIBUTARIO (el SII, vía Wasabil)
           "esta factura existe y vale $X"
              /                      \
             /                        \
   REGISTRO INTERNO  ────────────  BANCO (cartola Santander)
   (cont_compra, el pasivo         "salieron $X el día D"
    que el ERP reconoce)
```

Cuando los tres vértices cuadran, "conciliar" deja de ser un mes de Excel y pasa a ser una **lista corta de excepciones con nombre**. Cuando no cuadran, pasa lo que pasa hoy: la CxP nace con fecha de HOY en vez de la fecha de la factura, con el acreedor de la OC extranjera en vez del emisor real del DTE, y con el RUT tecleado en formato libre (plan, C3 y Regla 18/19) — tres copias del mismo hecho que no se reconocen entre sí.

Cada fase del plan cierra UN lado del triángulo y responde UNA pregunta de negocio concreta:

| Fase | Lado que cierra | Pregunta de negocio que responde |
|---|---|---|
| **A1 — Espejo** (barrido nocturno del libro a la tabla sii_libro_doc) | Trae el vértice "documento" adentro de la casa | «¿Qué existe ante el SII a mi nombre, cuánto suma, y cambió algo desde ayer?» Sin esto, el SII sabe más de tus deudas que tu propio sistema. |
| **A2 — Bandeja** (cruce espejo ↔ cont_compra por RUT canónico + folio) | Documento ↔ Registro interno | «¿Qué facturas existen ante el SII y NO están en mi sistema?» = **deuda invisible** y crédito fiscal IVA que se puede estar perdiendo (el plazo de imputación es fecha de emisión + 2 períodos, Art. 24 DL 825). También la inversa: qué CxP registré que el SII no respalda. |
| **Cartola CSV Santander en Tesorería** (ya construido: tesoreria/router.py:692 `importar_cartola`, acepta .csv/.xlsx — router.py:86; el cargo del banco se enlaza al Comprobante de Egreso vía conc_conciliacion, tesoreria/models.py:121-161, y el abono a la cobranza/adelanto vía conc_conciliacion_ingreso, models.py:164-203) | Registro interno ↔ Banco | «¿Cada peso que salió del banco corresponde a una obligación registrada, y cada pago que dice el sistema salió de verdad?» Es el control de caja y el antifraude: un egreso sin cargo en la cartola, o un cargo sin egreso, es la primera señal de un problema. |
| **Fase C (futura) — empujar la cartola a Wasabil** (POST /api/financials/transactions/bulk, con modo check→apply y dedup, Addendum §1) | Documento ↔ Banco | «¿Esta factura del SII está pagada, con qué movimiento y cuándo?» Cierra el ciclo completo gasto→pago visto desde el lado tributario, que es el lado que mira el contador y el SII. |

**Regla del triángulo:** con dos lados conciliados, el tercero se deriva; con uno solo, todo es fe. Hoy Grupo AM tiene el lado interno↔banco construido (Tesorería) y el lado documento↔interno en plan. Ese orden es el correcto: primero saber qué debo (A), después probar que lo pagué (ya existe), y al final dejar que el mundo tributario lo vea (C).

---

## 2. El mapa de clasificación

### 2.1 El árbol de decisión (lo que el sistema aplica y el operador entiende)

Para **cada documento** del libro, en este orden. La primera regla que aplica, gana:

```
0. ¿Es nota de crédito (61) o débito (56)?
   → NO se clasifica sola: se engancha al documento que corrige (por related_to)
     y REABRE la clasificación de ese documento. Nunca a un embarque directo.
     (Regla 20 del plan; hay 11 NC por −$14,1 M esperando.)

1. ¿El RUT está BLOQUEADO? (financiero: Vector Capital 76.513.680-6, bancos,
   corredoras, seguros)
   → Jamás a un embarque, jamás al costo. Va a su centro de costos — y en el
     caso de Vector, NI SIQUIERA a gasto completo: ver Objeción 1.

2. ¿El RUT está en IGNORAR_AUTO? (recurrentes chicos: peajes, telefonía,
   comisiones Santander)
   → Se archiva solo con su centro de costos por defecto. No molesta al operador.

3. ¿Es mercadería nacional? (compra de repuestos a proveedor chileno con OC
   nacional — el módulo compras nacionales ya existe: cont_compra_item,
   compras_contab/models.py:180-206)
   → COSTO POR VENTA vía costo por ítem: cuenta 1.3.01 Existencias
     (default ya cableado: CUENTA_DEFAULT_CODIGO ("NACIONAL","cogs") → "1.3.01",
     compras_contab/service.py:34-35). NO es gasto, NO es landed de embarque.

4. ¿Es un servicio de INTERNACIÓN de un embarque, prestado ANTES de que la
   mercadería quede disponible en bodega? (agencia de aduana, desconsolidación,
   flete internacional, almacenaje aeroportuario pre-nacionalización)
   → COSTO POR VENTA: capitaliza al landed vía la tabla puente, con monto
     asignado por embarque (Decisión D2 del dueño: una compra local puede
     cubrir varios embarques; Σ asignado ≤ total del documento).

5. ¿Es la compra de un bien duradero grande? (estanterías, obras, equipos —
   caso vivo: STEEL INGENIERIA $28 M en una factura)
   → ACTIVO FIJO 1.5.01. Ver Objeción 3: esta salida faltaba en el plan.

6. TODO LO DEMÁS → CENTRO DE COSTOS (gasto del período), con el centro
   sugerido por el RUT y confirmación del operador.
```

**El default es gasto, y eso es deliberado** (Decisión D4 del dueño, Addendum §2): el error barato es gastar de más un mes; el error caro es activar de más para siempre. Capitalizar exige acción explícita del operador, nunca ocurre por omisión.

### 2.2 Destino A — COSTO POR VENTA (capitaliza)

Hay **dos caminos** al costo, y no hay que confundirlos:

**A1. Landed de embarque (importación).** Según NIC 2.10-11, capitalizan los costos necesarios para dejar la mercadería en su ubicación y condición actuales. En ESTA operación eso significa, con los proveedores reales del libro:

| Tipo de gasto (catálogo del sistema, embarques_pricing/service.py:132-139) | Proveedores reales | Nota tributaria |
|---|---|---|
| Agencia de aduana | AG.AD. RICARDO CANCINO (78.903.460-5) | Afecta a IVA; el IVA es crédito, NO capitaliza. |
| Desconsolidación | forwarders/almacenes según embarque | Ídem. |
| Flete internacional / aéreo de importación | DACHSER (76.147.894-K), TRANSPORTES FASTMARK (78.958.160-6), LATAM (89.862.200-2), SKY | **Exento** por Art. 12 E N°2 DL 825 — y aun así capitaliza: es el corazón del CIF. El neto que prorratea INCLUYE el exento (Regla 14 del plan, verificada al peso sobre 24 meses). |
| Arancel / derechos | Aduana (vía agencia) | Capitaliza. El IVA de importación (DIN) NO capitaliza: es crédito (1.4.02) y ni siquiera está en este libro — ver sección 5. |
| Almacenaje ANTES de nacionalizar | FastAir (96.631.520-2) — almacenes de carga aérea, $3 M/18 docs | Capitaliza: la carga aún no está disponible. |
| Courier de importación | SAMEX (76.629.600-9), $2,8 M/9 docs | Capitaliza cuando trae mercadería o documentos del embarque. |

El par que le deja el criterio claro a cualquier operador: **el almacenaje de FastAir (aeropuerto, la carga todavía no es tuya operativamente) capitaliza; la bodega de Maquirent (la mercadería ya recibida esperando cliente) es gasto**. Misma palabra "almacenaje", dos destinos distintos, y la frontera es la recepción.

Mecánica: el monto asignado viaja a la línea de gasto del embarque (emb_pricing_gasto, con su flag `capitaliza` — embarques_pricing/models.py:92), la CxP nace con origen EMBARQUE contra **1.3.02 Mercadería en tránsito** (default ya cableado, compras_contab/service.py:33) y al vender se traslada a **5.1.01 Costo de mercadería vendida (landed)**.

**A2. Mercadería nacional.** La factura ES el costo, línea por línea, sin prorrateo (cont_compra_item, compras_contab/models.py:180-206), contra **1.3.01 Existencias**. Si BERLIAM resulta ser proveedor de mercadería (Objeción 2), va por acá — y entonces el "costo por venta" del libro no es 1% sino un tercio: por eso esa clasificación es la más urgente de todas.

### 2.3 Destino B — CENTROS DE COSTOS (gasto del período)

Catálogo que propongo para Grupo AM. No exige tablas nuevas: `cont_compra` ya tiene `categoria` (texto libre, models.py:54) y `cuenta_contable_id` (models.py:56); lo único que pido es **cerrar el catálogo a esta lista fija** (hoy CATEGORIAS_SUGERIDAS de service.py:22-28 es solo sugerencia) para que los KPIs de la sección 4 sumen sin limpieza manual.

| # | Centro | Qué cae ahí (RUT reales de hoy) | Cuenta NIIF del plan del dueño | Riesgo si se clasifica MAL | Severidad |
|---|---|---|---|---|---|
| CC-1 | **Financiero** | Comisión/spread de compra de moneda (VECTOR CAPITAL 76.513.680-6 — SOLO la comisión, ver Objeción 1), comisiones bancarias Santander, intereses | 6.3.03 Intereses y comisiones bancarias; diferencias de cambio a 6.3.04 / 4.2.02 (NIC 21) | Al costo → un doc de Vector prorratea $ millones por CIF a todos los ítems y el precio de venta hereda el error. Como gasto por el TOTAL → P&L destruido: $550 M de "gasto" que no existe. | **CRÍTICA** |
| CC-2 | **Seguros** | BCI SEGUROS ($497 k exenta) | 6.2.04 (póliza general) — PERO si la póliza es del transporte de la carga importada, es la "S" del CIF y capitaliza al embarque (override por documento) | Seguro de carga a gasto → landed subvaluado; póliza general al costo → inventario inflado | MEDIA |
| CC-3 | **Logística de distribución** | Transportes locales: APM, YOB, Barriga, Retornos Chile, Grúas Jorge Contador (~$16,9 M/año). TRANSLOG ($59 M/2) queda acá SOLO si el dueño confirma que es distribución — el monto no calza | 6.1.01 Distribución nacional (fletes de venta) | Si capitaliza → infla inventario y difiere el gasto de vender; NIC 2.16(d) lo prohíbe expresamente | MEDIA (ALTA si Translog cae acá sin revisar) |
| CC-4 | **Bodegaje** | BODEGAS MAQUIRENT (76.780.738-4): $4 M netos fijos/mes = $48 M/año, 11 folios idénticos + 1 NC que anula un mes | 6.2.02 Arriendo (categoría "Bodegaje" para no mezclarlo con oficina) | Si capitaliza → ~$48 M/año inflando existencias y difiriendo pérdida (NIC 2.16(b): almacenaje posterior es gasto salvo etapa productiva). El plan original lo tenía como LOGISTICO capitalizable — la Decisión D4 del dueño lo corrigió; hay que corregir también la semilla de reglas (ver Anexo) | **ALTA** |
| CC-5 | **Administración y generales** | Peajes (Costanera Norte), telefonía (Telefónica), oficina, software — el "ruido" de 131 receptores distintos | 6.2.04 Gastos generales de oficina; software y asesorías a 6.2.03 | Bajo en plata; el riesgo real es de ATENCIÓN: si esto no se archiva solo (IGNORAR_AUTO), la bandeja nace con cientos de filas y se abandona — el destino que ya tuvo el módulo de gastos | BAJA (plata) / ALTA (adopción) |
| CC-6 | **Honorarios** | ALEXIS MONTENEGRO ($2,9 M/15 docs) | 6.2.01 Sueldos y honorarios | Riesgo de forma, no de monto: la retención de honorarios vigente (15,25% en 2026) y su declaración son del contador; el sistema solo debe dejarlos visibles y separados | BAJA |
| CC-7 | **Intercompañía (MonzaParts)** | LOPEZ HERNANDEZ INVERSIONES (78.121.316-0): $45,0 M/12 docs | Depende del contenido: si es mercadería → 1.3.01 con marca; si es servicio → su centro. La marca `es_relacionado` va SIEMPRE (columna ya prevista en el plan, alter a cont_compra) | Al costo sin revisión → el landed de MachParts incluye el margen que el grupo se cobra a sí mismo: decisiones de precio sobre costo inflado, y flanco abierto ante el SII (facultad de tasación, Art. 64 Código Tributario) | **ALTA** |
| CC-8 | **Servicios a la operación/venta** | RAMAQ ($19 M/11 docs ≈ $1,7 M c/u — confirmar con el dueño: ¿maestranza, reparaciones, servicio técnico?) | 6.1.02 si es comisión/servicio de venta; 6.2.04 si es mantención general | Ambiguo hasta confirmar; el guard de magnitud (Regla 17 del plan) es el único freno mientras tanto | MEDIA |

**Los tres niveles por RUT** (sii_proveedor_regla del plan, Regla 16) se leen así en este catálogo: BLOQUEADO = CC-1 y CC-7 nunca tocan un embarque sin revisión; IGNORAR_AUTO = CC-5 y CC-6 se archivan solos con su centro; LOGISTICO = candidatos al destino A1, pero **con la corrección del dueño (D4): LOGISTICO ya no significa "capitaliza", significa "aparece primero y PUEDE capitalizar"** — cada documento se decide al vincular, y el default sigue siendo gasto.

### 2.4 Destino C — Activo fijo (la salida que faltaba)

STEEL INGENIERIA: $28 M en UNA factura de una empresa de ingeniería. Eso no es gasto del mes ni costo de un embarque: tiene toda la pinta de racks, obras o equipamiento de bodega → **1.5.01 Propiedad, planta y equipo**, con su depreciación (6.2.05). Un mapa con solo dos salidas lo forzaría a una de dos mentiras: a gasto (castiga el resultado del mes en $28 M y pierde el activo del balance) o al costo (inventario fantasma). La salida C se usa poco — quizá una vez al año — pero cuando se necesita, no hay sustituto. Se implementa sin tocar nada: es un centro más de la lista cuya cuenta es de ACTIVO.

### 2.5 Tabla de riesgos — qué pasa cuando una rama se equivoca

| Error | Efecto en los números | Quién lo sufre | Severidad |
|---|---|---|---|
| Gasto del período metido al landed (ej. Vector, Maquirent, transporte local) | Costo unitario inflado → precios de venta inflados o margen aparente menor; EBITDA maquillado (el gasto "desaparece" dentro del inventario) | Pricing y competitividad; el dueño decide con un costo que no es | CRÍTICA con montos grandes |
| Costo de internación mandado a gasto (ej. flete DACHSER a "distribución") | Landed subvaluado → margen bruto SOBREestimado por venta → se vende barato creyendo que se gana más | Margen real | MEDIA (montos chicos hoy, ~1% del libro) pero sistemática |
| Mercadería nacional a gasto | Margen bruto irreal (costo de venta subvaluado) y rentabilidad por venta imposible de calcular | Estado de resultados completo | ALTA (si Berliam es esto: $408 M) |
| Intercompañía al costo sin marca | Margen del grupo inflado con plata propia; riesgo SII | Consolidado del dueño | ALTA |
| CAPEX a gasto o a costo | Resultado del mes castigado / inventario fantasma | Balance | MEDIA-ALTA |
| NC ignorada sobre costo ya congelado | Costo del embarque inflado por un servicio acreditado (caso vivo: NC Maquirent folio 95, −$4,76 M sobre un mes completo) | Margen histórico informado | ALTA |

---

## 3. Las reglas del día a día

### 3.1 Cómo se decide en la bandeja

1. **El RUT trae el default, el documento manda.** Cada RUT tiene nivel (BLOQUEADO / IGNORAR_AUTO / LOGISTICO) + centro de costos sugerido. El operador ve el documento con su sugerencia ya puesta; un clic confirma. El override por documento existe siempre y **su default es gasto del período** — capitalizar es acción explícita (Decisión D4).
2. **Las tres cubetas del reporte** (está / no está / no pude determinarlo) se respetan a rajatabla: un barrido incompleto NUNCA afirma que algo falta (Reglas 2 y 3 del plan). Un informe que acusa faltantes falsos muere el día uno.
3. **La bandeja nace despejada, no llena.** Con las reglas por RUT cargadas, de los ~553 documentos históricos deben quedar menos de 30 pendientes reales (validación de Fase A2 del plan). Si el operador abre la bandeja y ve 500 filas, el módulo se abandona en dos semanas — ya pasó una vez.

### 3.2 Quién decide qué

| Decisión | Quién | Por qué |
|---|---|---|
| Clasificar un documento en su centro (default sugerido) | Operador de contabilidad | Volumen diario, riesgo bajo con defaults correctos |
| Crear/cambiar una regla por RUT; mover un RUT de nivel | Dueño o controller, con motivo registrado (la tabla ya lo exige: usuario + motivo) | Una regla equivale a cientos de documentos futuros |
| Capitalizar un documento a un embarque (override) | Operador, PERO sobre el umbral de magnitud (Regla 17) exige confirmación con motivo | El error se prorratea y se congela |
| Todo documento intercompañía (CC-7) | SIEMPRE revisión humana del dueño/controller; jamás auto | Ver riesgo Art. 64 |
| Reabrir un pricing cerrado / resolver una NC sobre costo congelado | Dueño, con motivo obligatorio (parche del auditor contable al `_reabrir_pricing_tx`) | Reescribe márgenes ya informados |
| Conciliar banco (cartola ↔ egresos/cobranzas) | Tesorería | Ya opera así hoy |

### 3.3 Los cinco controles que un auditor pediría (y que el diseño ya permite)

1. **Σ asignado ≤ total del documento**, con tolerancia 1 CLP, y en los TRES montos (neto, IVA y exento — el parche del auditor: sin el tope de IVA, un documento repartido puede generar más crédito fiscal del que recargó). El residuo no asignado se imputa EXPLÍCITO a gasto, nunca queda mudo.
2. **La nota de crédito reabre la clasificación** del documento padre y jamás se procesa en silencio: si el costo ya está congelado, alerta visible con monto en el tablero (hoy hay 11 NC por −$14,1 M que el ERP ni siquiera puede representar — schemas.py:61-62 exige montos ≥ 0).
3. **Intercompañía nunca al costo sin revisión**: `es_relacionado` es marca dura, todo documento de 78.121.316-0 pasa por decisión humana registrada (quién, cuándo, por qué).
4. **Cuadratura mensual de tres puntas**, publicada en el tablero: Σ libro SII del mes (monto_efectivo = total × signo, para que las NC resten — Regla 9) = Σ CxP registradas + Σ clasificado-como-no-CxP + pendientes nombrados. Y por el lado del banco: Σ egresos del mes vs Σ cargos conciliados de la cartola. Las ecuaciones E1-E6 del plan son exactamente esto; la que importa al dueño es E6: la lista de excepciones con nombre.
5. **Segregación y rastro**: quien clasifica no aprueba pagos (Tesorería aprueba y concilia); toda regla, override, reapertura y desvinculación queda con usuario+fecha+motivo. El sistema ya tiene la cultura (snapshots que restauran al desconciliar — tesoreria/models.py:154-159); se extiende, no se inventa.

---

## 4. Los números que el dueño debe mirar

Siete KPIs que este sistema habilita y que antes eran imposibles o manuales. Todos salen de tablas que ya existen o están en el plan; ninguno exige módulos nuevos.

| # | KPI | Fórmula | Qué decisión alimenta |
|---|---|---|---|
| 1 | **% del libro clasificado (en plata)** | Σ \|monto_efectivo\| de documentos con decisión ÷ Σ \|monto_efectivo\| del período. Meta: ≥95% al cierre de cada mes | Salud del proceso. Si baja, la bandeja se está abandonando — intervenir antes de que muera como el módulo anterior |
| 2 | **Deuda invisible (libro vs ERP)** | Σ monto_efectivo del espejo del mes − Σ cont_compra activas del mes (ligadas + manuales). Es la ecuación E6 hecha número | Cuánto pasivo real aún no está en el sistema. Es LA pregunta que motivó todo el proyecto |
| 3 | **Costo de internación como % del landed** | Σ gastos capitaliza=True de embarques cerrados del período ÷ Σ costo_total_clp de esos embarques | Negociación con forwarders, agencia y almacenes; sensibilidad del precio de venta al costo logístico |
| 4 | **Costo de comprar dólares** | (comisión/spread Vector + comisiones bancarias del mes) ÷ CLP cambiados en el mes | Cuándo y con quién comprar moneda; comparar corredora vs banco. Hoy ese costo está invisible dentro de $550 M |
| 5 | **Bodegaje sobre inventario** | Gasto CC-4 del mes ($4 M) ÷ valor promedio de existencias (1.3.01 + 1.3.02) | ¿La bodega está dimensionada para el stock real? $48 M/año es plata que se renegocia con datos |
| 6 | **Plata sin conciliar con el banco** | n° y $ de cont_egreso con conciliado=False a más de 7 días + movimientos de cartola sin enlace (conc_movimiento conciliado=False) | Control de caja y fraude. Este número debe tender a cero cada semana; su tendencia importa más que su valor |
| 7 | **Intercompañía del período** | Σ documentos es_relacionado del mes, y su % sobre el costo total | Precios de transferencia, margen real consolidado del grupo (el margen de MachParts que viene de facturarle a Monza no es margen del grupo) |

Regla de lectura para todos: **primero la tendencia, después el valor**. Un KPI 6 en $8 M estable es menos grave que uno en $2 M que viene doblándose hace tres semanas.

---

## 5. Lo que este sistema NO ve (honestidad) — y cómo se compensa

| Ceguera | Por qué | Compensación operativa |
|---|---|---|
| **La factura del proveedor extranjero** (los repuestos CAT, la plata más grande de la operación) | El libro del SII solo trae proveedores chilenos: 389 docs, 100% CLP (plan, Fuera de alcance) | Ya está cubierta por otro camino: OC proveedor + embarques + FOB real + invoices. El control cruzado que pido: cuadrar trimestralmente Σ FOB embarcado vs Σ SWIFT enviados (cont_egreso con es_anticipo, NIC 21) |
| **El DIN y el IVA de importación** — típicamente el crédito fiscal más grande del mes de un importador | No existe en Wasabil; la línea iva_importacion es carga manual (capitaliza=False, service.py:138) | Disciplina de cierre: ningún pricing de embarque se cierra sin N° de DIN y fecha de pago cargados (campos propios, no reciclar nro_factura — plan, Fuera de alcance). Checklist mensual contra la agencia Cancino. La pantalla debe DECIR que ese dato es manual, para que nadie lea el silencio como cero |
| **El detalle de líneas de los documentos recibidos** | Los 397 detalles dicen "Detalle no disponible", sin XML ni PDF (plan, C9). El sistema sabe cuánto y de quién; nunca PARA QUÉ | Por eso la clasificación es humana y por eso las reglas por RUT existen. Para montos sobre el umbral de magnitud: pedir el PDF al proveedor y adjuntarlo antes de capitalizar. Intercompañía: respaldo siempre |
| **El estado de aceptación/reclamo ante el SII** (plazo de 8 días para reclamar un DTE) | exchange_status viene null en los 553 documentos de 24 meses (plan, C9); el filtro existe en el API (Addendum §1) pero el dato no llega | Rutina semanal del contador en el portal SII. El espejo SÍ ayuda indirectamente: la bandeja ordenada por primera_vista_at es la lista de "documentos nuevos de la semana" que hay que revisar dentro del plazo — aunque el sistema no sepa el estado, sabe la novedad |
| **Las NC dentro del ERP** | El ERP hoy no puede representar montos negativos (schemas.py:61-62; router de pricing valida gastos no negativos) | Mientras se decide el diseño (Decisión D5 pendiente), las NC se muestran, se bloquean con motivo y se ALERTAN con monto. Un bloqueo silencioso de −$4,76 M es un costo inflado que nadie irá a buscar |

---

## 6. Objeciones del controller

Para esto estoy. Tres cosas del plan me parecen financieramente incompletas o equivocadas, y una cuarta que es simplificación aceptable pero debe quedar escrita.

### Objeción 1 — VECTOR CAPITAL: "compra de moneda" NO es gasto, y el plan la trata como si lo fuera. CRÍTICA.

El Addendum (D3) resuelve bien la mitad del problema: BLOQUEADO para embarques, clasificación "financiero / compra de moneda". Pero deja implícito que esos documentos van a un centro de costos — es decir, a GASTO. **$550,9 millones de compra de divisas no son gasto de nada: son un cambio de forma de la misma plata** (salen CLP de 1.1.02 Banco Santander, entran USD a 1.1.03 Fondo/Banco USD — las dos cuentas existen en el plan del dueño). El gasto real es la comisión/spread de la corredora, que es una fracción mínima. Si la bandeja registra esos 23 documentos como CxP de gasto financiero, el estado de resultados muestra un "gasto" del 43% del libro que no existe, y cualquier KPI muere de risa.

**Recomendación concreta:** los documentos de 76.513.680-6 se clasifican en CC-1 pero con tratamiento especial: NO generan cont_compra de gasto por el total; su reflejo correcto es el que Tesorería ya sabe hacer (egreso CLP ↔ ingreso USD), con la factura exenta como respaldo y solo la comisión identificable a 6.3.03. La diferencia entre el TC al que se compró y el TC al que después se paga al proveedor extranjero va a 6.3.04 / 4.2.02 (NIC 21). Pregunta subordinada al contador del dueño: ¿las facturas de Vector documentan el monto total cambiado o solo la comisión? La respuesta define si el problema es de $550 M o ya está resuelto — hay que responderla ANTES de encender la bandeja.

### Objeción 2 — BERLIAM: el 32% del libro no está clasificado en ninguna parte. CRÍTICA.

$408 millones en 7 documentos (~$58 M cada uno) y no aparece ni en la semilla de reglas del plan (líneas 201-206 del plan) ni en las decisiones del dueño. Es el segundo proveedor del libro y el primero de los que sí podrían ser costo. Mi hipótesis de controller — por el patrón de pocos documentos grandes en una operación de repuestos — es **mercadería nacional** (destino A2, costo por ítem, 1.3.01), y el módulo para recibirla ya existe. Pero es una hipótesis: si fuera maquinaria, sería activo; si fuera un servicio grande, gasto. **Con $408 M en juego, la clasificación de BERLIAM es la decisión pendiente más cara de todo el proyecto** y debe responderse junto con las de la Decisión D10, antes de cargar la semilla de reglas.

### Objeción 3 — El mapa de dos salidas es corto: falta ACTIVO FIJO. ALTA.

El encargo pide dos destinos (costo por venta / centro de costos). El libro real contiene al menos un documento — STEEL INGENIERIA, $28 M en una factura — que no pertenece a ninguno: es CAPEX (1.5.01). Forzarlo a gasto castiga el resultado de un mes en $28 M; forzarlo a costo infla el inventario. La salida C cuesta cero (un centro más cuya cuenta es de activo) y evita que el caso raro rompa el mapa. TRANSLOG ($59 M/2) y RAMAQ ($19 M/11) también necesitan confirmación del dueño antes de heredar un default.

### Nota 4 — Transporte local todo a gasto: simplificación correcta, pero que quede escrita. MEDIA-BAJA.

La regla del dueño (D4: "transporte local = distribución = gasto") es defendible por materialidad (~$17 M/año, 1,3% del libro) y por prudencia (gastar de más es conservador; capitalizar de más infla margen). Pero técnicamente NIC 2.10 capitalizaría el tramo de ENTRADA (aeropuerto→bodega). No pido cambiar la regla: pido que quede documentada como decisión consciente de materialidad, y que el override por documento se use cuando una factura sea inequívocamente el flete de internación de un embarque específico. Así, cuando un auditor pregunte, la respuesta es "lo decidimos y está escrito aquí", no un silencio.

---

## Anexo — Semilla de reglas por RUT, actualizada con las decisiones del dueño

Corrige la semilla original del plan (que era anterior al Addendum) e incorpora este documento:

| RUT | Proveedor | Nivel | Centro / destino default | Cambio vs plan original |
|---|---|---|---|---|
| 76.513.680-6 | VECTOR CAPITAL | BLOQUEADO | CC-1 Financiero — tratamiento especial Objeción 1 | Etiqueta: corredora/compra de moneda, no factoring (D3) |
| 78.121.316-0 | LOPEZ HERNANDEZ (MonzaParts) | es_relacionado + revisión SIEMPRE | CC-7 Intercompañía | Sin cambio, con revisión humana obligatoria |
| 76.780.738-4 | BODEGAS MAQUIRENT | LOGISTICO (visible) | **CC-4 Bodegaje, GASTO** | **Era capitalizable en la semilla; D4 lo corrigió** |
| 96.631.520-2 | FastAir | LOGISTICO | Destino A1 — almacenaje pre-nacionalización, capitaliza | Sin cambio |
| 76.629.600-9 | SAMEX | LOGISTICO | Destino A1 — courier de importación | Sin cambio |
| 78.903.460-5 | AG.AD. RICARDO CANCINO | LOGISTICO | Destino A1 — agencia de aduana | Sin cambio |
| 76.147.894-K / 78.958.160-6 / 89.862.200-2 | DACHSER / FASTMARK / LATAM | LOGISTICO | Destino A1 — flete internacional (exento, capitaliza) | Sin cambio |
| — | Transportes locales (APM, YOB, Barriga, Retornos, Grúas J. Contador) | IGNORAR_AUTO con centro | CC-3 Distribución, gasto | Nuevo (D4) |
| — | Santander (comisiones), Costanera Norte, Telefónica | IGNORAR_AUTO | CC-1 / CC-5 | Nuevo — despeja la bandeja |
| — | ALEXIS MONTENEGRO | IGNORAR_AUTO con centro | CC-6 Honorarios | Nuevo |
| — | BCI SEGUROS | LOGISTICO (visible) | CC-2 Seguros; capitaliza SOLO si es póliza de carga | Nuevo |
| **— pendientes del dueño —** | **BERLIAM ($408 M) · TRANSLOG ($59 M) · STEEL ($28 M) · RAMAQ ($19 M)** · LB INVERSIONES ($9 M) · SENNA MOTORS ($9,1 M) | sin regla → aparecen como "no clasificado" | — | **Bloquean el encendido de la bandeja: son el 41% del libro** |

*Un documento de un RUT sin regla nace "proveedor no clasificado" y el único freno es el guard de magnitud — está bien para el ruido chico; no está bien para $408 M. Por eso los pendientes van primero.*
