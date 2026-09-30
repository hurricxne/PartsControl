"""Configuración común de pytest del backend.

Registro manual verificado de folios MonzaParts (2026-09-30, verificacion_folio.py): con
MONZA_FOLIO_VERIFICACION en true (default, PROD) todo registro manual de factura/boleta
exige origen y consulta Wasabil. Las suites anteriores registran facturas manuales sin
origen, con folios no numéricos y sin simular Wasabil, así que corren con la regla
ANTERIOR (flag apagado) sin depender de una variable de entorno. La suite propia
(monza_contabilidad/tests/test_folio_manual_verificado.py) lo enciende ella misma.

OJO: este archivo solo lo carga pytest. Las suites que registran facturas manuales y se
corren como script (`./venv/bin/python .../test_x.py` sin pytest) necesitan
`MONZA_FOLIO_VERIFICACION=false` en el entorno. Correrlas con pytest.
"""
import pytest

from config import settings


@pytest.fixture(autouse=True)
def _regla_anterior_del_folio_manual():
    original = settings.MONZA_FOLIO_VERIFICACION
    settings.MONZA_FOLIO_VERIFICACION = False
    yield
    settings.MONZA_FOLIO_VERIFICACION = original
