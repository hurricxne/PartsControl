# Guía firmada obligatoria — MonzaParts (2026-08-06)

> Regla de negocio: **una guía de despacho solo se factura si el cliente la firmó**
> (foto/PDF + fecha de la firma registradas). El retiro en oficina ("sin guía") solo
> factura mercadería que **no** está comprometida en ningún despacho. La única factura
> que no nace de una guía es la de **anticipo** (vía B).
>
> Es la paridad con MachParts (`routers/contabilidad.py`: "REGLA RECTORA: SOLO se
> factura una guía FIRMADA"), que Monza no tenía: su marca de firma era un toggle
> informativo en Contabilidad que no bloqueaba nada.

## 1. El flujo, de punta a punta

```
Bodega/Ventas          Despachos (panel "Despachos en curso")            Contabilidad
─────────────          ─────────────────────────────────────            ────────────
crear despacho ──────► en_preparacion (borrador)
                          │  emitir guía SII (o N° papel + fecha en Editar)
                          ▼
                       Confirmar (cerrar) ──► estado 'despachado'
                          │                        │
                          │   entrega física       │  ✗ FACTURAR: bloqueado
                          ▼                        │    "guía no FIRMADA…"
                       [Marcar guía firmada]       │
                       foto/PDF + fecha firma      ▼
                          └──────────────────► guia_firmada=1 ──► ✓ FACTURAR (33 manual o SII)
                                                                       │
                                                                       ▼
                                                                  cobranza (OC + factura
                                                                  + guía firmada)
```

Las **cuatro fechas** de un despacho (no confundirlas):

| Columna | Qué es | Quién la pone |
|---|---|---|
| `fecha` | creación del despacho | el sistema, al crear |
| `fecha_despacho` | cierre (la mercadería salió) | el sistema, al confirmar |
| `fecha_guia` | **emisión** de la guía ante el SII (solo guía en PAPEL; con guía electrónica manda el `documentDate` del DTE 52). Es el `FchRef` de la referencia 52 de la factura | el operador, en Editar |
| `fecha_firma` | **cuándo el cliente firmó** la guía recibida | el operador, en Marcar guía firmada |

## 2. Dónde vive cada pieza

### Base de datos
En `monza_despachos`:
- `guia_firmada` (INT 0/1) y `guia_firmada_archivo` (VARCHAR) — ya existían.
- `fecha_firma` (DATETIME) y `usuario_firma_id` (INT) — **nuevas**.

En `monza_cont_factura_cliente`:
- `sin_guia` (INT NOT NULL DEFAULT 0) — **nueva**: el **canal** de la factura
  (1 = retiro en oficina). Sin ella el neteo guía↔retiro descontaba la misma
  mercadería dos veces (ver §3).

Las tres columnas nuevas las agregan **dos caminos idempotentes** (a prueba del olvido
de cualquiera de los dos):

```bash
python -m migrations.monza_despachos_fecha_firma
```
```bash
python -m monza_contabilidad.init_db
```

### Backend — marcar la firma (`monza_router_despachos.py`)
- `POST /api/monza/despachos/entidades/{id}/firmar` — **multipart** en un solo request
  (`file` + `fecha_firma` + `numero_guia` opcional). Exige:
  - despacho existente y de una venta válida (anti-IDOR) y **cerrado** (`despachado`);
  - archivo no vacío, ≤ 20 MB, extensión foto/PDF (`.pdf .jpg .jpeg .png .webp .heic`);
  - `fecha_firma` con formato `AAAA-MM-DD`, **no futura** (contra hoy-Chile), no más
    vieja que 2 años (malla anti-dedazo) y **no anterior a `fecha_guia`** si existe;
  - si viaja `numero_guia`, pasa por `_rechazar_si_pisa_folio` (no pisa folios SII).
  - Graba: `guia_firmada=1`, `fecha_firma` (a medianoche del día firmado),
    `usuario_firma_id`, `guia_firmada_archivo` (uuid hex32 + ext en `uploads/docs`),
    y una fila `MonzaLog` con `accion='GUIA_FIRMADA'`.
- `GET /api/monza/despachos/docs/{filename}` — sirve la foto/PDF (validación estricta
  del nombre + resolución contra el directorio, espejo del serve de GA). Vive en el
  router Monza porque el serve de GA exige empresa `mineria` (un usuario Monza recibía
  403 por su propia guía).
- `GET /entidades` y `GET /entidades/{id}` serializan `guia_firmada`, `fecha_firma`,
  `guia_firmada_archivo`.

### Backend — exigir la firma (`monza_contabilidad/router.py`, `_construir_factura`)
Es la **única fuente de verdad** de las reglas de facturación: la vía manual, el
preview y la emisión SII (33) pasan por ella. El gate tiene tres piezas:

1. **Modo `despacho_id`** (y el guard B8 de ítems explícitos que traen `despacho_id`):
   además de estado `despachado`, exige `guia_firmada==1`. Error accionable:
   *"La guía de este despacho no está FIRMADA por el cliente: márcala en Despachos…"*
2. **Tope agregado del flujo con guía** (`desp_qty_item_firmada`): cuenta SOLO
   despachos firmados. Sin esto, los ítems explícitos "sueltos" (sin
   `despacho_item_id`) colaban cantidades de guías sin firmar. La derivación del modo
   despacho usa el MISMO diccionario que la validación (derivar contra el global
   generaba líneas que la propia validación rechazaba).
3. **Tope del retiro en oficina** (`sin_guia`): por ítem,
   `vendido − facturado_total − pendiente_guias`, donde
   `pendiente_guias = max(0, comprometida_en_despachos_vivos − facturado_del_canal_guía)`.
   - "Vivos" = `en_preparacion` + `despachado` (un borrador es mercadería comprometida
     a salir con guía; si se anula, el cupo vuelve solo). Anulados no cuentan.
   - Si todo lo pendiente está en guías: 409 *"…factúralo desde su guía (firmada)"*.

**Neteo POR CANAL** (la pieza que hace que 1–3 convivan sin pisarse). Cada factura
guarda su canal en `sin_guia`, y de ahí salen dos consumos independientes:

```
fact_retiro_item  = Σ cantidades de facturas con sin_guia = 1
fact_guia_item    = facturado_total − fact_retiro_item      (canal guía)

tope canal guía   = min( firmado − fact_guia_item ,  vendido − facturado_total )
tope retiro       = vendido − facturado_total − pendiente_guias
```

Sin el canal, el tope de la guía restaba **también** lo facturado por retiro mientras
el retiro ya había reservado esa mercadería: doble descuento. Escenario real que eso
producía (reproducido por el multienjambre): venta de 10 u, guía cerrada de 6 sin
firmar → retiro factura las 4 libres (correcto) → se firma la guía → facturarla daba
**2 u en vez de 6**, y las 4 restantes quedaban **infacturables por todas las vías**
(el despacho cerrado tampoco se puede anular). Hoy la guía factura sus 6 u completas y
la venta cierra al 100% — fijado por sondas en la suite (venta B y su espejo, venta F).
El `min(...)` con el techo global `vendido − facturado_total` es la red que impide
sobre-facturar aunque la atribución de canal del legado sea imperfecta (las facturas
anteriores al cambio quedan como "canal guía").

El PATCH viejo `/ventas/despachos/{id}/guia-firmada` de Contabilidad **se eliminó**
(era un toggle sin validaciones); el schema `GuiaFirmadaIn` también. Contabilidad
ahora solo **lee** la firma (`despachos-facturables`, detalle de venta) y la **exige**.

### Frontend
- `MonzaDespachosPage.tsx` — columna **"Guía firmada"** entre Estado y Acciones del
  panel "Despachos en curso": borradores muestran "—"; cerrados sin firma muestran el
  botón **Marcar guía firmada**; firmados muestran badge verde con la fecha + "Ver"
  (abre la foto) + "Corregir" (re-firma). `FirmarGuiaModal`: foto/PDF + fecha (default
  hoy-Chile, tope hoy) + N° guía opcional con el candado del folio SII (mismos estados
  `verificando/en_emision/sin_folio` que Editar).
- `MonzaFacturasPage.tsx` — el selector de guías **deshabilita** las sin firmar (con
  el motivo en el texto y un aviso ámbar debajo); "Ver guía firmada" usa el serve
  Monza. El texto del retiro explica el tope nuevo.
- `MonzaVentasContabPage.tsx` — los chips de guías quedaron **solo lectura**
  (verde firmada+fecha / ámbar "sin firmar (se marca en Despachos)").
- `monzaApi.ts` — `monzaDespachosAPI.firmarEntidad(id, file, fecha, numeroGuia?)` y
  `monzaDespachosAPI.abrirGuiaFirmada(filename)`. `marcarGuiaFirmada` se eliminó.

## 3. Decisiones de diseño (y por qué)

| Decisión | Por qué |
|---|---|
| Multipart en UN request (vs. upload genérico + PATCH de GA) | Monza no tiene endpoint de docs propio y el de compras de GA está detrás del candado `mineria`. Un solo request además es atómico: no quedan firmas sin foto. |
| **No** existe des-firmar | La facturación depende de la marca: quitarla con una emisión en vuelo abriría la carrera firma↔factura. Un error se corrige **re-firmando** con los datos buenos. |
| Re-firmar deja el archivo anterior huérfano en disco | Mismo comportamiento del resto de uploads de la casa; el costo es bajo y borrar archivos referenciados por logs viejos es peor. |
| La fecha de firma no se valida contra `fecha` ni `fecha_despacho` | Son relojes del servidor en UTC y el registro puede ir atrasado respecto del hecho físico: validar contra ellas fabricaba falsos rechazos nocturnos (UTC ya es "mañana" pasadas las ~21:00 en Chile). Sí se valida contra `fecha_guia` (nadie firma una guía que no existe). |
| Los **borradores** cuentan en el tope del retiro | Facturar por caja mercadería que está por salir con guía es el mismo bypass que la regla cierra. Si el borrador se anula, el cupo vuelve sin tocar nada. |
| Guías sin firmar **se listan** en `despachos-facturables` (flag en falso) | Ocultarlas mandaba al operador a buscar una guía "desaparecida"; el selector la muestra deshabilitada y dice cómo destrabarla. |
| El gate **no** es retroactivo | Facturas emitidas antes del cambio quedan como están; los topes usan `max(0, …)` para que un legado sobre-facturado no descuadre el cálculo. |
| Todo despacho cerrado exige firma, tenga o no N° de guía | Si "sin número" eximiera de la firma, bastaría no teclear el número para saltarse el candado. |
| El PUT de cabecera no puede dejar `fecha_guia > fecha_firma` | Firmar valida firma ≥ emisión; editar la emisión ex-post rompía el invariante por la puerta de atrás. Si la fecha buena de la guía es posterior a la firma, lo que está mal es la firma: se corrige re-firmando. |
| Los cerrados **sin firmar** nunca desaparecen del panel | El botón de firmar vive solo ahí y el corte de 30/100 escondía justo las guías que llevan semanas esperando la firma: quedaban infacturables desde la interfaz. Mismo criterio que ya protegía a los borradores. |
| Firmar y el PUT de cabecera toman `FOR UPDATE` del despacho | Sin el lock, la adopción del folio SII (botón "Reintentar" sobre una emisión ambigua) podía intercalarse y el N° manual lo pisaba, dejando un estado que ninguna API podía corregir. |
| El reintento de emisión SII **re-valida** la firma | Reintentar ejecuta un acto SII nuevo e irreversible: una factura pre-candado no puede re-emitirse citando una guía que el cliente nunca firmó (mismo criterio del cinturón de líneas que ya existía). |
| La factura de **anticipo** solo **advierte** (no bloquea) si hay mercadería en guías | El anticipo es la salida obvia del operador al que el gate le rechaza la guía, y un "anticipo" por mercadería ya entregada no respalda ningún depósito. No se bloquea porque la Fase 7 fijó que la vía manual no bloquea y existe flujo legítimo tardío. **Endurecerlo es decisión del dueño** (pendiente). |
| Crear un despacho **no** descuenta lo ya facturado por retiro | Facturar primero y trasladar después con guía es un flujo **legal y normal en Chile**, y MachParts se comporta igual (`_cupo_disponible` = tope físico − ya despachado). Consecuencia asumida: si el operador factura como retiro mercadería que **después** decide despachar con guía, esa guía no pasó por el gate de la firma. El bypass con el despacho **ya existente** sí está cerrado (los borradores cuentan). **Endurecerlo es decisión del dueño** (pendiente). |
| El estado SII del panel se consulta **en tandas de 200** | El endpoint rechaza más de 200 ids. Recortar la lista sería *fail-open* (las filas sin estado se tratarían como "sin guía electrónica" y dejarían editar el N° a mano); partir en tandas conserva el candado para todas. |
| El grupo "sin firmar" del panel tiene tope de **500** | Al activar la regla todo el histórico cerrado nace sin firma: sin tope, la respuesta traía la tabla entera. 500 está holgadamente por encima de cualquier backlog real de guías pendientes, así que el arreglo del panel sigue en pie. |

## 4. Deploy (orden estricto)

1. `cd backend && python -m migrations.monza_despachos_fecha_firma`  ← **ANTES de reiniciar**
   (el ORM ya declara `fecha_firma` y `sin_guia`; con las tablas viejas, cualquier
   escritura cae con *Unknown column*). Equivalente: `python -m monza_contabilidad.init_db`
   — ambos dejan el esquema listo, correr cualquiera de los dos (o los dos: son idempotentes).
2. Reiniciar backend. 3. Desplegar frontend (build ya validado).

**Impacto operativo** (avisar al equipo): toda guía Monza ya despachada y aún sin
facturar queda **bloqueada para facturar hasta que alguien la marque firmada** en
Despachos. Es el comportamiento pedido; el mensaje de error dice exactamente qué hacer.

## 5. Cómo debuggear

| Síntoma | Dónde mirar |
|---|---|
| "La guía de este despacho no está FIRMADA…" | `SELECT guia_firmada, fecha_firma, guia_firmada_archivo FROM monza_despachos WHERE id=…` — si `guia_firmada=0`, falta marcarla en Despachos. |
| "…cantidad excede lo vendido no facturado y sin guía asociada…" (retiro) | La qty está comprometida en un despacho vivo: `SELECT d.id,d.estado,di.qty_despachada FROM monza_despachos d JOIN monza_despacho_items di ON di.despacho_id=d.id WHERE d.cotizacion_id=… AND d.estado IN ('en_preparacion','despachado')`. Se factura por la guía, o se anula el borrador. |
| "Primero confirma (cierra) el despacho…" | El despacho sigue `en_preparacion`: confirmarlo primero (la firma es de la ENTREGA). |
| "…ANTERIOR a la emisión de la guía…" | `fecha_firma < fecha_guia`: revisar ambas fechas (Editar / re-firmar). |
| ¿Quién firmó y cuándo? | `monza_logs` con `accion='GUIA_FIRMADA'` (email, fecha, archivo), y `usuario_firma_id` en el despacho. |
| La foto no abre (403) | Se está usando el serve de GA (`/api/despachos/docs/…`) en vez del de Monza (`/api/monza/despachos/docs/…`). |
| Unknown column `fecha_firma` o `sin_guia` | Falta la migración del punto 4.1. |
| La guía se factura por MENOS de lo que despachó | Revisar el canal de las facturas de la venta: `SELECT id, numero_factura, sin_guia, es_anticipo FROM monza_cont_factura_cliente WHERE cotizacion_id=…`. Una factura de retiro marcada como canal guía (`sin_guia=0`) le come cupo a la guía. Facturas anteriores al 2026-08-06 quedan todas en 0 por diseño. |
| Un despacho cerrado sin firmar no aparece en el panel | No debería: `GET /entidades` los trae siempre completos. Verificar `guia_firmada` en BD (un `NULL` legado cuenta como sin firmar y también debe aparecer). |

Prueba manual rápida (con sesión válida):

```bash
curl -s -X POST "http://localhost:8000/api/monza/despachos/entidades/123/firmar" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@guia-firmada.jpg" -F "fecha_firma=2026-08-06"
```

## 6. Tests

- **Suite propia del gate**: `monza_contabilidad/tests/test_guia_firmada_gate.py`
  (34 checks: endpoint de firma completo, gate en los 3 modos, tope del retiro con
  guía cerrada y con borrador, **neteo por canal en los dos sentidos** (retiro→guía y
  sueltos→retiro), invariante firma≥emisión también por el PUT, anticipo intacto,
  serve + path traversal, y el cinturón del REINTENTO SII en sus dos sentidos). Construida con **sondas de poder discriminante**: se
  verificó apagando el guard y viendo la suite caer.

### Auditoría multienjambre — ronda 1 (2026-08-07)
15 agentes en 6 lentes (ruta del dinero, concurrencia, SII, seguridad, frontend,
cazador de bypass) + verificación adversarial de cada hallazgo. Resultado: **9
confirmados, todos reparados** — el doble descuento entre canales (HIGH, la reparación
mayor de esta entrega), la suite SII que el invariante nuevo dejó roja, el panel que
escondía las guías viejas sin firmar, el TOCTOU del folio, el reintento SII sin
re-validar la firma, y 4 detalles de interfaz. El bypass del anticipo quedó como
**advertencia** + decisión pendiente del dueño (ver §3).

### Ronda 2 — auditoría de LAS REPARACIONES (2026-08-08)
Los 4 lentes que completaron (canal, SII, panel, cazador) declaran **sólidas** las
reparaciones: **0 CRITICAL / 0 HIGH**, ninguna vía de sobre-facturación ni de
re-atrapamiento. El lente del canal lo demostró con aritmética (invariante
`V−F−r ≥ P ≥ Df−G`: el retiro nunca achica el cupo de la guía) y el de SII verificó lo
más delicado — que el cinturón nuevo **no** bloquea ninguna vía de *recuperación* de un
documento ya emitido (todas retornan antes). Se repararon además 6 hallazgos menores:

| Hallazgo | Reparación |
|---|---|
| **MEDIUM** — al mostrarse completos los sin firmar, el estado SII podía superar los 200 ids y caer entero: el panel perdía **todos** los badges y el candado del folio quedaba en "Verificando" | Consulta **en tandas de 200** (no recorte: sería fail-open) |
| **MEDIUM** — el cinturón del reintento SII no tenía sonda: borrarlo dejaba la suite verde | Sonda con **poder discriminante verificado** (sin cinturón la suite cae) + espejo positivo |
| **MEDIUM** — la advertencia del anticipo usaba el facturado TOTAL y se callaba justo en el caso mixto (retiro + guía sin firmar) | Usa el facturado **del canal guía**, igual que `pendiente_guias_item` |
| **LOW** — `/entidades` podía crecer sin tope y duplicar una fila si una transición se commiteaba entre sus 3 SELECTs | Tope de 500 en el grupo sin firmar + **dedup por id** |
| **LOW** — `firmar` validaba firma ≥ emisión sobre el snapshot pre-lock: una carrera con el PUT persistía el par que R9 prohíbe | **Re-validación bajo el `FOR UPDATE`** |
| **LOW** — con facturas legadas, una guía nueva y firmada podía quedar sin cupo y el 409 decía "ya fue facturado por completo" (falso) | Mensaje específico que nombra la salida real (firmar también la guía antigua) |

Los 2 residuos restantes se registraron como **decisiones** en §3 (retiro anterior a que
exista el despacho; creación de despacho sobre mercadería ya facturada), no como
defectos: ambos son fronteras del diseño y ambos esperan decisión del dueño.
- **Suites adaptadas**: los despachos de prueba de las suites históricas nacen
  `guia_firmada=1` (el gate tiene su suite; ellas ejercitan lo suyo). El harness
  compartido `monza_wasabil_dte/tests/factura_harness.py` firma de fábrica.
  `test_por_facturar_fisico` y `test_integration` firman por el **endpoint real**
  (e2e del multipart) y limpian el archivo subido.
- **Gate del proyecto**: `cd backend && ./venv/bin/python -m pytest` pelado —
  **248 verdes** al cierre de este cambio.
