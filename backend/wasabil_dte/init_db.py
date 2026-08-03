"""Inicialización standalone del módulo Wasabil DTE (patrón de compras_contab).

Crea la tabla `wasabil_dte` si no existe y agrega columnas nuevas si la tabla ya
existía de una versión anterior (el `create_all` de arranque NO agrega columnas).
Idempotente: correr las veces que sea.

Corre con:  cd backend && ./venv/bin/python wasabil_dte/init_db.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import inspect, text  # noqa: E402

from database import engine, Base  # noqa: E402
from wasabil_dte import models  # noqa: E402  (registra la tabla en Base.metadata)


def init():
    Base.metadata.create_all(bind=engine, tables=[models.WasabilDte.__table__])
    insp = inspect(engine)
    columnas = {c["name"] for c in insp.get_columns("wasabil_dte")}
    # Columnas agregadas después de la primera versión de la tabla
    nuevas = {
        "en_vuelo_desde": "ALTER TABLE wasabil_dte ADD COLUMN en_vuelo_desde DATETIME NULL",
    }
    with engine.begin() as conn:
        for nombre, ddl in nuevas.items():
            if nombre not in columnas:
                conn.execute(text(ddl))
                print(f"columna agregada: {nombre}")
        # Índices únicos agregados después de la primera versión (create_all no los
        # agrega a tablas existentes). Fase B: candado anti doble emisión por factura.
        indices = {i["name"] for i in insp.get_indexes("wasabil_dte")}
        if "uq_wasabil_dte_factura" not in indices:
            conn.execute(text(
                "ALTER TABLE wasabil_dte ADD CONSTRAINT uq_wasabil_dte_factura "
                "UNIQUE (factura_id)"))
            print("índice único agregado: uq_wasabil_dte_factura")
    print("wasabil_dte OK")


if __name__ == "__main__":
    init()
