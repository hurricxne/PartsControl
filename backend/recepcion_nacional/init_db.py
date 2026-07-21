"""Crea/actualiza las tablas del módulo Recepción Nacional (standalone, idempotente).

Uso (desde backend/):
    python -m recepcion_nacional.init_db

1) Importa los modelos referenciados por las FK (oc_proveedor, oc_proveedor_items,
   items_cotizacion, users) para resolver las referencias y crea SOLO las tablas que
   falten (checkfirst=True): recepcion_nacional, recepcion_nacional_item.
2) Migra la columna ADITIVA que gobierna el camino físico (create_all NO altera
   tablas existentes):
     · oc_proveedor.tipo_origen  ('internacional' | 'nacional'; default 'internacional')
   Se co-ubica aquí porque tipo_origen es el flag del camino físico y este init_db
   corre primero en el deploy. El histórico queda 'internacional' por el DEFAULT.
Re-ejecutable sin efecto (idempotente).
"""
from sqlalchemy import text

from database import Base, engine
import models.models  # noqa: F401  (oc_proveedor, oc_proveedor_items, items_cotizacion, users)
from recepcion_nacional import models as _rn_models  # noqa: F401

TABLAS = ("recepcion_nacional", "recepcion_nacional_item")


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
    print(f"[recepcion_nacional] + {msg}")


def main() -> None:
    print("[recepcion_nacional] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)

    with engine.begin() as conn:
        for t in TABLAS:
            print(f"  - {t}: {'OK' if _tabla_existe(conn, t) else 'FALTA'}")

        # ── Migración aditiva: origen de la OC-Proveedor (nacional | internacional) ──
        if not _columna_existe(conn, "oc_proveedor", "tipo_origen"):
            _alter(conn,
                   "ALTER TABLE oc_proveedor ADD COLUMN tipo_origen VARCHAR(20) "
                   "NOT NULL DEFAULT 'internacional'",
                   "columna oc_proveedor.tipo_origen")
        else:
            print("[recepcion_nacional] = oc_proveedor.tipo_origen ya existe")
        if not _indice_existe(conn, "oc_proveedor", "ix_oc_proveedor_tipo_origen"):
            _alter(conn,
                   "CREATE INDEX ix_oc_proveedor_tipo_origen ON oc_proveedor (tipo_origen)",
                   "índice ix_oc_proveedor_tipo_origen")
        else:
            print("[recepcion_nacional] = índice ix_oc_proveedor_tipo_origen ya existe")

    print("[recepcion_nacional] Listo.")


if __name__ == "__main__":
    main()
