# Audita columnas declaradas en los modelos vs columnas reales en MariaDB.
# Reporta faltantes (causa de errores 1054 "Unknown column" -> HTTP 500).
import os, sys
sys.path.insert(0, "/var/www/machparts.bigcode.cl/backend")
os.chdir("/var/www/machparts.bigcode.cl/backend")

from sqlalchemy import create_engine, inspect
from database import engine as _eng


from models.models import Base  # registra todos los modelos core
try:
    import monza_models  # registra modelos monza (comparten Base?)
except Exception as e:
    print("(monza_models no cargado:", e, ")")

url = None
eng = _eng
insp = inspect(eng)
real_tables = set(insp.get_table_names())

problemas = 0
for tname, table in sorted(Base.metadata.tables.items()):
    if tname not in real_tables:
        print(f"[TABLA FALTANTE] {tname}")
        problemas += 1
        continue
    real_cols = {c["name"] for c in insp.get_columns(tname)}
    model_cols = {c.name for c in table.columns}
    faltan = model_cols - real_cols
    if faltan:
        problemas += 1
        print(f"[COLUMNAS FALTANTES] {tname}:")
        for c in sorted(faltan):
            col = table.columns[c]
            print(f"    - {c}  ({col.type})  nullable={col.nullable}")

print("\n=== sin problemas ===" if problemas == 0 else f"\n=== {problemas} tabla(s) con desalineación ===")
