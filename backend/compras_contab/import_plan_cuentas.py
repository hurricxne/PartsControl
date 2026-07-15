"""Importa el plan de cuentas del Excel del dueño a la tabla cont_plan_cuenta.

Lee la hoja 'Plan de cuentas' del Excel de referencia y hace UPSERT por
(empresa, codigo). NO inventa cuentas: copia 1:1 lo que el dueño ya definió
(con su clasificación NIIF / NIC 7 / NIC 21).

Uso (desde backend/):
    python -m compras_contab.import_plan_cuentas
    python -m compras_contab.import_plan_cuentas "/ruta/al/archivo.xlsx"   (opcional)
"""
import glob
import os
import sys

from database import SessionLocal, Base, engine
import models.models  # noqa: F401  (FKs)
from . import models as _m  # noqa: F401
from .models import ContPlanCuenta

EMPRESA = "mineria"  # Grupo AM
SHEET = "Plan de cuentas"
DEFAULT_GLOB = os.path.join(
    os.path.dirname(__file__), "..", "..", "Excel grupo am actual", "*.xlsx"
)


def _b(v) -> bool:
    return str(v).strip().lower() in ("si", "sí", "true", "1", "x")


def _s(v):
    s = (str(v).strip() if v is not None else "")
    return s or None


def _flujo(v):
    s = (str(v).strip().upper() if v else "")
    if s in ("N/A", "NA", ""):
        return "NA"
    return s


def find_excel(arg=None) -> str:
    if arg:
        return arg
    matches = sorted(glob.glob(DEFAULT_GLOB))
    if not matches:
        raise SystemExit(f"No se encontró ningún .xlsx en {DEFAULT_GLOB}")
    return matches[0]


def main(path=None) -> None:
    import openpyxl
    Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura la tabla
    xlsx = find_excel(path)
    print(f"[plan_cuentas] Leyendo {xlsx} hoja '{SHEET}'…")
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb[SHEET]
    rows = list(ws.iter_rows(values_only=True))
    header = [(_s(c) or "").upper() for c in rows[0]]
    idx = {name: i for i, name in enumerate(header)}

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    db = SessionLocal()
    creadas = actualizadas = 0
    try:
        orden = 0
        for row in rows[1:]:
            codigo = _s(cell(row, "CODIGO"))
            nombre = _s(cell(row, "CUENTA"))
            if not codigo or not nombre:
                continue
            orden += 10
            data = dict(
                nombre=nombre,
                clase=_s(cell(row, "CLASE")),
                grupo=_s(cell(row, "GRUPO")),
                actividad_flujo=_flujo(cell(row, "ACTIVIDAD_FLUJO")),
                efectivo_equiv=_b(cell(row, "EFECTIVO_EQUIV")),
                monetaria=_b(cell(row, "MONETARIA")),
                moneda=(_s(cell(row, "MONEDA")) or "CLP"),
                requiere_auxiliar=_b(cell(row, "AUXILIAR")),
                cod_f29=_s(cell(row, "COD_F29")),
                notas=_s(cell(row, "NOTAS")),
                # Solo las cuentas HOJA (código x.y.zz, 2 puntos) son imputables; una
                # cuenta de título/mayor (ej "1" o "1.1") NO debe poder elegirse al imputar.
                imputable=(codigo.count(".") >= 2),
                activa=True,
                orden=orden,
            )
            existing = (
                db.query(ContPlanCuenta)
                .filter(ContPlanCuenta.empresa == EMPRESA, ContPlanCuenta.codigo == codigo)
                .first()
            )
            if existing:
                for k, v in data.items():
                    setattr(existing, k, v)
                actualizadas += 1
            else:
                db.add(ContPlanCuenta(empresa=EMPRESA, codigo=codigo, **data))
                creadas += 1
        db.commit()
        total = db.query(ContPlanCuenta).filter(ContPlanCuenta.empresa == EMPRESA).count()
        print(f"[plan_cuentas] OK — creadas {creadas}, actualizadas {actualizadas}, total en DB {total}")
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
