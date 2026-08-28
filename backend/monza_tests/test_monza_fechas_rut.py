"""Los helpers de FECHA DE CHILE y RUT normalizado (arreglo leads+deudas, 2026-08-22).

QUÉ PINEA
    · monza_fechas.rango_utc: el día que el operador escribe entra COMPLETO (semiabierto
      al día siguiente) y el borde se calcula en hora de CHILE, no en UTC.
    · monza_fechas.rango_dias: las columnas Date civiles conservan su `<=` INCLUSIVO —
      el anti-arreglo: aplicarles el semiabierto UTC rompería un filtro que estaba sano.
    · monza_fechas.parse_fecha_op: fecha inválida ⇒ 422 (falla CERRADO). Antes, tres
      endpoints respondían 500 y otros dos IGNORABAN el filtro en silencio.
    · monza_rut: normalización bilateral para buscar, y el guard que evita que un texto
      cualquiera active la rama de RUT.

SONDAS DE PODER DISCRIMINANTE
    · §1 compara VERANO (UTC-3) contra INVIERNO (UTC-4) con los mismos días del mes: un
      `-3` hardcodeado en vez de la zona horaria pasa el primer check y CAE en el segundo.
    · §1c usa el día del arranque del horario de verano (ese día dura 23 h): el rango no
      levanta excepción y sigue siendo monótono.
    · §2 pinea que rango_dias NO adelanta el día (si alguien lo "arregla" copiando el
      semiabierto, cae).
    · §4 monkeypatchea `ahora_chile` a las 22:30 de Chile del último día del mes — que en
      UTC ya es el mes siguiente: `inicio_mes_utc` debe seguir apuntando al mes de CHILE.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_monza_fechas_rut.py -q
"""
import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import monza_fechas  # noqa: E402
from monza_fechas import (  # noqa: E402
    hoy_chile, inicio_mes_utc, parse_fecha_op, rango_dias, rango_utc,
)
from monza_rut import parece_rut, rut_identidad, rut_norm_py  # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def run():
    # ── 1) rango_utc: día completo, en hora de Chile ─────────────────────────────
    d, h = rango_utc("2026-01-30", "2026-01-30")
    check("1a VERANO (UTC-3): el día empieza a las 03:00 UTC",
          d == datetime(2026, 1, 30, 3, 0), d)
    check("1b y termina en la medianoche de Chile del día SIGUIENTE (día completo)",
          h == datetime(2026, 1, 31, 3, 0), h)
    d2, h2 = rango_utc("2026-06-15", "2026-06-15")
    check("1c SONDA: INVIERNO (UTC-4) da un offset DISTINTO — con un -3 fijo esto cae",
          d2 == datetime(2026, 6, 15, 4, 0) and h2 == datetime(2026, 6, 16, 4, 0),
          (d2, h2))
    # Día del arranque del horario de verano chileno (dura 23 h): no explota y es monótono.
    d3, h3 = rango_utc("2026-09-06", "2026-09-06")
    check("1d el día del salto DST no levanta excepción y sigue siendo monótono",
          d3 is not None and h3 is not None and d3 < h3 and (h3 - d3) == timedelta(hours=23),
          (d3, h3, h3 - d3))
    check("1e un solo extremo deja el otro en None (sin filtro)",
          rango_utc("2026-06-15", None) == (datetime(2026, 6, 15, 4, 0), None)
          and rango_utc(None, None) == (None, None), rango_utc("2026-06-15", None))
    check("1f cadena vacía = sin filtro (los inputs date vacíos mandan '')",
          rango_utc("", "") == (None, None), rango_utc("", ""))
    dd, hh = rango_utc("2026-06-20", "2026-06-15")
    check("1g desde > hasta NO es error: rango vacío honesto (0 filas)",
          dd is not None and hh is not None and dd >= hh, (dd, hh))

    # El uso del rango es SIEMPRE `col >= desde` y `col < hasta`: se verifica que un
    # registro de las 15:00 hora de Chile del propio día `hasta` cae DENTRO.
    tarde_chile_utc = datetime(2026, 6, 15, 19, 0)  # 15:00 Chile (UTC-4) = 19:00 UTC
    check("1h SONDA del bug original: lo creado a las 15:00 del día 'hasta' ENTRA",
          d2 <= tarde_chile_utc < h2, (d2, tarde_chile_utc, h2))
    noche_chile_utc = datetime(2026, 7, 1, 1, 30)  # 21:30 del 30-jun en Chile = 01:30 UTC del 1-jul
    dj, hj = rango_utc("2026-06-30", "2026-06-30")
    check("1i SONDA del borde UTC: lo creado a las 21:30 del 30 (Chile) ENTRA en 'hasta=30'",
          dj <= noche_chile_utc < hj, (dj, noche_chile_utc, hj))

    # ── 2) rango_dias: columnas Date civiles, INCLUSIVO ──────────────────────────
    check("2a devuelve dates puros, sin conversión de zona",
          rango_dias("2026-06-15", "2026-06-20") == (date(2026, 6, 15), date(2026, 6, 20)),
          rango_dias("2026-06-15", "2026-06-20"))
    check("2b SONDA anti-arreglo: NO adelanta el día (si alguien copia el semiabierto, cae)",
          rango_dias(None, "2026-06-20")[1] == date(2026, 6, 20),
          rango_dias(None, "2026-06-20"))

    # ── 3) parse_fecha_op: falla CERRADO ─────────────────────────────────────────
    for malo in ("26-08-2026", "2026-13-40", "basura", "2026-08-26T15:00:00", "  "):
        try:
            parse_fecha_op(malo, "hasta")
            check(f"3 '{malo}' debería rebotar con 422", False, "no levantó")
        except HTTPException as e:
            check(f"3 '{malo}' → 422 nombrando el campo",
                  e.status_code == 422 and "hasta" in str(e.detail), (e.status_code, e.detail))
    check("3f una fecha válida sí pasa", parse_fecha_op("2026-08-26", "desde") == date(2026, 8, 26),
          parse_fecha_op("2026-08-26", "desde"))
    # Formatos que `date.fromisoformat` acepta desde 3.11 pero que NADIE teclea en un
    # <input type="date">: la semana ISO resuelve a OTRO AÑO en silencio.
    for exotico in ("2026-W01-1", "20260315"):
        try:
            parse_fecha_op(exotico, "desde")
            check(f"3g '{exotico}' debería rebotar (no es AAAA-MM-DD)", False, "no levantó")
        except HTTPException as e:
            check(f"3g SONDA: '{exotico}' → 422 en vez de resolverse en silencio",
                  e.status_code == 422, e.status_code)
    # 9999-12-31 es una fecha VÁLIDA, pero sumarle el día del borde semiabierto se sale
    # del calendario: sin el try/except, esto era un 500 con traceback en 4 endpoints.
    try:
        rango_utc(None, "9999-12-31")
        check("3h hasta=9999-12-31 debería rebotar con 422", False, "no levantó")
    except HTTPException as e:
        check("3h SONDA: el borde del calendario da 422, no 500",
              e.status_code == 422 and "hasta" in str(e.detail), (e.status_code, e.detail))
    except OverflowError as e:
        check("3h SONDA: el borde del calendario da 422, no 500", False,
              f"subió OverflowError sin envolver: {e}")
    check("3i y el mismo día como 'desde' (que no suma) sigue funcionando",
          rango_utc("9999-12-31", None)[0] is not None, "")

    # ── 4) inicio_mes_utc: la frontera del mes es la de CHILE ────────────────────
    # 22:30 del 31-ene en Chile (UTC-3) = 01:30 UTC del 1-feb: en UTC ya es febrero, pero
    # el negocio sigue en enero.
    original = monza_fechas.ahora_chile
    try:
        monza_fechas.ahora_chile = lambda: datetime(
            2026, 1, 31, 22, 30, tzinfo=monza_fechas.TZ_CHILE)
        check("4a SONDA: a las 22:30 del 31-ene (Chile) el mes en curso sigue siendo ENERO",
              inicio_mes_utc() == datetime(2026, 1, 1, 3, 0), inicio_mes_utc())
        check("4b y hoy_chile es el 31 de enero (no el 1 de febrero de UTC)",
              hoy_chile() == date(2026, 1, 31), hoy_chile())
    finally:
        monza_fechas.ahora_chile = original

    # ── 5) monza_rut: normalización bilateral y guard del término ────────────────
    check("5a quita puntos, guiones y espacios; sube a mayúscula",
          rut_norm_py("76.000.000-0") == "760000000"
          and rut_norm_py(" 9123456-k ") == "9123456K", rut_norm_py("76.000.000-0"))
    check("5b None / vacío → cadena vacía (no revienta)",
          rut_norm_py(None) == "" and rut_norm_py("") == "", "")
    check("5c los dos formatos del MISMO rut normalizan igual (match bilateral)",
          rut_norm_py("76.000.000-0") == rut_norm_py("76000000-0"), "")
    check("5d SONDA: un texto cualquiera NO activa la rama de RUT",
          not parece_rut("MARIA") and not parece_rut("2026") and not parece_rut("filtro"), "")
    check("5e un RUT tecleado en cualquier formato SÍ la activa",
          parece_rut("76.000.000-0") and parece_rut("76000000-0") and parece_rut("9123456-K"), "")
    check("5f un número corto (teléfono parcial, monto) tampoco la activa",
          not parece_rut("76000"), "")

    # ── 6) rut_identidad: la llave que decide QUIÉN ES QUIÉN ─────────────────────
    check("6a SONDA: un RUT de pura puntuación NO identifica a nadie "
          "(con el normalizador de BUSCAR colapsaba a '' y enganchaba con terceros)",
          all(rut_identidad(v) == "" for v in ("-", ".", "   ", "..-", "")), "")
    check("6b los ceros a la izquierda son el MISMO contribuyente",
          rut_identidad("076.543.210-8") == rut_identidad("76543210-8") != "", "")
    check("6c la K del DV vale en cualquier caja",
          rut_identidad("9123456-k") == rut_identidad("9.123.456-K") != "", "")
    check("6d un número que no tiene forma de RUT tampoco identifica",
          rut_identidad("232") == "" and rut_identidad("MARIA") == "", "")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_monza_fechas_rut():
    run()


if __name__ == "__main__":
    run()
