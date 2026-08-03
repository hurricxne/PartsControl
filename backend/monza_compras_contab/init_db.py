"""Crea/actualiza las tablas del módulo Compras / Cuentas por Pagar MonzaParts.

Uso (desde backend/):
    python -m monza_compras_contab.init_db

ORDEN DE DEPLOY: correr PRIMERO monza_recepcion_nacional/init_db (crea las tablas de
recepción + la columna monza_oc_proveedor.tipo_origen) y DESPUÉS este init_db, AMBOS
antes de reiniciar el backend.

Importa todos los modelos referenciados por las FK (users, monza_proveedores,
monza_embarques, monza_emb_pricing_gasto, monza_oc_proveedor, monza_cotizacion_items)
para que SQLAlchemy resuelva las referencias, y:

1) Crea SOLO las tablas que falten (checkfirst=True), incluida la nueva
   monza_cont_compra_item (costo por ítem de la compra nacional). NO recrea lo existente.
2) Migra la columna ADITIVA en la tabla existente (create_all NO altera tablas ya
   creadas):
     · monza_cont_compra.oc_proveedor_id  (FK suave a la OC-Proveedor; pista de cabecera)

Idempotente: re-ejecutable sin efecto (detección vía information_schema).
"""
from sqlalchemy import inspect, text

from database import Base, engine
import models.models                  # noqa: F401  users
import monza_models                   # noqa: F401  monza_proveedores, monza_oc_proveedor, monza_cotizacion_items...
import monza_embarques_pricing.models  # noqa: F401  monza_emb_pricing_gasto (FK destino)
from . import models as _m            # noqa: F401  monza_cont_*

TABLES = ("monza_cont_plan_cuenta", "monza_cont_compra",
          "monza_cont_egreso", "monza_cont_egreso_detalle", "monza_cont_compra_item")


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _indice_en_columna(conn, tabla: str, columna: str) -> bool:
    """¿Existe ALGÚN índice sobre esa columna? (por COLUMNA, no por nombre). En una BD
    fresca, create_all crea el índice de `index=True` con el autoname de SQLAlchemy
    (ix_monza_cont_compra_oc_proveedor_id); en una BD ya poblada lo crea este init_db.
    Detectar por columna evita un índice DUPLICADO en el bootstrap fresco (nombres
    distintos)."""
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _fk_en_columna(conn, tabla: str, columna: str) -> bool:
    """¿Existe ALGUNA FK saliente sobre esa columna? (por COLUMNA, no por nombre). En una
    BD fresca create_all crea la FK con un nombre autogenerado por MySQL
    (monza_cont_compra_ibfk_N) que NO es predecible; detectar por columna evita una FK
    DUPLICADA en el bootstrap."""
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.key_column_usage "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c "
            "AND referenced_table_name IS NOT NULL"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _alter(conn, sql: str, msg: str) -> None:
    conn.execute(text(sql))
    print(f"[monza_compras_contab] + {msg}")


def main() -> None:
    print("[monza_compras_contab] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    insp = inspect(engine)
    for t in TABLES:
        print(f"  - {t}: {'OK' if insp.has_table(t) else 'FALTA'}")

    # ── Migración aditiva: puntero suave a la OC-Proveedor (compra nacional) ──
    with engine.begin() as conn:
        if not _columna_existe(conn, "monza_cont_compra", "oc_proveedor_id"):
            _alter(conn,
                   "ALTER TABLE monza_cont_compra ADD COLUMN oc_proveedor_id INT NULL",
                   "columna monza_cont_compra.oc_proveedor_id")
        else:
            print("[monza_compras_contab] = monza_cont_compra.oc_proveedor_id ya existe")
        # Índice/FK detectados por COLUMNA (no por nombre): en una BD fresca create_all
        # ya los creó con sus autonames; detectar por columna evita duplicarlos. Nombre
        # alineado al autoname de SQLAlchemy para que los entornos frescos y migrados
        # converjan al mismo nombre.
        if not _indice_en_columna(conn, "monza_cont_compra", "oc_proveedor_id"):
            _alter(conn,
                   "CREATE INDEX ix_monza_cont_compra_oc_proveedor_id "
                   "ON monza_cont_compra (oc_proveedor_id)",
                   "índice ix_monza_cont_compra_oc_proveedor_id")
        else:
            print("[monza_compras_contab] = índice sobre monza_cont_compra.oc_proveedor_id ya existe")
        if not _fk_en_columna(conn, "monza_cont_compra", "oc_proveedor_id"):
            _alter(conn,
                   "ALTER TABLE monza_cont_compra ADD CONSTRAINT fk_monza_cont_compra_oc_proveedor_id "
                   "FOREIGN KEY (oc_proveedor_id) REFERENCES monza_oc_proveedor(id) ON DELETE SET NULL",
                   "FK monza_cont_compra.oc_proveedor_id")
        else:
            print("[monza_compras_contab] = FK sobre monza_cont_compra.oc_proveedor_id ya existe")

    print("[monza_compras_contab] Listo.")


if __name__ == "__main__":
    main()
