# Paquete «cierre-de-venta-y-fechas» — 2026-10-06

Siete arreglos alrededor del Cierre de Venta de Grupo AM y de la cuenta de días hábiles:
el reloj de los días restantes, el plazo que no sirve para calcular, el ítem sin plazo
propio, dos calendarios de feriados para una misma promesa, dos cierres simultáneos de la
misma venta, el 409 que la pantalla Clientes no mostraba, y dos comentarios que prometían
una independencia del portal que no existe. Sin migración nueva. Backend y frontend salen
juntos.

## 1. Qué se arregló, por ítem

| Id | Qué pasaba | Qué hace ahora |
|---|---|---|
| **C42** | `business_days_remaining` contaba desde `date.today()` (el servidor corre en UTC): entre las 20-21 h y la medianoche de Chile, Despachos pintaba «Vencido 1d» lo que vence hoy, y el `dias_restantes` de Compras erraba por un día | Cuenta desde el día de Chile (`hoy_chile`), igual que el «vencida» del portal y que la otra marca. `hoy_chile` sube a la cabecera de `routers/compras.py` (un solo punto parcheable; `wasabil_dte/service.py` solo importa stdlib, no hay ciclo) y desaparece el import local del cierre. Los 4 llamadores (`dias_restantes_oc`, `worst_item_days` y `dias_rest` en Despachos; `_item_to_dict` en los 5 listados de Compras) se corrigen dentro del helper |
| **C41** | Un plazo guardado sin cota llegaba a `add_business_days` por tres puertas: un 3651 daba una fecha de 2041 en Despachos y `dias_restantes` 3629 en Compras; un -5 devolvía el día de creación de la cotización; un 99999999 colgaba el listado completo (`OverflowError` tras segundos) | Un solo filtro, `plazo_creible` (junto a `MAX_PLAZO_DIAS_PROMESA`): entero entre 1 y 3650, o `None`. Lo usan el congelado del cierre (vía `plazo_prometido`), `_item_deadline` (Despachos → cae a la fecha de entrega de la orden) y `_item_to_dict` (Compras → `dias_restantes` queda `None`, el dict sigue devolviendo `plazo_entrega_max` tal cual). El try/except de `_item_deadline` queda igual |
| **C20** | El congelado solo miraba `plazo_entrega_max`; pero el ítem sin plazo propio SÍ tiene promesa escrita: la cotización formal imprime `plazo_entrega_max or plazo_max_default` (`or 45`). En una cotización mixta (10 días + sin plazo con default 45) el segundo heredaba la fecha de cabecera = hoy + 10 y el portal lo habría mostrado comprometido 35 días hábiles antes de lo cotizado. Que el 45 estuviera guardado o en NULL era un accidente del editor | `plazo_prometido(plazo_item, plazo_por_defecto)`: el plazo del ítem o, si no tiene, el plazo máximo por defecto del cotizador leído al cierre (respaldo `PLAZO_MAX_SIN_CONFIG = 45`, con sonda de paridad contra `_config_to_dict`). Un plazo propio que no sirve NO cae al default (queda NULL). `cfg_obj` sale del `if not cot.pricing_snapshot` para leer el default siempre. La cabecera que sugiere la pantalla del Cierre cuenta igual (`plazoDeLaOrden`): con el plazo máximo tal como está guardado, sin los respaldos de la fila |
| **C19** | Dos tablas de feriados para una misma promesa: la pantalla descontaba Jueves Santo y el «tercer lunes de junio»; el backend, el 20 de junio fijo. Cierre 2027-02-11 + 30 → backend 2027-03-25, pantalla 2027-03-29; en 2026-2028 difieren 1.318 de 5.480 sumas, y la dirección no es siempre la misma | Manda la tabla del backend (con ella ya hay fechas selladas y semáforos de ventas en curso; NO se toca). La pantalla se alinea borrando Jueves Santo y el tercer lunes (y `nthWeekday`, que quedaba sin uso) y agregando `[6,20]`. Un espejo compila el TypeScript real y lo compara con el Python para que no vuelvan a divergir. Qué calendario manda es decisión del dueño (§6) |
| **P17** | `crear_oc_cliente` consultaba y después insertaba, sin lock ni UNIQUE: dos cierres simultáneos de la misma venta dejaban dos OC (y no hay endpoint para borrar una) | La cotización se lee con `populate_existing().with_for_update()` (regla 3 de `docs/regla-lecturas-de-plata.md`): el segundo cierre espera y cae en el early-return idempotente. Depende de READ COMMITTED (`database.py`); no se agrega `with_for_update` a la consulta de `oc_existente` (rango vacío: regla 4). Queda diferido el `UNIQUE(cotizacion_id)` (§5) |
| **C14, C21** | El 409 de `eliminar_cliente` («tiene cuentas del Portal de Clientes… elimínalas primero») no lo pintaba nadie: `deleteMutation` era la única mutación de `ClientesPage.tsx` que descartaba el error | `onError` muestra `e?.response?.data?.detail` durante 8 s, copiando el patrón de las dos mutaciones hermanas del mismo archivo. El backend no cambia |
| **C8** | Comentarios que prometían que `compras.py` y `clientes.py` «no dependen del módulo portal», a pocas líneas de un import que SÍ es opcional (la notificación, dentro de un try) | Solo texto: los imports de `portal` son locales por costumbre, pero la dependencia es dura (sin try; si el portal falla, el cierre / la ficha no se guardan). Los imports no se mueven |

## 2. Archivos tocados

| Archivo | Símbolo | Ítems |
|---|---|---|
| `backend/routers/compras.py` | cabecera (`from wasabil_dte.service import hoy_chile`) · `_is_chile_holiday` (docstring y comentario del 20 de junio; la tabla no cambia) · `add_business_days` (docstring) · comentario de `MAX_PLAZO_DIAS_PROMESA` · `plazo_creible` (nuevo) · `PLAZO_MAX_SIN_CONFIG` y `plazo_prometido` (nuevos) · `business_days_remaining` · `_item_to_dict` · `crear_oc_cliente` (lock, `cfg_obj`, congelado, comentario del vínculo, sin import local de `hoy_chile`) | C42 C41 C20 C19 P17 C8 |
| `backend/routers/despachos.py` | import de `plazo_creible` · `_item_deadline` | C41 |
| `backend/routers/clientes.py` | comentario de bloque «Portal de Clientes: la ficha es el ANCLA del aislamiento» | C8 |
| `backend/models/models.py` | comentario de `ItemCotizacion.fecha_promesa_cliente` | C20 C19 |
| `backend/migrations/item_fecha_promesa_cliente.py` | viñeta del docstring (sin cambio de lógica) | C20 |
| `frontend-src/src/pages/CierreVentaPage.tsx` | `isHoliday` · `nthWeekday` (borrada) · `plazoDeLaOrden` (nueva) · cálculo de `maxPlazo` en el `useEffect` que carga los ítems | C19 C20 |
| `frontend-src/src/pages/ClientesPage.tsx` | `deleteMutation.onError` | C14 C21 |
| `backend/routers/tests/test_fecha_promesa.py` | §A (a2 reescrita, a2b nueva; la siembra agrega un ítem «solo mínimo»), §E (e3 reescrita, e4 y e5 nuevas), §F (carrera), §G (reloj), §H (`_item_to_dict`), §I (tablas de `plazo_creible` y `plazo_prometido` + paridad) | todos |
| `backend/routers/tests/test_cierre_espejo_frontend.py` | NUEVO: m0 (literales del backend), m1 (feriados 2026-2035), m2 (sumas 2026-2028 × 5 plazos), m3 (`plazoDeLaOrden`) | C19 C20 |
| `backend/routers/tests/test_clientes_espejo_frontend.py` | NUEVO: mitad pantalla (estática) + mitad backend (sesión de mentira) | C14 C21 |
| `backend/portal/tests/mutaciones/cierre.py` | NUEVO: lista de mutaciones de las tres suites para el arnés | todos |
| `docs/fase-2-portal-fecha-promesa-2026-08-30.md`, `docs/plan-portal-clientes-2026-08-20.md`, `docs/fase-3-4-5-portal-pedidos-2026-10-04.md`, `docs/CHECKLIST-DEPLOY-2026-07-20.md`, `docs/regla-lecturas-de-plata.md`, `docs/fase-1-portal-identidad-2026-10-02.md` | las frases que este paquete dejaba falsas o incompletas (cada una nombrada en el plan) | — |

Los archivos del programador se editaron con reemplazos exactos (`git diff --numstat HEAD`:
compras.py 95/39, despachos.py 11/8, clientes.py 5/4, models.py 13/10, migración 6/6,
CierreVentaPage.tsx 21/26, ClientesPage.tsx 1/1); todos siguen en LF.

## 3. Migración y deploy

- **Sin migración nueva.** La columna `items_cotizacion.fecha_promesa_cliente` es la de la
  Fase 2; si el servidor todavía no la tiene, va ANTES de reiniciar el backend:

  ```bash
  cd backend && python -m migrations.item_fecha_promesa_cliente   # idempotente
  ```

- Backend y frontend salen **juntos**: reinicio completo de uvicorn (no `--reload`) y
  build del frontend (`npm run build`). Si uno sale antes que el otro no hay estado peor
  que el de hoy: con solo el backend, la cabecera del Cierre sigue sugiriendo con la tabla
  y la regla viejas; con solo el frontend, los ítems sin plazo siguen la cabecera, que ya
  cuenta el default.
- Comprobación rápida en local después de reiniciar: cerrar una cotización mixta (un ítem
  a 10 días y otro sin plazo): la cabecera sugiere hoy + 45 hábiles y la base muestra las
  dos fechas por ítem; abrir Despachos y Compras (ningún ítem con plazo absurdo responde
  500); borrar en Clientes una ficha con cuenta del portal (se ve el motivo durante 8 s).

## 4. Cómo verificar

Desde `backend/`, con uvicorn APAGADO (pytest se cuelga con el servidor arriba), de a una:

```bash
./venv/bin/python -m pytest routers/tests/test_fecha_promesa.py -q           # 5 passed
./venv/bin/python -m pytest routers/tests/test_cierre_espejo_frontend.py -q  # 4 passed (necesita node y npm install en frontend-src)
./venv/bin/python -m pytest routers/tests/test_clientes_espejo_frontend.py -q # 2 passed
./venv/bin/python -c "import routers.compras, routers.despachos, main"       # imports OK
cd ../frontend-src && npx tsc -b                                              # sin errores
```

Resultado obtenido al construir el paquete: **5 + 4 + 2 passed**, `tsc` limpio.

Las sondas nacieron ROJAS con el código anterior (escritas antes del arreglo): a2 y a2b
(NULL en vez de hoy + 45), e4 (2041-04-11), e5 (el día de creación), f1/f2/f3 (dos OC,
dos ids, dos avisos), h1 (`dias_restantes` 3627), m1 (28 fechas con dos calendarios), m2
(1.318 de 5.480 sumas distintas), m3 (la pantalla no definía `plazoDeLaOrden`) y la mitad
pantalla de Clientes (el `onError` no recibía el error).

**Mutaciones** (cada una tiene que poner su suite en rojo; una «VERDE (NO DISCRIMINA)»
significa que la prueba no prueba lo que dice). El arnés copia `backend/` a una carpeta
temporal; los espejos leen la pantalla en `frontend-src` junto a `backend/`, así que se le
indica dónde está la real:

```bash
cd backend
PARTSCONTROL_FRONTEND_SRC="$(pwd)/../frontend-src" \
  ./venv/bin/python -m portal.tests.mutaciones fecha_promesa cierre_espejo clientes_espejo
```

Resultado obtenido: **25 de 25 mutaciones detectadas** — `test_fecha_promesa.py` 17/17,
`test_cierre_espejo_frontend.py` 6/6, `test_clientes_espejo_frontend.py` 2/2 (lista en
`portal/tests/mutaciones/cierre.py`).

Las 5 mutaciones que viven en la pantalla (`CierreVentaPage.tsx`: reponer Jueves Santo,
quitar `[6,20]`, cambiar la regla de fin de semana, `plazoDeLaOrden` ignorando los ítems
sin plazo; `ClientesPage.tsx`: `onError` sin el error) no entran al arnés (copia solo
`backend/`). Se verificaron a mano sobre una COPIA temporal de cada pantalla (carpeta con
`src/pages/…` y `node_modules` enlazado, apuntada con `PARTSCONTROL_FRONTEND_SRC`), sin
tocar el árbol de trabajo (hash igual antes y después): **5 de 5 en rojo** — m1 con 10
fechas distintas en cada caso de feriados, m2 con 4.523 sumas distintas, m3 y Clientes
con el mensaje esperado.

## 5. Cómo revertir

- Todo el paquete: revertir el commit (backend y frontend juntos). No hay migración ni
  dato nuevo en la base: las fechas que se congelen mientras tanto para ítems sin plazo
  propio quedan guardadas (son fechas válidas, hoy + default), y Despachos y el portal las
  siguen leyendo igual.
- Solo el lock (P17): quitar `.populate_existing().with_for_update()` de la lectura de la
  cotización en `crear_oc_cliente`; la sonda f1 se pone roja a propósito.
- Solo la regla del ítem sin plazo (C20): en `crear_oc_cliente` volver a
  `plazo_it = plazo_creible(it_cot.plazo_entrega_max)` y en `CierreVentaPage.tsx` al
  filtro que ignora los ítems sin plazo; a2, a2b y m3 se ponen rojas a propósito.
- Solo la tabla de la pantalla (C19): no conviene revertirla sola (vuelven los dos
  calendarios); si se hace, m1 y m2 quedan rojas.

## 6. Decisiones que quedan para el dueño

- ¿Con qué calendario de feriados se cuentan los días hábiles de las fechas prometidas? Hoy el sistema usa una lista aproximada (feriados de fecha fija + Viernes Santo; no conoce los feriados que la ley corre de día, los de elecciones, ni que el Día de los Pueblos Indígenas algunos años cae el 21 de junio), y la pantalla de Cierre de Venta usaba OTRA lista (con Jueves Santo y el «tercer lunes de junio»): por eso la fecha que veía el comercial y la que quedaba guardada por ítem diferían 1-2 días hábiles cerca de Semana Santa y de junio. MIENTRAS TANTO se aplica lo conservador: manda la lista que el sistema ya usa para sellar fechas y semáforos, y la pantalla se alinea a ella; ninguna fecha ya guardada se mueve. Pasar al calendario legal exacto es un cambio aparte: mueve semáforos de ventas en curso y obliga a mantener la lista cada año.

- Cuando el comercial cambia a mano la «Fecha prometida de entrega» al cerrar la venta (o después, con «Editar OC»), ¿esa fecha debe mover la que el cliente ve por cada ítem en el portal? Hoy NO: por ítem manda día del cierre + plazo de la cotización (tu decisión D11); la fecha tecleada queda como «Entrega» en Despachos, en el aviso a Abastecimiento y como respaldo de los ítems sin fecha propia. MIENTRAS TANTO se mantiene así, sin inventar una regla nueva. Ojo: si el comercial acuerda con el cliente una fecha distinta de la del plazo cotizado y solo la escribe ahí, el portal seguirá mostrando la del plazo. Para que el portal muestre otra fecha hay que corregir el plazo del ítem en el cotizador ANTES de cerrar la venta.

- Ítems que se cotizaron sin plazo propio (el editor les muestra el plazo por defecto, por ejemplo 45 días, y el PDF le dice al cliente «de 30 a 45 días hábiles»): desde ahora su fecha queda sellada al cerrar la venta como día del cierre + ese plazo por defecto, igual que el resto. Hasta hoy quedaban sin fecha propia y heredaban la fecha general de la orden; en una cotización mixta (un ítem a 10 días y otro por defecto) el portal le habría mostrado al cliente el segundo comprometido 35 días hábiles ANTES de lo cotizado. Además, que un ítem tuviera o no el 45 «guardado» dependía de si alguien había tocado esa fila en el editor, así que dos ítems idénticos en pantalla se comportaban distinto. SE APLICA ESTO por ser lo que el cliente ya tiene por escrito y lo que pediste en D11 (fecha por ítem, congelada al cierre). La alternativa es que esos ítems sigan la fecha general de la orden (la que el comercial puede ajustar al cerrar); si la prefieres, hay que decirlo antes de abrir el portal a clientes, porque las ventas que se cierren mientras tanto quedan selladas (las ya cerradas no se tocan). Caso borde anotado para el programador: si un ítem solo trae plazo mínimo o un plazo escrito como texto en el Excel de origen, la fila de la pantalla de Cierre todavía muestra ese número, mientras la fecha sellada usa el plazo por defecto.

- Antes de abrir el portal a clientes: pedirle al programador que corra UNA consulta de solo lectura en producción para saber si existe alguna venta con dos órdenes de cliente (podía pasar si dos personas cerraban la misma venta al mismo tiempo). Y decidir si se agrega el candado definitivo en la base (una sola orden por cotización), que requiere un script de migración sobre una tabla del programador. MIENTRAS TANTO: el sistema pone en fila dos cierres simultáneos de la misma venta, y el portal ya muestra una sola orden por venta aunque existiera un duplicado.

## 7. Lo que queda escrito para el programador (fuera de este paquete)

- **Misma clase que C42, en tus archivos:** `routers/bodega.py::_notificar_ocs_cliente` y
  `::alertas_plazo_critico`, y `scheduler.py::_checks_grupo_am` restan `oc.fecha_entrega`
  contra `date.today()`. El barrido corre a las 06:00 de Santiago (las dos fechas
  coinciden); el aviso instantáneo de Bodega sí puede errar por un día de noche. Mismo
  cambio de una línea (`hoy_chile()`).
- **Cota del plazo donde ENTRA el dato (C41):** `Field(ge=0, le=3650)` en `ItemUpdate`
  (`routers/cotizador.py`), `ManualItemRequest` (`routers/cotizaciones.py`), `ItemPlazo` y
  `AsignacionParcialItem` (`routers/compras.py`); recorte de `_pmax` en `upload_venta_clp`
  (`routers/venta_clp.py`); cota de `plazo_max_default` en `ConfigUpdate`. En el navegador
  falta el mismo tope: `addBusinessDays` de `CierreVentaPage.tsx` y `PreEmbarquesPage.tsx`
  recorren día por día y congelan la pestaña con un plazo gigante. Misma clase:
  `business_days_remaining` recorre día por día hasta `oc.fecha_entrega`, una fecha
  tecleada sin tope (un año 9999 cuesta segundos por listado).
- **Mapeo de ítems de `CierreVentaPage.tsx` (C20):** la fila usa `plazo_entrega_min` y el
  texto `plazo` como respaldo del máximo, mientras el PDF, el editor, la cabecera y el
  congelado usan el máximo guardado o el default. Arreglo exacto: dejar
  `plazo_entrega_max: it.plazo_entrega_max ?? undefined` en ese mapeo y borrar
  `parsePlazoStr`; con eso fila, cabecera, PDF y backend usan una sola regla.
- **`UNIQUE(cotizacion_id)` en `oc_cliente` (P17):** la capa que no depende del nivel de
  aislamiento. Exige script de migración propio, revisar antes los duplicados históricos
  (`SELECT cotizacion_id, COUNT(*) FROM oc_cliente WHERE cotizacion_id IS NOT NULL GROUP BY
  cotizacion_id HAVING COUNT(*) > 1`) y capturar `IntegrityError` alrededor del commit
  (rollback, releer y devolver la OC existente).
- **Puertas gemelas de C19:** `MonzaCotizacionesPage.tsx` tiene la misma tabla vieja contra
  `monza_fechas` (idéntica a la del backend de GA): efecto de 1-2 días en su semáforo, y su
  comentario «Copia LITERAL del motor del Cierre de Venta de Grupo AM … mismo Jueves/Viernes
  Santo» queda FALSO con este cambio; conviene aplicarle la misma edición de `isHoliday` y
  sumar su página al espejo. `PreEmbarquesPage.tsx` tiene una tercera tabla (estimación del
  proveedor, no es promesa al cliente). `monza_fechas._is_chile_holiday` repite el
  comentario «tercer lunes junio — aproximado fijo». Alternativa de raíz: un GET de vista
  previa (el backend calcula, la pantalla pinta), que reescribe `CierreVentaPage` y exige
  `services/api.ts`.
- **Textos que quedaron viejos fuera del paquete:** `scheduler.py`, docstring de
  `_dias_habiles_chile` («monza_fechas.business_days_remaining … toma "hoy" por su cuenta con
  date.today()» ya es falso: usa `hoy_chile`); `backend/monza_fechas.py` cabecera «COPIA
  deliberada de routers/compras.py:69-141» cita números de línea que este cambio corre;
  `routers/auth.py::_entrada_portal` («El import es local para que el login del personal no
  dependa del módulo portal»: literalmente cierto pero inútil — «los imports son locales por
  costumbre; solo corre para cuentas portal y, si el portal falla, ese login falla»);
  `backend/portal/ordenes.py` cabecera «LA UNIDAD ES LA VENTA»: la duplicación «ocurrió antes
  de que el Cierre de Venta fuera idempotente» → lo exacto es «antes de que el cierre leyera
  la cotización bajo lock».
- **Opcional (C14):** darle a `updateMutation` de `ClientesPage.tsx` la misma duración de 8 s
  (su 409 sí se ve, pero dura lo normal). `portal/tests/test_cuentas.py` §G (g1 comprueba solo
  el status 409) puede sumar el texto del detail; el contrato ya queda fijado en
  `test_clientes_espejo_frontend.py`.
