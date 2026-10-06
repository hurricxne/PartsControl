"""El 403 de "no hay sesión" se distingue del 403 de "tu sesión no alcanza".
Fase 0 del Portal de Clientes (2026-08-20).

QUÉ PROTEGE
    `auth.py` usa `HTTPBearer()`, que responde **403** —no 401— cuando falta el header
    Authorization. Sin distinguirlo, el frontend no puede saber si tiene que mandar al
    usuario al login (no hay sesión) o solo avisarle (la sesión no alcanza para esa
    marca o ese rol). `main.py` traduce el primer caso a 401 usando esta función.

    Se prueba acá, y no contra la app entera, porque importar `main` arrastra el
    create_all, los 30 routers y el planificador: un test de una decisión de tres
    líneas no debería pagar eso.

SONDA DE PODER DISCRIMINANTE
    Si alguien "simplifica" `es_403_por_falta_de_sesion` a `status_code == 403` (o sea,
    tratar TODO 403 como falta de sesión), caen `test_2` y `test_3`: los guards de
    empresa y de rol empezarían a desloguear a gente que sí tenía sesión válida.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_auth_errors_clasificacion.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from auth_errors import es_403_por_falta_de_sesion, DETALLES_SIN_SESION  # noqa: E402

# Las etapas se llaman `_paso_*` y NO `test_*` a propósito: `check()` acumula en
# `_fails` en vez de lanzar, así que si pytest las recolectara por separado cada una
# sería un VERDE INCONDICIONAL. Verificado por el comité: forzando
# `es_403_por_falta_de_sesion` a devolver siempre False, las cinco seguían pasando.
# La única función recolectable es la de más abajo, que revisa `_fails`.
_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _paso_1_falta_de_sesion_se_reclasifica():
    for detalle in DETALLES_SIN_SESION:
        check(f"403 '{detalle}' cuenta como falta de sesión",
              es_403_por_falta_de_sesion(403, detalle) is True)


def _paso_2_el_guard_de_empresa_sigue_siendo_403():
    # Texto EXACTO de backend/empresa_guard.py
    check("el 403 de empresa NO se reclasifica",
          es_403_por_falta_de_sesion(403, "Acceso restringido a esta empresa") is False)


def _paso_3_el_guard_de_rol_sigue_siendo_403():
    # Texto EXACTO de backend/role_guard.py
    check("el 403 de rol NO se reclasifica",
          es_403_por_falta_de_sesion(403, "Acceso restringido para tu rol") is False)


def _paso_4_otros_codigos_no_se_tocan():
    for codigo in (200, 400, 401, 404, 409, 422, 429, 500):
        check(f"el {codigo} pasa intacto",
              es_403_por_falta_de_sesion(codigo, "Not authenticated") is False)


def _paso_5_detalles_raros_no_rompen():
    for detalle in (None, "", 123, {"a": 1}, ["x"]):
        check(f"detalle {detalle!r} no revienta ni reclasifica",
              es_403_por_falta_de_sesion(403, detalle) is False)


def run():
    _paso_1_falta_de_sesion_se_reclasifica()
    _paso_2_el_guard_de_empresa_sigue_siendo_403()
    _paso_3_el_guard_de_rol_sigue_siendo_403()
    _paso_4_otros_codigos_no_se_tocan()
    _paso_5_detalles_raros_no_rompen()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_auth_errors_clasificacion():
    run()


if __name__ == "__main__":
    run()
