"""Tests de la lógica pura del módulo Tesorería MonzaParts (sin BD).

Corre con:  cd backend && ./venv/bin/python monza_tesoreria/tests/test_service.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from monza_tesoreria.service import (  # noqa: E402
    parse_monto_cl, _parse_date, bucket_de, parse_cartola, FLUJO_BUCKETS, TOL,
)

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── parse_monto_cl (formato chileno) ──
check("miles con punto", parse_monto_cl("1.234.567") == 1234567.0)
check("decimales con coma", parse_monto_cl("1.234.567,89") == 1234567.89)
check("negativo entre paréntesis", parse_monto_cl("($1.000)") == -1000.0)
check("con $ y espacios", parse_monto_cl("$ 500.000") == 500000.0)
check("NBSP (espacio duro) como separador", parse_monto_cl("1\xa0234\xa0567") == 1234567.0)
check("numérico directo", parse_monto_cl(2500) == 2500.0)
check("vacío -> None", parse_monto_cl("") is None)
check("basura -> None", parse_monto_cl("abc") is None)

# ── parse_monto_cl BILINGÜE CL/US (el último separador decide; un grupo final de
#    EXACTAMENTE 3 dígitos con un solo separador se trata como miles) ──
check("US: ambos separadores", parse_monto_cl("1,234,567.89") == 1234567.89)
check("CL: ambos separadores", parse_monto_cl("1.234.567,89") == 1234567.89)
check("US: miles con coma", parse_monto_cl("50,000") == 50000.0)
check("CL: miles con punto (grupo de 3)", parse_monto_cl("1.234") == 1234.0)
check("CL: coma decimal corta", parse_monto_cl("123,45") == 123.45)
check("US: punto decimal", parse_monto_cl("1234.56") == 1234.56)
check("CL: ambos, coma al final", parse_monto_cl("1.234,50") == 1234.50)

# ── _parse_date ──
check("ISO", _parse_date("2026-07-01") == date(2026, 7, 1))
check("dd-mm-aaaa", _parse_date("01-07-2026") == date(2026, 7, 1))
check("dd/mm/aa", _parse_date("01/07/26") == date(2026, 7, 1))
check("None -> None", _parse_date(None) is None)

# ── bucket_de (flujo de caja) ──
hoy = date(2026, 7, 1)
check("sin fecha", bucket_de(None, hoy) == "sin_fecha")
check("ayer -> vencido", bucket_de(hoy - timedelta(days=1), hoy) == "vencido")
check("hoy -> d0_7", bucket_de(hoy, hoy) == "d0_7")
check("en 7 -> d0_7", bucket_de(hoy + timedelta(days=7), hoy) == "d0_7")
check("en 8 -> d8_30", bucket_de(hoy + timedelta(days=8), hoy) == "d8_30")
check("en 30 -> d8_30", bucket_de(hoy + timedelta(days=30), hoy) == "d8_30")
check("en 45 -> d31_60", bucket_de(hoy + timedelta(days=45), hoy) == "d31_60")
check("en 60 -> d31_60", bucket_de(hoy + timedelta(days=60), hoy) == "d31_60")
check("en 61 -> d61_mas", bucket_de(hoy + timedelta(days=61), hoy) == "d61_mas")
check("en 90 -> d61_mas", bucket_de(hoy + timedelta(days=90), hoy) == "d61_mas")
check("6 buckets", len(FLUJO_BUCKETS) == 6)

# ── parse_cartola: CSV con Cargo/Abono ──
csv1 = (
    "Fecha;Detalle;Cargos;Abonos;Saldo\n"
    "01-07-2026;TRANSFERENCIA A PROVEEDOR;150.000;;1.000.000\n"
    "02-07-2026;DEPOSITO CLIENTE;;2.975.000;3.975.000\n"
    "03-07-2026;;;;\n"                          # fila vacía → se ignora
    "TOTALES;;150.000;2.975.000;\n"             # sin fecha → se ignora
).encode("utf-8")
r = parse_cartola(csv1, "cartola.csv")
check("CSV: 2 movimientos", len(r["movimientos"]) == 2, r)
m1, m2 = r["movimientos"]
check("CSV: cargo 150.000", m1["tipo"] == "cargo" and m1["monto"] == 150000.0, m1)
check("CSV: abono 2.975.000", m2["tipo"] == "abono" and m2["monto"] == 2975000.0, m2)
check("CSV: glosa", m1["glosa"] == "TRANSFERENCIA A PROVEEDOR", m1)
check("CSV: saldo parseado", m2["saldo"] == 3975000.0, m2)

# ── parse_cartola: CSV con columna Monto única (signo manda) ──
csv2 = (
    "Fecha,Descripcion,Monto,N Documento\n"
    "2026-07-01,PAGO SERVICIOS,-45.000,OP-1\n"
    "2026-07-02,ABONO VENTA,120.000,OP-2\n"
).encode("utf-8")
r2 = parse_cartola(csv2, "banco.csv")
check("CSV monto: 2 movs", len(r2["movimientos"]) == 2, r2)
check("CSV monto: negativo -> cargo", r2["movimientos"][0]["tipo"] == "cargo")
check("CSV monto: positivo -> abono", r2["movimientos"][1]["tipo"] == "abono")
check("CSV monto: referencia", r2["movimientos"][0]["referencia"] == "OP-1")

# ── parse_cartola: warnings de filas descartadas (transparencia) ──
csv3 = (
    "Fecha;Detalle;Cargos;Abonos\n"
    "01-07-2026;PAGO OK;10.000;\n"
    "TOTALES;;10.000;\n"            # sin fecha reconocible → se omite con warning
    "02-07-2026;SIN MONTO;;\n"      # con fecha pero sin monto → se omite con warning
).encode("utf-8")
r3 = parse_cartola(csv3, "c.csv")
check("warnings: 1 movimiento válido", len(r3["movimientos"]) == 1, r3["movimientos"])
check("warning fila sin fecha", any("sin fecha" in w for w in r3["warnings"]), r3["warnings"])
check("warning fila sin monto", any("sin monto" in w for w in r3["warnings"]), r3["warnings"])

# ── parse_cartola: encabezados irreconocibles ──
try:
    parse_cartola("A;B;C\n1;2;3\n".encode("utf-8"), "x.csv")
    check("sin encabezados lanza", False)
except ValueError:
    check("sin encabezados lanza", True)

check("TOL razonable", 0 < TOL <= 5)

print()
if _fails:
    print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails}")
    sys.exit(1)
print("RESULTADO: TODO OK")
