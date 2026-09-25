"""Regresión del incidente 2026-09-25 — la factura se rechazaba a sí misma por FLOAT.

COT-2026-000195 (ZURICH, 4 líneas) no se podía facturar: las líneas sumaban 1.299.986
neto → 1.546.983 bruto, pero `monza_cotizaciones.total_bruto` es FLOAT y esta MariaDB
solo conserva 6 CIFRAS SIGNIFICATIVAS, así que la cabecera quedó guardada en 1.546.980
(`CAST(1546983 AS FLOAT)` → `1546980`, medido en PROD). El tope Σ brutos comparaba el
neto+IVA exacto contra esa cabecera mutilada y devolvía 409 «La factura excede el total
de la venta» por $3 que no existen. Medido en PROD: 79 de las 179 ventas sobre el
millón quedaron infacturables, y el 100 % de las bloqueadas está sobre el millón.

El arreglo deriva el total de la venta de las líneas CONGELADAS (la misma base de la
que salen las líneas de la factura), con un cinturón: la cabecera solo se corrige
cuando la diferencia es explicable por la pérdida del FLOAT. Estas pruebas fijan las
dos mitades — que el falso positivo desaparece Y que el cinturón no deja subir el cupo.

No toca la BD: `_total_venta_para_tope` es una función pura sobre la cotización.

Corre con:
  cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_tope_float_cabecera.py -q
  (o directo: ./venv/bin/python monza_contabilidad/tests/test_tope_float_cabecera.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from monza_contabilidad.router import _paso_float, _total_venta_para_tope  # noqa: E402

IVA = 0.19


def _cot(total_bruto, subtotales):
    return SimpleNamespace(
        total_bruto=total_bruto,
        items=[SimpleNamespace(subtotal_clp=s) for s in subtotales],
    )


def test_paso_float_por_tramo():
    """La granularidad del FLOAT: nada bajo el millón, decena sobre el millón,
    centena sobre los diez millones."""
    assert _paso_float(999_999) == 0.0
    assert _paso_float(1_546_980) == 10.0
    assert _paso_float(11_945_600) == 100.0
    assert _paso_float(-1_546_980) == 10.0, "el signo no cambia la granularidad"


def test_bajo_el_millon_no_cambia_nada():
    """Hasta $999.999 el FLOAT guarda el peso exacto: la cabecera manda igual que antes."""
    cot = _cot(714_000, [300_000, 300_000])
    assert _total_venta_para_tope(cot, IVA) == 714_000


def test_cot195_el_caso_del_incidente():
    """El caso real: cabecera 1.546.980, líneas 1.299.986 → el tope pasa a 1.546.983."""
    cot = _cot(1_546_980, [899_957, 250_931, 67_618, 81_480])
    assert _total_venta_para_tope(cot, IVA) == 1_546_983


def test_cot528_granularidad_de_centena():
    """Sobre los diez millones el FLOAT corre hasta la centena (aquí $25) y el tope
    tiene que absorberlo igual."""
    cot = _cot(11_945_600, [10_038_340])
    assert _total_venta_para_tope(cot, IVA) == 11_945_625


def test_cinturon_las_lineas_no_pueden_subir_el_cupo():
    """Si las líneas se despegan de la cabecera por algo que NO es la pérdida del
    FLOAT, manda la cabecera: esto no es una puerta para inflar la venta editando
    ítems."""
    cot = _cot(1_546_980, [1_700_000])
    assert _total_venta_para_tope(cot, IVA) == 1_546_980


def test_venta_sin_lineas_usa_la_cabecera():
    """Sin ítems no hay de dónde derivar."""
    assert _total_venta_para_tope(_cot(1_546_980, []), IVA) == 1_546_980


def test_subtotales_nulos_usan_la_cabecera():
    """Un legado con subtotal NULL no inventa un tope en $0 (ver la regla del None en
    `_clonar_item_remanente`)."""
    cot = _cot(1_546_980, [None, None])
    assert _total_venta_para_tope(cot, IVA) == 1_546_980


if __name__ == "__main__":
    fallos = 0
    for nombre, fn in sorted(list(globals().items())):
        if not nombre.startswith("test_"):
            continue
        try:
            fn()
            print(f"  OK   {nombre}")
        except AssertionError as e:
            fallos += 1
            print(f"  FALLA {nombre}: {e}")
    print(("TODO OK" if not fallos else f"{fallos} FALLA(S)"))
    sys.exit(1 if fallos else 0)
