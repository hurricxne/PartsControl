# ENTREGA MonzaParts — 2026-08-27

**Rama:** `feature/adelantos-clientes` · **Gate:** 355 pytest verdes · `tsc --noEmit` limpio · `npm run build` limpio · árbol sin cambios sueltos.

Documento de traspaso. Si vas a revisar esto y solo puedes leer una cosa, lee las secciones
2 (qué correr al instalar) y 5 (lo que quedó pendiente).

---

## 1. Qué se hizo, y por qué

Partió de dos pedidos del dueño —los 8 arreglos que pidió el equipo comercial y la queja
«en leads solo se ven los primeros, no puedo buscar los pasados»— y terminó incluyendo las
9 deudas técnicas registradas en actas anteriores. Sobre eso corrieron **siete rondas de
auditoría en enjambre** (~180 agentes), cada hallazgo grave filtrado por un revisor
independiente cuyo trabajo era refutarlo.

**Los dos hallazgos críticos**, ambos reproducidos y cerrados:

1. **La factura salía al RUT equivocado.** El dedupe de clientes comparaba el teléfono
   antes que el RUT: dos empresas distintas que comparten un número —la recepción de un
   taller, o un `-` de relleno tecleado en las dos fichas— terminaban en la MISMA ficha, y
   el RUT recién digitado se descartaba en silencio. De ahí en adelante la cotización, el
   cierre y el DTE 33 colgaban de la ficha ajena.
2. **Escape de carpeta + XSS almacenado en los adjuntos.** El nombre del archivo se armaba
   con un campo del formulario, así que una `entidad` con `../..` escribía FUERA de
   `static/docs`. Apuntada a `uploads/bodega` —que `main.py` publica como StaticFiles—
   dejaba un `.html` con `<script>` servido por el propio dominio: robo de sesión de los
   operadores de las DOS marcas.

**La queja de los leads tenía cuatro causas, no una:** el backend siempre paginó pero la
pantalla nunca mandaba `page` (solo 20 leads alcanzables); el buscador prometía VIN, N° de
parte y N° de cotización y no buscaba ninguno; el filtro de estado no tenía «Cerrado»,
que es donde terminan los leads ganados; y «Míos» comparaba ids de **tablas distintas**.
Se agregó, además, que un solo lead con `fecha_creacion` NULL tumbaba la lista entera con
un 500 — y como el orden va del más nuevo al más viejo, se caía justo al llegar a los
antiguos.

**Las dos deudas que resultaron ser de plata:** el LTV del cliente casi nunca se sumaba
(los tres escritores colgaban de `lead_id`, un dato borrable, y el flujo normal —cerrar el
último despacho— no llamaba a ninguno), y la mercadería salía con el pago pendiente (el
cortafuego estaba en *crear* el despacho, pero *cerrarlo* es cuando la mercadería sale).

---

## 2. Qué correr al instalar

> ⚠️ Esta lista es la de MonzaParts. **El paquete completo (las dos marcas) va en
> `docs/ENTREGA-2026-08-27.md` §0, y trae pasos que ACÁ NO ESTÁN** — entre ellos
> `pip install -r requirements.txt` y las dos migraciones de MachParts. El checklist
> `docs/CHECKLIST-DEPLOY-2026-07-20.md` es la fuente de verdad.

**ANTES de reiniciar** (esquema — sin esto hay 1054):

```bash
cd backend
python -m migrations.monza_lead_item_flete       # 🔴 CRÍTICA. moneda_tarifa + tarifa_aerea.
                                                 # Los modelos YA declaran las columnas: sin
                                                 # ella, Leads y Cotizaciones de Monza caen
                                                 # con 1054 INCLUSO CON EL GATE APAGADO.
python -m migrations.monza_unique_correlativos   # fail-closed si hay duplicados.
                                                 # ⚠️ Verificar que la salida diga
                                                 # «uq_monza_ocp_numero creado» (o «ya existe»)
                                                 # y NO «la tabla no existe todavía»: ese
                                                 # camino sale con éxito SIN crear el índice.
cd ../frontend-src && npm install && npm run build
cp -r dist/assets/* ../assets/ && cp dist/index.html ../index.html
```

**DESPUÉS de reiniciar**, con el dueño mirando (migración de DATOS, mueve cifras en
pantalla — el checklist §4.a-ter manda que sea post-reinicio):

```bash
cd backend
python -m migrations.monza_backfill_ltv_despachado --dry-run   # revisar los números
python -m migrations.monza_backfill_ltv_despachado             # solo tras aprobar el dry-run
```

**ANTES de reiniciar:** censar `users.empresa`. Seis routers de Monza estrenan candado de
empresa, así que un operador de Monza mal marcado como `mineria` va a recibir 403 en
pantallas que antes usaba.

**Tres números cambian el primer día, y no son errores:**
- Los **LTV de las fichas suben** — es plata que un bug nunca contó, no ventas nuevas.
- Los **KPIs del mes** cambian de frontera (ahora cortan con la hora de Chile).
- La tarjeta **«Vendidos»** muestra otro monto: antes sumaba una cohorte distinta a la que
  contaba (número por `fecha_venta`, monto por `fecha_creacion`).

El detalle largo de cada paso está en `CHECKLIST-DEPLOY-2026-07-20.md`.

---

## 3. Los commits

Los de la jornada de MonzaParts, del más nuevo al más viejo (el paquete
completo son **39 commits**: 20 de MonzaParts y 19 de MachParts — ver
`docs/ENTREGA-2026-08-27.md`):

| Commit | Qué |
|---|---|
| `ea9058d` | docs(monza): documento de ENTREGA + el aislamiento entre marcas en sus DOS sentidos |
| `b5dc635` | fix(monza): la sexta puerta del mismo contador, y cierre de la jornada |
| `8d07c90` | docs(monza): las 7 deudas que quedan, con su análisis y el motivo de cada decisión |
| `5a3455e` | fix(monza): la quinta puerta — el lock iba donde se DECIDE, no donde se escribe |
| `becf228` | fix(monza): el candado que corta la clase entera — reintentar después de guardar duplica |
| `0381d93` | fix(monza): el buscador deja de mentir y la pantalla no se cuelga en silencio |
| `4e14582` | fix(monza): el monto de «Vendidos» y el contador ante un re-cierre |
| `62a031a` | fix(monza): ronda 4 — la puerta que dejé sin reintento y el lock que faltaba en la plata |
| `55fdca3` | fix(monza): ronda 3 — la regresión del correlativo, los adjuntos rotos y 12 hallazgos más |
| `533270a` | docs(monza): acta de la revisión en enjambre — 2 críticos, 17 altos y las lecciones de método |
| `2f98584` | fix(monza): hallazgos del comité — la pantalla deja de mentir y los comentarios también |
| `6befbf4` | fix(monza): 2 críticos de seguridad e identidad + 6 candados + robustez de Leads |
| `3bc0106` | fix(monza): 4 efectos secundarios del testing — contador de ventas, «hoy» de Chile y avisos de dedupe |
| `5a51bd9` | fix(monza): 11 hallazgos del equipo de testing sobre leads+deudas (0 críticos, 11 altos) |

> ⚠️ En el mismo rango hay **10 commits de otra sesión** que trabajó en paralelo sobre
> Grupo AM (`routers/despachos.py`, `routers/contabilidad.py`, `FacturasPage.tsx`,
> `wasabil_dte/`). No son parte de esta entrega y no se tocaron.

---

## 4. Módulos nuevos y suites

**Helpers nuevos** (`backend/`): `monza_rut.py` (identidad y búsqueda del RUT — separa a
propósito «buscar», que es laxo, de «identificar», que es estricto), `monza_telefono.py`
(lo mismo para el abonado), `monza_correlativos.py` (numeración con reintento) y la sección
«Hora de Chile» de `monza_fechas.py`.

**Suites nuevas** (`backend/monza_tests/`): `test_monza_fechas_rut`,
`test_leads_paginacion_busqueda`, `test_ltv_flip_despacho`, `test_clientes_dedupe`,
`test_cortafuego_salida_adelanto`, `test_documentos_seguridad`, `test_leads_robustez`,
`test_leads_diseno`, `test_ltv_lock_concurrencia`, `test_correlativos`,
`test_dedupe_telefono`.

**Método de las pruebas:** cada arreglo se verificó por **mutación** — se quita el arreglo y
se comprueba que la prueba se pone roja. Dos veces eso reveló sondas que pasaban con y sin
el arreglo (una chocaba contra una defensa anterior; la otra no lograba abrir la ventana de
carrera) y hubo que reescribirlas. Si tocas algo de acá, usa el mismo criterio: una prueba
que no se cae al quitar el código que protege no está probando nada.

**El gate es `pytest` PELADO desde `backend/`.** Correr la lista de carpetas a mano da
menos y parece completo.

---

## 5. Lo que quedó pendiente

Todo en **`deudas-monza-2026-08-27.md`**: ocho puntos, cada uno con qué pasa, por qué no se
arregló, qué haría falta y cuánto duele. Ninguno pierde plata ni emite documentos
incorrectos.

Los tres que necesitan decisión del dueño:

- **Aislamiento entre marcas (deuda 3)** — cuatro módulos abiertos en los DOS sentidos. El
  más expuesto NO es el que se sospechaba: la cartera de clientes de Grupo AM (RUT, nombre,
  contacto) es visible para cualquier cuenta de Monza que escriba la dirección. **El dueño
  lo revisó el 2026-08-27 y decidió dejarlo pendiente.** No tocar sin su visto bueno.
- **`auth.py` devuelve 500 en vez de 401** ante un token malformado. Es código compartido
  por las dos marcas.
- **La vía de compra más usada no reintenta el correlativo**: el índice único nuevo
  convirtió una corrupción silenciosa en un error visible. Cambio de fallo deliberado.

---

## 6. Dos cosas que conviene saber antes de tocar este código

**El patrón que dominó la jornada.** Seis veces seguidas apareció el mismo error: *arreglar
una puerta de un flujo y olvidar la gemela*. El correlativo, el dedupe de fichas, la subida
de archivos, el log post-commit, los buscadores de clientes y el lock del contador. Cinco de
esas seis las introdujo el propio arreglo del hallazgo anterior. Antes de tocar un flujo,
enumera TODOS sus puntos de entrada con `grep` — y sospecha siempre de la puerta automática
(webhook, job, bridge), que es la que nadie mira.

**Lo que finalmente cortó la clase entera** no fue otro parche sino un cambio estructural:
`reintentar_carrera` escucha los commits de la sesión y se niega a reintentar algo que ya se
guardó. Protege las tres puertas de hoy y la que se agregue mañana sin leer nada. Cuando un
mismo bug aparece por tercera vez, el arreglo correcto ya no es el caso — es la clase.

---

## 7. El árbol vivo

El código real está en **`Parts control actual/PartsControl-main/`**. La raíz del proyecto
(`PartsControl/backend/`, `PartsControl/frontend-src/`) es una foto de julio y produce
hallazgos falsos: no la uses como referencia.
