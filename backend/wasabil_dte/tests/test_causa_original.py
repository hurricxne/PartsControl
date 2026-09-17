# -*- coding: utf-8 -*-
"""El mensaje de verificacion fallida debe exponer la CAUSA ORIGINAL, no solo el sintoma.

Regresion del incidente 2026-09-17 (MonzaParts DSP-2026-0027): la creacion fallo con un HTTP
500 de Wasabil (tabla ausente en SU base) y el mensaje al operador mostraba unicamente el 405
del listado que usa la verificacion posterior. Con ese dato a la vista, el soporte del
proveedor concluyo que la culpa era del ERP. El error de origen YA estaba guardado en la fila.
"""
from types import SimpleNamespace

from wasabil_dte.router import _causa_original as causa_ga
from monza_wasabil_dte.router import _causa_original as causa_monza

AMBAS = (causa_ga, causa_monza)


def _dte(error):
    return SimpleNamespace(error=error)


def test_sin_causa_no_ensucia_el_mensaje():
    """Sin error previo el sufijo es vacio: el mensaje queda como estaba."""
    for fn in AMBAS:
        assert fn(_dte(None)) == ""
        assert fn(_dte("")) == ""
        assert fn(_dte("   ")) == ""


def test_expone_el_error_de_origen():
    """El caso real: el 500 del proveedor que el mensaje escondia tras el 405 del reintento."""
    causa = ("Wasabil respondió 500: SQLSTATE[42S02]: Base table or view not found: 1146 "
             "Table 'wasabil_prod.taxes' doesn't exist")
    for fn in AMBAS:
        salida = fn(_dte(causa))
        assert "Causa del intento anterior" in salida
        assert "taxes" in salida          # el dato que permite diagnosticar
        assert "500" in salida


def test_causa_larga_se_trunca():
    """El `error` puede traer el JSON completo del proveedor: no debe reventar el mensaje."""
    for fn in AMBAS:
        salida = fn(_dte("x" * 2000))
        assert len(salida) < 500
        assert salida.endswith("…")


def test_no_revienta_con_fila_rara():
    """Un objeto sin `error` (o con un tipo raro) no puede tumbar el manejo del error."""
    for fn in AMBAS:
        assert fn(SimpleNamespace()) == ""
        assert fn(_dte(0)) == ""
