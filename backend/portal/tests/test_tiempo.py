"""FECHAS del Portal de Clientes — casos sobre funciones puras (sin base, sin HTTP).

LO QUE ESTA SUITE PROTEGE (portal/tiempo.py)
  §A  EL DÍA ES EL DE CHILE: un instante guardado en UTC cae en el día de Chile, no en
      el del servidor (a las 22:30 de Chile el servidor ya está en «mañana»).
  §B  UN DÍA TECLEADO NO SE CORRE: la firma de la guía se guarda a medianoche sin zona y
      es ESE día; tratarla como instante la adelanta un día.
  §C  UNA FECHA ESCRITA A MANO llega normalizada o no llega: nunca el texto crudo, y un
      año de dos dígitos solo vale cerca de una fecha conocida.

MUTACIONES verificadas al construir la suite (cambiar → rojo → restaurar → verde):
  hoy_chile con el reloj del servidor (→ §A), dia_chile sin convertir (→ §A),
  dia_tecleado convirtiendo siempre (→ §B), la holgura del año corto y el filtro de años
  creíbles (→ §C).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_tiempo.py -q
"""
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest  # noqa: E402

from portal import tiempo  # noqa: E402


class _RelojFijo(datetime):
    """datetime cuyo «ahora» es siempre el mismo instante (2026-03-06 01:00 UTC, que en
    Chile todavía es la noche del 5 de marzo)."""

    @classmethod
    def now(cls, tz=None):
        instante = datetime(2026, 3, 6, 1, 0, tzinfo=timezone.utc)
        return instante.astimezone(tz) if tz else instante.replace(tzinfo=None)


def test_a_hoy_es_el_dia_de_chile_aunque_el_servidor_ya_este_en_manana(monkeypatch):
    monkeypatch.setattr(tiempo, "datetime", _RelojFijo)
    assert tiempo.hoy_chile() == date(2026, 3, 5)


@pytest.mark.parametrize("instante,esperado", [
    (datetime(2026, 3, 6, 2, 30), "2026-03-05"),    # verano: UTC-3
    (datetime(2026, 7, 15, 3, 30), "2026-07-14"),   # invierno: UTC-4
    (datetime(2026, 7, 15, 12, 0), "2026-07-15"),
    (datetime(2026, 3, 6, 2, 30, tzinfo=timezone.utc), "2026-03-05"),
    (date(2026, 3, 5), "2026-03-05"),               # un día ya es un día
    (None, None),
])
def test_a_un_instante_cae_en_su_dia_de_chile(instante, esperado):
    assert tiempo.dia_chile(instante) == esperado


@pytest.mark.parametrize("guardado,esperado", [
    (datetime(2026, 9, 10), "2026-09-10"),            # lo que escribe routers/despachos.py al firmar
    (datetime(2026, 1, 1), "2026-01-01"),
    (datetime(2026, 3, 6, 1, 30), "2026-03-05"),      # sin día tecleado quedó el reloj del servidor
    (date(2026, 9, 10), "2026-09-10"),
    (None, None),
])
def test_b_un_dia_tecleado_no_se_corre(guardado, esperado):
    assert tiempo.dia_tecleado(guardado) == esperado


REFERENCIA = date(2026, 6, 20)


@pytest.mark.parametrize("texto,esperado", [
    ("2026-06-10", "2026-06-10"), ("10/06/2026", "2026-06-10"), ("10-06-2026", "2026-06-10"),
    ("10.06.2026", "2026-06-10"), ("2026/06/10", "2026-06-10"), ("  10/06/2026  ", "2026-06-10"),
    ("2026-06-10T15:30:00", "2026-06-10"), ("10/06/2026 15:30", "2026-06-10"),
    ("10/06/26", "2026-06-10"), ("10-06-26", "2026-06-10"),
    # Año de dos dígitos que cae lejos de la referencia: es otra cosa, no una fecha de esta orden.
    ("26-06-10", None), ("12-06-10", None), ("10/06/99", None),
    ("31/12/1999", None), ("01/01/2101", None),
    ("20260610", None), ("10-jun-2026", None), ("31/02/2026", None), ("no es una fecha", None),
    ("", None), ("   ", None), (None, None),
])
def test_c_fecha_escrita_a_mano(texto, esperado):
    assert tiempo.fecha_de_texto(texto, REFERENCIA) == esperado


def test_c_sin_referencia_no_se_acepta_un_anio_de_dos_digitos():
    assert tiempo.fecha_de_texto("10/06/26") is None
    assert tiempo.fecha_de_texto("10/06/2026") == "2026-06-10"


def test_c_la_holgura_del_anio_corto_es_de_dos_anios():
    assert tiempo.fecha_de_texto("10/07/24", REFERENCIA) == "2024-07-10"   # menos de dos años antes
    assert tiempo.fecha_de_texto("10/05/28", REFERENCIA) == "2028-05-10"   # menos de dos años después
    assert tiempo.fecha_de_texto("10/06/23", REFERENCIA) is None           # tres años antes
    assert tiempo.fecha_de_texto("10/06/29", REFERENCIA) is None           # tres años después
