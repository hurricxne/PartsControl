"""Crea las tablas del módulo Tesorería MonzaParts (standalone, idempotente).

Uso (desde backend/):
    python -m monza_tesoreria.init_db

Importa todos los modelos referenciados por las FK (users, monza_cont_egreso,
monza_cont_adelanto, monza_cont_cobranza) para que SQLAlchemy resuelva las referencias,
y crea SOLO las tablas que falten (checkfirst=True). NO modifica ninguna tabla existente.
"""
from sqlalchemy import inspect

from database import Base, engine
import models.models              # noqa: F401  users
import monza_models               # noqa: F401  monza_cotizaciones...
import monza_contabilidad.models  # noqa: F401  monza_cont_adelanto / monza_cont_cobranza (FK destino)
import monza_compras_contab.models  # noqa: F401  monza_cont_egreso (FK destino)
from . import models as _m        # noqa: F401  monza_tes_*

TABLES = ("monza_tes_cuenta_bancaria", "monza_tes_cartola",
          "monza_tes_movimiento", "monza_tes_conciliacion",
          "monza_tes_conciliacion_ingreso")


def main() -> None:
    print("[monza_tesoreria] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    insp = inspect(engine)
    for t in TABLES:
        print(f"  - {t}: {'OK' if insp.has_table(t) else 'FALTA'}")
    print("[monza_tesoreria] Listo.")


if __name__ == "__main__":
    main()
