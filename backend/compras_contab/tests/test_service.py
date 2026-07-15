"""Tests unitarios (puros, sin DB) de la lógica de service.py.

Corre con:  ./venv/bin/python compras_contab/tests/test_service.py
(también descubrible por pytest si está instalado).
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from compras_contab.service import (  # noqa: E402
    _f, _parse_date, parse_date_estricta, _semaforo, _estado_pago,
    _recompute_compra, IVA_RATE, TOL, DIAS_POR_VENCER, TIPOS_GASTO,
)


def _compra(**kw):
    base = dict(anulado=False, fecha_vencimiento=None, monto_total_clp=0,
                monto_pagado_clp=0, saldo_clp=0, estado_pago=None, egreso_detalles=[])
    base.update(kw)
    return SimpleNamespace(**base)


def _det(monto):
    """Asignación de egreso (línea de pago)."""
    return SimpleNamespace(monto_clp=monto)


def test_f_convierte_seguro():
    assert _f(None) == 0.0
    assert _f("1234.5") == 1234.5
    assert _f("basura") == 0.0


def test_parse_date_formatos_y_basura():
    assert _parse_date("2026-06-28") == date(2026, 6, 28)
    assert _parse_date("28-06-2026") == date(2026, 6, 28)
    assert _parse_date("28/06/2026") == date(2026, 6, 28)
    assert _parse_date("") is None
    assert _parse_date("no-fecha") is None


def test_parse_date_estricta_falla_no_cae_a_hoy():
    assert parse_date_estricta("", campo="x") is None
    assert parse_date_estricta(None, campo="x") is None
    assert parse_date_estricta("2026-01-15", campo="x") == date(2026, 1, 15)
    try:
        parse_date_estricta("31/31/9999-mal", campo="fecha")
        assert False, "debió lanzar ValueError"
    except ValueError:
        pass


def test_semaforo_bordes():
    assert _semaforo(None, 0) == "al_dia"            # saldo 0 → saldada
    assert _semaforo(None, 1000) == "sin_fecha"
    hoy = date.today()
    assert _semaforo(hoy - timedelta(days=1), 1000) == "vencida"
    assert _semaforo(hoy + timedelta(days=DIAS_POR_VENCER - 1), 1000) == "por_vencer"
    assert _semaforo(hoy + timedelta(days=DIAS_POR_VENCER + 30), 1000) == "vigente"


def test_estado_pago_anulado_prevalece():
    c = _compra(anulado=True)
    assert _estado_pago(c, 0, 1000) == "anulado"      # aún con saldo, anulado manda


def test_estado_pago_estados():
    hoy = date.today()
    assert _estado_pago(_compra(), 0, 0) == "pagado"                       # saldo 0
    assert _estado_pago(_compra(), 500, 500) == "parcial"                  # algo pagado, no vencida
    assert _estado_pago(_compra(), 0, 1000) == "pendiente"                 # nada pagado, no vencida
    venc = _compra(fecha_vencimiento=hoy - timedelta(days=1))
    assert _estado_pago(venc, 0, 1000) == "vencido"                        # vencida sin pago
    assert _estado_pago(venc, 500, 500) == "vencido"                       # vencida con abono


def test_recompute_saldo_no_negativo_y_estado():
    c = _compra(monto_total_clp=1000, egreso_detalles=[_det(400)])
    _recompute_compra(c)
    assert c.monto_pagado_clp == 400 and c.saldo_clp == 600 and c.estado_pago == "parcial"
    # pago total (varias asignaciones) → pagado, saldo 0
    c = _compra(monto_total_clp=1000, egreso_detalles=[_det(600), _det(400)])
    _recompute_compra(c)
    assert c.saldo_clp == 0 and c.estado_pago == "pagado"
    # sobre-pago acumulado nunca deja saldo negativo persistido
    c = _compra(monto_total_clp=1000, egreso_detalles=[_det(1500)])
    _recompute_compra(c)
    assert c.saldo_clp == 0


def test_constantes_dominio():
    assert IVA_RATE == 0.19 and TOL > 0
    assert TIPOS_GASTO == ["cogs", "gasto_operacional", "gasto_no_operacional", "otros"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK {fn.__name__}")
    print(f"\n✅ {len(fns)} tests de service OK")
