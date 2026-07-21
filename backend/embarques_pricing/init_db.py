"""Crea/actualiza las tablas del módulo Embarques Pricing (idempotente).

Uso (desde backend/):
    python -m embarques_pricing.init_db

1) Crea SOLO las tablas que falten (checkfirst=True). En una BD nueva quedan ya
   con la columna peso_origen (models.py la declara). No recrea lo existente.
2) Migra la columna ADITIVA en la tabla existente (create_all NO altera tablas
   ya creadas):
     · emb_pricing_item.peso_origen  (origen del peso del prorrateo: auto|manual)
Las tablas del módulo y sus datos se conservan intactos.
"""
from sqlalchemy import text

from database import Base, engine
# Registra en el metadata las tablas referenciadas por las FK (users, embarques,
# embarque_items, items_cotizacion) y los modelos propios, antes del create_all.
import models.models  # noqa: F401  (users, embarques, embarque_items, items_cotizacion)
from embarques_pricing import models as _ep_models  # noqa: F401

TABLAS = ("emb_pricing", "emb_pricing_gasto", "emb_pricing_item")


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _alter(conn, sql: str, msg: str) -> None:
    conn.execute(text(sql))
    print(f"[embarques_pricing] + {msg}")


def main() -> None:
    print("[embarques_pricing] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)

    with engine.begin() as conn:
        for t in TABLAS:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = :t"
                ),
                {"t": t},
            ).scalar()
            print(f"  - {t}: {'OK' if row else 'FALTA'}")

        # ── Migración aditiva: peso editable por ítem ───────────────────────────
        # Espejo de fob_origen. 'auto' = el peso se lee de la cotización; 'manual'
        # = Contabilidad lo fijó a mano (re-prorratea el flete). Las filas viejas
        # quedan en NULL y el backend las trata como 'auto' (s.peso_origen or ...).
        if not _columna_existe(conn, "emb_pricing_item", "peso_origen"):
            _alter(conn,
                   "ALTER TABLE emb_pricing_item "
                   "ADD COLUMN peso_origen VARCHAR(20) DEFAULT 'auto'",
                   "columna emb_pricing_item.peso_origen")
        else:
            print("[embarques_pricing] = emb_pricing_item.peso_origen ya existe")

    print("[embarques_pricing] Listo.")


if __name__ == "__main__":
    main()
