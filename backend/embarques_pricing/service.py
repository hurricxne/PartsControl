"""Cálculo de costo landed por ítem de un embarque.

Réplica EXACTA de la lógica de los creadores de embarque del Google Sheet de
GRUPO AM (`escribirTablaItemsAM_`). Función pura: no toca la base de datos.

Metodología por ítem
---------------------
    FOB Total (ME)  = cantidad × FOB unit                         (moneda extranjera)
    FOB CLP         = FOB Total × TC
    Shipping CLP    = shipping_total_clp × (peso_total_i / Σ peso_total)   ← prorrateo por PESO
    CIF CLP         = FOB CLP + Shipping CLP
    Gastos Loc CLP  = total_gastos × (CIF_i / Σ CIF)                       ← prorrateo por CIF
    Costo Total CLP = CIF CLP + Gastos Loc CLP
    Costo Unit CLP  = Costo Total / cantidad

`total_gastos` (lo que prorratea) capitaliza SOLO los netos de Desconsolidación,
Almacenaje, Agencia de Aduana, Arancel/Derechos y Otros. EXCLUYE el IVA y el
IVA Importación, que son recuperables (crédito fiscal) y NO son costo.

Notas de robustez (no rompen el cuadre):
  · Si la suma de pesos es 0, el shipping se prorratea por FOB CLP; si tampoco
    hay FOB, se reparte en partes iguales. Así el shipping nunca se "pierde".
  · Si la suma de CIF es 0, los gastos se reparten en partes iguales.
  · Las sumas de los prorrateos dan 1, por lo que Σ shipping_i = shipping_total
    y Σ gastos_i = total_gastos (cuadre al peso, antes de redondear para mostrar).
"""
from typing import Dict, List, Tuple


def _f(v) -> float:
    """Convierte a float de forma segura (None / texto → 0.0)."""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _prorratear(rows: List[Dict], total: float, base_key: str, out_key: str) -> None:
    """Reparte `total` entre `rows` proporcional a `base_key`.

    Fallback en cascada si la base suma 0: reparte en partes iguales.
    """
    n = len(rows)
    if n == 0:
        return
    suma = sum(_f(r.get(base_key)) for r in rows)
    if suma > 0:
        for r in rows:
            r[out_key] = total * (_f(r.get(base_key)) / suma)
    else:
        parte = total / n
        for r in rows:
            r[out_key] = parte


def calcular_landed(
    items: List[Dict],
    shipping_total_clp: float,
    total_gastos_capitaliza: float,
) -> Tuple[List[Dict], Dict]:
    """Calcula el costo landed por ítem.

    Args:
        items: lista de dicts con al menos:
            cantidad, peso_unit_lbs, fob_unit, tc_valor
        shipping_total_clp: flete total del embarque, en CLP.
        total_gastos_capitaliza: suma de gastos locales que capitalizan (netos),
            en CLP. Excluye IVA e IVA Importación.

    Returns:
        (items_calculados, totales)
    """
    shipping_total_clp = _f(shipping_total_clp)
    total_gastos_capitaliza = _f(total_gastos_capitaliza)

    rows: List[Dict] = []
    for it in items:
        cant = _f(it.get("cantidad"))
        peso_u = _f(it.get("peso_unit_lbs"))
        fob_u = _f(it.get("fob_unit"))
        tc = _f(it.get("tc_valor"))

        peso_total = cant * peso_u
        fob_total = cant * fob_u
        fob_clp = fob_total * tc

        row = dict(it)
        row.update({
            "cantidad": cant,
            "peso_unit_lbs": peso_u,
            "fob_unit": fob_u,
            "tc_valor": tc,
            "peso_total_lbs": peso_total,
            "fob_total": fob_total,
            "fob_clp": fob_clp,
        })
        rows.append(row)

    # Shipping: prorrateo por peso (fallback: por FOB CLP → partes iguales).
    suma_peso = sum(r["peso_total_lbs"] for r in rows)
    base_ship = "peso_total_lbs" if suma_peso > 0 else "fob_clp"
    _prorratear(rows, shipping_total_clp, base_ship, "shipping_clp")

    for r in rows:
        r["cif_clp"] = r["fob_clp"] + r["shipping_clp"]

    # Gastos locales: prorrateo por CIF (fallback: partes iguales).
    _prorratear(rows, total_gastos_capitaliza, "cif_clp", "gastos_clp")

    for r in rows:
        r["costo_total_clp"] = r["cif_clp"] + r["gastos_clp"]
        r["costo_unit_clp"] = (
            r["costo_total_clp"] / r["cantidad"] if r["cantidad"] else 0.0
        )

    totales = {
        "n_items": len(rows),
        "peso_total_lbs": sum(r["peso_total_lbs"] for r in rows),
        "fob_total_me": sum(r["fob_total"] for r in rows),
        "fob_clp": sum(r["fob_clp"] for r in rows),
        "shipping_clp": sum(r["shipping_clp"] for r in rows),
        "cif_clp": sum(r["cif_clp"] for r in rows),
        "gastos_clp": sum(r["gastos_clp"] for r in rows),
        "costo_total_clp": sum(r["costo_total_clp"] for r in rows),
    }
    return rows, totales


# ─── Catálogo de gastos locales (réplica del bloque GASTOS LOCALES del Sheet) ──
# `capitaliza=True`  → su NETO entra a TOTAL GASTOS (prorratea el landed).
# `capitaliza=False` → IVA Importación: recuperable, se trackea pero NO es costo.
GASTOS_CATALOGO = [
    {"tipo": "desconsolidacion", "glosa": "Desconsolidación",   "capitaliza": True,  "orden": 1},
    {"tipo": "almacenaje",       "glosa": "Almacenaje",          "capitaliza": True,  "orden": 2},
    {"tipo": "agencia",          "glosa": "Agencia de Aduana",   "capitaliza": True,  "orden": 3},
    {"tipo": "arancel",          "glosa": "Arancel / Derechos",  "capitaliza": True,  "orden": 4},
    {"tipo": "otros",            "glosa": "Otros",               "capitaliza": True,  "orden": 5},
    {"tipo": "iva_importacion",  "glosa": "IVA Importación",     "capitaliza": False, "orden": 6},
]

IVA_RATE = 0.19


def total_gastos_que_capitalizan(gastos: List[Dict]) -> float:
    """Suma los netos de los gastos con capitaliza=True (lo que prorratea el landed)."""
    return sum(_f(g.get("monto_neto")) for g in gastos if g.get("capitaliza"))
