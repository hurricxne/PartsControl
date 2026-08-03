"""Tests de la lógica pura del módulo Compras/CxP MonzaParts (sin BD).

Corre con:  cd backend && ./venv/bin/python monza_compras_contab/tests/test_service.py
(también:   ./venv/bin/python -m pytest monza_compras_contab/tests/test_service.py -q)
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from monza_compras_contab.service import (  # noqa: E402
    _f, _semaforo, _estado_pago, _recompute_compra, parse_date_estricta,
    cuenta_default_codigo, TIPOS_GASTO, TOL,
)

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def mk(total, pagos, venc=None, anulado=False):
    c = SimpleNamespace(
        monto_total_clp=total, fecha_vencimiento=venc, anulado=anulado,
        egreso_detalles=[SimpleNamespace(monto_clp=p) for p in pagos],
        monto_pagado_clp=0, saldo_clp=0, estado_pago="",
    )
    _recompute_compra(c)
    return c


# Los checks viven en run() y no a nivel de módulo (espejo GA): a nivel de módulo
# pytest los ejecutaba en el IMPORT y un fallo pasaba en silencio (verde falso).
def run():
    hoy = date.today()

    # ── _f ──
    check("_f None -> 0", _f(None) == 0.0)
    check("_f '12.5' -> 12.5", _f("12.5") == 12.5)
    check("_f basura -> 0", _f("abc") == 0.0)

    # ── parse_date_estricta ──
    check("fecha ISO ok", parse_date_estricta("2026-06-01", campo="x") == date(2026, 6, 1))
    check("fecha vacía -> None", parse_date_estricta("", campo="x") is None)
    try:
        parse_date_estricta("junio", campo="x")
        check("fecha inválida lanza", False)
    except ValueError:
        check("fecha inválida lanza", True)

    # ── semáforo ──
    check("saldo 0 -> al_dia", _semaforo(hoy, 0) == "al_dia")
    check("sin fecha -> sin_fecha", _semaforo(None, 100) == "sin_fecha")
    check("vencida ayer -> vencida", _semaforo(hoy - timedelta(days=1), 100) == "vencida")
    check("vence en 3d -> por_vencer", _semaforo(hoy + timedelta(days=3), 100) == "por_vencer")
    check("vence en 30d -> vigente", _semaforo(hoy + timedelta(days=30), 100) == "vigente")

    # ── estado de pago ──
    c = mk(100000, [])
    check("sin pagos -> pendiente", c.estado_pago == "pendiente", c.estado_pago)
    c = mk(100000, [40000])
    check("pago parcial -> parcial + saldo 60.000", c.estado_pago == "parcial" and c.saldo_clp == 60000, (c.estado_pago, c.saldo_clp))
    c = mk(100000, [60000, 40000])
    check("pagos completan -> pagado", c.estado_pago == "pagado", c.estado_pago)
    c = mk(100000, [10000], venc=hoy - timedelta(days=2))
    check("vencida con abono -> vencido", c.estado_pago == "vencido", c.estado_pago)
    c = mk(100000, [], anulado=True)
    check("anulada -> anulado", c.estado_pago == "anulado", c.estado_pago)
    c = mk(100000, [100000, 500])   # sobre-pago contable: saldo nunca negativo
    check("saldo nunca negativo", c.saldo_clp == 0.0, c.saldo_clp)
    check("estado_pago dentro de catálogo", c.estado_pago in ("pendiente", "parcial", "pagado", "vencido", "anulado"))

    # ── cuenta default por (origen, tipo) ──
    check("EMBARQUE+cogs -> 1.3.02", cuenta_default_codigo("EMBARQUE", "cogs") == "1.3.02")
    check("MANUAL+cogs -> 1.3.01", cuenta_default_codigo("manual", "cogs") == "1.3.01")
    check("MANUAL+gasto_operacional -> 6.2.04", cuenta_default_codigo(None, "gasto_operacional") == "6.2.04")
    check("combinación desconocida -> None", cuenta_default_codigo("EMBARQUE", "otros") is None)
    check("4 tipos de gasto", len(TIPOS_GASTO) == 4)
    check("TOL razonable", 0 < TOL < 10)


def test_monza_compras_contab_service():
    """Wrapper para pytest (espejo de compras_contab/tests/test_service.py de GA):
    sin él los checks corrían en el import y un fallo no rompía nada."""
    run()
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    run()
    print()
    if _fails:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
