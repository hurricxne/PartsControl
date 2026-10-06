# Fase 2 del Portal de Clientes — la fecha prometida se congela al Cierre de Venta

**Fecha:** 2026-08-30 · **Marca:** solo Grupo AM (MachParts) · **Plan:** `docs/plan-portal-clientes-2026-08-20.md` §7

Este documento existe para el programador: qué se cambió, por qué, cómo verificarlo y
cómo revertirlo. La Fase 0 tiene su propio documento (`docs/fase-0-portal-cambios-2026-08-20.md`).

---

## 1. El problema que esto arregla

La cotización guarda el **plazo prometido por ítem en días hábiles**
(`items_cotizacion.plazo_entrega_max`), pero ese campo **se pisa después del cierre**:

- `asignar_items` (`routers/compras.py`, su update de `plazo_entrega_map`) lo sobrescribe
  con lo que venga en el body,
- la asignación parcial (`_asignar_parcial_tx`) hace lo mismo con el trozo asignado,
- y el editor de cotizaciones puede tocar una línea `cerrado` sin vínculo.

Resultado: cualquier pantalla que calcule "fecha prometida" **en vivo** a partir del
plazo muestra una fecha que **se mueve sola** después de vendida la venta. El Portal de
Clientes (Fases 3-4) no puede colgarse de un dato así: la fecha comprometida que ve el
cliente tiene que ser la del momento de la venta, inmutable.

Peor: hoy conviven **tres anclas distintas** para "fecha prometida" según la pantalla:

| Pantalla | Ancla de la cuenta |
|---|---|
| Despachos (tarjetas, listado, detalle) | `cotizacion.created_at` + plazo hábiles (`_item_deadline`) |
| Compras / Seguimiento | `fecha_asignacion` a la OC de proveedor + plazo hábiles |
| **Cierre de Venta** (lo que confirma el comercial) | **hoy** + el plazo del ítem que más tarda, contando el plazo por defecto para el ítem sin plazo propio (`plazoDeLaOrden`, `CierreVentaPage.tsx`) |

## 2. Qué se construyó

Una columna congelada + un solo punto de escritura + herencia en el split + un lector.

| # | Archivo | Qué |
|---|---|---|
| 1 | `models/models.py` | `ItemCotizacion.fecha_promesa_cliente` (`Date`, `NULL`) con el comentario-doctrina del congelado |
| 2 | `migrations/item_fecha_promesa_cliente.py` | Migración aditiva idempotente (molde exacto de `cotizacion_pricing_snapshot.py`) |
| 3 | `routers/compras.py` — `crear_oc_cliente` | El congelado: por cada ítem de la cotización, `fecha_promesa_cliente = add_business_days(hoy_chile(), plazo_prometido(plazo_entrega_max, plazo_max_default))` — el plazo del ítem o, si no tiene, el plazo máximo por defecto del cotizador leído al cierre (`plazo_prometido`, la misma regla que imprime la cotización formal) —, con guarda «solo si es NULL» (patrón `pricing_snapshot`), **después** del early-return idempotente y **dentro de la misma transacción** del cierre. La idempotencia se serializa con un lock sobre la cotización (`populate_existing().with_for_update()`): dos cierres simultáneos de la misma venta quedan en fila y el segundo cae en el early-return; depende de que el motor corra en READ COMMITTED (`database.py`) |
| 4 | `routers/compras.py` — `MAX_PLAZO_DIAS_PROMESA = 3650` y `plazo_creible` | Tope de cordura: `add_business_days` avanza día por día y un plazo basura (dedazo `99999999`) **colgaría el request** (el Cierre, el listado de Despachos o el de Compras). `plazo_creible` es el único filtro entre un plazo guardado y `add_business_days`: fuera del tope, cero o negativo → la fecha queda NULL y el respaldo es `oc_cliente.fecha_entrega` en Despachos **y** en el portal, porque también filtra el cálculo en vivo de `_item_deadline` y el de `_item_to_dict` (que deja `dias_restantes` en None en vez de responder 500). Mismo valor y motivo que `MAX_PLAZO_DIAS` del scheduler |
| 5 | `routers/compras.py` — `_clone_item_cotizacion` | El clon del split **hereda** `fecha_promesa_cliente`. Es el ÚNICO dueño de la copia de línea (lo usan los 3 splits: preparar-parcial, cierre de pre-embarque, asignación parcial), así que una línea cubre las tres puertas |
| 6 | `routers/despachos.py` — `_item_deadline` | **Prefiere la congelada** cuando existe; con NULL mantiene el cálculo legado (`created_at` + plazo, si el plazo sirve: `plazo_creible`) y el respaldo `oc.fecha_entrega`. Así la columna tiene un consumidor vivo desde HOY (un dato que ninguna pantalla pinta es un arreglo que no existe) y operador y cliente verán UNA fecha. Los días restantes de Despachos y de los listados de Compras se cuentan desde el día de Chile (`business_days_remaining` usa `hoy_chile`), igual que el «vencida» del portal |
| 7 | `routers/tests/test_fecha_promesa.py` | Suite de la fecha congelada, **verificada por mutación** con el arnés del repo (ver §6) |
| 8 | `frontend-src/src/pages/CierreVentaPage.tsx` — `isHoliday`, `plazoDeLaOrden` | La pantalla del Cierre descuenta la **misma tabla de feriados** que el backend (fechas fijas + Viernes Santo; sin Jueves Santo ni «tercer lunes de junio») y sugiere la fecha de la orden con la **misma regla** con que el backend congela: el ítem sin plazo propio cuenta con el plazo por defecto |
| 9 | `routers/tests/test_cierre_espejo_frontend.py` | Espejo: compila el TypeScript real de la pantalla (esbuild de `frontend-src/node_modules`) y lo compara con el backend — feriados 2026-2035, sumas 2026-2028 × 5 plazos y `plazoDeLaOrden`. Necesita node y `npm install` en `frontend-src` para no saltarse |

## 3. Dos correcciones al plan §7.2, con evidencia

El plan se ejecutó verificando cada supuesto contra el código (regla de la casa: nunca
ejecutar un plan viejo sin verificar). Dos supuestos resultaron falsos:

**(a) «El split hereda la fecha sin código extra» — FALSO.**
`_clone_item_cotizacion` (`routers/compras.py`) enumera los campos **a mano** (21
campos). Sin la línea nueva, todo remanente de split nacería con la fecha en NULL — justo
la línea pendiente que más la necesita. Se agregó la herencia explícita; la sonda d1 la
vigila.

**(b) «Anclar en `cotizacion.created_at`» — REFUTADO.**
El plan proponía `add_business_days(cotizacion.created_at, plazo)`. Pero:
1. La promesa que el comercial **confirma al cerrar** es la de `CierreVentaPage.tsx`:
   `addBusinessDays(new Date(), maxPlazo)` — anclada en **el día del cierre**. La fecha
   por ítem congelada es el refinamiento por línea de esa misma promesa; para el ítem de
   plazo máximo coincide con la fecha **sugerida** porque la pantalla usa la misma tabla
   de feriados que el backend (las compara `test_cierre_espejo_frontend.py`); si el
   comercial cambia la fecha a mano, la del ítem no la sigue.
2. Con el ancla `created_at`, una cotización que durmió semanas antes de cerrarse
   pariría fechas **ya vencidas el día uno** — promesas rotas de nacimiento, que además
   envenenarían la condición de continuidad del portal (>25% atrasado ⇒ se apaga).

El ancla definitiva es **el día del cierre en hora de Chile** (`hoy_chile()`, el mismo
helper de la fecha tributaria del DTE). La sonda a1 retro-data la cotización 30 días
antes de cerrar: si alguien vuelve al ancla `created_at`, la suite se pone roja.

## 4. Qué NO cambia (freeze-forward)

- **Ventas cerradas antes de esta fase**: columna NULL → `_item_deadline` sigue con el
  cálculo de siempre y el respaldo `oc.fecha_entrega`. Ni un día de diferencia (sonda e2).
- **Sin backfill**: mismo criterio que el dueño aprobó para `pricing_snapshot` / TC congelado.
  Las ventas cerradas antes con ítems sin plazo propio quedan en NULL. Desde ahora el ítem
  sin plazo propio se congela con el plazo por defecto del cotizador (`plazo_prometido`) y
  deja de seguir a `oc_cliente.fecha_entrega`; el caso «todos los ítems por defecto»
  tampoco sigue ya la fecha de cabecera.
- **Los días restantes** (semáforo de Despachos y de Compras) se cuentan desde el día de
  Chile (`business_days_remaining` usa `hoy_chile`), igual que el «vencida» del portal.
- **Nada re-sella la fecha**: ni `asignar_items`, ni la asignación parcial, ni el editor
  (`update_item` hace `setattr` solo de los campos del schema `ItemUpdate`, que **no**
  incluye la columna — no agregarla ahí jamás), ni `restaurar_version` (restaura una
  lista fija de campos que tampoco la incluye, y bloquea con 409 si hubo splits).
- **Compras/Seguimiento** sigue mostrando su fecha anclada en `fecha_asignacion`: esa es
  la vista del **proveedor** (cuándo nos entrega él a nosotros), una pregunta distinta de
  la promesa al cliente. Se deja a propósito.

## 5. Deploy

```bash
# ANTES de reiniciar el backend (create_all NO agrega columnas a tablas existentes):
cd backend && python -m migrations.item_fecha_promesa_cliente
```

Sin la migración, el primer `SELECT` que toque `items_cotizacion` con el modelo nuevo
(cualquier pantalla de Ventas/Compras/Despachos) revienta con `Unknown column
'fecha_promesa_cliente'`. La línea está agregada en `docs/CHECKLIST-DEPLOY-2026-07-20.md`.

**Revertir** (si hiciera falta): `git revert` del commit de la Fase 2. La columna puede
quedarse en la base — nada del código viejo la toca, y `create_all` no la elimina.

## 6. Pruebas

`routers/tests/test_fecha_promesa.py` — corre sola y dentro del gate (`pytest` pelado
desde `backend/`):

| Sección | Qué protege |
|---|---|
| §A | El cierre congela hoy-Chile + plazo hábiles (cotización **retro-datada 30 días**: discrimina el ancla); sin plazo propio → se congela con el plazo por defecto del cotizador (a2; con solo plazo mínimo, también: a2b); plazo absurdo → NULL **y el request responde** (NULL solo para un plazo que no sirve) |
| §C | `asignar_items` pisa el plazo (se verifica que el pisado OCURRIÓ) y la fecha congelada queda intacta |
| §D | El clon del split hereda la fecha (no nace NULL) |
| §E | `_item_deadline` prefiere la congelada; con NULL, cálculo legado y respaldo idénticos a hoy |
| §B | El reintento idempotente del cierre NO re-sella (centinela plantado y sobrevive) |
| §F | Dos cierres simultáneos de la misma venta dejan UNA OC (lock sobre la cotización; carrera determinista con un punto de encuentro en `hoy_chile`) |
| §G | Los días restantes se cuentan desde el día de Chile (`business_days_remaining`) |
| §H | `_item_to_dict` no calcula con un plazo que no sirve (`dias_restantes` None, no 500) |
| §I | Tablas de verdad de `plazo_creible` y `plazo_prometido`; paridad del respaldo 45 con `_config_to_dict` |

**Verificación por mutación** — la corre el arnés del repo sobre una copia de `backend/`
(`python -m portal.tests.mutaciones fecha_promesa`; lista en
`portal/tests/mutaciones/cierre.py`), que exige cada mutación en rojo:

| Mutación | Sonda que la caza |
|---|---|
| Matar el bloque de congelado | a1, c2, d1, e1 |
| Quitar la herencia del clon | d1 |
| `_item_deadline` ignora la congelada | e1 |
| Quitar el tope de cordura (hoy, las cotas de `plazo_creible`) | a3, e4, e5, h1 y la tabla §I (con plazo 3651, no 99999999: si el tope se cae, la suite se pone roja en segundos en vez de colgar el gate) |
| Congelar solo con el plazo propio / usar el mínimo como respaldo | a2, a2b |
| Leer la cotización sin lock en el cierre | f1 |
| `business_days_remaining` vuelve a `date.today()` | §G |
| Congelar también en el early-return (re-sellado) | b1 |

## 7. Deuda declarada (no incluida a propósito)

| Qué | Por qué queda fuera |
|---|---|
| Espejo Monza | El portal es de Grupo AM (clientes mineros). La deuda exacta, por si Monza tiene portal algún día: (a) su cierre de venta (`es_cierre_nuevo`, `monza_router_cotizaciones.py`) no congela ninguna fecha por ítem; (b) falta la materia prima — `MonzaCotizacionItem.plazo_entrega` es texto libre `String(100)` sin parsear (la receta ya está anotada en `monza_router_despachos.py`, donde el espejo del worst-case quedó pendiente); (c) la promesa Monza vive a nivel cotización (`fecha_entrega_est`) y ES re-escribible después del cierre por el `setattr` del PATCH — un portal Monza NO puede leer `fecha_entrega_est` tal cual: mostraría una promesa móvil, justo lo que esta fase elimina en GA |
| `oc_cliente.fecha_entrega` editable sin guarda (H11 del plan) | Riesgo menor aceptado en el plan; mitigación opcional (bitácora de cambios) es de otra fase |
| Exponer la columna en los serializadores del portal | Es la Fase 3 (API del portal). Los consumidores internos ya la leen vía `_item_deadline` |
| Pintar la fecha congelada en Compras/Seguimiento | Seguimiento responde otra pregunta (proveedor→nosotros); unificarlo es decisión de pantalla aparte, no de esta fase |
| Cota del plazo donde ENTRA el dato | `plazo_creible` filtra al calcular, no al guardar. Falta `Field(ge=0, le=3650)` en `ItemUpdate` (`routers/cotizador.py`), `ManualItemRequest` (`routers/cotizaciones.py`), `ItemPlazo` y `AsignacionParcialItem` (`routers/compras.py`), el recorte de `_pmax` en `upload_venta_clp` (`routers/venta_clp.py`) y una cota de `plazo_max_default` en `ConfigUpdate`; en el navegador, el mismo tope en los `addBusinessDays` de `CierreVentaPage.tsx` y `PreEmbarquesPage.tsx` (recorren día por día). Son contratos de la API y archivos del programador |
| Mapeo de ítems de `CierreVentaPage.tsx` | La fila usa `plazo_entrega_min` y el texto `plazo` como respaldo del máximo; el PDF, el editor, la cabecera sugerida y el congelado usan el plazo máximo guardado o el por defecto. Un ítem con solo mínimo (o plazo en texto) muestra ese número en la fila y se sella con el por defecto. Arreglo: `plazo_entrega_max: it.plazo_entrega_max ?? undefined` en ese mapeo y borrar `parsePlazoStr` |
