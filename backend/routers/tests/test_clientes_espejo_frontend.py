"""ESPEJO de la pantalla Clientes: el motivo del 409 de eliminar_cliente llega al operador.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
Borrar una ficha con cuentas del Portal de Clientes se rechaza con 409 y un motivo que
trae el siguiente paso (routers/clientes.py::eliminar_cliente). Ese motivo lo tiene que
PINTAR la pantalla: deleteMutation era la única de las tres mutaciones de
ClientesPage.tsx que descartaba el error y mostraba «Error al eliminar» a secas, así que
el guard fallaba cerrado pero el operador no sabía que debía eliminar las cuentas antes.

QUÉ FIJA (cinturón estático: el frontend no tiene corredor de pruebas; mismo patrón que
monza_tests/test_adelanto_espejo_frontend.py)
    · Mitad pantalla: el onError de deleteMutation RECIBE el error y lee de él
      response?.data?.detail, que es donde axios deja el cuerpo del 409.
    · Mitad backend: eliminar_cliente, ante un IntegrityError al borrar, responde 409 con
      un detail de TEXTO que nombra las cuentas del Portal de Clientes, y deshace la
      transacción (rollback). Se llama a la función real con una sesión de mentira: no
      necesita la base.

DÓNDE BUSCA LA PANTALLA
    En frontend-src, junto a backend/. PARTSCONTROL_FRONTEND_SRC apunta a otra carpeta
    con la misma forma (src/pages/…): sirve desde una copia de backend/ (el arnés de
    mutaciones copia solo backend/) y para probar una COPIA mutada de la pantalla sin
    tocar el árbol de trabajo. Sin la pantalla, esa mitad se SALTA con motivo; sin el
    bloque de deleteMutation, FALLA.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_clientes_espejo_frontend.py -q
"""
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from routers.clientes import eliminar_cliente  # noqa: E402

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_FRONT = (os.environ.get("PARTSCONTROL_FRONTEND_SRC")
          or os.path.join(os.path.dirname(_BACKEND), "frontend-src"))
_PAGINA = os.path.join(_FRONT, "src", "pages", "ClientesPage.tsx")

_INICIO_BLOQUE = "const deleteMutation = useMutation({"
_FIN_BLOQUE = "\n  })"


# ══ Mitad pantalla ═══════════════════════════════════════════════════════════════════

def _bloque_delete_mutation() -> str:
    """El bloque de deleteMutation tal cual está en el .tsx."""
    if not os.path.isfile(_PAGINA):
        pytest.skip(f"no está {_PAGINA} (PARTSCONTROL_FRONTEND_SRC apunta a la carpeta "
                    "frontend-src): la mitad pantalla NO se verificó")
    with open(_PAGINA, encoding="utf-8") as f:
        src = f.read()
    ini = src.find(_INICIO_BLOQUE)
    assert ini != -1, "no se encontró deleteMutation en ClientesPage.tsx: el espejo quedaría ciego"
    fin = src.find(_FIN_BLOQUE, ini)
    assert fin != -1, "deleteMutation no cierra como las demás mutaciones: el espejo quedaría ciego"
    return src[ini:fin + len(_FIN_BLOQUE)]


def test_pantalla_el_onerror_de_eliminar_muestra_el_motivo_del_backend():
    bloque = _bloque_delete_mutation()
    recibe = re.search(r"onError:\s*\(\s*(\w+)[^)]*\)\s*=>", bloque)
    assert recibe, (
        "el onError de deleteMutation no recibe el error: el 409 (ficha con cuentas del "
        "portal) se pinta como «Error al eliminar» a secas, sin el siguiente paso")
    error = recibe.group(1)
    assert f"{error}?.response?.data?.detail" in bloque, (
        f"el onError de deleteMutation recibe `{error}` pero no muestra "
        f"{error}?.response?.data?.detail (el motivo que manda eliminar_cliente)")


# ══ Mitad backend ════════════════════════════════════════════════════════════════════

class _SesionQueNoDejaBorrar:
    """Sesión de mentira: encuentra la ficha y, al hacer flush, la base rechaza el
    borrado (la FK de usuario_cliente): IntegrityError, como MySQL."""

    def __init__(self):
        self.rollbacks = 0
        self.commits = 0
        self.borrados = []

    def query(self, *_):
        return self

    def filter(self, *_):
        return self

    def first(self):
        return SimpleNamespace(id=1, rut="76.000.000-0")

    def delete(self, obj):
        self.borrados.append(obj)

    def flush(self):
        raise IntegrityError("DELETE FROM clientes", {}, Exception("fk usuario_cliente"))

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def test_backend_eliminar_cliente_responde_409_con_motivo_de_texto_y_deshace():
    db = _SesionQueNoDejaBorrar()
    with pytest.raises(HTTPException) as capturada:
        eliminar_cliente(1, db=db, current_user=SimpleNamespace(id=1))
    assert capturada.value.status_code == 409, capturada.value.status_code
    detail = capturada.value.detail
    # Texto, no dict: es lo que la pantalla pinta tal cual en el toast.
    assert isinstance(detail, str), type(detail)
    assert "cuentas del Portal de Clientes" in detail, detail
    assert db.rollbacks == 1, "el borrado rechazado tiene que deshacerse"
    assert db.commits == 0, "no hay nada que confirmar después del rechazo"
