"""Crea/actualiza las tablas del módulo Recepción Nacional MonzaParts
(standalone, idempotente).

Uso (desde backend/):
    ./venv/bin/python -m monza_recepcion_nacional.init_db

1) Importa los modelos referenciados por las FK (monza_oc_proveedor,
   monza_cotizacion_items, users) para resolver las referencias y crea SOLO las
   tablas que falten (checkfirst=True): monza_recepcion_nacional,
   monza_recepcion_nacional_item.
2) Migra la columna ADITIVA que gobierna el camino físico (create_all NO altera
   tablas existentes):
     · monza_oc_proveedor.tipo_origen  ('internacional' | 'nacional';
       default 'internacional')
   Se co-ubica aquí porque tipo_origen es el flag del camino físico y este init_db
   corre PRIMERO en el deploy (antes que monza_compras_contab/init_db). El
   histórico queda 'internacional' por el DEFAULT. El ALTER va por SQL crudo
   contra information_schema (no depende del atributo del modelo).
Re-ejecutable sin efecto (idempotente).
"""
from sqlalchemy import text

from database import Base, engine
import models.models  # noqa: F401  (users: FK destino de usuario_id)
import monza_models  # noqa: F401  (monza_oc_proveedor, monza_cotizacion_items)
from monza_recepcion_nacional import models as _mrn_models  # noqa: F401

TABLAS = ("monza_recepcion_nacional", "monza_recepcion_nacional_item")


def _tabla_existe(conn, tabla: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    ).scalar()
    return bool(row)


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _indice_existe(conn, tabla: str, indice: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
        ),
        {"t": tabla, "i": indice},
    ).scalar()
    return bool(row)


def _alter(conn, sql: str, msg: str) -> None:
    conn.execute(text(sql))
    print(f"[monza_recepcion_nacional] + {msg}")


def main() -> None:
    print("[monza_recepcion_nacional] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)

    with engine.begin() as conn:
        for t in TABLAS:
            print(f"  - {t}: {'OK' if _tabla_existe(conn, t) else 'FALTA'}")

        # ── Migración aditiva: origen de la OC-Proveedor (nacional | internacional) ──
        if not _columna_existe(conn, "monza_oc_proveedor", "tipo_origen"):
            _alter(conn,
                   "ALTER TABLE monza_oc_proveedor ADD COLUMN tipo_origen VARCHAR(20) "
                   "NOT NULL DEFAULT 'internacional'",
                   "columna monza_oc_proveedor.tipo_origen")
        else:
            print("[monza_recepcion_nacional] = monza_oc_proveedor.tipo_origen ya existe")
        if not _indice_existe(conn, "monza_oc_proveedor", "ix_monza_oc_proveedor_tipo_origen"):
            _alter(conn,
                   "CREATE INDEX ix_monza_oc_proveedor_tipo_origen "
                   "ON monza_oc_proveedor (tipo_origen)",
                   "índice ix_monza_oc_proveedor_tipo_origen")
        else:
            print("[monza_recepcion_nacional] = índice ix_monza_oc_proveedor_tipo_origen ya existe")

    print("[monza_recepcion_nacional] Listo.")


if __name__ == "__main__":
    main()
