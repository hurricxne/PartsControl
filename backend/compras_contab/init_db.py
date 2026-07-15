"""Crea las tablas del módulo Compras / Cuentas por Pagar (standalone).

Uso (desde backend/):
    python -m compras_contab.init_db

Importa todos los modelos referenciados por las FK (proveedores, embarques,
facturas_proveedor, users, emb_pricing_gasto) para que SQLAlchemy resuelva las
referencias, y crea SOLO las tablas que falten (checkfirst=True). NO modifica
ninguna tabla existente. No depende del create_all de main.py: sirve para probar
el módulo en aislamiento antes de cablearlo.
"""
from sqlalchemy import inspect

from database import Base, engine
import models.models            # noqa: F401  proveedores, embarques, users, facturas_proveedor...
import embarques_pricing.models  # noqa: F401  emb_pricing_gasto (FK destino)
from . import models as _compras_models  # noqa: F401  cont_plan_cuenta, cont_compra, cont_egreso(_detalle)

TABLES = ("cont_plan_cuenta", "cont_compra", "cont_egreso", "cont_egreso_detalle")


def main() -> None:
    print("[compras_contab] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    insp = inspect(engine)
    for t in TABLES:
        print(f"  - {t}: {'OK' if insp.has_table(t) else 'FALTA'}")
    print("[compras_contab] Listo.")


if __name__ == "__main__":
    main()
