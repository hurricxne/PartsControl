"""Regresión del incidente 2026-09-25 — la factura se rechazaba a sí misma por FLOAT.

COT-2026-000195 (ZURICH, 4 líneas) no se podía facturar: las líneas sumaban 1.299.986
neto → 1.546.983 bruto, pero `monza_cotizaciones.total_bruto` era FLOAT y esta MariaDB
lo ENTREGA con 6 CIFRAS SIGNIFICATIVAS, así que la app leía la cabecera como 1.546.980.
El tope Σ brutos comparaba el neto+IVA exacto contra esa cabecera mutilada y devolvía
409 «La factura excede el total de la venta» por $3 que no existen. Medido en PROD:
79 de las 179 ventas sobre el millón quedaron infacturables, y el 100 % de las
bloqueadas está sobre el millón.

El arreglo deriva el total de la venta con la MISMA fórmula de la línea de factura
(precio × cantidad, half-up), con un cinturón: la cabecera solo se corrige cuando la
diferencia es explicable por la lectura del FLOAT. Estas pruebas fijan las tres cosas:
que el falso positivo desaparece, que la base es la de la factura y no el subtotal, y
que el cinturón no deja subir el cupo.

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


def _linea(precio, cantidad=1, subtotal="auto"):
    if subtotal == "auto":
        subtotal = None if precio is None else precio * cantidad
    return SimpleNamespace(precio_unitario_clp=precio, cantidad=cantidad, subtotal_clp=subtotal)


def _cot(total_bruto, lineas):
    return SimpleNamespace(total_bruto=total_bruto, items=lineas)


def test_paso_float_por_tramo():
    """La granularidad de lectura del FLOAT: nada bajo el millón, decena sobre el
    millón, centena sobre los diez millones."""
    assert _paso_float(999_999) == 0.0
    assert _paso_float(1_546_980) == 10.0
    assert _paso_float(11_945_600) == 100.0
    assert _paso_float(-1_546_980) == 10.0, "el signo no cambia la granularidad"


def test_bajo_el_millon_no_cambia_nada():
    """Hasta $999.999 el FLOAT se lee exacto: el tope es el mismo de siempre."""
    cot = _cot(714_000, [_linea(300_000), _linea(300_000)])
    assert _total_venta_para_tope(cot, IVA) == 714_000


def test_cot195_el_caso_del_incidente():
    """El caso real: cabecera leída 1.546.980, líneas 1.299.986 → el tope pasa a 1.546.983."""
    cot = _cot(1_546_980, [_linea(899_957), _linea(250_931), _linea(67_618), _linea(81_480)])
    assert _total_venta_para_tope(cot, IVA) == 1_546_983


def test_cot528_granularidad_de_centena():
    """Sobre los diez millones la lectura corre hasta la centena (aquí $25) y el tope
    tiene que absorberlo igual."""
    cot = _cot(11_945_600, [_linea(10_038_340)])
    assert _total_venta_para_tope(cot, IVA) == 11_945_625


def test_base_es_precio_por_cantidad_no_el_subtotal():
    """COT-2026-000494/000525: el subtotal se leyó truncado por DEBAJO de precio × qty.
    La factura suma precio × qty, así que el tope tiene que sumar lo mismo — sumando
    subtotales quedaba $5 corto y bloqueaba una venta que antes pasaba."""
    linea = _linea(612_347, cantidad=2, subtotal=1_224_690)   # real 1.224.694
    cot = _cot(1_457_390, [linea])                             # real 1.457.386
    assert _total_venta_para_tope(cot, IVA) == 1_457_386


def test_cinturon_las_lineas_no_pueden_subir_el_cupo():
    """Si las líneas se despegan de la cabecera por algo que NO es la lectura del
    FLOAT, manda la cabecera: esto no es una puerta para inflar la venta editando
    ítems."""
    cot = _cot(1_546_980, [_linea(1_700_000)])
    assert _total_venta_para_tope(cot, IVA) == 1_546_980


def test_linea_sin_precio_aporta_su_subtotal():
    """Legado sin precio unitario: la línea aporta su subtotal guardado."""
    cot = _cot(714_000, [_linea(None, subtotal=300_000), _linea(300_000)])
    assert _total_venta_para_tope(cot, IVA) == 714_000


def test_venta_sin_lineas_usa_la_cabecera():
    """Sin ítems no hay de dónde derivar."""
    assert _total_venta_para_tope(_cot(1_546_980, []), IVA) == 1_546_980


def test_lineas_sin_precio_ni_subtotal_usan_la_cabecera():
    """Un legado sin precio ni subtotal no inventa un tope en $0 (ver la regla del None
    en `_clonar_item_remanente`)."""
    cot = _cot(1_546_980, [_linea(None, subtotal=None), _linea(None, subtotal=None)])
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
