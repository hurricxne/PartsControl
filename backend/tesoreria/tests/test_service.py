"""Tests unitarios (puros, sin DB) del parser de cartolas y helpers de Tesorería.

Corre con:  ./venv/bin/python tesoreria/tests/test_service.py
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tesoreria.service import (  # noqa: E402
    parse_monto_cl, parse_cartola, _parse_date, bucket_de, FLUJO_BUCKETS,
)


def test_parse_monto_cl():
    assert parse_monto_cl("1.234.567") == 1234567.0
    assert parse_monto_cl("1.234.567,89") == 1234567.89
    assert parse_monto_cl("50.000") == 50000.0
    assert parse_monto_cl("$ 119.000") == 119000.0
    assert parse_monto_cl("(1.000)") == -1000.0
    assert parse_monto_cl("") is None
    assert parse_monto_cl(None) is None
    assert parse_monto_cl(119000) == 119000.0


def test_parse_date_formatos():
    assert _parse_date("01/06/2026") == date(2026, 6, 1)
    assert _parse_date("2026-06-01") == date(2026, 6, 1)
    assert _parse_date("basura") is None


def test_bucket_de():
    hoy = date(2026, 7, 1)
    assert bucket_de(None, hoy) == "sin_fecha"
    assert bucket_de(date(2026, 6, 30), hoy) == "vencido"
    assert bucket_de(date(2026, 7, 1), hoy) == "d0_7"
    assert bucket_de(date(2026, 7, 8), hoy) == "d0_7"
    assert bucket_de(date(2026, 7, 9), hoy) == "d8_30"
    assert bucket_de(date(2026, 7, 31), hoy) == "d8_30"
    assert bucket_de(date(2026, 8, 30), hoy) == "d31_60"
    assert bucket_de(date(2026, 12, 1), hoy) == "d61_mas"
    assert set(FLUJO_BUCKETS) == {"vencido", "d0_7", "d8_30", "d31_60", "d61_mas", "sin_fecha"}


def test_cartola_csv_cargo_abono():
    csv = (
        "Fecha;Glosa;Cargo;Abono;Saldo\n"
        "01/06/2026;PAGO PROVEEDOR;119.000;;500.000\n"
        "02/06/2026;DEPOSITO;;50.000;550.000\n"
        "TOTALES;;;;\n"  # fila sin fecha → se ignora
    )
    out = parse_cartola(csv.encode("utf-8"), "cartola.csv")
    movs = out["movimientos"]
    assert len(movs) == 2, movs
    cargo = next(m for m in movs if m["tipo"] == "cargo")
    abono = next(m for m in movs if m["tipo"] == "abono")
    assert cargo["monto"] == 119000.0 and cargo["fecha"] == "2026-06-01"
    assert abono["monto"] == 50000.0
    assert cargo["saldo"] == 500000.0


def test_cartola_csv_monto_unico_con_signo():
    csv = (
        "Fecha,Descripcion,Monto\n"
        "2026-06-01,Pago,-119000\n"     # negativo → cargo
        "2026-06-02,Ingreso,50000\n"     # positivo → abono
    )
    out = parse_cartola(csv.encode("utf-8"), "c.csv")
    movs = out["movimientos"]
    assert len(movs) == 2
    assert {m["tipo"] for m in movs} == {"cargo", "abono"}
    cargo = next(m for m in movs if m["tipo"] == "cargo")
    assert cargo["monto"] == 119000.0   # siempre positivo; el signo lo da el tipo


def test_cartola_headers_no_reconocidos():
    csv = "ColA;ColB\n1;2\n"
    try:
        parse_cartola(csv.encode("utf-8"), "c.csv")
        assert False, "debió lanzar ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK {fn.__name__}")
    print(f"\n✅ {len(fns)} tests de service OK")
