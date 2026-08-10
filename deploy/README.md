# Despliegue / Promoción a producción

> Desde 2026-07-16, **PROD y QA comparten código idéntico**. La diferencia vive
> solo en variables de entorno (feature flags). Ya **no** existe la "variante
> curada" ni hay que editar fuentes al promover.

## Infraestructura (confirmada 2026-07-15)

| Entorno | URL | IP | SSH | ¿Repo git? |
|---|---|---|---|---|
| **PROD** | `appmachparts.cl` | `187.77.38.19` | `root` | **No** |
| **QA / staging** | `machparts.bigcode.cl` | `72.60.48.233` | `deploy` | **Sí** (este repo) |

Docroot en ambos: `/var/www/machparts.bigcode.cl/`. API: pm2 `machparts-api`, puerto 8002.

## Feature flags (la única diferencia PROD ↔ QA)

Decisión del cliente (2026-07-15): PROD lleva el subsistema contable de **MachParts**;
el de **MonzaParts va apagado**. Se controla por flags — el código es el mismo:

| Flag | QA | PROD | Efecto |
|---|---|---|---|
| `MONZA_CONTAB_ENABLED` (`backend/.env`) | *(ausente = true)* | `false` | Con `false`, `main.py` **ni importa** los 6 módulos Monza-contab (contabilidad, tesorería, compras/CxP, pricing, DTE y Libro de compras del SII) → sus modelos no se cargan → `create_all` **no crea** sus 25 tablas (`monza_cont_*`, `monza_tes_*`, `monza_emb_pricing*`, `monza_sii_*`) → sus rutas responden 404. **Verificado el 2026-08-08: son 6 módulos y 25 tablas** (el 2026-07-30 eran 5 y 18; antes de eso decía 4 y 17) |
| `VITE_MONZA_CONTAB` (`frontend-src/.env.local`, build-time) | *(ausente = true)* | `false` | Quita las **5** rutas de `App.tsx` (`ventas-contab`, `compras-contab`, `libro-sii`, `tesoreria`, `embarques-pricing`) y sus entradas del menú en `MonzaLayout`. **Verificado el 2026-08-08: son 5** (acá decía 4; la que faltaba era `libro-sii`) |
| `SII_MATCHER_NOCTURNO` / `SII_MATCHER_NOCTURNO_MONZA` (`backend/.env`) | *(ausente = false)* | *(ausente = false)* | ¿El matcher banco↔libro corre **solo, de noche y sin nadie mirando**, dentro del job de las 05:30? Nace apagado en las dos marcas: el matcher no tiene ventana de fecha, así que la primera noche recorrería la cartola histórica completa y podría dejar movimientos del banco conciliados por una decisión que nadie revisó. Se prende después del estreno — `docs/CHECKLIST-DEPLOY-2026-07-20.md` §4.b |
| `AUTO_CREATE_TABLES` (`backend/.env`) | *(ausente = true)* | *(ausente = true)* | Gate del `create_all` que corre al importar `main.py` |

Además, `config.py` hace **fail-fast**: si `SECRET_KEY` es la default
(`changeme-in-production`), la app **no arranca**.

### ⚠️ Apagar el flag NO exime de correr las migraciones de MonzaParts

Esto es la trampa que muerde una y otra vez. `MONZA_CONTAB_ENABLED=false` apaga las
**rutas contables** de MonzaParts, no sus **tablas del núcleo**: `monza_models.py` se
importa SIEMPRE y `main.py` monta **fuera** del `if` del flag los routers de
configuración, cotizador, cotizaciones, ventas, despachos, bodega, logística,
notificaciones y recepción nacional de Monza. Esos routers hacen `SELECT` de **todas** las
columnas que el modelo declara: si a la tabla le falta una, MariaDB lanza `error 1054` y la
pantalla cae con **HTTP 500** — con el flag apagado.

Tablas del núcleo Monza que se leen con el flag en `false`, y de dónde salen sus columnas:

| Tabla | Columnas que exige el modelo | Script que las crea |
|---|---|---|
| `monza_config` | `desconsolidado_clp`, `bodegaje_clp`, `costo_agencia_minimo_clp` | `monza_embarques_pricing.init_db` |
| `monza_cotizaciones` | `pct_adelanto`, `adelanto_verificado`, `guia_firmada`, `guia_firmada_archivo`, `oc_fecha`, `moneda_tarifa` | `monza_contabilidad.init_db`, `migrations.monza_guia_firmada_cotizacion`, `migrations.monza_oc_fecha_fase3`, `migrations.monza_moneda_tarifa` |
| `monza_despachos` | `guia_firmada`, `fecha_despacho`, `numero_expedicion` | `monza_contabilidad.init_db`, `migrations.monza_despachos_ciclo_vida` |
| `monza_oc_proveedor` | `tipo_origen` | `monza_recepcion_nacional.init_db` |
| `monza_notificaciones` | `destinatario_rol`, `severidad`, `regla` | `migrations.monza_notif_alertas` |
| `monza_wasabil_dte` (tabla entera) | la consultan los guards de anular despacho y editar OC, con import local | `monza_wasabil_dte.init_db` |

Las columnas de `monza_cotizaciones` / `monza_despachos` de la primera tanda ya están
aplicadas en PROD; las de `monza_config` son **nuevas del 2026-07-30** y sin ellas el
cotizador, la Configuración y las Cotizaciones de MonzaParts quedan en 500 aunque el flag
vaya apagado.

**La lista completa, en orden, con qué se rompe si se salta cada una, está en
[`docs/CHECKLIST-DEPLOY-2026-07-20.md`](../docs/CHECKLIST-DEPLOY-2026-07-20.md) §1** — 32
scripts, marcados 🔴 (obligatorio siempre) / 🟡 (solo con el gate) / ⛓️ (orden obligatorio).
Ese checklist es la fuente de verdad; este README solo explica el porqué. El número sale
de `deploy/audit_schema.py --pasos`, que compara el árbol de `backend/` contra el
checklist: si no coinciden, se pone rojo.

> Historia: hasta 2026-07-16 esto se resolvía con `deploy/curar_prod_monza.py`, que
> recortaba `main.py`/`App.tsx`/`MonzaLayout.tsx` por regex. Se eliminó tras la revisión
> de código del cliente (frágil: fallaba en silencio ante un rename/reformat). Si se
> necesita, está en el historial de git (commit `f811c91`).

## Procedimiento de promoción

```bash
# 0) RESPALDO (no negociable)
mysqldump -u<user> -p<pass> --single-transaction --routines --triggers machparts_db \
  > /root/backups-migracion/machparts_db-$(date +%Y%m%d-%H%M).sql
tar czf /root/backups-migracion/codigo-$(date +%Y%m%d-%H%M).tar.gz \
  --exclude=backend/venv --exclude=frontend-src/node_modules --exclude=frontend-src/dist \
  backend frontend-src assets index.html
cp backend/.env /root/backups-migracion/env-$(date +%Y%m%d-%H%M).bak

# 1) Código completo (nunca sobrescribir .env / uploads / results / venv)

# 2) Confirmar flags de PROD (una sola vez; persisten)
grep MONZA_CONTAB_ENABLED backend/.env          # -> false
cat frontend-src/.env.local                     # -> VITE_MONZA_CONTAB=false

# 3) Migraciones: create_all NO agrega columnas a tablas existentes.
#    Son 32 scripts y hay ORDEN obligatorio entre varios. La lista, con qué se rompe si
#    se salta cada uno, está en docs/CHECKLIST-DEPLOY-2026-07-20.md §1: copiar y correr
#    ESE bloque completo, desde backend/, con el venv del servidor.
#    (Hasta 2026-07-30 este README listaba 1 de los 22 y no citaba el checklist. Los
#     scripts eran los mismos: lo que faltaba era decir que existían.)

# 3.b) Auditar el esquema ANTES de reiniciar
venv/bin/python deploy/audit_schema.py             # debe decir "sin problemas"
venv/bin/python deploy/audit_schema.py --autoprueba  # el auditor no está ciego: VERDE
venv/bin/python deploy/audit_schema.py --pasos      # el checklist quedó completo

# 4) Verificación crítica ANTES de reiniciar (con el flag apagado).
#    AUTO_CREATE_TABLES=false NO es opcional: `import main` ejecuta el create_all, así que
#    sin esa variable el chequeo CREA las tablas que dice estar verificando.
MONZA_CONTAB_ENABLED=false AUTO_CREATE_TABLES=false venv/bin/python -c "
import main
from models.models import Base
PREF_TABLA = ('monza_cont', 'monza_tes', 'monza_emb_', 'monza_sii_')
PREF_RUTA  = ('/api/monza/contabilidad', '/api/monza/tesoreria',
              '/api/monza/compras-contab', '/api/monza/embarques-pricing',
              '/api/monza/sii-libro', '/api/monza/wasabil')
rutas = [r.path for r in main.app.routes
         if getattr(r, 'path', '').startswith(PREF_RUTA)]
tablas = [t for t in Base.metadata.tables if t.startswith(PREF_TABLA)]
assert not rutas, f'rutas monza-contab montadas con el flag apagado: {rutas[:5]}'
assert not tablas, f'tablas monza-contab en metadata: {tablas}'
print('OK: gate cerrado ·', len(main.app.routes), 'rutas · 0 monza-contab')"
# Lo PRIMERO que se verifica son las RUTAS, porque es lo que el flag gobierna de verdad
# (deben dar 404). Lo segundo, la metadata: es la comprobación que atrapa un import
# incondicional colado — y ojo, verifica la METADATA DEL PROCESO, no la BD. Si un init_db
# de Monza ya creó esas 25 tablas en la base, este chequeo igual pasa y no hay nada que
# arreglar: tablas vacías con las rutas en 404 no hacen daño.
#
# Historia (2026-08-08): entre agosto y esta fecha este paso FALLABA en toda promoción.
# El Libro de compras del SII de Monza se montaba fuera del gate, y su models.py importa
# tesorería/contabilidad/compras/pricing de Monza para cerrar el grafo de FKs (sin eso el
# arranque revienta con NoReferencedTableError y caen las DOS marcas): resultado, 25 tablas
# en la metadata con el flag apagado y un AssertionError que se leía como "el gate se
# filtró". Se arregló donde correspondía —el router del libro Monza ahora obedece el mismo
# flag que su pantalla y su Tesorería—, no bajándole el precio a la verificación.

# 5) Frontend + reinicio
cd frontend-src && npm run build     # lee .env.local automáticamente
cp -r dist/assets/* ../assets/ && cp dist/index.html ../index.html
pm2 restart machparts-api

# 6) Verificar: MachParts 200 · Monza core 200 · monza/compras-contab 404
#    · monza/sii-libro 404 (desde 2026-08-08 también obedece al gate)
#    · y el ESTRENO del Libro de Compras del SII de Grupo AM, que es el módulo nuevo y el
#      único que empieza a trabajar solo: docs/CHECKLIST-DEPLOY-2026-07-20.md §4.b
```

## Lecciones aprendidas (jul 2026)

- **Las migraciones de `backend/migrations/` hay que CORRERLAS.** `create_all` crea tablas
  nuevas pero **no agrega columnas a tablas existentes**. `add_despacho_guia_fields.py`
  venía en el paquete y nunca se ejecutó → `error 1054` → **HTTP 500 en cascada** que dejó
  invisibles el detalle de Despachos, su botón "Crear Despacho" y el detalle de Ventas.
  El síntoma no se parecía en nada a la causa. Ante un 500 raro: `deploy/audit_schema.py`.
- **`main.py` ejecuta `Base.metadata.create_all()` al importarse**: un simple
  `python -c "import main"` ya crea tablas (gate: `AUTO_CREATE_TABLES=false`).
- **Un auditor callado y un auditor ciego se ven igual.** `audit_schema.py` decía «sin
  problemas» mirando 58 de 95 tablas: registraba `models.models` + `monza_models` y no los
  11 módulos satélite (contabilidad, tesorería, compras/CxP, pricing, DTE y recepción
  nacional de las 2 marcas), que declaran sus modelos en `<paquete>/models.py`. Se
  corrigió el 2026-07-30 y quedó con `--autoprueba`: planta defectos falsos en memoria y
  exige que el auditor los reporte. **Correr `--autoprueba` en cada deploy**, antes de
  creerle al «sin problemas». Su versión del checklist es `--pasos`: compara el árbol de
  archivos contra `docs/CHECKLIST-DEPLOY-2026-07-20.md` y contra `backend/.env.example`,
  así que un módulo o una variable nuevos sin documentar se cantan solos.
- **Un paso obligatorio que sale rojo POR DISEÑO es peor que no tenerlo.** Cada tabla nueva
  que "la crea sola el `create_all` del arranque" nace DESPUÉS del auditor de esquema, que
  corre antes de reiniciar: el auditor la ve ausente, la cuenta como problema, sale `rc=1`
  y ordena «CORRER LAS MIGRACIONES», que no existen. Pasó con las 14 tablas del libro de
  compras del SII y con `monza_cotizacion_cierre`, y la salida del auditor se vuelve ruido
  que el operador aprende a saltarse — justo lo contrario de para lo que existe. **Regla:
  toda tabla nueva entra al deploy con un script propio** (un `init_db` acotado o una
  migración), aunque `create_all` pudiera crearla. Bonus: si una FK falla (errno 150), el
  error aparece durante las migraciones y no como crash-loop del backend al arrancar.
- **Una migración puede saltarse sola y salir con éxito.** `tesoreria/init_db.py` no crea
  `UNIQUE(egreso_id)` si encuentra duplicados legados: avisa por pantalla y devuelve rc=0.
  Depende de que alguien lea la salida — por eso el auditor ahora compara también los
  índices UNIQUE, no solo las columnas.
- El Excel del dueño (`Excel grupo am actual/`) está en `.gitignore`: contiene Libro
  diario / Mayor / Clientes. No versionarlo. `CLAUDE.md` también (tiene credenciales).
- `tar -T lista.txt` falla en silencio si la lista se generó en Windows (CRLF).
  Normalizar: `sed -i 's/\r$//' lista.txt`.
- En SQL `LIKE`, `_` es comodín: `monza_emb_%` matchea `monza_embarques`. Escapar
  (`monza\_emb\_%`) o comparar con `startswith` en Python.
