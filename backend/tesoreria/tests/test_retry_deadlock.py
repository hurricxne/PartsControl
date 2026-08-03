"""Regresión del hallazgo T1 de la paridad contable (deadlock 1213 sin reintento entre
Tesorería y Facturas y Cobranzas de Grupo AM).

Espejo de monza_tesoreria/tests/test_retry_deadlock.py. Acá vive SOLO la prueba unitaria
del helper `_con_retry_deadlock` (sin BD y sin datos reales: una sesión falsa): que
reintenta ante 1213/1205, que hace rollback antes de cada reintento, que termina en un 409
accionable y que NO disfraza los errores que no son deadlock ni las HTTPException de negocio.

Lo demás se probaba acá leyendo el CÓDIGO FUENTE (`inspect.getsource` + `find`) y esas
sondas NO discriminaban: un auditor reintrodujo el ciclo InnoDB dejando intactas las cadenas
que buscaban (gate verde con el bug de vuelta) y, al revés, agregar un `populate_existing()`
las ponía rojas sin cambio de conducta. Se movieron a sondas de CONDUCTA con dos sesiones
MySQL reales:
  · orden de candados (CAPA 1 y 2 de los dos pares) y candado de empresa
    → `tesoreria/tests/test_locks_concurrencia.py`
  · `populate_existing()` de las lecturas de plata
    → `tesoreria/tests/test_lecturas_de_plata.py`

Corre con:  cd backend && ./venv/bin/python tesoreria/tests/test_retry_deadlock.py
(también:   ./venv/bin/python -m pytest tesoreria/tests/test_retry_deadlock.py -q)
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from tesoreria.router import _con_retry_deadlock, aprobar_adelanto  # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


class _DbFalsa:
    """Sesión mínima: solo cuenta rollbacks (el helper no necesita nada más)."""

    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def _error_mysql(codigo: int) -> OperationalError:
    orig = Exception()
    orig.args = (codigo, "Deadlock found when trying to get lock; try restarting transaction")
    return OperationalError("SELECT 1", {}, orig)


def run():
    # ── CAPA 2: el helper de reintento ──
    db = _DbFalsa()
    intentos = {"n": 0}

    def _op_que_se_recupera():
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise _error_mysql(1213)
        return {"ok": True}

    check("1213 dos veces → reintenta y devuelve el resultado",
          _con_retry_deadlock(db, _op_que_se_recupera) == {"ok": True} and intentos["n"] == 3)
    check("hizo rollback antes de cada reintento", db.rollbacks == 2, db.rollbacks)

    db = _DbFalsa()
    try:
        _con_retry_deadlock(db, lambda: (_ for _ in ()).throw(_error_mysql(1205)))
        check("1205 persistente → 409", False, "no levantó")
    except HTTPException as e:
        check("1205 persistente → 409 accionable (no 500)",
              e.status_code == 409 and "reintente" in e.detail.lower(), e.detail)
    check("agotó los 3 intentos con rollback en cada uno", db.rollbacks == 3, db.rollbacks)

    db = _DbFalsa()
    try:
        # Un error de BD que NO es deadlock/lock-timeout NO se reintenta ni se disfraza
        # de 409: se propaga tal cual para no esconder un bug real (ej. 1146 tabla ausente).
        _con_retry_deadlock(db, lambda: (_ for _ in ()).throw(_error_mysql(1146)))
        check("error no-deadlock se propaga", False, "no levantó")
    except OperationalError:
        check("error no-deadlock (1146) se propaga sin reintentar", db.rollbacks == 1, db.rollbacks)
    except HTTPException as e:
        check("error no-deadlock (1146) se propaga sin reintentar", False, f"lo convirtió en {e.status_code}")

    # Una HTTPException del cuerpo (409/400/404 de negocio) NO se reintenta: sube tal cual.
    db = _DbFalsa()
    try:
        _con_retry_deadlock(db, lambda: (_ for _ in ()).throw(HTTPException(400, "montos no coinciden")))
        check("HTTPException de negocio se propaga sin reintentar", False, "no levantó")
    except HTTPException as e:
        check("HTTPException de negocio se propaga sin reintentar (0 rollbacks)",
              e.status_code == 400 and db.rollbacks == 0, (e.status_code, db.rollbacks))

    # ── T12: la relectura de plata de aprobar_adelanto usa populate_existing ──
    # Estructura, no texto: se cuentan las relecturas bloqueantes que SÍ repueblan la fila.
    # (Las 2 que deciden plata —el adelanto y sus facturas— deben llevarlo; el tercer lock,
    # la OC, es solo el portón de serialización y no lee montos.)
    src_apr = inspect.getsource(aprobar_adelanto)
    check("las 2 relecturas de plata de aprobar_adelanto llevan populate_existing",
          src_apr.count("populate_existing().with_for_update") == 2,
          src_apr.count("populate_existing().with_for_update"))


def test_tesoreria_retry_deadlock():
    """Wrapper para pytest (mismo patrón que test_service.py): sin él los checks
    correrían en el import y un fallo pasaría en silencio (verde falso)."""
    run()
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    run()
    print()
    if _fails:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
