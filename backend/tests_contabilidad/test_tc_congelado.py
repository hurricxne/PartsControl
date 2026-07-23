"""TC congelado (freeze-forward) — prueba empírica de que una venta cerrada NO se
re-precia cuando después cambian el dólar u otros parámetros globales del cotizador.

Dos capas:
  1) UNIDAD — config_efectivo / snapshot_desde_config (lógica pura, sin BD).
  2) EMPÍRICA — con el MOTOR REAL de precios (calcular_cotizacion): un total congelado
     se reproduce idéntico con cualquier dólar global; sin congelar, sí deriva.

No toca la base de datos ni datos reales. El stamping en el Cierre de Venta se prueba
aparte en test_cierre_congela_snapshot.py.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_tc_congelado.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.pricing_service import (  # noqa: E402
    calcular_cotizacion, config_efectivo, snapshot_desde_config, CLAVES_PRICING,
)


def _cfg(**over) -> dict:
    """Config global completo (los 8 parámetros de precio) con defaults del motor."""
    base = {
        "tipo_cambio_usd": 940.0,
        "costo_shipping_usd_kg": 3.8,
        "adicionales_shipping_usd": 440.0,
        "costo_agencia_pct": 0.01,
        "costo_agencia_minimo_clp": 160000.0,
        "desconsolidado_clp": 90000.0,
        "bodegaje_clp": 90000.0,
        "margen_venta_pct": 0.19,
    }
    base.update(over)
    return base


def _items():
    return [
        {"id": 1, "cantidad": 10, "precio_unit_cotizacion": 100.0, "peso_unit_lbs": 5.0, "margen_pct": 0.19},
        {"id": 2, "cantidad": 3,  "precio_unit_cotizacion": 250.0, "peso_unit_lbs": 12.0, "margen_pct": 0.25},
    ]


def _total(cfg: dict) -> float:
    return calcular_cotizacion(_items(), {**cfg, "origen": "costo"})["totales"]["total_con_iva_clp"]


# ── 1 · UNIDAD: config_efectivo ────────────────────────────────────────────────
def test_config_efectivo_sin_foto_usa_global():
    g = _cfg()
    assert config_efectivo(None, g) is g
    assert config_efectivo("", g) is g


def test_config_efectivo_con_foto_usa_congelado():
    g = _cfg(tipo_cambio_usd=1020.0)          # global "de hoy" = 1020
    foto = json.dumps({"tipo_cambio_usd": 940.0})  # venta cerrada a 940
    eff = config_efectivo(foto, g)
    assert eff["tipo_cambio_usd"] == 940.0          # manda la foto
    assert eff["margen_venta_pct"] == g["margen_venta_pct"]  # clave no congelada cae al global


def test_config_efectivo_corrupto_o_vacio_cae_al_global():
    g = _cfg()
    assert config_efectivo("{no es json", g)["tipo_cambio_usd"] == 940.0
    assert config_efectivo("[1,2,3]", g)["tipo_cambio_usd"] == 940.0   # no es dict
    assert config_efectivo("{}", g)["tipo_cambio_usd"] == 940.0        # dict vacío


def test_config_efectivo_ignora_claves_desconocidas_y_none():
    g = _cfg()
    foto = json.dumps({"tipo_cambio_usd": 1111.0, "basura": 999, "margen_venta_pct": None})
    eff = config_efectivo(foto, g)
    assert eff["tipo_cambio_usd"] == 1111.0
    assert "basura" not in eff
    assert eff["margen_venta_pct"] == 0.19   # None en la foto → cae al global


def test_snapshot_desde_config_solo_claves_de_precio():
    snap = snapshot_desde_config(_cfg(tipo_cambio_usd=1000.0, plazo_min_default=30, otra="x"))
    assert set(snap.keys()) == set(CLAVES_PRICING)
    assert "plazo_min_default" not in snap and "otra" not in snap
    assert snap["tipo_cambio_usd"] == 1000.0


# ── 2 · EMPÍRICA: el motor real con la foto ────────────────────────────────────
def test_venta_congelada_no_se_mueve_al_cambiar_dolar_global():
    """El corazón del cambio: cierro a 940, mañana el dólar sube a 1020, el total NO cambia."""
    baseline = _total(_cfg(tipo_cambio_usd=940.0))
    foto = json.dumps(snapshot_desde_config(_cfg(tipo_cambio_usd=940.0)))

    global_manana = _cfg(tipo_cambio_usd=1020.0)          # el dueño actualiza el dólar
    congelado = _total(config_efectivo(foto, global_manana))
    assert abs(congelado - baseline) < 0.01, (
        f"la venta congelada se movió: {baseline:.2f} → {congelado:.2f}")


def test_sin_foto_si_deriva_al_cambiar_dolar():
    """Control: sin congelar, el mismo total SÍ cambia cuando cambia el dólar (el bug)."""
    a = _total(config_efectivo(None, _cfg(tipo_cambio_usd=940.0)))
    b = _total(config_efectivo(None, _cfg(tipo_cambio_usd=1020.0)))
    assert abs(b - a) > 1.0, "sin foto el total debería derivar con el dólar"
    assert b > a  # más dólar = más caro


def test_congela_los_8_parametros_no_solo_el_dolar():
    """Cambiar margen, flete o bodegaje tampoco debe mover una venta cerrada."""
    baseline = _total(_cfg())
    foto = json.dumps(snapshot_desde_config(_cfg()))
    # global cambia TODOS los parámetros
    global_cambiado = _cfg(
        tipo_cambio_usd=1050.0, costo_shipping_usd_kg=5.0, adicionales_shipping_usd=600.0,
        costo_agencia_pct=0.02, costo_agencia_minimo_clp=200000.0, desconsolidado_clp=120000.0,
        bodegaje_clp=150000.0, margen_venta_pct=0.35,
    )
    congelado = _total(config_efectivo(foto, global_cambiado))
    assert abs(congelado - baseline) < 0.01, "la foto debe congelar los 8 parámetros"
    # y sin foto, con esos parámetros, el total es MUY distinto (sanity)
    assert abs(_total(global_cambiado) - baseline) > 1.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
