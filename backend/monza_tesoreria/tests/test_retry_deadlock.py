"""Regresión del HALLAZGO #1 de la auditoría integral (deadlock 1213 sin reintento
entre Conciliación Bancaria y Cobranzas).

Cubre las DOS capas del arreglo en monza_tesoreria/router.py, sin BD y sin datos
reales (no crea ni toca filas: todo es introspección + una sesión falsa):
  · CAPA 1 — `_conciliar_tx` bloquea en el orden canónico FACTURA → COBRANZA (antes
    era un JOIN con_for_update cuya tabla motriz era la cobranza, o sea al revés que
    Facturas y Cobranzas: ese cruce es el ciclo InnoDB que se medía 6/6 rondas).
  · CAPA 2 — el endpoint `conciliar` delega en `_con_retry_deadlock`, que reintenta
    la transacción completa ante 1213/1205 y responde 409 accionable en vez de 500.

Corre con:  cd backend && ./venv/bin/python monza_tesoreria/tests/test_retry_deadlock.py
(también:   ./venv/bin/python -m pytest monza_tesoreria/tests/test_retry_deadlock.py -q)
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from monza_tesoreria.router import (  # noqa: E402
    _con_retry_deadlock, _conciliar_tx, conciliar,
)

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
        # de 409: se propaga tal cual para no esconder un bug real.
        _con_retry_deadlock(db, lambda: (_ for _ in ()).throw(_error_mysql(1146)))
        check("error no-deadlock se propaga", False, "no levantó")
    except OperationalError:
        check("error no-deadlock (1146) se propaga sin reintentar", db.rollbacks == 1, db.rollbacks)
    except HTTPException as e:
        check("error no-deadlock (1146) se propaga sin reintentar", False, f"lo convirtió en {e.status_code}")

    # ── CAPA 2: el endpoint delega en el helper (el cuerpo es reintentable) ──
    src_endpoint = inspect.getsource(conciliar)
    check("el endpoint conciliar envuelve el cuerpo en _con_retry_deadlock",
          "_con_retry_deadlock" in src_endpoint and "_conciliar_tx" in src_endpoint, src_endpoint)

    # ── CAPA 1: orden de locks FACTURA → COBRANZA en la vía cobranza ──
    src_tx = inspect.getsource(_conciliar_tx)
    # El SELECT ... FOR UPDATE de la factura debe aparecer ANTES del de la cobranza.
    # El ancla del lock de la cobranza se busca DESDE su propia query, no como el primer
    # `.populate_existing().with_for_update()` del texto: las otras ramas (egreso /
    # adelanto) también leen bajo lock y una de ellas aparece ANTES en el archivo, así
    # que el ancla suelta daba un falso positivo en cuanto alguien endurecía otra rama
    # (pasó al agregar populate_existing al adelanto). La intención del check no cambia.
    i_fact = src_tx.find("MonzaContFacturaCliente.id == factura_id_previa")
    i_cob = src_tx.find("cobranza = (db.query(MonzaContCobranza)")
    i_cob_lock = src_tx.find(".populate_existing().with_for_update()", i_cob)
    check("la factura se bloquea PRIMERO y la cobranza DESPUÉS",
          i_fact > 0 and i_cob > 0 and i_cob_lock > 0 and i_fact < i_cob_lock,
          (i_fact, i_cob, i_cob_lock))
    # El JOIN bloqueante (cobranza motriz + factura arrastrada) es justo lo que invertía
    # el orden respecto de Facturas y Cobranzas: no debe volver.
    check("ya no queda el JOIN con_for_update que invertía el orden",
          ".join(MonzaContFacturaCliente" not in src_tx, "sigue el JOIN bloqueante")
    check("revalida la pertenencia cobranza→factura tras tomar los locks",
          "cobranza.factura_id != _factura.id" in src_tx)
    check("la relectura bajo lock usa populate_existing (no se queda con valores viejos)",
          "populate_existing" in src_tx)


def test_monza_tesoreria_retry_deadlock():
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
