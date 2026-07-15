"""Crea las tablas del módulo Tesorería (standalone).

Uso (desde backend/):
    python -m tesoreria.init_db

Importa los modelos referenciados por las FK (users, cont_egreso, cont_cobranza) para
resolver las referencias y crea SOLO las tablas que falten (checkfirst=True). NO
modifica ninguna tabla existente: las conc_* del antiguo módulo Conciliación Bancaria
se conservan tal cual (con sus datos); lo único nuevo es conc_conciliacion_ingreso.
"""
from sqlalchemy import inspect

from database import Base, engine
import models.models            # noqa: F401  users / cont_factura_cliente / cont_cobranza
import compras_contab.models    # noqa: F401  cont_egreso (FK destino de conc_conciliacion)
from . import models as _tes_models  # noqa: F401  conc_* + conc_conciliacion_ingreso

TABLES = (
    "conc_cuenta_bancaria", "conc_cartola", "conc_movimiento",
    "conc_conciliacion", "conc_conciliacion_ingreso",
)


def main() -> None:
    print("[tesoreria] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    insp = inspect(engine)
    for t in TABLES:
        print(f"  - {t}: {'OK' if insp.has_table(t) else 'FALTA'}")
    print("[tesoreria] Listo.")


if __name__ == "__main__":
    main()
