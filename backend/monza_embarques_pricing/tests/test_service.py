"""Tests de la matemática pura del costo landed (sin BD).

Corre con:  cd backend && ./venv/bin/python monza_embarques_pricing/tests/test_service.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from monza_embarques_pricing.service import (  # noqa: E402
    calcular_landed, total_gastos_que_capitalizan, GASTOS_CATALOGO,
)

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def approx(a, b, tol=0.01):
    return abs(float(a) - float(b)) <= tol


# ── 1) Caso base: 2 ítems, prorrateo por peso (shipping) y por CIF (gastos) ──
items = [
    {"cantidad": 2, "peso_unit": 10, "fob_unit": 100, "tc_valor": 1000},  # peso 20, fob 200, fob_clp 200.000
    {"cantidad": 1, "peso_unit": 30, "fob_unit": 50,  "tc_valor": 1000},  # peso 30, fob 50,  fob_clp 50.000
]
shipping_total = 50000.0
total_gastos = 25000.0
rows, tot = calcular_landed(items, shipping_total, total_gastos)

check("n_items = 2", tot["n_items"] == 2)
# Pesos: 20 y 30 → shipping 50.000 se reparte 20.000 / 30.000
check("shipping item1 = 20.000", approx(rows[0]["shipping_clp"], 20000))
check("shipping item2 = 30.000", approx(rows[1]["shipping_clp"], 30000))
check("Σ shipping = shipping_total", approx(tot["shipping_clp"], shipping_total))
# CIF: item1 = 200.000+20.000=220.000 ; item2 = 50.000+30.000=80.000 ; total 300.000
check("cif item1 = 220.000", approx(rows[0]["cif_clp"], 220000))
check("cif item2 = 80.000", approx(rows[1]["cif_clp"], 80000))
# Gastos 25.000 prorrateado por CIF (220/300, 80/300) = 18.333,33 / 6.666,67
check("gastos item1 ≈ 18.333,33", approx(rows[0]["gastos_clp"], 25000 * 220000 / 300000))
check("gastos item2 ≈ 6.666,67", approx(rows[1]["gastos_clp"], 25000 * 80000 / 300000))
check("Σ gastos = total_gastos", approx(tot["gastos_clp"], total_gastos))
# Costo total = CIF + gastos ; suma = 300.000 + 25.000 = 325.000
check("Σ costo_total = 325.000", approx(tot["costo_total_clp"], 325000))
# Costo unit item1 = (220.000+18.333,33)/2
check("costo_unit item1 correcto", approx(rows[0]["costo_unit_clp"], (220000 + 25000 * 220000 / 300000) / 2))

# ── 2) Fallback: Σ pesos = 0 → shipping prorratea por FOB CLP ──
items0 = [
    {"cantidad": 1, "peso_unit": 0, "fob_unit": 100, "tc_valor": 1000},  # fob_clp 100.000
    {"cantidad": 1, "peso_unit": 0, "fob_unit": 300, "tc_valor": 1000},  # fob_clp 300.000
]
rows0, tot0 = calcular_landed(items0, 40000, 0)
check("fallback peso0: shipping por FOB (10.000 / 30.000)",
      approx(rows0[0]["shipping_clp"], 10000) and approx(rows0[1]["shipping_clp"], 30000))
check("fallback peso0: Σ shipping cuadra", approx(tot0["shipping_clp"], 40000))

# ── 3) Fallback total: sin peso ni FOB → shipping en partes iguales ──
items_eq = [
    {"cantidad": 1, "peso_unit": 0, "fob_unit": 0, "tc_valor": 1000},
    {"cantidad": 1, "peso_unit": 0, "fob_unit": 0, "tc_valor": 1000},
]
rows_eq, tot_eq = calcular_landed(items_eq, 10000, 6000)
check("fallback total: shipping partes iguales (5.000 c/u)",
      approx(rows_eq[0]["shipping_clp"], 5000) and approx(rows_eq[1]["shipping_clp"], 5000))
check("fallback total: gastos partes iguales (3.000 c/u)",
      approx(rows_eq[0]["gastos_clp"], 3000) and approx(rows_eq[1]["gastos_clp"], 3000))

# ── 4) Lista vacía no rompe ──
rows_v, tot_v = calcular_landed([], 1000, 1000)
check("lista vacía → n_items 0", tot_v["n_items"] == 0)
check("lista vacía → costo total 0", approx(tot_v["costo_total_clp"], 0))

# ── 5) total_gastos_que_capitalizan: excluye los que no capitalizan (IVA importación) ──
gastos = [
    {"tipo": "desconsolidacion", "monto_neto": 1000, "capitaliza": True},
    {"tipo": "arancel", "monto_neto": 500, "capitaliza": True},
    {"tipo": "iva_importacion", "monto_neto": 9999, "capitaliza": False},
]
check("total capitaliza = 1500 (excluye IVA imp.)", approx(total_gastos_que_capitalizan(gastos), 1500))

# ── 6) Catálogo: 6 tipos, IVA importación NO capitaliza ──
check("catálogo tiene 6 gastos", len(GASTOS_CATALOGO) == 6)
iva_imp = [g for g in GASTOS_CATALOGO if g["tipo"] == "iva_importacion"][0]
check("iva_importacion NO capitaliza", iva_imp["capitaliza"] is False)

print()
if _fails:
    print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails}")
    sys.exit(1)
print("RESULTADO: TODO OK")
