# Plan v3 — Leads pasados + TODAS las deudas registradas (MonzaParts)

**Fecha:** 2026-08-22 · **Árbol vivo:** `Parts control actual/PartsControl-main/` (HEAD `49f7891`)
**Estado:** ESPERANDO CONFIRMACIÓN DEL DUEÑO — no se escribe código hasta el visto bueno.
**Método:** plan de leads revisado por multienjambre (8 agentes, 4/4 graves REALES) + enjambre PLANIFICADOR de deudas (8 agentes: 24 diseños con contrato exacto, 32 hallazgos, 4/4 graves verificados REALES).

---

## 0. Qué pidió el dueño

1. Los vendedores no pueden ver ni buscar **leads pasados** (solo se ven los primeros).
2. Incluir el arreglo de **TODAS las deudas registradas** en las actas anteriores.

## 1. Resumen del diagnóstico (todo verificado con `archivo:línea`)

**Leads (la queja directa):** el backend pagina bien pero la pantalla nunca manda `page` (solo 20 leads alcanzables, pie sin botones); el buscador promete VIN/N° parte/COT y no los busca; el filtro de estado no tiene "Cerrado" (donde terminan los leads ganados); "Míos" compara ids de **tablas distintas** (bug vivo que esconde leads propios); el `hasta` de fechas **excluye su propio día**; el orden sin desempate puede **saltarse leads entre páginas**.

**Deudas — lo que el enjambre planificador midió de verdad:**
- El bug del `hasta` no vive solo en Ventas: hay **2 gemelos no registrados** (lista de Cotizaciones y visor de Logs — Logs además **falla abierto**: fecha inválida = filtro ignorado en silencio, en la pantalla de auditoría).
- **D4 es la deuda de PLATA**: el cierre físico del despacho (el flujo NORMAL) flipea la venta a `despachado` **sin sumar el LTV ni cerrar el lead**, y la idempotencia bloquea el re-PATCH — el LTV real **casi nunca se suma**. Confirmado: solo existen 2 escritores de LTV en todo el backend.
- `fecha_despacho` se estampa con el **día UTC** (un cierre a las 21:30 de Chile queda fechado mañana).
- `create_cliente` **renombra la ficha compartida** al dedupear — y el dedupe compara RUT **crudo**, así que "76.000.000-0" y "76000000-0" crean dos fichas.
- El candado de empresa falta en 4 routers del CRM (leads, cotizador, ventas **y clientes** — este último no estaba registrado y es la puerta del costado al mismo dato).

---

## 2. FASE 1 — Ejecutable con tu "sí" (sin decisiones pendientes)

Commits chicos y auto-contenidos, **gate en verde tras CADA commit** (pytest pelado + tsc/build):

### C1 · Los helpers (la base de todo lo demás)
- **`monza_fechas.py` gana la sección «Hora de Chile»** (aditivo puro, cero cambios a días hábiles): `TZ_CHILE` (zoneinfo America/Santiago, DST correcto), `ahora_chile()/hoy_chile()` (seam de test), `parse_fecha_422` (solo `YYYY-MM-DD`; basura/ISO-con-hora → **422 fail-closed**, jamás ignorar un filtro), `rango_utc(desde, hasta)` → par **UTC-naive semiabierto** `[00:00 Chile, 00:00 Chile del día siguiente)` comparable con las columnas `utcnow`, `rango_dias_422` para columnas **Date civiles** (despachos — el anti-arreglo F3: ahí el `<=` inclusivo es CORRECTO y no se toca), `inicio_mes_utc()`.
- **`monza_rut.py` nuevo** (~25 líneas, espejo del precedente `monza_wasabil_compras/rut.py`): `rut_norm_py` + `rut_norm_sql` (normaliza la COLUMNA con `func.replace` — bilateral, sin migración, portable MySQL/SQLite).
- `tzdata` explícito en `requirements.txt` (hoy solo llega de arrastre — un rebuild del venv en Hostinger podría dejarlo fuera).
- Suite `test_monza_rango_fechas.py`: pares invierno/verano con **offsets distintos** (mata el −3 hardcodeado por mutación), día del salto DST de septiembre (no-excepción documentada), 422s, rango vacío honesto.

### C2 · Leads v2 completo (P1–P6, sobre los helpers)
- **P1** paginador (PAGE_SIZE 30, ← Anterior/Siguiente →, "Mostrando X–Y de N"); reset de `page` + colapso del expandido **en el mismo batch** del cambio de filtro; **guardia de secuencia** `seqRef`; KPIs en fetch aparte (no se recalculan por cada clic).
- **P2** búsqueda honesta: `q` + `vin` + RUT (vía `monza_rut`, rama activa solo si el término canónico tiene ≥7 caracteres `[0-9K]`) + `EXISTS` por N° de parte y N° de cotización (`.any()`, SQL compilado y verificado: cero duplicados, `count()` intacto).
- **P3** "Cerrado / Ganado" = `in_("vendido","cerrado")` (+ comentario del modelo actualizado: el vocabulario no incluía 'cerrado' que el código escribe hace meses).
- **P4** fechas con `rango_utc` (el día `hasta` ENTRA, interpretado como día de Chile) + 422.
- **P5** debounce 350 ms + Enter saltea la espera. **P6** "Míos" arreglado (`_get_asesor_id(..., con_fallback=False)`; sin asesor → lista vacía, jamás leads ajenos).
- Título "Leads" a secas, `Fragment key`, placeholder + "RUT".
- Suite `test_leads_paginacion_busqueda.py` **forward-compatible con la fase 2** (fake user con `empresa='automotriz'` y rol; fechas de siembra en UTC explícito comentando su equivalente Chile): 12 leads escalonados + `page_size=5`, **empates deliberados** → unión de páginas completa y disjunta (cae sin el desempate `id.desc()`), tokens únicos por vía + negativa de control, `page=99` → vacío con total intacto, `page_size=500` → 422, lead de las 15:00 del día `hasta` APARECE, "Míos" con asesor de id ≠ user.id.

### C3 · La familia `hasta`-excluyente COMPLETA + 422 en los 5
- `monza_router_ventas.py` (D1), `monza_router_cotizaciones.py` (gemelo no registrado) y `monza_router_logs.py` (gemelo no registrado, **eliminando el `try/except pass`** que fallaba abierto) adoptan `rango_utc` + 422.
- `monza_router_despachos.py`: solo `rango_dias_422` (el filtro era correcto; gana el 422 en vez del fail-open).
- **F2**: `fecha_despacho` se estampa con `hoy_chile()` en las 2 escrituras (despachos y cotizaciones) — freeze-forward, las filas históricas corridas no tienen reparación posible (un Date no conserva la hora).
- Suite `test_filtros_fecha_semiabierto.py`: venta de las 14:00 UTC del día X aparece con `hasta=X` (RED hoy) en los 3 endpoints; despachos conserva su inclusividad (sonda de no-regresión); 422 ante basura en los 5.

### C4 · D4 — el LTV del flujo físico (la deuda de plata)
- Helper `_aplicar_efectos_venta_despachada(db, cot)`: espejo **LITERAL** del bloque del PATCH (lead → 'cerrado' + LTV al cliente FACTURADO `cot.cliente_id`; gate `cot.lead_id` intacto; **ni vendidos_total ni notificaciones** — eso colaría la decisión D9 por la puerta de atrás), llamado desde el PATCH y desde `_cerrar_despacho_tx`, con la **transición de estado como guard de idempotencia**.
- **D4-B prerequisito**: `_cerrar_despacho_tx` toma la cotización con `populate_existing().with_for_update()` **sin joinedload** (hoy la lee sin lock: carrera real de doble suma con el PATCH concurrente).
- Reversa: resta SOLO en la transición efectiva `despachado→vendida` (jamás en el modo reparación de datos legados — restaría plata que nunca entró).
- Suite `test_ltv_flip_despacho.py`: flujo físico completo → `ltv == total` (**HOY: ltv == 0** — sonda de máximo poder discriminante); re-PATCH sin doble suma; ambos órdenes de los dos caminos; ciclo con reversa simétrica.

### C5 · D6 + la costura del dedupe — `create_cliente` deja de renombrar
- Dedup-hit = **fill-if-empty, jamás overwrite**: `nombre` NUNCA se toca, `vehiculos` deja de pisarse, rut/teléfono/email solo se COMPLETAN si estaban vacíos; respuesta gana `"reutilizado": true|false`.
- **El dedupe compara RUT canónico** (`monza_rut`) — sin esto, D3 arreglaba el buscador y el POST seguía duplicando fichas (la lección de las costuras).
- Frontend: los 4 toasts anti-renombre pasan al flag `reutilizado` (el de ClienteLeadModal hoy está MUERTO justamente por el renombre — revive solo, no tocarlo dos veces).
- `search_clientes` y `list_clientes` ganan la rama RUT canónica (D3 completo).
- Suite `test_clientes_dedupe.py` (hoy CERO cobertura del POST): renombre bloqueado (RED hoy), dedupe con RUT en otro formato encuentra la MISMA ficha (RED hoy), fill-if-empty, `reutilizado` correcto.

### C6 · D7 — el parpadeo del buscador (3 copias del flag, no hook)
- `busquedaResuelta` en los 3 buscadores de cliente de `MonzaLeadsPage` (~5 líneas c/u): el empty-state solo aparece con búsqueda RESUELTA. Se eligen 3 copias sobre un hook porque los efectos tienen gates distintos y reescribirlos es el refactor que la casa veta; regla escrita: a la 4ª copia se extrae el hook. **QA manual documentada en el acta** (no hay runner de UI; una sonda-grep decorativa no prueba nada — honestidad primero).

**Tamaño fase 1:** ~10 archivos backend + 1 frontend, 5 suites nuevas, **cero migraciones de esquema**.

---

## 3. FASE 2 — Las 5 DECISIONES que son tuyas (cada una excluible, commits aislados)

| # | Decisión | Recomendación del enjambre | Qué implica |
|---|---|---|---|
| **DEC-1** | **Candado de empresa** en los 4 routers del CRM (leads, cotizador, ventas + **clientes**) — **REVIERTE tu aplazamiento anterior**, commit titulado así para excluirlo limpio | **SÍ** — despachos/bodega/cotizaciones ya lo llevan en producción sin un solo bloqueo: el miedo original tiene contraevidencia | 4 líneas (patrón router-level de bodega). **Paso 0 obligatorio pre-deploy**: censo `users.empresa` con JOIN a asesores activos — si algún operador Monza real figura minería, se corrige SU fila antes de reiniciar, no se relaja el guard. Sin `require_rol` (sin `User.rol` sería un control que no controla). Deuda nueva anotada: quedan 7 routers Monza sin candado (config y logs primero) |
| **DEC-2** | **Backfill del LTV** nunca sumado (D4-C): recompute canónico idempotente por cliente facturado, con `--dry-run` que imprime el delta ANTES/DESPUÉS por cliente | **SÍ** — no es freeze-forward de parámetros: es plata VISIBLE en la ficha (y en el futuro Portal) que un bug dejó en cero; el recompute además borra los dobles conteos históricos | Script one-shot `migrations/monza_backfill_ltv_despachado.py`; se ejecuta SOLO tras ver el dry-run; re-verificar el grep de escritores el día del deploy |
| **DEC-3** | **KPIs de mes con frontera de Chile** (`inicio_mes_utc` en leads/ventas KPIs y Dashboard) | SÍ, en su propio commit | Los números del Dashboard saltan el día del deploy (ventas de las 21:00-24:00 del último día cambian de mes) → aviso en el checklist, precedente §4.a-bis |
| **DEC-4** | **Cortafuego de pago-sin-verificar extendido a las 3 puertas de salida** (crear/cerrar despacho + guía 52 + factura 33) — **paquete completo o nada**: aprobarlo solo-guía deja la puerta de servicio del canal `sin_guia` | SÍ como paquete — hoy lo cubre de rebote el camino físico, pero mercadería en bodega por otra vía sale despachada y facturada con el pago pendiente | 3 guards 409 con el texto espejo del de Abastecimiento + puerta de emergencia; avisar al equipo de bodega |
| **DEC-5** | **¿El cierre de venta marca el lead 'vendido' automáticamente?** Hoy solo el clic del asesor lo hace; si lo olvida, el lead queda 'en_proceso' para siempre e infla "sin contactar" | **Opción B** (auto-marca idempotente al cierre, freeze-forward sin backfill) — coherente con el embudo; la alternativa A es solo un badge visual sin mutar | ~10 líneas en el cierre + sondas de idempotencia (sin doble `vendidos_total`); KPIs de leads suben el día del deploy → aviso |

## 4. Deudas NUEVAS registradas (quedan anotadas, NO en esta entrega)
- Teléfono sin normalizar (misma enfermedad del RUT en el otro campo del dedupe) — necesita tu decisión de formato canónico antes de diseñar.
- Los 7 routers Monza restantes sin candado (si DEC-1 = sí).
- LTV: suma con total vivo vs resta con total de la versión (residuo si el total se editó entre cierre y despacho — asimetría preexistente del PATCH).
- Correlativos anuales UTC (cosmético) y presets de compras GA (exige el gemelo `ga_fechas`).
- GA/MachParts: el auto-flip gemelo existe pero el hueco D4 **no aplica** (GA no tiene CRM/LTV — verificado, cero código allá).
- `AgregarItemModal` (catálogo) tiene el mismo flicker — 5 minutos futuros.

## 5. Validación y deploy
```bash
cd "Parts control actual/PartsControl-main/backend"
python -m pytest -q          # tras CADA commit, no al final (hoy 277 verdes)
cd ../frontend-src && npm run build
python ../deploy/audit_schema.py --pasos   # debe seguir verde (sin migraciones de esquema en fase 1)
```
Checklist de deploy: sin migraciones en fase 1; DEC-2 agrega el backfill (dry-run primero); DEC-3/DEC-5 agregan el aviso de salto de KPIs; DEC-1 agrega el censo de `users.empresa` como paso 0.

**Complejidad:** Fase 1 = MEDIA-ALTA · Fase 2 = S/M por decisión · **Riesgo mayor:** el cambio de borde de fechas mueve qué filas caen en cada filtro (es la corrección deseada — el operador verá aparecer los registros de la tarde-noche de Chile que antes se caían).

---

## 6. ACTA DE LA ENTREGA (2026-08-27) — fases 1 y 2 COMPLETAS

**Commits:** `6eaedbc` (leads + las 9 deudas) → `5a51bd9` (11 hallazgos altos del testing) → `3bc0106` (4 efectos secundarios).
**Estado:** implementado, probado e iterado hasta cero errores conocidos. **Gate: 297 pytest verdes · `tsc`/build limpios · árbol limpio.**

### 6.1 Qué se construyó

**El pedido (leads pasados):** paginación real de 30 con ← Anterior / Siguiente → (el backend siempre paginó; la pantalla nunca mandaba `page`); búsqueda honesta que cumple su placeholder — N° de lead, cliente, teléfono, vehículo, VIN, **RUT en cualquier formato**, **N° de parte Y descripción del repuesto**, **N° de cotización** y la **patente**; filtro «Cerrado / Ganado» que alcanza a los leads ganados; **«Míos» arreglado** (comparaba ids de tablas distintas: escondía los leads propios); orden con desempate (sin él la paginación podía repetir o saltarse leads); rango de fechas; debounce 350 ms + Enter; KPIs con refresco propio.

**Las 9 deudas:** hora de Chile en los filtros de los 5 endpoints (+ 2 gemelos que nadie había registrado, y el visor de Logs que **ignoraba en silencio** una fecha inválida); RUT normalizado bilateral; **el LTV del flujo físico** (el cierre del último despacho —el camino real— nunca sumaba, y la idempotencia bloqueaba al PATCH: el LTV casi nunca se sumaba); candado de empresa en los **5** routers del CRM; `create_cliente` que ya no renombra la ficha compartida ni duplica por formato de RUT; fin del parpadeo del buscador; cortafuego de pago-sin-verificar en las puertas de **salida**; y el cierre de venta que marca el lead vendido.

### 6.2 Lo que el equipo de testing encontró (y se corrigió)

6 testers, **88 pruebas de flujos reales**, 42 hallazgos (**0 críticos**, 11 altos). Los 8 graves verificados adversarialmente resultaron **todos REALES**:

| Hallazgo | Corrección |
|---|---|
| **La mercadería salía con el pago pendiente**: el guard estaba en *crear* el despacho, pero *cerrarlo* es cuando sale | Guard en `_cerrar_despacho_tx` bajo FOR UPDATE |
| **El LTV se apagaba al borrar un lead**: los 3 escritores dependían de `lead_id`, un dato borrable — la venta quedaba sumada y nadie podía devolverla | La plata se gatea por el CLIENTE de la venta; el backfill cubre el mismo conjunto |
| `hasta=9999-12-31` → **500** en 4 endpoints (regresión) | 422 fail-closed |
| El normalizador de **buscar** RUT se usó para decidir **identidad**: un `-` enganchaba con la ficha de un tercero (regresión) | Nace `rut_identidad` (estricta) + `buscar_ficha_por_rut` como única puerta |
| **KPIs congelados** hasta recargar la página (regresión) | Refresco en las acciones que los mueven |
| La búsqueda por repuesto estaba **muerta para los leads automáticos** (el bridge llena `descripcion`, no `numero_parte`) | El EXISTS mira ambas columnas + la patente del comentario |
| Comodines de LIKE sin escapar: `q='%'` devolvía el universo | `_escapar_like` + `escape` en cada cláusula |
| `page` sin clamp encerraba al operador en una página vacía | Clamp a la última página con contenido |
| El 5º router del CRM (cotizaciones) exponía nombre/RUT/teléfono a minería | Candado a nivel de router |
| `create_lead` seguía dedupeando por RUT literal | Misma puerta que `create_cliente` |
| `vendidos_total` quedó **muerto** con el auto-marcado del lead | Se suma en la misma transición |

**Decisión de diseño tomada durante la implementación:** el cortafuego **no bloquea la factura con guía** — facturar es cómo se le cobra al cliente (el adelanto se aplica retroactivamente), y bloquearla sería circular. Queda acotado a las puertas por donde la mercadería **sale**: crear/cerrar despacho, guía 52 y factura de **retiro en oficina** (con puerta de emergencia que deja rastro en el documento).

### 6.3 Verificación final (reproducida a mano, 2026-08-27)

Los dos escapes de plata de la ronda 1 se reprodujeron y **ambos rebotan ahora**; el LTV es simétrico con y sin lead, y sobrevive al borrado del lead; los 5 endpoints responden 422 a fechas imposibles (`/despachos` acepta la fecha extrema legítimamente: usa columna civil y no suma el día); el RUT basura no identifica a nadie y el mismo RUT en dos formatos sí; `q='%'` ya no devuelve el universo; la búsqueda encuentra por descripción y patente; los 3 routers rebotan a minería con 403.

### 6.4 Deploy

```bash
cd backend
python -m migrations.monza_backfill_ltv_despachado --dry-run   # revisar los números
python -m migrations.monza_backfill_ltv_despachado             # solo tras aprobar el dry-run
cd ../frontend-src && npm install && npm run build
cp -r dist/assets/* ../assets/ && cp dist/index.html ../index.html
```
**Sin migraciones de esquema.** Avisos al dueño: (a) los **LTV de las fichas suben** el día del backfill —es plata que un bug nunca contó, no ventas nuevas—; (b) los **KPIs del mes** cambian de frontera (ahora la de Chile); (c) el equipo verá **403** si algún operador Monza figura en minería → censar `users.empresa` antes de reiniciar; (d) **Contado y los adelantos frenan la salida de mercadería** hasta que Tesorería verifique.

### 6.5 Deudas nuevas registradas (no en esta entrega)
Teléfono sin normalizar (misma enfermedad del RUT en el otro campo del dedupe); los 7 routers Monza restantes sin candado; la reversa del LTV sin puerta por API (hoy solo vía anular despacho); el KPI de Ventas corta por `fecha_venta` mientras su lista corta por `fecha_creacion`; el 422 fail-closed se muestra al operador con un toast genérico; `delete_lead` permite borrar un lead que originó una venta cerrada.

---

## 7. ACTA DE LA REVISIÓN EN ENJAMBRE (2026-08-27) — 2 críticos, 17 altos

**Commits:** `6befbf4` (críticos + candados + robustez) → `2f98584` (comité: UX, KPIs, comentarios).
**Gate:** 342 pytest verdes · `tsc` limpio · build limpio · verificación visual en el navegador.

### 7.1 Cómo se revisó

Tres enjambres, todos con **refutador independiente** por hallazgo grave (un hallazgo falso cuesta más que uno que se escapa: manda a arreglar lo que no está roto):

| Enjambre | Lentes | Resultado |
|---|---|---|
| **Testing ronda 2** (25 agentes) | plata · leads · fechas · identidad · seguridad · diseño | 14/15 correcciones anteriores verificadas reproduciendo su escenario original; **2 CRÍTICOS y 17 ALTOS** nuevos |
| **Comité de revisión** (21 agentes) | código · UX · documentación (estructura y fiabilidad murieron con el Mac dormido) | 10 hallazgos reales, 8 refutados |
| **Ronda 3** | regresiones · fiabilidad · estructura · seguridad · plata · UX | en curso al cierre de esta acta |

### 7.2 Los dos críticos

**La factura salía al RUT equivocado.** Dos contribuyentes distintos que comparten un teléfono —la recepción del taller, el celular del gestor, o un `-` de relleno tecleado en las dos fichas— terminaban en la MISMA ficha, y el RUT recién digitado se descartaba en silencio. De ahí en adelante la cotización, el cierre y el **DTE 33** colgaban de la ficha ajena. Nace `monza_telefono.py`, hermano de `monza_rut.py`, con la misma regla: **buscar tolera, identificar no**. El teléfono ya no puede fusionar fichas cuyos RUT se contradicen, y un número de menos de 8 dígitos no identifica a nadie — `2342` existe hoy en la base y se habría comido a cualquier cliente nuevo con ese número.

**Escape de carpeta + XSS almacenado en los adjuntos.** El nombre del archivo se armaba con el campo `entidad` del formulario, así que una entidad con `../..` escribía FUERA de `static/docs`. Apuntada a `uploads/bodega` —que `main.py` publica como StaticFiles— dejaba un `.html` con `<script>` servido por el propio dominio de la aplicación: robo de sesión de los operadores de **las dos marcas**, desde una cuenta de la marca contraria. Ahora el nombre lo genera el servidor y el destino se confina en dos capas.

### 7.3 El síntoma original, explicado por tercera vez

La queja era «solo veo los primeros leads». Las dos primeras causas ya estaban cerradas (la pantalla no paginaba; el buscador no buscaba lo que prometía). El testing encontró **una tercera**: un solo lead con `fecha_creacion` en NULL tumbaba la lista entera con un 500, y como el orden es `fecha DESC` y MySQL manda los NULL al final, esas filas caen en las **últimas** páginas. La pantalla andaba en la página 1 y se caía justo al llegar a los leads viejos.

Y el comité encontró **la cuarta, que no era un bug sino una mitad faltante**: el buscador ya encontraba por repuesto, patente, VIN y N° de cotización, pero **ninguno de esos datos se ve en la tabla**. El vendedor recibía ocho filas de aspecto idéntico. Se agregó la insignia de motivo (espejo de `_match_ventas` de Despachos): cada fila dice por qué calzó.

### 7.4 Lo demás

Seis routers Monza que seguían sin candado de empresa —el peor, Configuración: una cuenta de minería podía dejar el **tipo de cambio en 1** y toda cotización nueva salía regalada—; la carrera del correlativo de leads (el generador estaba duplicado y la carrera era justo entre las dos copias); el teléfono normalizado en los tres buscadores; tres relojes de Chile que quedaron a medias; la tasa de cierre que daba «300%» dividiendo dos universos distintos; el contador de ventas que se duplicaba; dos puertas sin guarda (reabrir y eliminar un lead con la venta ya cerrada); y **cinco comentarios que mentían** sobre el candado del LTV — el hallazgo más incómodo, porque el próximo programador podía leerlos, creerles y «restaurar» un gate que se quitó a propósito, reabriendo los dos agujeros de plata recién cerrados.

### 7.5 Lecciones de método

- **Una sonda de seguridad no probaba nada.** La primera versión del test del escape de carpeta subía un `.html`, que rebotaba en la lista blanca de extensiones **antes** de llegar a la capa del nombre: pasaba en verde con el bug puesto. Con defensa en profundidad hay que atravesar la primera capa a propósito para probar la segunda. Se detectó por mutación, no leyendo.
- **Los datos de prueba se inventan, no se toman prestados.** Una sonda usó un teléfono que pertenece a un cliente real y el dedupe —funcionando como debe— le escribió un RUT a esa ficha. Se restauró desde el respaldo de julio y la sonda pasó a usar números propios.
- **Un comentario que miente cuesta lo mismo que un guard que falla abierto.**

### 7.6 Deudas registradas (no en esta entrega)
`auth.py:47` hace `int(sub)` sin proteger: un token malformado da **500** en vez de 401 (código compartido por las dos marcas, no se tocó); Abastecimiento Monza sigue sin candado por decisión del dueño; la reversa del LTV no tiene puerta por API (solo vía anular despacho); bajar `pct_adelanto` a 0 con un PATCH libera la mercadería sin dejar rastro explícito.
