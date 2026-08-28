# Visibilidad «listo para despachar» + selector de facturas sin desplegables — MachParts (2026-08-27)

**Deploy: SOLO CÓDIGO — cero migraciones, cero init_db.** Nada que correr antes de
reiniciar. Solo MachParts (espejo Monza en `deudas-diferidas-decision-dueno.md` §5).

## Pieza 1 — Despachos: la verdad única del cupo físico

**El problema**: la pestaña «Listas», la insignia de cada OC y los KPIs contaban ítems
`en_bodega` SIN descontar lo tomado por despachos abiertos — una OC podía decir «Listo»
con cero unidades despachables, y el número real solo existía expandiendo el detalle
OC por OC.

**Lo construido**:
1. **Fórmula ÚNICA del disponible** (`_disponibles_por_item`, routers/despachos.py):
   la usan el detalle, el listado, el panel, los counts, el guard de crear **y el
   buscador de Bodega** (la última copia se extinguió). Sonda de paridad triple: mutar
   el helper tumba detalle+panel+guard JUNTOS.
2. **Insignia honesta** por OC: verde «Listo · 3 de 7 ítems · 14 un. por despachar»;
   con cupo tomado por despachos abiertos: gris «En despachos abiertos · 0 un. por
   despachar» (gris a propósito: es un estado bueno). Las OCs en 0 van al final de la
   pestaña. Residuos flotantes ≤ 0.001 cuentan como cero (misma tolerancia del guard).
3. **KPIs reales y clickeables** → abren el **panel «Listo para despachar»**: todas
   las OCs con mercadería lista → ítems «4 de 6», orden por urgencia de entrega,
   botón «Ir a la OC». Un solo GET (`/despachos/listo-para-despachar`, registrado
   ANTES de `/{despacho_id}` — trampa 422 comentada y pinzada), datos siempre frescos
   (staleTime 0: una recepción de Bodega no invalida nada de esta página).
4. Si crear un despacho rebota por carrera (400), la página se refresca sola Y el
   modal abierto se sincroniza con el cupo real sin perder lo marcado.

## Pieza 2 — Emitir factura: selector con búsqueda (muere el desplegable eterno)

**El problema**: el `<select>` cargaba TODA la historia de ventas pasando por el motor
de precios, sin distinguir qué se puede facturar.

**Lo construido**:
1. **Endpoint liviano** `GET /contabilidad/ventas/opciones` (ANTES de `/ventas/{oc_id}`
   — misma trampa 422): 5-6 consultas, **cero motor de precios** (sonda-bomba: si
   alguien lo reconecta, el test explota). Devuelve por OC el contador de guías
   facturables y sus fechas, más el `hoy` de Chile del servidor.
2. **Selector nuevo** (`components/SelectorOcFactura.tsx`) en ambos modales (factura y
   anticipo): «LISTAS PARA FACTURAR (N)» siempre arriba — ordenada por guía más
   antigua, chip tributario (ámbar: guía de período anterior, facturar antes del 10;
   rojo: período vencido; «sin fecha» primero: exige acción) — y «OTRAS VENTAS» (8
   recientes + búsqueda instantánea por N° OC, cliente, cotización, RUT con o sin
   puntos, o N° de guía; nada se esconde). Teclado completo, ARIA, colapso a barra
   con snapshot (sobrevive al ida-y-vuelta del flujo SII).
3. **Criterio «facturable» ÚNICO**: `_guias_facturables_por_ocs` (batch por tuplas)
   alimenta al selector, al selector de guías histórico (contrato intacto) y al
   faltante del listado de ventas — firma parcial, re-firma y cerrados-sin-firmar
   heredados por construcción. El coalesce de la firma es `is not None` (JAMÁS `or`:
   qty_firmada==0 es legítimo y con `or` se ofrecía facturar mercadería no recibida
   — pinzado con mutación).
4. **Cascada de fecha de la guía = la del emisor real**: DTE 52 EMITIDO (status 3 Y
   folio) → documentDate; guía en papel → fecha_guia; sin dato → «sin fecha».
   **Jamás fecha_despacho** (produjo DTEs reales con fecha equivocada en julio).
   Estado `'bloqueada'` para el DTE contradictorio (status 3 sin folio): la guía
   cuenta, pero el chip avisa que la emisión la bloqueará.
5. Guías como filas-radio con la única preseleccionada; carga y errores visibles
   (murió el `.catch(() => {})` mudo); `listar_ventas` intacto para sus otros 2
   consumidores (+ campo aditivo `guias_facturables_n`).

## Dónde vive cada pieza

| Pieza | Archivo |
|---|---|
| Fórmula única + listado + panel + counts | `backend/routers/despachos.py` |
| Bodega delegando en la fórmula única | `backend/routers/bodega.py` |
| Helpers facturables + /opciones + refactors | `backend/routers/contabilidad.py` |
| Suite panel (paridad triple, centinela, 25+ checks) | `backend/routers/tests/test_listo_para_despachar.py` |
| Suite opciones (bomba anti-motor, cascada, 42+ checks) | `backend/tests_contabilidad/test_ventas_opciones.py` |
| Panel + insignia + KPIs | `frontend-src/src/pages/DespachosPage.tsx` |
| Selector + modales | `frontend-src/src/pages/FacturasPage.tsx`, `components/SelectorOcFactura.tsx` |
| Helpers puros del selector | `frontend-src/src/facturas/selectorOc.ts` |
| Resaltado compartido | `frontend-src/src/components/Resaltado.tsx` |

## La costura que el comité de prueba encontró (y por qué importa)

Sincronizar el detalle bajo el modal abierto (para que el operador vea el cupo real
tras un rebote) abrió un agujero: una línea YA MARCADA que pierde su cupo en ese
refresco salía de `disponibles` — dejaba de renderizarse **y de validarse** — pero
seguía en `selectedItems`, así que viajaba igual en el envío. Rebote 400 eterno, sin
nada visible que lo explicara y sin forma de desmarcarla.

Cerrado con un **invariante**, no con un parche: todo id marcado o está en
`disponibles` (lo valida el chequeo de cantidades) o está en `marcadasSinCupo` (lo
bloquea el guard, con aviso ámbar que nombra las partes y botón «Quitar de la
selección»). Ninguno escapa por el medio. Lección general: **un arreglo que cambia
datos bajo una pantalla abierta debe revisar qué otras estructuras dependían de esos
datos** — aquí, la selección viva del operador.

## Ronda de revisión en enjambre (2026-08-27) — 20 hallazgos confirmados

Seis lentes independientes (correctitud backend · correctitud frontend · estructura · UX ·
robustez · pruebas) sobre TODO lo entregado, con **verificación adversarial**: cada
hallazgo pasó por un agente cuyo trabajo era REFUTARLO, y que además juzgaba si el arreglo
propuesto era de raíz. Sobrevivieron 20; varios arreglos fueron reescritos por el
verificador porque la propuesta original rompía otra cosa.

### El más grave (ALTO) — un `<label>` que cambiaba un documento irreversible

`Field` envolvía a sus hijos en `<label>`. Con un `<select>` adentro era correcto; desde
que envuelve widgets COMPUESTOS, el navegador redirige el clic sobre cualquier texto no
interactivo al primer control etiquetable. Verificado con clics reales en un navegador:
(a) clic en el texto de la OC o su rótulo → activa «cambiar» → **borra el formulario a
medio llenar**; (b) clic en el texto de ayuda bajo las guías → **marca la primera guía en
silencio**, y esa es la que la factura 33 cita como referencia 52. Un DTE irreversible
citando la guía equivocada, sin nada visible que lo delate.
Arreglo: componente hermano `Campo` (con `<div>`) SOLO en los 3 campos con widgets
compuestos — los otros 27 conservan su `<label>`, que ahí es correcto — y la asociación
accesible repuesta según lo que cada widget es (`htmlFor` a la caja de búsqueda;
`role="radiogroup"` + `aria-labelledby` en las guías).

### Las familias del resto

| Familia | Qué pasaba | Cómo se cerró |
|---|---|---|
| Callejón sin salida (3 lentes, mismo bug) | El aviso «líneas sin cupo» y su botón de salida vivían en el paso de picking, pero el bloqueo dispara en el de resumen | Banda propia bajo la cabecera, visible en LOS DOS pasos |
| Copia divergente del cupo | El frontend decidía con `> 0` donde el backend ya usaba `> 0.001` | Umbral espejo documentado (`esDespachable`) + el backend colapsa el residuo en el origen |
| La pantalla se desmentía a sí misma | Toda guía electrónica salía «(sin fecha ⚠)» mientras el chip de arriba mostraba su fecha | `despachos-facturables` emite la misma cascada real (`fecha` + `fuente`) |
| Se ofrecía lo que la emisión rechaza | El chip reproducía 1 de ~5 motivos de bloqueo del SII | Se cuelga de `_guia_no_referenciable`, la MISMA función con que la emisión rechaza |
| La insignia culpaba a un inocente | Decía «en despachos abiertos» y mandaba a anular, aunque el cupo lo tuvieran despachos CERRADOS | `motivo_sin_cupo` derivado en el backend (`en_preparacion`/`sin_stock`/`despachado`), la pantalla solo elige texto |
| El mail mezclaba envíos | El reparto juntaba lo entregado hace semanas con la caja de hoy | Dos secciones («por salir» / «ya despachado»); estado desconocido = por salir (nunca esconder una caja) |
| Aviso tributario tardío | El tope de 10 ítems se avisaba al emitir, cuando dividir ya cuesta anular | Se avisa al armar el despacho, contando las líneas que VIAJAN (qty > 0), no las claves marcadas |
| Despacho zombi por API | Con disponible 0, un payload de 7e-9 pasaba el guard | Guard con la tolerancia real; reproducido y pinzado con mutación |
| Sanitizador que pegaba palabras | Descartaba controles y espacios Unicode sin reemplazarlos | Todo separador invisible → espacio ANTES de descartar |
| 4 agujeros en las pruebas | Entre ellos: bajar `NOMBRE_MAX` de 80 a 25 no ponía nada en rojo; y la suite reescribía código de producción | Valor fijado; maquinaria de mutación en disco ELIMINADA (los checks planos ya eran su complemento exacto) |

### Lecciones que deja la ronda

- **Un widget compuesto dentro de un `<label>` es un botón invisible**: el clic viaja al
  primer control. Al reemplazar un `<select>` por algo compuesto hay que mirar el
  envoltorio, no solo el contenido.
- **El verificador puede equivocarse y el corrector debe atraparlo**: la regla propuesta
  para el motivo del cupo daba el resultado incorrecto en el escenario del propio
  verificador; el corrector lo demostró y la reemplazó.
- **Eliminar una técnica de prueba es a veces mejor que endurecerla**: las sondas que
  reescribían el fuente se borraron cuando se comprobó que los checks planos ya pinzaban
  lo mismo — se fue una clase entera de riesgo operacional.

### Segunda iteración: verificar los arreglos de la propia ronda

Se corrió una pasada dedicada a cazar lo que ESTOS 20 arreglos pudieran haber roto —
precedente que la justifica: el arreglo de la ronda anterior (sincronizar el detalle bajo
el modal abierto) creó el bug de las líneas marcadas invisibles. Resultado: **1 ALTO nuevo
introducido por nosotros**, 1 ALTO heredado que la revisión destapó, y 13 menores.

**La regresión (ALTO)**: el reparto de bultos decidía si un despacho «salió hoy»
comparando `fecha_despacho` —que el server estampa en UTC y MySQL devuelve sin offset—
contra el día del NAVEGADOR. Cerrar a las 20:30 de Chile → fechado mañana → la caja de hoy
DESAPARECÍA del mail al transportista; a la mañana siguiente, lo inverso. Ventana diaria de
3-4 h. Es la **tercera vez** que este sistema aprende que «el hoy del negocio es el de
Chile y se calcula en el backend», ahora por una puerta nueva. Cerrado con `_cerrado_hoy()`
en el servidor (una sola conversión), `cerrado_hoy` en los dos serializers, y un único
«hoy de Chile» en la pantalla — la línea «Fecha del reparto» del mail y la fecha por
defecto al firmar también salían del navegador.

**El ALTO heredado**: `qty_firmada == 0` publicándose como «firma completa» en el detalle
de venta si alguien cambiaba un `is not None` por `or` — el gemelo exacto del bug que sí
habíamos pinzado en la otra pantalla, sin ninguna prueba que lo protegiera. Junto con otros
3 puntos de código correcto pero desprotegido, ahora con sondas cuya mutación se verificó
empíricamente.

**Lección que agrega esta iteración**: *un arreglo se verifica a sí mismo o no está
terminado*. Las dos rondas encontraron su bug más caro revisando lo que la ronda anterior
había arreglado, no lo que había construido.

## Reglas que NO se negocian

- Las fórmulas (disponible físico / facturable / fecha de guía) tienen **UNA sola
  implementación** cada una. Cualquier pantalla nueva las importa; re-implementarlas
  es el bug de mañana (las sondas de mutación lo vigilan).
- El selector es **asesor, no guardián**: el backend re-valida todo al crear/emitir
  (locks y topes intactos). Un dato viejo en pantalla jamás factura de más.
- Las «otras ventas» **nunca se esconden** — anticipo y registro manual las necesitan.

## Proceso (2026-08-26/27)

Enjambre completo: 3 recon + 4 expertos de diseño (UX, arquitecto, veterano de
facturación, adversario de robustez — que pilló la cascada de fechas invertida ANTES
de construir) → 4 constructores en paralelo con contrato (deriva de contrato: CERO)
→ comité de revisión (2 revisores: 0 críticos backend, 3 altos frontend) → 2
correctores (21 ítems) → comité de prueba final. Gate: 292 pytest verdes en MachParts
(2 rojos Monza son de otra sesión concurrente, ajenos a esta entrega) + tsc + build.
