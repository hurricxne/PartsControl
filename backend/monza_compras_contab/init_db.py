"""Crea las tablas del módulo Compras / Cuentas por Pagar MonzaParts (standalone).

Uso (desde backend/):
    python -m monza_compras_contab.init_db

Importa todos los modelos referenciados por las FK (users, monza_proveedores,
monza_embarques, monza_emb_pricing_gasto) para que SQLAlchemy resuelva las
referencias, y crea SOLO las tablas que falten (checkfirst=True). NO modifica
ninguna tabla existente. Idempotente: se puede correr las veces que sea.
"""
from sqlalchemy import inspect

from database import Base, engine
import models.models                  # noqa: F401  users
import monza_models                   # noqa: F401  monza_proveedores, monza_embarques...
import monza_embarques_pricing.models  # noqa: F401  monza_emb_pricing_gasto (FK destino)
from . import models as _m            # noqa: F401  monza_cont_*

TABLES = ("monza_cont_plan_cuenta", "monza_cont_compra",
          "monza_cont_egreso", "monza_cont_egreso_detalle")


def main() -> None:
    print("[monza_compras_contab] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    insp = inspect(engine)
    for t in TABLES:
        print(f"  - {t}: {'OK' if insp.has_table(t) else 'FALTA'}")
    print("[monza_compras_contab] Listo.")


if __name__ == "__main__":
    main()
