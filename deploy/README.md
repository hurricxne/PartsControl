# Despliegue / Promoción a producción

> Leer completo antes de promover. Hay una **divergencia deliberada** entre PROD y QA
> que, si se ignora, rompe MonzaParts en producción.

## Infraestructura (confirmada 2026-07-15)

| Entorno | URL | IP | SSH | ¿Repo git? |
|---|---|---|---|---|
| **PROD** | `appmachparts.cl` | `187.77.38.19` | `root` | **No** |
| **QA / staging** | `machparts.bigcode.cl` | `72.60.48.233` | `deploy` | **Sí** (este repo) |

Docroot en ambos: `/var/www/machparts.bigcode.cl/`. API: pm2 `machparts-api`, puerto 8002.

## ⚠️ PROD lleva SOLO la contabilidad de MachParts

Decisión del cliente (2026-07-15): en PROD se despliega el subsistema contable de
**MachParts**; el de **MonzaParts NO**. Pero el código está entrelazado, así que PROD
corre una **variante curada**:

**No se despliegan a PROD:**

- Backend: `monza_contabilidad/`, `monza_tesoreria/`, `monza_compras_contab/`, `monza_embarques_pricing/`
- Frontend: `MonzaComprasPage.tsx`, `MonzaEmbarquesPricingPage.tsx`, `MonzaVentasContabPage.tsx`, `MonzaTesoreriaPage.tsx`
- BD: las 17 tablas `monza_cont_*` / `monza_tes_*` / `monza_emb_pricing*`

**Excepción:** `frontend-src/src/monza-embarques-pricing/{types,compute}.ts` SÍ se despliegan
(quedan inertes: ninguna ruta los alcanza), porque `services/monzaApi.ts` importa sus tipos
y sin ellos el build falla con `TS2307`.

**Sí se aplican en PROD** las 6 columnas aditivas de Monza (`monza_cotizaciones`:
`pct_adelanto`, `adelanto_verificado`, `guia_firmada`, `guia_firmada_archivo`;
`monza_despachos`: `guia_firmada`, `guia_firmada_archivo`). Son inertes (sin sus routers
nadie las lee) pero **imprescindibles**: `monza_models.py` las declara, y si el modelo
declara una columna que la tabla no tiene, MariaDB responde `error 1054` y **cualquier
SELECT sobre esa tabla revienta con HTTP 500 en cascada**.

### 🔴 En cada promoción a PROD hay que RE-APLICAR el curado

`main.py`, `App.tsx` y `MonzaLayout.tsx` contienen referencias a los módulos Monza-contab.
Si se copian de QA tal cual, la API **no arranca** y el menú de Monza queda roto:

```bash
python3 deploy/curar_prod_monza.py     # neutraliza esas referencias (idempotente)
```

## Procedimiento de promoción

```bash
# 0) RESPALDO (no negociable)
mysqldump -u<user> -p<pass> --single-transaction --routines --triggers machparts_db \
  > /root/backups-migracion/machparts_db-$(date +%Y%m%d-%H%M).sql
tar czf /root/backups-migracion/codigo-$(date +%Y%m%d-%H%M).tar.gz \
  --exclude=backend/venv --exclude=frontend-src/node_modules --exclude=frontend-src/dist \
  backend frontend-src assets index.html
cp backend/.env /root/backups-migracion/env-$(date +%Y%m%d-%H%M).bak

# 1) Código (nunca sobrescribir .env / uploads / results / venv)

# 2) Curado obligatorio
python3 deploy/curar_prod_monza.py

# 3) Migraciones: create_all NO agrega columnas a tablas existentes
venv/bin/python -m migrations.add_despacho_guia_fields
venv/bin/python deploy/audit_schema.py          # debe decir "sin problemas"

# 4) Plan de cuentas (si aplica; ver backend/compras_contab/README.md)
venv/bin/python -m compras_contab.import_plan_cuentas

# 5) Frontend + reinicio
cd frontend-src && npm run build
cp -r dist/assets/* ../assets/ && cp dist/index.html ../index.html
pm2 restart machparts-api

# 6) Verificar MachParts Y MonzaParts (que Monza siga en 200)
```

## Lecciones aprendidas (jul 2026)

- **Las migraciones de `backend/migrations/` hay que CORRERLAS.** `create_all` crea tablas
  nuevas pero **no agrega columnas a tablas existentes**. `add_despacho_guia_fields.py`
  venía en el paquete y nunca se ejecutó → `error 1054` → **HTTP 500 en cascada** que dejó
  invisibles el detalle de Despachos, su botón "Crear Despacho" y el detalle de Ventas.
  El síntoma no se parecía en nada a la causa. Ante un 500 raro: correr `deploy/audit_schema.py`.
- **`main.py` ejecuta `Base.metadata.create_all()` al importarse** (línea ~39): un simple
  `python -c "import main"` ya crea tablas. Tenerlo presente al verificar.
- El Excel del dueño (`Excel grupo am actual/`) está en `.gitignore`: contiene Libro
  diario / Mayor / Clientes. No versionarlo.
- `tar -T lista.txt` falla en silencio si la lista se generó en Windows (CRLF).
  Normalizar: `sed -i 's/\r$//' lista.txt`.
