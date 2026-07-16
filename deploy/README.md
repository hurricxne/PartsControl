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
| `MONZA_CONTAB_ENABLED` (`backend/.env`) | *(ausente = true)* | `false` | Con `false`, `main.py` **ni importa** los 4 módulos Monza-contab → sus modelos no se cargan → `create_all` **no crea** sus 17 tablas → sus rutas responden 404 |
| `VITE_MONZA_CONTAB` (`frontend-src/.env.local`, build-time) | *(ausente = true)* | `false` | Quita las 4 rutas de `App.tsx` y sus entradas del menú en `MonzaLayout` |
| `AUTO_CREATE_TABLES` (`backend/.env`) | *(ausente = true)* | *(ausente = true)* | Gate del `create_all` que corre al importar `main.py` |

Además, `config.py` hace **fail-fast**: si `SECRET_KEY` es la default
(`changeme-in-production`), la app **no arranca**.

**Columnas Monza aditivas ya aplicadas en PROD** (`monza_cotizaciones`: `pct_adelanto`,
`adelanto_verificado`, `guia_firmada`, `guia_firmada_archivo`; `monza_despachos`:
`guia_firmada`, `guia_firmada_archivo`): inertes con el flag apagado, pero necesarias
porque `monza_models.py` las declara — sin ellas MariaDB lanza `error 1054` y cualquier
SELECT sobre esas tablas revienta con HTTP 500 en cascada.

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

# 3) Migraciones: create_all NO agrega columnas a tablas existentes
venv/bin/python -m migrations.add_despacho_guia_fields
venv/bin/python deploy/audit_schema.py          # debe decir "sin problemas"

# 4) Verificación crítica ANTES de reiniciar (con el flag apagado):
venv/bin/python -c "
import main
from models.models import Base
tm = [t for t in Base.metadata.tables if t.startswith(('monza_cont','monza_tes','monza_emb_'))]
assert not tm, f'tablas monza-contab en metadata: {tm}'
print('OK: metadata sin monza-contab ·', len(main.app.routes), 'rutas')"

# 5) Frontend + reinicio
cd frontend-src && npm run build     # lee .env.local automáticamente
cp -r dist/assets/* ../assets/ && cp dist/index.html ../index.html
pm2 restart machparts-api

# 6) Verificar: MachParts 200 · Monza core 200 · monza/compras-contab 404
```

## Lecciones aprendidas (jul 2026)

- **Las migraciones de `backend/migrations/` hay que CORRERLAS.** `create_all` crea tablas
  nuevas pero **no agrega columnas a tablas existentes**. `add_despacho_guia_fields.py`
  venía en el paquete y nunca se ejecutó → `error 1054` → **HTTP 500 en cascada** que dejó
  invisibles el detalle de Despachos, su botón "Crear Despacho" y el detalle de Ventas.
  El síntoma no se parecía en nada a la causa. Ante un 500 raro: `deploy/audit_schema.py`.
- **`main.py` ejecuta `Base.metadata.create_all()` al importarse**: un simple
  `python -c "import main"` ya crea tablas (gate: `AUTO_CREATE_TABLES=false`).
- El Excel del dueño (`Excel grupo am actual/`) está en `.gitignore`: contiene Libro
  diario / Mayor / Clientes. No versionarlo. `CLAUDE.md` también (tiene credenciales).
- `tar -T lista.txt` falla en silencio si la lista se generó en Windows (CRLF).
  Normalizar: `sed -i 's/\r$//' lista.txt`.
- En SQL `LIKE`, `_` es comodín: `monza_emb_%` matchea `monza_embarques`. Escapar
  (`monza\_emb\_%`) o comparar con `startswith` en Python.
