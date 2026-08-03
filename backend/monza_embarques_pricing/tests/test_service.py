"""Tests de la matemática pura del costo landed (sin BD).

Corre con:  cd backend && ./venv/bin/python monza_embarques_pricing/tests/test_service.py
También es descubrible por pytest: funciones test_* con assert que fallan de verdad
(espejo del estilo de backend/embarques_pricing/tests/test_service.py de Grupo AM;
antes los checks corrían a nivel de módulo y pytest no ejecutaba NINGUNO).
"""
import os
import sys

# Permite ejecutar el archivo directamente (agrega backend/ al path).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from monza_embarques_pricing.service import (  # noqa: E402
    calcular_landed, total_gastos_que_capitalizan, GASTOS_CATALOGO,
)

TOL = 0.01


def _aprox(a, b, tol=TOL):
    return abs(float(a) - float(b)) <= tol


def test_prorrateo_por_peso_y_por_cif():
    # Arrange — 2 ítems: shipping prorratea por PESO, gastos locales por CIF
    items = [
        {"cantidad": 2, "peso_unit": 10, "fob_unit": 100, "tc_valor": 1000},  # peso 20, fob 200, fob_clp 200.000
        {"cantidad": 1, "peso_unit": 30, "fob_unit": 50,  "tc_valor": 1000},  # peso 30, fob 50,  fob_clp 50.000
    ]
    shipping_total = 50000.0
    total_gastos = 25000.0

    # Act
    rows, tot = calcular_landed(items, shipping_total, total_gastos)

    # Assert
    assert tot["n_items"] == 2
    # Pesos: 20 y 30 → shipping 50.000 se reparte 20.000 / 30.000
    assert _aprox(rows[0]["shipping_clp"], 20000), rows[0]["shipping_clp"]
    assert _aprox(rows[1]["shipping_clp"], 30000), rows[1]["shipping_clp"]
    assert _aprox(tot["shipping_clp"], shipping_total)
    # CIF: item1 = 200.000+20.000=220.000 ; item2 = 50.000+30.000=80.000 ; total 300.000
    assert _aprox(rows[0]["cif_clp"], 220000), rows[0]["cif_clp"]
    assert _aprox(rows[1]["cif_clp"], 80000), rows[1]["cif_clp"]
    # Gastos 25.000 prorrateado por CIF (220/300, 80/300) = 18.333,33 / 6.666,67
    assert _aprox(rows[0]["gastos_clp"], 25000 * 220000 / 300000), rows[0]["gastos_clp"]
    assert _aprox(rows[1]["gastos_clp"], 25000 * 80000 / 300000), rows[1]["gastos_clp"]
    assert _aprox(tot["gastos_clp"], total_gastos)
    # Costo total = CIF + gastos ; suma = 300.000 + 25.000 = 325.000
    assert _aprox(tot["costo_total_clp"], 325000), tot["costo_total_clp"]
    # Costo unit item1 = (220.000+18.333,33)/2
    assert _aprox(rows[0]["costo_unit_clp"], (220000 + 25000 * 220000 / 300000) / 2)
    print("OK test_prorrateo_por_peso_y_por_cif")


def test_fallback_peso_cero_prorratea_por_fob():
    # Arrange — Σ pesos = 0 → el shipping debe prorratearse por FOB CLP, no perderse
    items = [
        {"cantidad": 1, "peso_unit": 0, "fob_unit": 100, "tc_valor": 1000},  # fob_clp 100.000
        {"cantidad": 1, "peso_unit": 0, "fob_unit": 300, "tc_valor": 1000},  # fob_clp 300.000
    ]
    # Act
    rows, tot = calcular_landed(items, 40000, 0)
    # Assert — 100k:300k → 10.000 / 30.000
    assert _aprox(rows[0]["shipping_clp"], 10000), rows[0]["shipping_clp"]
    assert _aprox(rows[1]["shipping_clp"], 30000), rows[1]["shipping_clp"]
    assert _aprox(tot["shipping_clp"], 40000)
    print("OK test_fallback_peso_cero_prorratea_por_fob")


def test_fallback_total_partes_iguales():
    # Arrange — sin peso ni FOB → shipping y gastos en partes iguales
    items = [
        {"cantidad": 1, "peso_unit": 0, "fob_unit": 0, "tc_valor": 1000},
        {"cantidad": 1, "peso_unit": 0, "fob_unit": 0, "tc_valor": 1000},
    ]
    # Act
    rows, _tot = calcular_landed(items, 10000, 6000)
    # Assert
    assert _aprox(rows[0]["shipping_clp"], 5000) and _aprox(rows[1]["shipping_clp"], 5000)
    assert _aprox(rows[0]["gastos_clp"], 3000) and _aprox(rows[1]["gastos_clp"], 3000)
    print("OK test_fallback_total_partes_iguales")


def test_lista_vacia_no_rompe():
    rows, tot = calcular_landed([], 1000, 1000)
    assert rows == []
    assert tot["n_items"] == 0
    assert _aprox(tot["costo_total_clp"], 0)
    print("OK test_lista_vacia_no_rompe")


def test_total_gastos_excluye_los_que_no_capitalizan():
    # Arrange — el IVA importación es recuperable: NO capitaliza al costo
    gastos = [
        {"tipo": "desconsolidacion", "monto_neto": 1000, "capitaliza": True},
        {"tipo": "arancel", "monto_neto": 500, "capitaliza": True},
        {"tipo": "iva_importacion", "monto_neto": 9999, "capitaliza": False},
    ]
    # Act / Assert — solo capitalizan 1.000 + 500
    assert _aprox(total_gastos_que_capitalizan(gastos), 1500)
    print("OK test_total_gastos_excluye_los_que_no_capitalizan")


def test_catalogo_tiene_seis_gastos_y_un_no_capitaliza():
    assert len(GASTOS_CATALOGO) == 6
    no_cap = [g for g in GASTOS_CATALOGO if not g["capitaliza"]]
    assert len(no_cap) == 1 and no_cap[0]["tipo"] == "iva_importacion"
    print("OK test_catalogo_tiene_seis_gastos_y_un_no_capitaliza")


def test_detect_tipo_por_forwarder_y_moneda():
    # Espejo GA: el tipo de embarque se auto-detecta por forwarder/moneda (editable luego)
    from monza_embarques_pricing.integration import detect_tipo
    assert detect_tipo("LATAM Cargo", "USD") == "normal"
    assert detect_tipo("DHL Express", "USD") == "courier"
    assert detect_tipo("BAUKAT GmbH", "EUR") == "baukat"
    assert detect_tipo("Fast Mark", "CLP") == "fastmark"
    assert detect_tipo("", "EUR") == "baukat"           # EUR sin forwarder → baukat
    assert detect_tipo("Naviera X", "USD") == "normal"  # default
    print("OK test_detect_tipo_por_forwarder_y_moneda")


def test_flete_default_por_tipo():
    # Regla del dueño: Baukat prepagado por proveedor (ME); LATAM/DHL/FastMark
    # default CLP local (editable). Esto fija el default, no lo obliga.
    from monza_embarques_pricing.integration import FLETE_EN_ME_DEFAULT
    assert FLETE_EN_ME_DEFAULT["baukat"] is True
    assert FLETE_EN_ME_DEFAULT["normal"] is False
    assert FLETE_EN_ME_DEFAULT["courier"] is False
    assert FLETE_EN_ME_DEFAULT["fastmark"] is False
    print("OK test_flete_default_por_tipo")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nRESULTADO: {len(fns)} tests OK")
