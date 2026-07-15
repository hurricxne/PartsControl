"""Tests unitarios de la lógica pura (sin BD) del módulo Contabilidad MonzaParts.

Corre con:  cd backend && ./venv/bin/python monza_contabilidad/tests/test_service.py
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from monza_contabilidad.service import (  # noqa: E402
    _f, _parse_date, _es_medio_factoring, iva_rate_de,
    _semaforo, _estado_pago, _recompute_factura, IVA_DEFAULT, TOL,
)

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _fac(bruto, cobranzas, factoring=None, venc=None):
    return SimpleNamespace(
        monto_bruto=bruto,
        cobranzas=[SimpleNamespace(monto=m, medio=med) for (m, med) in cobranzas],
        factoring=factoring,
        fecha_vencimiento=venc,
        monto_pagado=0, saldo=0, estado_pago=None,
    )


def run():
    # _f
    check("_f None=0", _f(None) == 0.0)
    check("_f texto=0", _f("x") == 0.0)
    check("_f '12.5'=12.5", _f("12.5") == 12.5)

    # _parse_date
    check("parse ISO", _parse_date("2026-06-29") == date(2026, 6, 29))
    check("parse dd-mm-yyyy", _parse_date("29-06-2026") == date(2026, 6, 29))
    check("parse None", _parse_date(None) is None)

    # _es_medio_factoring
    check("factoring_adelanto es factoring", _es_medio_factoring("factoring_adelanto"))
    check("transferencia no es factoring", not _es_medio_factoring("transferencia"))

    # iva_rate_de: porcentaje (19) y fracción (0.19) y fallback
    check("iva 19 -> 0.19", abs(iva_rate_de(SimpleNamespace(iva_pct=19), None) - 0.19) < 1e-9)
    check("iva 0.19 -> 0.19", abs(iva_rate_de(SimpleNamespace(iva_pct=0.19), None) - 0.19) < 1e-9)
    check("iva desde cfg", abs(iva_rate_de(SimpleNamespace(iva_pct=None), SimpleNamespace(iva_pct=19)) - 0.19) < 1e-9)
    check("iva fallback default", abs(iva_rate_de(None, None) - IVA_DEFAULT) < 1e-9)

    # _semaforo
    check("semaforo saldo 0 -> al_dia", _semaforo(date.today(), 0) == "al_dia")
    check("semaforo sin fecha", _semaforo(None, 100) == "sin_fecha")
    check("semaforo vencida", _semaforo(date.today() - timedelta(days=1), 100) == "vencida")
    check("semaforo por_vencer", _semaforo(date.today() + timedelta(days=3), 100) == "por_vencer")
    check("semaforo vigente", _semaforo(date.today() + timedelta(days=60), 100) == "vigente")

    # _estado_pago
    check("estado pagada", _estado_pago(_fac(1000, [(1000, "transferencia")]), 1000, 0) == "pagada")
    check("estado parcial", _estado_pago(_fac(1000, [(500, "transferencia")], venc=date.today() + timedelta(days=5)), 500, 500) == "parcial")
    check("estado por_cobrar", _estado_pago(_fac(1000, [], venc=date.today() + timedelta(days=5)), 0, 1000) == "por_cobrar")
    check("estado vencida", _estado_pago(_fac(1000, [], venc=date.today() - timedelta(days=1)), 0, 1000) == "vencida")
    check("estado factorizada", _estado_pago(_fac(1000, [], factoring=SimpleNamespace(estado="vigente")), 0, 1000) == "factorizada")

    # _recompute_factura
    f = _fac(1190000, [(500000, "transferencia"), (200000, "factoring_adelanto")])
    _recompute_factura(f)
    check("recompute pagado", f.monto_pagado == 700000)
    check("recompute saldo", f.saldo == 490000)
    check("recompute estado parcial", f.estado_pago in ("parcial", "vencida"))

    f2 = _fac(1000000, [(1000000, "transferencia")])
    _recompute_factura(f2)
    check("recompute saldada -> 0 y pagada", f2.saldo == 0 and f2.estado_pago == "pagada")

    # saldo nunca negativo
    f3 = _fac(1000, [(1500, "transferencia")])
    _recompute_factura(f3)
    check("saldo no negativo", f3.saldo == 0)

    print()
    if _fails:
        print(f"=== {len(_fails)} FALLO(S): {_fails} ===")
        sys.exit(1)
    print("=== TODO OK ===")


if __name__ == "__main__":
    run()
