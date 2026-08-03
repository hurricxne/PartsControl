"""Inicialización standalone del módulo Wasabil DTE de MonzaParts.

Patrón de la casa (wasabil_dte/init_db.py, compras_contab): crea la tabla
`monza_wasabil_dte` si no existe y agrega columnas/índices nuevos si la tabla ya
existía de una versión anterior (el `create_all` de arranque NO altera tablas
existentes). Idempotente: correr las veces que sea.

Migración de la Fase 6 (facturas electrónicas 33):
  · columna  monza_wasabil_dte.factura_id       (FK a monza_cont_factura_cliente)
  · índice   ix_monza_wasabil_dte_factura_id
  · UNIQUE   uq_monza_wasabil_dte_factura       (ancla anti doble emisión por factura)

DEPLOY (orden OBLIGATORIO — regla de la casa):
  1. `./venv/bin/python -m monza_contabilidad.init_db`  ← crea monza_cont_factura_cliente
  2. `./venv/bin/python monza_wasabil_dte/init_db.py`   ← este script (la FK exige (1))
  3. Recién entonces reiniciar el backend.

Corre con:  cd backend && ./venv/bin/python monza_wasabil_dte/init_db.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import inspect, text  # noqa: E402

from database import engine, Base  # noqa: E402
# Registra en el metadata las tablas destino de las FK ANTES del create_all
# (patrón monza_contabilidad/init_db.py: sin esto, resolver la FK revienta con
# NoReferencedTableError aunque las tablas existan en la BD).
import models.models  # noqa: E402,F401  (users)
import monza_models  # noqa: E402,F401  (monza_despachos)
import monza_contabilidad.models  # noqa: E402,F401  (monza_cont_factura_cliente)
from monza_wasabil_dte import models  # noqa: E402  (registra la tabla en Base.metadata)

TABLA = "monza_wasabil_dte"
TABLA_FACTURA = "monza_cont_factura_cliente"


def _tabla_existe(conn, tabla: str) -> bool:
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.tables "
             "WHERE table_schema = DATABASE() AND table_name = :t"),
        {"t": tabla},
    ).scalar())


def _indice_en_columna(conn, tabla: str, columna: str) -> bool:
    """¿Existe ALGÚN índice sobre esa columna? (por COLUMNA, no por nombre).

    En una BD fresca `create_all` ya creó el índice de `index=True` con el autoname
    de SQLAlchemy; en una BD ya poblada lo crea este script. Detectar por columna
    evita un índice DUPLICADO en el bootstrap fresco (nombres distintos)."""
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.statistics "
             "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": tabla, "c": columna},
    ).scalar())


def _indice_por_nombre(conn, tabla: str, indice: str) -> bool:
    """Los UNIQUE con nombre EXPLÍCITO sí se detectan por nombre: create_all los crea
    con ese mismo nombre, así que fresco y migrado convergen."""
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.statistics "
             "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"),
        {"t": tabla, "i": indice},
    ).scalar())


def _fk_en_columna(conn, tabla: str, columna: str) -> bool:
    """¿Existe ALGUNA FK saliente sobre esa columna? (por COLUMNA, no por nombre): en
    una BD fresca MySQL le pone un autoname impredecible (monza_wasabil_dte_ibfk_N)."""
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.key_column_usage "
             "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c "
             "AND referenced_table_name IS NOT NULL"),
        {"t": tabla, "c": columna},
    ).scalar())


def init():
    # La FK factura_id apunta a monza_cont_factura_cliente: si esa tabla no existe,
    # MySQL falla con errno 150 y un mensaje críptico. Fallar ANTES y en claro.
    with engine.connect() as conn:
        if not _tabla_existe(conn, TABLA_FACTURA):
            raise SystemExit(
                f"FALTA la tabla {TABLA_FACTURA}: corre PRIMERO "
                "`./venv/bin/python -m monza_contabilidad.init_db` y vuelve a intentar "
                "(la FK monza_wasabil_dte.factura_id la necesita)."
            )

    # create_all con checkfirst (default): si la tabla ya existe no la toca.
    Base.metadata.create_all(bind=engine, tables=[models.MonzaWasabilDte.__table__])
    insp = inspect(engine)
    columnas = {c["name"] for c in insp.get_columns(TABLA)}
    # Columnas agregadas después de la primera versión de la tabla. La tabla nació
    # COMPLETA en Fase 5 (claim en_vuelo_desde incluido); la Fase 6 (facturas 33)
    # agrega el segundo origen, factura_id, con su propio índice, FK y UNIQUE.
    nuevas = {
        "factura_id": f"ALTER TABLE {TABLA} ADD COLUMN factura_id INT NULL",
    }
    with engine.begin() as conn:
        for nombre, ddl in nuevas.items():
            if nombre not in columnas:
                conn.execute(text(ddl))
                print(f"columna agregada: {nombre}")
            else:
                print(f"= columna ya existe: {nombre}")

        # Índice de búsqueda de factura_id (el `index=True` del modelo). Va ANTES del
        # UNIQUE a propósito: así la BD migrada queda con los MISMOS dos índices que
        # una BD fresca creada por create_all (el UNIQUE también indexa la columna,
        # y si se creara primero este chequeo por columna saltaría el ix_).
        if not _indice_en_columna(conn, TABLA, "factura_id"):
            conn.execute(text(
                f"CREATE INDEX ix_{TABLA}_factura_id ON {TABLA} (factura_id)"))
            print(f"índice agregado: ix_{TABLA}_factura_id")
        else:
            print("= ya hay índice sobre factura_id")

        if not _fk_en_columna(conn, TABLA, "factura_id"):
            conn.execute(text(
                f"ALTER TABLE {TABLA} ADD CONSTRAINT fk_{TABLA}_factura_id "
                f"FOREIGN KEY (factura_id) REFERENCES {TABLA_FACTURA}(id)"))
            print(f"FK agregada: fk_{TABLA}_factura_id")
        else:
            print("= ya hay FK sobre factura_id")

        # Índices únicos anti doble emisión: create_all los crea con la tabla, pero si
        # una versión vieja de la tabla existiera sin ellos, se agregan aquí (create_all
        # no agrega índices a tablas existentes).
        if not _indice_por_nombre(conn, TABLA, "uq_monza_wasabil_dte_despacho"):
            conn.execute(text(
                f"ALTER TABLE {TABLA} ADD CONSTRAINT uq_monza_wasabil_dte_despacho "
                "UNIQUE (despacho_id)"))
            print("índice único agregado: uq_monza_wasabil_dte_despacho")
        else:
            print("= índice único ya existe: uq_monza_wasabil_dte_despacho")

        # Fase 6: 1 factura electrónica (33) por factura de cliente.
        if not _indice_por_nombre(conn, TABLA, "uq_monza_wasabil_dte_factura"):
            conn.execute(text(
                f"ALTER TABLE {TABLA} ADD CONSTRAINT uq_monza_wasabil_dte_factura "
                "UNIQUE (factura_id)"))
            print("índice único agregado: uq_monza_wasabil_dte_factura")
        else:
            print("= índice único ya existe: uq_monza_wasabil_dte_factura")
    print("monza_wasabil_dte OK")


if __name__ == "__main__":
    init()
