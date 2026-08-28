# Picking & Packing en Despachos — MachParts (2026-08-26)

**Migración:** `python -m migrations.despacho_bulto_numero` **ANTES de reiniciar**
(checklist §1.a, 🔴 CRÍTICA — el modelo declara la columna: sin ella, expandir una OC,
cerrar, firmar, anular, emitir guías al SII y facturar revientan con 1054; el listado
de OCs SOBREVIVE y el sistema PARECE sano — la trampa de siempre).

Solo MachParts por decisión del dueño («esto es para machparts»). El espejo MonzaParts
quedó en `docs/deudas-diferidas-decision-dueno.md` §4 — con una advertencia que NO es
del bulto sino del DTE: **el formato v4 debe portarse a `monza_wasabil_dte` ANTES de la
primera emisión real de esa marca**, o sus guías saldrán sin número de parte.

## Las 3 piezas

### 1 · Formato v4 de las líneas DTE (guías 52 y facturas 33)

**El bug que mata**: el PDF de Wasabil imprime las líneas en dos modos — con ≤5 líneas,
dos renglones (nombre / descripción + « - » + código); con **≥6 líneas, UN renglón: solo
el campo `name`** — y bota descripción y código. Como v2 mandaba la descripción en
`name` y el n° de parte solo en `code`, toda guía de 6+ líneas (el caso normal) salía
**sin ningún número de parte y con el nombre repetido**. Evidencia: PDFs reales del
2026-08-25 — folios 233 (4 líneas, bien) vs 234/235 (10 líneas, sin números) — con
datos idénticos en el API. El umbral 5→6 quedó clavado con pares del mismo día.

**El arreglo** (`wasabil_dte/service.py`):
- `name` = **«NUMERO_PARTE Descripción»** — la parte va PRIMERO y jamás se corta; si el
  conjunto excede 80, se corta solo la descripción en límite de palabra. El único campo
  que se imprime en TODOS los modos ahora lleva los dos datos.
- `NOMBRE_MAX = 80`: el tope REAL de NmbItem (Formato DTE v2.5 pág. 37 + XSD
  maxLength=80; Openfactura y LibreDTE emiten 80 exactos en producción desde 2021). El
  25 anterior decía «límite SII» y era falso — y era la causa del corte a media palabra
  que en su día (folio 136) hizo mover la parte a `code`.
- `sanitizar_latin1()` sobre name, description, **code** y receiverContact: el XML DTE
  viaja en ISO-8859-1 y la ÚNICA causa documentada de rechazo por contenido es
  «Invalid Character» (Instructivo SII pág. 20) — comillas tipográficas y guiones
  largos se transliteran, emojis y controles C0 se descartan, **tildes y ñ pasan
  intactas**. El largo no era el enemigo; los caracteres sí.
- `external_id` (snake_case) viaja JUNTO a `externalId`: el API esperaba snake_case y
  botaba el camelCase en silencio (verificado: null en TODOS los docs reales). El
  matching local del precio congelado sigue leyendo `externalId` del payload_json — por
  eso van ambos. El cruce por n° de parte sanitiza SUS DOS lados
  (`routers/contabilidad.py`) para que payloads viejos y nuevos sigan matcheando.
- **Advertencia >10 líneas** en el verificar previo (guía y factura), NO bloqueante: la
  cuenta emite por la vía SII gratuito, cuyos únicos 3 rechazos históricos fueron todos
  por la regla no documentada de **máximo 10 ítems por documento**.
- La previsualización de la app ahora pinta también el código de cada línea.

**Validación física pendiente del dueño**: la primera guía real ≥6 líneas con formato
nuevo — mirar el PDF antes de repartir (primer ejercicio de nombres >31 chars de la
cuenta y del truncado IT1 del timbre, que hace Wasabil). El borrador
`PRUEBA-FORMATO-V4-NO-EMITIR` de Wasabil se puede eliminar cuando el dueño quiera.

### 2 · Buscador de picking en «Crear despacho»

El ciclo real del operador: caja en mano → teclea el número → **aparecen las líneas
propuestas** → clickea y elige cantidad igual que siempre. Sobre el modal existente
(`DespachosPage.tsx`, helpers puros en `frontend-src/src/picking/picking.ts`):

- Normalización ESPEJO del buscador de servidor (`_colapsar`): mayúsculas + sin guiones
  ni espacios, en los dos sentidos (7T1997 ≡ 7T-1997), con aviso «también busqué X».
  Los puntos NO se eliminan (regla del backend; sonda §E la congela).
- Coincidencia única se ilumina con badge «Enter para marcar» → Enter marca y salta a la
  cantidad con el texto seleccionado; con VARIAS (líneas partidas del split comparten
  n° de parte) jamás se auto-elige — badge «línea 1 de 2» las distingue.
- Enter en la cantidad limpia la búsqueda y vuelve al buscador (siguiente caja).
  Handlers LOCALES (jamás listener global); Esc solo limpia; re-buscar una marcada
  enfoca su cantidad con aviso «ya marcada con N».
- Contador fijo «Marcadas X de Y líneas · Z unidades» SIEMPRE sobre la selección
  completa (+ «N marcadas ocultas por el filtro»); toggle «Ocultar marcadas» al que la
  búsqueda le gana; tres estados de vacío distintos y honestos.
- **Cantidad como TEXTO** (precedente FirmarGuiaModal): el input numérico controlado se
  comía el punto decimal (2.5 → 25/tope). Clamps al enviar, no por tecla.
- Paso de resumen antes de crear: qué va, qué **queda pendiente** (vocabulario a
  propósito: «parcial»/«faltante» pertenecen a la firma de guía), botón «Crear despacho
  (Z unidades)». Cierre por overlay/X/Cancelar con confirmación si hay trabajo tipeado.
- El mismo filtro quedó en la tabla de ítems de la OC expandida (paso 8 pendiente de la
  spec de buscadores 2026-08-05).

### 3 · Bultos por OC

El operador empaca MIENTRAS crea despachos y cada despacho (= una guía) viaja en un
bulto (caja) que él rotula. `despachos.bulto_numero` VARCHAR(50) NULL, **texto libre a
propósito** («1», «B2», «Cajas 2-3» — un despacho grande puede ocupar 2 cajas y un
entero no lo expresa). Rotulado logístico puro: cero cálculos, cero contabilidad.

- Campo opcional al crear (excepción deliberada al «paso 1 = solo qué se despacha»: el
  bulto es un hecho físico del empaque, no un dato de transporte) y editable después en
  todo estado salvo anulado. Backend: trim, «»→NULL, >50 → 400 claro.
- Chip «📦 B2» en la fila del despacho.
- Botón **«Bultos»** en la OC expandida → reparto agrupado (bulto → guía → «3 x 1R-0716
  - Rodillo inferior») con encabezado (cliente, dirección, totales), «Guía N°
  PENDIENTE» si falta folio, sección «SIN BULTO ASIGNADO», aviso si dos rótulos solo
  difieren en mayúsculas (b2/B2 = probable misma caja; no se unifica en silencio), y
  botón copiar — el texto listo para el mail al transportista (Samex). **Usa SIEMPRE
  qty_despachada**: el mail se manda ANTES del viaje; la firma y el faltante ocurren
  después y no participan.
- El bulto NO entra al buscador del listado (decisión de alcance; anotado como v2).

## Dónde vive cada pieza

| Pieza | Archivo |
|---|---|
| Formato v4 + sanitización + external_id | `backend/wasabil_dte/service.py` |
| Advertencia >10 líneas (verificar previo) | `backend/wasabil_dte/router.py` |
| Cruce precio congelado sanitizado (2 lados) | `backend/routers/contabilidad.py` |
| Columna bulto + migración | `backend/models/models.py` · `backend/migrations/despacho_bulto_numero.py` |
| Bulto en crear/editar/serializers | `backend/routers/despachos.py` |
| Suites | `backend/wasabil_dte/tests/test_service.py` · `backend/routers/tests/test_bulto_despacho.py` |
| Buscador + bulto + reparto (UI) | `frontend-src/src/pages/DespachosPage.tsx` |
| Helpers puros de picking | `frontend-src/src/picking/picking.ts` |

## Método de la entrega

Plan revisado por enjambre de 4 (diagnóstico con PDFs y API reales + 2 revisores de
pieza + adversario) + certificador de consolidación; decisión del tope 80 con doble
investigación (spec SII/XSD/mercado + escaneo empírico de la cuenta); construcción con
3 constructores de archivos disjuntos sobre cimientos del orquestador; revisión
multiángulo (adversario de bordes + React/TS + backend/contrato) → lote de arreglos
aplicado e iterado a cero. Gate: pytest pelado + tsc + build.
