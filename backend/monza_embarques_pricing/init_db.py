"""Inicializa el módulo Embarques Pricing MonzaParts en la BD (idempotente).

Crea SOLO las 3 tablas nuevas (monza_emb_pricing / _gasto / _item). No altera
ninguna tabla existente (los embarques/ítems ya los crea Logística). Es seguro correr
las veces que sea; create_all no recrea lo que ya existe.

Correr una vez por entorno (local y producción):
    cd backend && python -m monza_embarques_pricing.init_db
"""
from sqlalchemy import text

from database import Base, engine
# Registra en el metadata las tablas referenciadas por las FK (users, monza_embarques)
# y los modelos propios del módulo, antes del create_all.
import models.models  # noqa: F401  (users, etc.)
import monza_models  # noqa: F401  (monza_embarques, monza_embarque_items, etc.)
from monza_embarques_pricing import models as _ep_models  # noqa: F401

TABLAS = ("monza_emb_pricing", "monza_emb_pricing_gasto", "monza_emb_pricing_item")


def _tabla_existe(conn, tabla: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    ).scalar()
    return bool(row)


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for t in TABLAS:
            estado = "OK" if _tabla_existe(conn, t) else "FALTA"
            print(f"[monza_embarques_pricing] tabla {t}: {estado}")
    print("[monza_embarques_pricing] init OK.")


if __name__ == "__main__":
    main()
