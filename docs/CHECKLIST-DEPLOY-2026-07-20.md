# Checklist de deploy — rama `feature/adelantos-clientes` (corte 2026-07-21)

Para el programador: esta rama acumula los módulos y endurecimientos listados abajo.
**Orden del deploy: primero los scripts de BD, DESPUÉS reiniciar el backend** (todos
los scripts son idempotentes: correr de nuevo no rompe nada).

## 1. Qué correr en el deploy, EN ORDEN

Desde `backend/`, con el venv del servidor, **antes de reiniciar uvicorn**. Todos son
idempotentes: correrlos de nuevo no rompe nada (se probó cada uno dos veces).

**Cómo leer las marcas de cada línea:**

| Marca | Significado |
|---|---|
| 🔴 **SIEMPRE** | No se puede saltar, en ningún entorno. En las líneas de MonzaParts significa además **«aunque `MONZA_CONTAB_ENABLED=false`»**: apagar el flag apaga las *rutas contables*, no las *tablas del núcleo*. `monza_models` se importa siempre y los routers de configuración, cotizador, cotizaciones, ventas, despachos, bodega, notificaciones y recepción nacional de Monza se montan **fuera** del `if` del flag; hacen `SELECT` de todas las columnas del modelo, así que si falta una, MySQL responde `1054 Unknown column` y la pantalla cae con **HTTP 500**. |
| 🟡 **solo con el gate** | Solo hace falta con `MONZA_CONTAB_ENABLED=true`. Correrlo con el flag apagado no rompe nada: adelanta la creación de sus tablas. |
| ⛓️ **ORDEN** | Depende de que otro script haya corrido antes (FK o tabla previa). No es opcional: al revés, el script falla. |

> Actualizado el **2026-07-30**: se agregaron las 4 migraciones nuevas de esa ronda, las 4
> líneas que faltaban (`add_despacho_guia_fields`, `monza_guia_firmada_cotizacion` y los 2
> `import_plan_cuentas`) y las marcas 🔴/🟡/⛓️. `deploy/audit_schema.py --pasos` compara
> este archivo contra el árbol de `backend/`: si aparece un script nuevo y nadie lo
> documenta acá, se pone rojo.
>
> Y la trampa del `rc=0` —un script que se salta un candado de plata y aun así sale con
> éxito— quedó documentada para **los 3** scripts a los que les pasa, no solo para
> tesorería: ver **§1.d-bis** antes de correr §1.e.
>
> Actualizado el **2026-08-08**: entraron 3 scripts (`wasabil_compras.init_db`,
> `monza_wasabil_compras.init_db`, `migrations.monza_cotizacion_cierre`) para las 15 tablas
> que hasta ahora solo nacían del `create_all` del arranque — es decir, DESPUÉS del §1.e,
> que por eso salía `rc=1` en todo primer deploy pidiendo migraciones que no existían.
> **Un paso obligatorio que sale rojo por diseño enseña a ignorar el rojo.** Y §2 estrena
> dos variables (`SII_MATCHER_NOCTURNO` / `_MONZA`) que nacen apagadas: ver §4.b.

### 1.a · Núcleo y Grupo AM / MachParts — 🔴 SIEMPRE

```bash
python -m embarques_pricing.init_db    # 🔴 NUEVO (2026-07-30). Crea configuracion_cotizador.tipo_cambio_eur (+ peso_origen en emb_pricing_item + UNIQUE uq_emb_pricing_gasto_tipo). SIN ESTO: 1054 EN CASCADA — cotizador, ventas, compras, contabilidad, wasabil_dte y pricing leen configuracion_cotizador con SELECT de todas las columnas. Es la línea que más rompe si se salta. ⚠️ LEER LA ÚLTIMA LÍNEA: si emb_pricing_gasto trae líneas duplicadas legadas, el script SALTA el UNIQUE y sale con éxito (rc=0) — ver «Los 3 scripts que pueden saltarse un candado», abajo
python -m recepcion_nacional.init_db   # 🔴 Crea oc_proveedor.tipo_origen + índice (y las 2 tablas de recepción nacional). ⚠️ TRAMPA: las 2 TABLAS se autocrean con create_all, así que el script "parece" opcional — y NO lo es: la COLUMNA no se autocrea, y sin tipo_origen `routers/compras.py` rompe TODA Compras y Seguimiento con 1054
python -m compras_contab.init_db       # 🔴 cont_compra_item (costeo por ítem) + cont_compra.oc_proveedor_id + FK e índice (vínculo compra ↔ OC de proveedor)
python -m wasabil_dte.init_db          # 🔴 tabla wasabil_dte (guías 52 + facturas 33) + en_vuelo_desde + UNIQUE por factura y por despacho. Ese UNIQUE es el candado anti DOBLE EMISIÓN al SII: sin él, dos clics pueden emitir dos documentos tributarios reales
python -m tesoreria.init_db            # 🔴 NUEVO (2026-07-30) el UNIQUE(egreso_id) de conc_conciliacion: respalda en la BD la conciliación 1:1 cargo↔egreso. Trae además conc_* + conciliación de ingresos + adelantos y normaliza cont_factura_cliente.es_anticipo a NOT NULL DEFAULT 0 (con NULLs, MySQL manda el anticipo al final del ORDER BY DESC y la plata del adelanto entraba a la factura equivocada). ⚠️ LEER LA ÚLTIMA LÍNEA: si la tabla trae egresos duplicados legados, el script AVISA, se salta el UNIQUE y sale con éxito (rc=0). Ahí hay que desconciliar el duplicado en Tesorería y volver a correrlo; `deploy/audit_schema.py` lo delata — ver «Los 3 scripts que pueden saltarse un candado», abajo
python migrate_awb_numero.py           # 🔴 columna awb_numero en embarques (N° de AWB escribible y buscable; `awb` guarda el nombre del archivo adjunto, no el número)
python -m migrations.add_despacho_guia_fields      # 🔴 FALTABA EN ESTE CHECKLIST. guia_firmada + fecha_firma + usuario_firma_id + numero_expedicion en `despachos`. Es LA migración del incidente de julio 2026: venía en el paquete, nadie la corrió, y el 1054 dejó invisibles el detalle de Despachos, su botón "Crear Despacho" y el detalle de Ventas
python -m migrations.fix_despacho_parcial_estado   # 🔴 repara líneas 'despachado' de despachos parciales legados (si no se corrió antes). No es de esquema: es de DATOS, y sin ella el remanente de una línea parcial queda bloqueado
python -m migrations.cotizacion_pricing_snapshot   # 🔴 TC congelado: 2 columnas en cotizaciones (sin backfill; ver docs/tc-congelado-cotizacion.md)
python -m migrations.despacho_fecha_guia           # 🔴 NUEVO (2026-07-30). fecha_guia DATE en `despachos` Y en `monza_despachos` (un solo script, las DOS marcas: no hay que repetirlo en §1.b). Es la fecha de EMISIÓN de la guía en papel, que la factura cita en su referencia 52. SIN ESTO: los modelos ya declaran la columna → SELECT con 1054 «Unknown column despachos.fecha_guia» y se caen la pantalla de Despachos y la emisión de facturas al SII de AMBAS marcas (Bodega NO: sus consultas no leen la entidad despacho completa). Ver docs/fecha-emision-guia-referencia-52.md
python -m migrations.proveedor_rut                 # 🔴 NUEVO (2026-08-06). proveedores.rut (RUT canónico, llave de cruce del libro de compras del SII) + índice. Los modelos ya declaran la columna → sin esto, cualquier pantalla que lea la entidad Proveedor completa (Compras, Abastecimiento) responde 1054
python -m wasabil_compras.init_db                  # 🔴 ⛓️ DESPUÉS de tesoreria.init_db (sus FKs apuntan a conc_movimiento; si falta, el script corta con un mensaje en castellano en vez de un errno 150). NUEVO (2026-08-08). Crea las 7 tablas del libro de compras del SII de Grupo AM: sii_libro_doc / sii_libro_sync_run / sii_libro_regla_rut (el espejo) + sii_libro_match / sii_match_run / sii_match_etiqueta_mov / sii_match_config (el matcher banco↔libro). ⚠️ POR QUÉ ES UN SCRIPT Y NO "lo crea el create_all": lo creaba, sí, pero AL REINICIAR — o sea DESPUÉS del §1.e, que corre antes. En el primer deploy el auditor las veía ausentes, salía rc=1 y ordenaba «correr las migraciones», que no existían: un rojo por diseño que enseña a ignorar el rojo. El barrido nocturno corre en su PROPIO job del scheduler (`run_sii_libro_job`, 05:30 America/Santiago — se separó del job de alertas de las 06:00, que no comparte nada con él) y usa el WASABIL_API_TOKEN que ya existe. El matcher corre al importar una cartola y con el botón de la pantalla; DESATENDIDO de noche solo si se prende `SII_MATCHER_NOCTURNO` (§2), que nace APAGADO — igual que la señal RUT-en-glosa en sii_match_config. Ver docs/plan-libro-compras-sii-2026-08-03.md y docs/spec-matcher-banco-libro-2026-08-06.md
```

### 1.b · MonzaParts — 🔴 SIEMPRE, aunque el gate contable vaya apagado

> **`monza_despachos.fecha_guia` NO está en esta lista y no es un olvido:** la crea
> `python -m migrations.despacho_fecha_guia` de **§1.a**, que parcha las dos marcas de una
> vez. Si se salta §1.a, MonzaParts también cae con 1054 en Despachos.

> **`monza_cotizacion_cierre` (historial de versiones del cierre de venta, 2026-08-05)
> SÍ está ahora, y era el último rojo por diseño del §1.e:** es tabla NUEVA y la creaba
> sola el `create_all`… pero al REINICIAR, o sea después del auditor de esquema. En el
> primer deploy salía «TABLA FALTANTE» y mandaba a correr una migración inexistente. Desde
> el 2026-08-08 tiene su script (`migrations.monza_cotizacion_cierre`, abajo). Los
> buscadores de Bodega/Despachos de ambas marcas (2026-08-05) no necesitan ningún script:
> solo consultan columnas que ya existen.

```bash
python -m monza_embarques_pricing.init_db          # 🔴 NUEVO (2026-07-30): desconsolidado_clp + bodegaje_clp + costo_agencia_minimo_clp en monza_config (gastos locales por defecto del pricing), más peso_origen en monza_emb_pricing_item y el UNIQUE uq_monza_emb_pricing_gasto_tipo. ⚠️ monza_config es tabla del NÚCLEO Monza: la leen con SELECT de todas las columnas la Configuración, el Cotizador y las Cotizaciones de MonzaParts, y esos 3 routers se montan FUERA del flag → sin esto quedan en 500 CON EL GATE APAGADO. ⚠️ LEER LA ÚLTIMA LÍNEA: si monza_emb_pricing_gasto trae líneas duplicadas legadas, el script SALTA el UNIQUE y sale con éxito (rc=0), y acá el auditor NO lo atrapa con el gate apagado — ver «Los 3 scripts que pueden saltarse un candado», abajo
python -m migrations.monza_guia_firmada_cotizacion # 🔴 FALTABA EN ESTE CHECKLIST. guia_firmada + guia_firmada_archivo en monza_cotizaciones: el modelo las declara, así que sin ellas cualquier INSERT del ORM de cotizaciones Monza falla con "Unknown column"
python -m migrations.monza_despachos_ciclo_vida    # 🔴 Monza F2: fecha_despacho + numero_expedicion + índice único DSP (ver docs/monza-flujo-bodega-despachos.md)
python -m migrations.monza_despachos_fecha_firma   # 🔴 NUEVO (2026-08-06). FALTABA EN ESTE CHECKLIST (hallazgo de la revisión adversarial 2026-08-06 — el patrón exacto del incidente 1054 de julio). fecha_firma + usuario_firma_id en monza_despachos ("Marcar guía firmada", paridad con MachParts). El ORM ya declara las columnas y los routers de Despachos Monza se montan FUERA del gate → sin esto, cualquier lectura/escritura de la entidad despacho Monza responde 1054 y la pantalla cae en 500, CON EL GATE APAGADO. Las hermanas guia_firmada/guia_firmada_archivo las crea monza_contabilidad.init_db; esta migración trae las otras dos
python -m migrations.monza_oc_fecha_fase3          # 🔴 Monza F3: oc_fecha en monza_cotizaciones (OC obligatoria + RUT validado al facturar). Esa fecha se imprime como referencia 801 del DTE
python -m migrations.monza_moneda_tarifa           # 🔴 Monza F3: moneda_tarifa en monza_cotizaciones (foto de precios completa; sin backfill)
python -m migrations.monza_notif_alertas           # 🔴 Monza F9: destinatario_rol + severidad + regla en monza_notificaciones (alertas del barrido diario de las 06:00). La tabla YA existe y create_all NO altera tablas existentes → sin esto MySQL responde "Unknown column 'regla'" y **NINGUNA notificación de MonzaParts se crea**: ni las del barrido (proveedor atrasado, venta lista, plazo crítico) ni las instantáneas que hoy funcionan (venta cerrada, despacho confirmado, embarque en tránsito, reclamos de bodega). El backend arranca igual → la falla es SILENCIOSA: la campana se queda vacía
python -m migrations.monza_cotizador_parametros    # 🔴 NUEVO (2026-08-05). costo/moneda/peso_kg/markup_pct/tc_aplicado en monza_lead_items + moneda_tarifa/tarifa_aerea en monza_leads: la calculadora de precios GUARDA sus parámetros (antes persistía solo precio_clp y al reabrir mostraba el precio como costo en CLP) y la moneda del flete se elige por cotización. ⚠️ Los modelos YA declaran las columnas y Leads se monta FUERA del gate → sin esto la pantalla de Leads de MonzaParts responde 1054 CON EL GATE APAGADO. Sin backfill deliberado: los ítems viejos quedan NULL y la calculadora cae al comportamiento anterior
python -m migrations.monza_proveedor_rut               # 🔴 NUEVO (2026-08-06). monza_proveedores.rut (RUT canónico, llave de cruce del libro de compras del SII Monza) + índice. El modelo ya declara la columna → sin esto, cualquier pantalla que lea la entidad MonzaProveedor completa (Compras, Abastecimiento) responde 1054. Las 7 tablas monza_sii_* las crea `monza_wasabil_compras.init_db` de §1.c (es 🟡 porque el Libro SII de Monza obedece al gate contable, igual que su pantalla y su Tesorería). El barrido nocturno corre en el MISMO job propio del scheduler (`run_sii_libro_job`, 05:30 America/Santiago) y usa el WASABIL_API_TOKEN_MONZA que ya existe. El matcher Monza corre al importar una cartola y con el botón de la pantalla; DESATENDIDO de noche solo si se prende `SII_MATCHER_NOCTURNO_MONZA` (§2), que nace APAGADO — igual que la señal RUT-en-glosa en monza_sii_match_config. Ver docs/plan-libro-compras-sii-2026-08-03.md y docs/spec-matcher-banco-libro-2026-08-06.md
python -m migrations.monza_cotizacion_cierre           # 🔴 NUEVO (2026-08-08). Tabla monza_cotizacion_cierre (historial de versiones del cierre de venta, del 2026-08-05): cada re-cierre guarda la FOTO de con qué datos se cerró — OC, fecha, % de adelanto, forma de pago, total. Es del NÚCLEO Monza (la escribe el cierre de venta, que se monta FUERA del gate), así que va 🔴. No es tabla de plata: es auditoría, y sin ella un re-cierre pisa los datos anteriores sin dejar huella. La creaba el create_all del arranque; tiene script para que el auditor del §1.e no salga rojo por diseño
python -m migrations.monza_tarifa_aerea_por_moneda     # 🔴 NUEVO (2026-08-08). tarifa_aerea_eur_por_kg + tarifa_aerea_usd_por_kg en monza_config: el courier cobra distinto por moneda y hasta ahora elegir USD cotizaba el número de EUR (flete irreal DENTRO del precio de venta). El modelo ya declara las columnas y Configuración/Cotizador se montan FUERA del gate → sin esto responden 1054 CON EL GATE APAGADO. Hace BACKFILL: copia la tarifa vieja a la columna de SU moneda (la que dice moneda_tarifa); la otra queda NULL y la calculadora BLOQUEA al elegirla hasta que se cargue en Configuración — es a propósito, un flete en 0 subvalúa la venta sin que se note. ⚠️ TAREA POST-DEPLOY: cargar esa segunda tarifa en Configuración
python -m migrations.monza_cliente_sin_oc              # 🔴 NUEVO (2026-08-08). monza_cotizaciones.cliente_sin_oc: venta a CLIENTE PARTICULAR (no emite OC → se usa el N° y la fecha de la cotización como referencia 801 del SII). El modelo ya declara la columna y Cotizaciones/Ventas se montan FUERA del gate → sin esto responden 1054 CON EL GATE APAGADO. Sin backfill: 0 = "se cerró con OC real del cliente", que es lo que pasó con todas las ventas existentes
python -m monza_recepcion_nacional.init_db         # 🔴 ⛓️ 1° de 2 (antes de monza_compras_contab). Tablas de recepción nacional + tipo_origen en monza_oc_proveedor. Obligatorio con el gate APAGADO: su router se monta fuera del flag (main.py) y Abastecimiento Monza filtra por tipo_origen
python -m monza_contabilidad.init_db               # 🔴 ⛓️ ANTES de monza_wasabil_dte (la FK apunta acá). NUEVO (2026-07-30): monza_cont_adelanto.estado — sin él, TODA la Contabilidad y TODA la Tesorería de Monza responden 1054 (lo filtran los lectores de adelantos, sugerencias, aprobaciones, flujo de caja y resumen). Trae además es_anticipo + anticipo_factura_id (factura de ANTICIPO vía B, docs/monza-factura-anticipo.md) y, por eso es 🔴, parchea columnas de tablas del NÚCLEO Monza que se leen con el gate apagado: pct_adelanto / adelanto_verificado / guia_firmada en monza_cotizaciones y guia_firmada en monza_despachos
python -m monza_wasabil_dte.init_db                # 🔴 ⛓️ DESPUÉS de monza_contabilidad.init_db (FK a monza_cont_factura_cliente). Tabla monza_wasabil_dte (guías 52 Y facturas 33) + factura_id + UNIQUE anti doble emisión. El create_all del arranque NO la crea. Obligatorio con el gate apagado: los guards de anular despacho y de editar la OC la consultan con import local y esos routers se montan siempre (si la tabla falta, el guard se apaga solo y se podría anular un despacho con guía SII viva)
```

### 1.c · MonzaParts contable — 🟡 solo con el gate encendido

```bash
python -m monza_compras_contab.init_db             # 🟡 ⛓️ 2° de 2 (después de monza_recepcion_nacional). monza_cont_compra_item (costeo por ítem) + monza_cont_compra.oc_proveedor_id + FK (docs/monza-compras-nacionales.md)
python -m monza_tesoreria.init_db                  # 🟡 snapshot fecha/ref del egreso en monza_tes_conciliacion (desconciliar restaura el dato original)
python -m monza_wasabil_compras.init_db            # 🟡 ⛓️ DESPUÉS de monza_tesoreria.init_db (sus FKs apuntan a monza_tes_movimiento). NUEVO (2026-08-08). Las 7 tablas monza_sii_* del libro de compras del SII de MonzaParts + su matcher. Espejo exacto de `wasabil_compras.init_db` de §1.a. Con el gate APAGADO el script se abstiene solo y lo dice ("no hay nada que crear — no es un error"): correrlo igual no rompe nada
```

> **2026-08-02 · Salidas de emergencia de MonzaParts (registrar folio del SII + revertir
> factoring): NO requieren ningún script.** Se anota aquí para que nadie lo busque: trabajan
> sobre tablas y columnas que ya existen (`monza_wasabil_dte`, `monza_cont_factoring`,
> `monza_cont_cobranza`). Deploy normal: código + `npm run build` + reiniciar.
> Ver `docs/monza-salidas-de-emergencia-2026-08-02.md`.

### 1.d · Plan de cuentas — una vez por marca, con el Excel del dueño a mano

```bash
python -m compras_contab.import_plan_cuentas       # 🔴 Grupo AM. Sin esto cont_plan_cuenta queda VACÍA y **toda compra nace sin imputación contable** (no tumba nada: el daño es silencioso y contable). UPSERT por (empresa, código): correrlo de nuevo no duplica
python -m monza_compras_contab.import_plan_cuentas # 🟡 MonzaParts (tabla monza_cont_plan_cuenta, del bloque contable)
```

⚠️ Los dos leen la hoja «Plan de cuentas» del Excel de referencia del dueño, que **NO está
en el repo** (`Excel grupo am actual/` está en `.gitignore`). Si no lo encuentran, cortan
con `No se encontró ningún .xlsx en …` — fallan fuerte, no en silencio. Se les puede pasar
la ruta: `python -m compras_contab.import_plan_cuentas "/ruta/al/archivo.xlsx"`.

### 1.d-bis · Los 3 scripts que pueden SALTARSE un candado y salir con éxito

Estos 3 crean un **UNIQUE** que respalda una invariante **de plata**. Si la tabla trae
duplicados legados, MySQL responde `1062` y —al ir la migración en una sola transacción— el
ALTER tumbaría el resto del módulo. Por eso los 3 **saltan el UNIQUE a propósito, avisan y
siguen**: es la conducta correcta. Lo que hay que saber es que **salen con `rc=0`**.

| Script | UNIQUE | Qué se rompe si queda ausente |
|---|---|---|
| `tesoreria.init_db` | `uq_conc_concil_egreso` (`conc_conciliacion.egreso_id`) | el MISMO pago puede quedar conciliado contra dos cargos del banco |
| `embarques_pricing.init_db` | `uq_emb_pricing_gasto_tipo` (`emb_pricing_gasto`) | **doble CxP del forwarder** (ver abajo) |
| `monza_embarques_pricing.init_db` | `uq_monza_emb_pricing_gasto_tipo` (`monza_emb_pricing_gasto`) | lo mismo, en MonzaParts |

**Cómo leer la salida (los 3 se comportan igual):** basta mirar la **ÚLTIMA línea**.

- Todo aplicado → `Listo (sin migraciones pendientes).` / `init OK (sin migraciones pendientes).`
- Un paso saltado → la última cosa que se imprime es un **recuadro** de 78 `=` con
  `ATENCIÓN: N migración(es) quedó/quedaron PENDIENTE(S)`, y dentro: **cuál** es el paso,
  **qué filas** lo bloquean (N° de embarque / id de egreso — lo que se busca en pantalla),
  **cuál NO borrar** y el **remedio** exacto para volver a correrlo.
- Si el deploy va dentro de un script y se quiere que **se caiga** ahí, los dos de pricing
  aceptan `--exigir-completo` → **`rc=2`**. Sin la bandera el `rc` es 0 **a propósito**:
  `embarques_pricing.init_db` es la 1ª línea de §1.a y `monza_embarques_pricing.init_db` la
  1ª de §1.b, y las líneas que vienen detrás son las que producen el `1054 → HTTP 500 en
  cascada`. Un `rc≠0` incondicional, con un `set -e` o un `&&` de por medio, cambiaría un
  riesgo latente por una caída segura de toda la app.

**Por qué el de pricing toca plata.** `cont_compra.emb_pricing_gasto_id` (y su espejo
`monza_cont_compra.emb_pricing_gasto_id`) apunta a la línea de gastos del embarque y esa FK
es **ON DELETE SET NULL**. Sin el UNIQUE, la identidad `(pricing_id, tipo)` de la línea no
tiene respaldo de BD: un `delete` + `re-insert` la deja con PK nueva, la CxP queda con la
llave en `NULL`, el overlay de Compras vuelve a mostrar «Registrar como compra» y **la misma
factura del forwarder se carga DOS veces** (Σ CxP al doble; el caso medido fue 380.800 por
una factura de 190.400).

**Cómo detectarlo después.** `deploy/audit_schema.py` (§1.e) compara el modelo contra la BD
y canta el índice por nombre:

```text
[emb_pricing_gasto] UNIQUE FALTANTE uq_emb_pricing_gasto_tipo (pricing_id, tipo) — migración saltada: la invariante no tiene respaldo de BD
=== 1 problema(s): CORRER LAS MIGRACIONES ANTES DE REINICIAR ===
```

y sale **`rc=1`**. Verificado en vivo (se soltó el índice y se corrió el auditor), igual que
`--autoprueba` → `AUTOPRUEBA VERDE`.

> ⚠️ **El límite del auditor en MonzaParts.** Las tablas `monza_emb_*` están en
> `PREFIJOS_SOLO_CON_GATE`, así que con `MONZA_CONTAB_ENABLED=false` el mismo hallazgo se
> degrada a `(aviso, gate apagado) [monza_emb_pricing_gasto] UNIQUE FALTANTE …`, el informe
> cierra en `=== sin problemas ===` y el auditor sale **`rc=0`**. Medido con el gate forzado
> en las dos posiciones. Con el gate apagado, **el recuadro del propio script es la única
> señal fuerte del deploy** — y `--exigir-completo` la única para una máquina.

Conducta probada de verdad (se suelta el índice, se siembra el duplicado marcado y se corre
el script real, como proceso y como función): `tesoreria/tests/test_lecturas_de_plata.py`,
`embarques_pricing/tests/test_migracion_llave_pendiente.py` y
`monza_embarques_pricing/tests/test_migracion_llave_pendiente.py`.

### 1.e · Verificar el esquema ANTES de reiniciar

```bash
python ../deploy/audit_schema.py               # debe decir "sin problemas"
python ../deploy/audit_schema.py --autoprueba  # ¿el auditor VE de verdad? debe decir VERDE
python ../deploy/audit_schema.py --pasos       # ¿este checklist quedó completo?
```

`audit_schema.py` compara los modelos contra la BD real: tabla ausente, **columna ausente**
(el 1054 que produce el 500 en cascada) y **UNIQUE ausente** (una migración saltada en
silencio, como el `UNIQUE(egreso_id)` de arriba). Cubre el núcleo de las 2 marcas **y los
11 módulos satélite** — hasta el 2026-07-30 solo veía 58 de las 95 tablas y decía «sin
problemas» sin haber mirado la contabilidad, la tesorería, las compras, el pricing ni los
DTE de ninguna de las dos marcas. `--autoprueba` existe porque «sin problemas» se ve
idéntico cuando no hay nada malo y cuando no se está mirando: planta defectos falsos en
memoria (no toca la BD) y exige que el auditor los cante.

> **Este paso tiene que salir VERDE, y desde el 2026-08-08 sale verde de verdad.** Hasta
> entonces había 15 tablas que solo nacían del `create_all` del arranque —las 14 del libro
> de compras del SII (7 por marca) y `monza_cotizacion_cierre`—, o sea DESPUÉS de este
> paso: el auditor las cantaba como «TABLA FALTANTE», salía `rc=1` y cerraba con «CORRER
> LAS MIGRACIONES ANTES DE REINICIAR», una orden imposible porque esas migraciones no
> existían. **Un paso obligatorio que sale rojo por diseño le enseña al operador a ignorar
> el rojo, que es exactamente lo que este auditor existe para impedir.** Se cerró dándoles
> script propio (§1.a `wasabil_compras.init_db`, §1.c `monza_wasabil_compras.init_db`, §1.b
> `migrations.monza_cotizacion_cierre`). Si acá sale rojo: falta correr algo de §1, punto.

Nota: la columna `cotizaciones.origen` ya existe en prod (vino de allá).

## 2. Variables de entorno (`backend/.env` del servidor)

La plantilla completa y comentada es **`backend/.env.example`**: ahí están las 13 variables
que `config.py` acepta, con qué pasa si falta cada una. `audit_schema.py --pasos` verifica
que ninguna variable nueva de `config.py` se quede sin documentar ahí (fue el caso del
token de MonzaParts: `config.py` lo declaraba y la plantilla no, así que un entorno nuevo
quedaba sin poder emitir al SII y sin pista de por qué).

Las dos que hay que completar a mano en el servidor:

- `WASABIL_API_TOKEN=...` — token del facturador (Wasabil, cuenta GRUPO AM SPA).
- `WASABIL_API_TOKEN_MONZA=...` — token de la cuenta Wasabil de MonzaParts (LOPEZ
  HERNANDEZ INVERSIONES SPA, RUT 78.121.316-0). OJO: `config.py` de esta rama YA
  declara la variable — con un config.py viejo, poner el token en `.env` tumba el
  backend al arrancar (pydantic extra_forbidden).
  Sin él la app funciona igual pero el botón "Emitir guía SII" queda bloqueado
  con aviso. **El token NUNCA va al repo.**

⚠️ **El token no es solo para emitir: también LLENA el Libro de Compras del SII.** El
barrido nocturno de las 05:30 usa el mismo token de cada marca (`WASABIL_API_TOKEN` /
`WASABIL_API_TOKEN_MONZA`). Sin él, el barrido **se omite** y la pantalla Contabilidad →
Libro SII queda vacía para siempre con el cartel «Nunca sincronizado» — nadie relaciona
"el libro está vacío" con "falta un token". Desde el 2026-08-08 el log del servidor lo
dice a gritos en cada corrida (`[scheduler][libro-sii] SIN WASABIL_API_TOKEN…`).

### Las dos variables NUEVAS del 2026-08-08 — dejarlas COMO ESTÁN en este deploy

- `SII_MATCHER_NOCTURNO` (Grupo AM) y `SII_MATCHER_NOCTURNO_MONZA` (MonzaParts).
  **Ausentes = `false`, y así tienen que quedar en el deploy.** Gobiernan una sola cosa:
  si el matcher banco↔libro puede correr **solo, de noche y sin nadie mirando**, dentro
  del job de las 05:30.
- Por qué nacen apagadas: el matcher **no tiene ventana de fecha**. La primera noche
  después del deploy recorrería la cartola histórica COMPLETA de Tesorería y podría dejar
  movimientos del banco con `conciliado = True` por una decisión automática que nadie
  revisó nunca. El encargado de Tesorería se encontraría al otro día con plata marcada
  por un módulo que todavía no conoce (y para soltarla hay que entrar a una pantalla que
  nadie le mostró). Es el mismo criterio que este proyecto ya aplicó a la señal
  RUT-en-glosa: **una señal nueva nace apagada hasta validarla con datos reales**.
- El **barrido del libro corre igual** con esto apagado: llenar el espejo del SII es
  lectura pura, y es justo lo que hay que mirar en el estreno (§4.b).
- Cuándo prenderlas: después del estreno de §4.b, **una marca a la vez**, agregando la línea
  al `backend/.env` y reiniciando. En el arranque el log lo confirma:
  `[scheduler] … matcher nocturno GA=ON`.

## 3. Frontend

```bash
cd frontend-src && npm install && npm run build
```

## 3.b ⚠️ REQUISITO NUEVO — nivel de aislamiento de MySQL

Esta rama pone el engine en **READ COMMITTED** (`backend/database.py`), lo que cierra una
clase de bug que costó cinco fugas de plata reproducibles. Antes de reiniciar:

```sql
SELECT @@global.binlog_format;   -- debe ser ROW o MIXED
```

- `ROW` / `MIXED` → seguir normal.
- `STATEMENT` → **NO desplegar todavía**: MySQL rechaza las escrituras bajo READ COMMITTED
  con ese binlog. Pedir el cambio a ROW al hosting y, mientras tanto, revertir SOLO el
  commit del aislamiento (los arreglos a mano ya protegen los caminos críticos por sí solos).

Detalle y fundamento: `docs/regla-lecturas-de-plata.md`.

## 4. Reiniciar el backend

Recién después de 1-3. **Reinicio COMPLETO del servicio, no `--reload`**: el nivel de
aislamiento se fija al abrir cada conexión, así que un reload deja workers viejos con el
nivel anterior. En el log de arranque debe aparecer `[startup] isolation=READ-COMMITTED`
(si dice otra cosa, algo pisó `backend/database.py`).

En el mismo log de arranque, dos líneas más que hay que LEER:

```text
[scheduler] APScheduler started — daily checks at 06:00, libro SII at 05:30 (Santiago) · matcher nocturno GA=OFF · Monza=gate apagado
```

`GA=OFF` es lo esperado en este deploy (ver §2). `Monza=gate apagado` confirma que la
contabilidad de MonzaParts —incluido su Libro SII— no se montó.

Verificación rápida post-deploy: login → Despachos
(botones ① Emitir guía SII ② Agregar transportista ③ Confirmar en un despacho
en preparación) → Contabilidad → Ventas (barra de avance + "Por facturar") →
Facturas y Cobranzas ("Emitir factura" debe abrir en modo SII con el folio
"Lo asigna el SII al emitir" y el enlace al registro manual).

### 4.b · ESTRENO del Libro de Compras del SII (módulo nuevo — hacerlo el mismo día)

Es el módulo que este deploy estrena y es el único que empieza a trabajar **solo**. Estos
tres pasos son a la vez la verificación y el estreno: **en horario de oficina y con el
dueño del proceso al lado**, no a las 05:30 de la mañana siguiente.

1. **Contabilidad → Libro SII → "Sincronizar ahora".** Debe terminar en verde y decir
   cuántos documentos trajo. Si falla, el error exacto queda escrito en la pantalla («El
   último barrido falló: …») y sobrevive al refresco — ahí está la causa (token, red,
   permisos). Sin este clic, la primera corrida real del módulo sería el job de las 05:30
   sobre datos vivos y sin nadie mirando.
2. **Revisar la bandeja de 3 cubetas y la cuadratura** (ESTÁ / NO ESTÁ / EN DUDA). Los
   RUTs de proveedor que no cruzan se arreglan desde ahí; la señal RUT-en-glosa nace
   apagada (`sii_match_config` arranca VACÍA: el valor por defecto vive en el código).
3. **Correr el matcher a mano una vez y mirar lo que propone**, antes de dejarlo suelto de
   noche. Recién cuando esos cruces convenzan, prender `SII_MATCHER_NOCTURNO` (§2) y
   reiniciar. **Mientras la variable no esté, el matcher NO escribe nada por su cuenta.**

Si el Libro SII se ve vacío y "Nunca sincronizado", el primer sospechoso es
`WASABIL_API_TOKEN` (§2), no el módulo.

> **MonzaParts queda fuera de este estreno a propósito.** Con `MONZA_CONTAB_ENABLED=false`
> su Libro SII no se monta, su barrido no corre y su pantalla no existe — las tres piezas
> apagadas juntas (hasta el 2026-08-08 el barrido corría igual y construía de noche un
> espejo que nadie podía mirar). Cuando la marca se encienda, el estreno es el mismo,
> con `SII_MATCHER_NOCTURNO_MONZA`.

## Qué trae esta rama (resumen para orientarse)

| Área | Qué cambió | Doc |
|---|---|---|
| Guías SII (Wasabil) | Módulo `wasabil_dte/` completo; PRIMERA EMISIÓN REAL OK (folio 136); formato v2: OC referenciada una sola vez, nombre de línea = descripción | `backend/wasabil_dte/README.md`, `docs/integracion-wasabil-guias.md` |
| Facturas SII (Wasabil, Fase B) | Emisión de la factura 33 desde Facturas y Cobranzas (normal y anticipo): folio lo asigna el SII; refs 801+52+33; descuento de anticipo como % por línea; adelantos diferidos hasta Emitida; modo manual conservado. **PRIMERA FACTURA REAL OK: folio 116** (2026-07-21, desde la guía 136, con el adelanto de $17.885.300 aplicado solo al confirmarse el folio) | `backend/wasabil_dte/README.md` (sección Fase B) |
| Referencias DTE — formato v3 | El motivo (RazonRef) ya no repite lo que el tipo y el folio imprimen: la OC salía escrita DOS veces en la guía 137. Verificado contra el API real con un borrador y contra PDFs del portal SII | `backend/wasabil_dte/README.md` (sección Formato v3) |
| Guard guía↔factura | No se puede facturar una guía cuyo folio del SII todavía viene en camino (referenciaría un folio inexistente, irreversible) | commit `a0d4671` |
| Flujo de plata — concurrencia | Lecturas BLOQUEANTES donde se decide un tope (cobranzas, adelantos, factoring, pago a proveedores) + engine en READ COMMITTED. Cierra 5 fugas de dinero reproducibles | `docs/regla-lecturas-de-plata.md` |
| Despachos | Flujo guía-primero (crear sin N° guía manual; transportista después); cobertura de estados por despachos CERRADOS + reversa del embarque | `docs/flujo-bodega-despachos.md` |
| Bodega→Despachos | Tope físico por lo RECIBIDO; recepción parcial con reclamos trazables | `docs/flujo-bodega-despachos.md` |
| Compras NACIONALES | OC nacional/internacional, recepción sin embarque, costo por ítem, CxP→Tesorería | `docs/plan-compras-nacionales-2026-07-18.md`, READMEs de `recepcion_nacional/` y `compras_contab/` |
| Adelantos de clientes | Vía A (cobranza) y vía B (factura de anticipo con descuento automático); Tesorería aprueba; % ↔ CLP espejo en Cierre de Venta | `docs/adelantos-clientes-grupo-am-2026-07-16.md` |
| Ventas—Contabilidad | Desglose por factura + avance real de la OC ("por facturar" con base física) + anti-muro para OCs grandes | commit `76bc331` |
| OC cliente | N° obligatorio al cerrar venta, edición ex-post controlada | `docs/integracion-oc-cliente.md` |
| Embarques | N° AWB escribible/buscable; peso editable en Embarques Pricing | `docs/awb-numero-embarques-2026-07-17.md`, `docs/peso-editable-embarques-pricing-2026-07-17.md` |
| **Monza SII (F5 guías 52 + F6 facturas 33)** | Emisión electrónica al SII para MonzaParts (cuenta LOPEZ HERNANDEZ INVERSIONES SPA, RUT 78.121.316-0): botón "Emitir guía SII" en Despachos y modo SII en Facturas (folio "lo asigna el SII"), con el protocolo anti doble emisión de GA, referencias 801+52 con el folio REAL de la guía y **adelantos DIFERIDOS** hasta que el SII confirma. Verificación post-deploy: Despachos → emitir guía (preview) · Facturas → "Nueva factura" abre en modo SII | `docs/monza-guias-sii.md`, `docs/monza-facturas-sii.md` |
| **Alertas automáticas MonzaParts** | El barrido diario de las 06:00 (America/Santiago) solo consultaba tablas de Grupo AM: **un proveedor de MonzaParts atrasado no avisaba NUNCA**, aunque el plazo estuviera cargado, y "venta lista para despacho" / "plazo crítico ≤3 días hábiles" solo se disparaban en el instante de cerrar una recepción en Bodega. Ahora las 3 reglas se re-evalúan todos los días, con idempotencia de 24 h (sin ella cada corrida repite el mismo aviso y el dueño deja de mirar la campana) y con cada marca aislada en su propio try/except (un error de Monza no puede dejar a Grupo AM sin alertas, ni al revés). Verificación post-deploy: la campana de MonzaParts debe mostrar avisos al día siguiente del deploy si hay OCs de proveedor atrasadas | `backend/scheduler.py`, `backend/monza_notif.py`, suite `backend/monza_tests/test_alertas_diarias.py` |
| **Libro de Compras del SII (LO NUEVO de este deploy)** | Espejo de los documentos RECIBIDOS desde Wasabil, en las DOS marcas: 14 tablas nuevas (`sii_libro_*` + `sii_match_*` y sus gemelas `monza_sii_*`), pantalla Contabilidad → **Libro SII** con bandeja de 3 cubetas (ESTÁ / NO ESTÁ / EN DUDA), cuadratura, reglas por RUT y conciliación bancaria contra Tesorería. Trae un **job propio del scheduler a las 05:30** (`run_sii_libro_job`, separado del de alertas de las 06:00) y el **matcher banco↔libro**, que corre al importar una cartola, con el botón de la pantalla y —solo si el dueño lo habilita— de noche. Verificación post-deploy y estreno: **§4.b** (no saltárselo: es el único módulo que empieza a trabajar solo). Scripts: §1.a `wasabil_compras.init_db`, §1.c `monza_wasabil_compras.init_db`, más `migrations.proveedor_rut` / `monza_proveedor_rut` | `docs/plan-libro-compras-sii-2026-08-03.md`, `docs/spec-matcher-banco-libro-2026-08-06.md` |
| **Higiene del deploy (2026-08-08)** | Cuatro arreglos para que el procedimiento **no salga rojo por diseño**: (1) el §1.e ya no reporta 15 «TABLA FALTANTE» inevitables — esas tablas tienen script; (2) el paso 4 de `deploy/README.md` (verificación del gate de Monza) volvió a ser cierto: el Libro SII de Monza se montaba SIEMPRE y su cierre del grafo de FKs metía 25 tablas en la metadata con el gate apagado, así que el `assert` fallaba en toda promoción — ahora el router, el barrido nocturno y la pantalla obedecen al MISMO flag; (3) el matcher nocturno nace apagado (§2) y el estreno tiene su paso (§4.b); (4) sin token de Wasabil, el log del servidor ahora dice que el libro no se va a llenar, en vez de callarse | `backend/scheduler.py`, `backend/main.py`, `deploy/README.md`, suite `backend/tests_infra/test_gate_monza_deploy.py` |
| **Espejo MonzaParts (F1–F4 + paridad)** | Blindaje del flujo de plata; integridad Despachos/Bodega (tope físico, parcial, guía-primero); datos maestros (OC obligatoria + fecha, RUT validado, `pct_adelanto` que se perdía); avance de la plata por venta (base física) + tableros de despachos; 27 reparaciones de paridad (tope Σ brutos, adelanto retroactivo, locks factoring, folio obligatorio, half-up, snapshot al desconciliar); suites Monza ahora visibles a pytest. Verificación post-deploy Monza: Cotizaciones (cerrar venta pide OC), Despachos (tablero de avance), Bodega (historial), Contab → Ventas (por facturar) | `docs/plan-espejo-monza-2026-07-23.md` + docs `monza-*.md` |

## El gate de pruebas

```bash
cd backend && python -m pytest -q
```

**200 verdes** al corte (107 MonzaParts + 91 Grupo AM + 2 del candado de suites), **más las
5 del candado del gate de Monza** que agregó el 2026-08-08 (`tests_infra/`). Necesitan
MySQL — salvo las de `tests_infra/`, que no tocan ni base ni red.

**Correr SIEMPRE el comando pelado, nunca una lista de carpetas escrita a mano.** No hay
`pytest.ini`: pytest recorre el árbol solo y encuentra las 30 suites. Una lista a mano es una
trampa comprobada — escribir `pytest tests_contabilidad wasabil_dte/tests routers/tests` da
**64 verdes** y parece un gate completo, pero se saltó en silencio `compras_contab`,
`embarques_pricing`, `recepcion_nacional` y `tesoreria`. Si el número no da 200+, faltan carpetas.

Desde el 2026-08-08 hay una suite más en `tests_infra/`:
**`test_gate_monza_deploy.py`** automatiza el «paso 4» de `deploy/README.md` (con
`MONZA_CONTAB_ENABLED=false`: cero rutas contables de MonzaParts montadas y cero de sus 25
tablas en la metadata) y las puertas del job del libro SII (el matcher no corre
desatendido sin habilitarlo; el barrido sí). Ese chequeo vivía SOLO como un bloque para
copiar y pegar en el servidor, y cuando dejó de ser cierto nadie se enteró hasta la
auditoría — **un chequeo que solo existe en un documento no protege nada entre deploy y
deploy**. No toca base ni red (subprocesos con `AUTO_CREATE_TABLES=false`).

La suite `backend/tests_infra/test_suites_visibles.py` es el candado contra la **suite
invisible**: el molde de este repo es `def run()` + un wrapper `def test_x(): run()`, y sin el
wrapper pytest no descubre nada — el archivo queda "verde" porque no existe. Ya pasó **dos veces**
(una de ellas con 9 comprobaciones de concurrencia de plata). Ahora el gate se pone rojo y dice
qué archivo es.

## Emisiones REALES ya hechas con esta rama (no tocar, son documentos tributarios)

| Documento | Folio SII | Cuándo | Nota |
|---|---|---|---|
| Guía de despacho 52 | **136** | 2026-07-20 | primera emisión real; formato v1 (de ahí salieron los arreglos v2) |
| Guía de despacho 52 | **137** | 2026-07-21 | formato v2; reveló la tercera duplicación → formato v3 |
| **Factura 33** | **116** | 2026-07-21 | primera factura real, desde la guía 136; refs 801+52 limpias; adelanto aplicado solo |

## Lo que un desarrollador debe saber antes de tocar este código

1. **Toda decisión sobre plata se relee BAJO LOCK** — nunca desde una relación perezosa
   ni desde un `selectinload`. Ver `docs/regla-lecturas-de-plata.md`; ahí está el porqué
   y lo que costó cada fuga.
2. **Los tests usan un login que CONSULTA la base** a propósito. No volver al `lambda`
   seco: con el atajo, las carreras de concurrencia se vuelven invisibles.
3. **El motivo de una referencia DTE nunca repite el tipo ni el folio** — el render los
   imprime solo. Hay un test que lo protege.
4. **Emitir al SII es irreversible.** El módulo solo manda `issue=true` tras la
   confirmación explícita del usuario, y el claim anti doble emisión se commitea ANTES
   del HTTP. No simplificar ese orden.
