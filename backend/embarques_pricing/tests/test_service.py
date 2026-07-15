"""Tests del cálculo de costo landed (función pura, sin DB).

Corre con:  ./venv/bin/python embarques_pricing/tests/test_service.py
(también es descubrible por pytest si está instalado).
"""
import os
import sys

# Permite ejecutar el archivo directamente (agrega backend/ al path).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from embarques_pricing.service import (  # noqa: E402
    calcular_landed, total_gastos_que_capitalizan, GASTOS_CATALOGO,
)

TOL = 0.5


def _aprox(a, b, tol=TOL):
    return abs(a - b) <= tol


def test_prorrateo_basico_cuadra_al_peso():
    # Arrange — 3 ítems, TC 962, shipping 450.000 CLP, gastos 340.000 CLP
    tc = 962.0
    items = [
        {"cantidad": 1, "peso_unit_lbs": 0.3, "fob_unit": 124, "tc_valor": tc},
        {"cantidad": 1, "peso_unit_lbs": 1.2, "fob_unit": 890, "tc_valor": tc},
        {"cantidad": 1, "peso_unit_lbs": 2.1, "fob_unit": 1420, "tc_valor": tc},
    ]
    shipping = 450_000.0
    gastos = 340_000.0

    # Act
    rows, tot = calcular_landed(items, shipping, gastos)

    # Assert — shipping prorrateado por PESO (0.3/1.2/2.1 sobre 3.6)
    assert _aprox(rows[0]["shipping_clp"], 37_500), rows[0]["shipping_clp"]
    assert _aprox(rows[1]["shipping_clp"], 150_000), rows[1]["shipping_clp"]
    assert _aprox(rows[2]["shipping_clp"], 262_500), rows[2]["shipping_clp"]
    # El shipping reparte exactamente el total (cuadre al peso)
    assert _aprox(sum(r["shipping_clp"] for r in rows), shipping)
    # Los gastos reparten exactamente el total
    assert _aprox(sum(r["gastos_clp"] for r in rows), gastos)
    # FOB CLP = FOB total × TC
    assert _aprox(rows[0]["fob_clp"], 124 * tc)
    # CIF = FOB CLP + shipping
    assert _aprox(rows[0]["cif_clp"], 124 * tc + 37_500)
    # Costo total global = ΣFOB_CLP + shipping + gastos
    fob_clp_total = sum(r["fob_clp"] for r in rows)
    assert _aprox(tot["costo_total_clp"], fob_clp_total + shipping + gastos)
    print("OK test_prorrateo_basico_cuadra_al_peso")


def test_costo_unitario_divide_por_cantidad():
    # Arrange — un solo ítem con cantidad 5
    items = [{"cantidad": 5, "peso_unit_lbs": 2, "fob_unit": 100, "tc_valor": 1000}]
    # Act
    rows, _ = calcular_landed(items, 50_000, 30_000)
    r = rows[0]
    # Assert — costo unit = costo total / 5
    assert _aprox(r["costo_unit_clp"], r["costo_total_clp"] / 5)
    # Con un solo ítem se lleva TODO el shipping y TODOS los gastos
    assert _aprox(r["shipping_clp"], 50_000)
    assert _aprox(r["gastos_clp"], 30_000)
    print("OK test_costo_unitario_divide_por_cantidad")


def test_sin_peso_usa_fallback_por_fob():
    # Arrange — pesos en 0 → el shipping debe prorratearse por FOB CLP, no perderse
    items = [
        {"cantidad": 1, "peso_unit_lbs": 0, "fob_unit": 100, "tc_valor": 1000},  # fob_clp 100k
        {"cantidad": 1, "peso_unit_lbs": 0, "fob_unit": 300, "tc_valor": 1000},  # fob_clp 300k
    ]
    # Act
    rows, _ = calcular_landed(items, 40_000, 0)
    # Assert — 100k:300k → 10.000 / 30.000
    assert _aprox(rows[0]["shipping_clp"], 10_000), rows[0]["shipping_clp"]
    assert _aprox(rows[1]["shipping_clp"], 30_000), rows[1]["shipping_clp"]
    assert _aprox(sum(r["shipping_clp"] for r in rows), 40_000)
    print("OK test_sin_peso_usa_fallback_por_fob")


def test_todo_cero_no_explota():
    # Arrange / Act — sin ítems con valor, sin shipping ni gastos
    items = [{"cantidad": 0, "peso_unit_lbs": 0, "fob_unit": 0, "tc_valor": 0}]
    rows, tot = calcular_landed(items, 0, 0)
    # Assert — no hay división por cero; todo 0
    assert rows[0]["costo_unit_clp"] == 0
    assert tot["costo_total_clp"] == 0
    print("OK test_todo_cero_no_explota")


def test_total_gastos_excluye_iva_importacion():
    # Arrange — un gasto que capitaliza y el IVA importación (no capitaliza)
    gastos = [
        {"tipo": "agencia", "monto_neto": 160_000, "capitaliza": True},
        {"tipo": "iva_importacion", "monto_neto": 500_000, "capitaliza": False},
    ]
    # Act
    total = total_gastos_que_capitalizan(gastos)
    # Assert — solo capitaliza la agencia
    assert _aprox(total, 160_000), total
    print("OK test_total_gastos_excluye_iva_importacion")


def test_catalogo_tiene_seis_lineas_y_un_no_capitaliza():
    # El bloque GASTOS LOCALES del Sheet tiene 6 líneas; solo IVA Importación no capitaliza
    assert len(GASTOS_CATALOGO) == 6
    no_cap = [g for g in GASTOS_CATALOGO if not g["capitaliza"]]
    assert len(no_cap) == 1 and no_cap[0]["tipo"] == "iva_importacion"
    print("OK test_catalogo_tiene_seis_lineas_y_un_no_capitaliza")


def test_detect_tipo_por_forwarder_y_moneda():
    from embarques_pricing.integration import detect_tipo
    assert detect_tipo("LATAM Cargo", "USD") == "normal"
    assert detect_tipo("DHL Express", "USD") == "courier"
    assert detect_tipo("BAUKAT GmbH", "EUR") == "baukat"
    assert detect_tipo("Fast Mark", "CLP") == "fastmark"
    assert detect_tipo("", "EUR") == "baukat"          # EUR sin forwarder → baukat
    assert detect_tipo("Naviera X", "USD") == "normal"  # default
    print("OK test_detect_tipo_por_forwarder_y_moneda")


def test_flete_default_por_tipo():
    # Regla del dueño: Baukat prepagado por proveedor (ME); LATAM/DHL/FastMark
    # default CLP local (editable). Esto fija el default, no lo obliga.
    from embarques_pricing.integration import FLETE_EN_ME_DEFAULT
    assert FLETE_EN_ME_DEFAULT["baukat"] is True
    assert FLETE_EN_ME_DEFAULT["normal"] is False
    assert FLETE_EN_ME_DEFAULT["courier"] is False
    assert FLETE_EN_ME_DEFAULT["fastmark"] is False
    print("OK test_flete_default_por_tipo")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\n✅ {len(fns)} tests OK")
