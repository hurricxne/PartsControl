"""Endurecimiento previo al pase del Portal de Clientes (2026-10-06) — hallazgos MEDIO 1 y 2.

  1. Límite de intentos por (llave, IP): 5 claves malas desde una IP no dejan fuera al
     dueño que entra desde otra; la fuerza bruta repartida topa en MAX_FALLOS_POR_CUENTA.
  2. /api/clientes solo para Grupo AM (y soporte @bigcode.cl): un usuario de MonzaParts
     recibe 403 y la ficha no cambia.

Escenarios: features/portal/endurecimiento-login-y-fichas.feature.
La parte 1 es pura (memoria del limitador, sin base). La parte 2 inserta UNA ficha marcada
`__TEST_MEDIO2__` y la borra en `finally`.
Corre con: cd backend && ./venv/bin/python -m pytest routers/tests/test_endurecimiento_medios.py -q
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import rate_limit_login as rl  # noqa: E402
from auth import get_current_user  # noqa: E402
from database import SessionLocal  # noqa: E402
from models.models import Cliente  # noqa: E402
from routers import clientes as clientes_router  # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _intento(llave, ip):
    """True si el limitador admite el intento (y lo anota), False si responde 429."""
    try:
        rl.reservar_intento(llave, ip, correo="prueba@test.invalid")
        return True
    except HTTPException as e:
        assert e.status_code == 429, e.status_code
        return False


def parte_1_limitador():
    rl.reset()
    victima = rl.llave_de_cuenta(910001)
    atacante, oficina = "203.0.113.7", "198.51.100.20"

    # 1a · 5 fallos desde la IP del atacante lo bloquean a ÉL...
    admitidos = [_intento(victima, atacante) for _ in range(rl.MAX_FALLOS_POR_LLAVE + 1)]
    check("1a la IP que falla 5 veces queda en 429 al 6.º intento",
          admitidos == [True] * rl.MAX_FALLOS_POR_LLAVE + [False], admitidos)
    # 1b · ...pero el dueño entra desde su oficina
    check("1b el dueño entra desde otra IP pese a los 5 fallos ajenos", _intento(victima, oficina))
    rl.limpiar_exito(victima, oficina)
    check("1c el diagnóstico desde la IP del atacante dice bloqueado por 'fallos'",
          rl.estado(victima, atacante)["motivo"] == rl.MOTIVO_FALLOS, rl.estado(victima, atacante))

    # 1d · éxito: limpia los fallos de SU IP, no el tope de la cuenta
    rl.reset()
    for _ in range(4):
        _intento(victima, oficina)
    _intento(victima, oficina)            # el 5.º: la clave correcta
    rl.limpiar_exito(victima, oficina)
    est = rl.estado(victima, oficina)
    check("1d tras entrar bien, los fallos de su IP quedan en 0", est["intentos_fallidos"] == 0, est)
    check("1e pero siguen contando para el tope de la cuenta (5)", est["fallos_cuenta"] == 5, est)
    check("1f y puede volver a equivocarse 5 veces desde su IP",
          all(_intento(victima, oficina) for _ in range(rl.MAX_FALLOS_POR_LLAVE)))

    # 1g · fuerza bruta repartida: MAX_FALLOS_POR_CUENTA en total bloquea desde una IP nueva
    rl.reset()
    n_ips = rl.MAX_FALLOS_POR_CUENTA // rl.MAX_FALLOS_POR_LLAVE
    for i in range(n_ips):
        for _ in range(rl.MAX_FALLOS_POR_LLAVE):
            _intento(victima, f"192.0.2.{i + 1}")
    check(f"1g {rl.MAX_FALLOS_POR_CUENTA} fallos repartidos en {n_ips} IPs: una IP nueva recibe 429",
          not _intento(victima, "192.0.2.250"), rl.estado(victima, "192.0.2.250"))
    check("1h el motivo es el tope de la cuenta",
          rl.estado(victima, "192.0.2.250")["motivo"] == rl.MOTIVO_CUENTA, rl.estado(victima, "192.0.2.250"))
    check("1i otra cuenta no se ve afectada", _intento(rl.llave_de_cuenta(910002), "192.0.2.250"))

    # 1j · sin IP que distinga al cliente (nginx sin X-Forwarded-For) rige la llave sola
    rl.reset()
    for _ in range(rl.MAX_FALLOS_POR_LLAVE):
        _intento(victima, "127.0.0.1")
    check("1j con IP de loopback el contador es la llave: el 6.º intento recibe 429",
          not _intento(victima, "127.0.0.1"))
    rl.reset()


def parte_2_fichas():
    app = FastAPI()
    app.include_router(clientes_router.router, prefix="/api")
    cliente = TestClient(app)
    # Ficha propia, marcada, insertada directo (sin el endpoint: no re-vincula ventas).
    db = SessionLocal()
    try:
        ficha = Cliente(nombre="__TEST_MEDIO2__ ficha", rut="11.111.111-1")
        db.add(ficha)
        db.commit()
        antes = (ficha.id, ficha.rut, ficha.nombre)
    finally:
        db.close()
    try:
        _parte_2_contra(cliente, app, antes)
    finally:
        db = SessionLocal()
        try:
            db.query(Cliente).filter(Cliente.nombre.like("__TEST_MEDIO2__%")).delete(
                synchronize_session=False)
            db.commit()
        finally:
            db.close()
        app.dependency_overrides.clear()


def _parte_2_contra(cliente, app, antes):

    def como(empresa, email):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=None, empresa=empresa, email=email, tipo_acceso="interno")

    como("automotriz", "vendedor@monza.test")
    r = cliente.get("/api/clientes/")
    check("2a MonzaParts lista fichas de Grupo AM → 403", r.status_code == 403, r.status_code)
    r = cliente.post("/api/clientes/", json={"nombre": "__TEST_MEDIO2__", "rut": "11.111.111-1"})
    check("2b MonzaParts crea una ficha → 403", r.status_code == 403, r.status_code)
    r = cliente.put(f"/api/clientes/{antes[0]}", json={"nombre": "__TEST_MEDIO2__ cambiado"})
    check("2c MonzaParts edita una ficha → 403", r.status_code == 403, r.status_code)
    r = cliente.delete(f"/api/clientes/{antes[0]}")
    check("2d MonzaParts borra una ficha → 403", r.status_code == 403, r.status_code)
    db = SessionLocal()
    try:
        f = db.query(Cliente).filter(Cliente.id == antes[0]).first()
        check("2e la ficha no cambió", f is not None and (f.id, f.rut, f.nombre) == antes,
              (f.id, f.rut, f.nombre) if f else None)
        check("2f MonzaParts no logró crear otra ficha",
              db.query(Cliente).filter(Cliente.nombre.like("__TEST_MEDIO2__%")).count() == 1)
    finally:
        db.close()

    como("mineria", "operador@grupoam.test")
    r = cliente.get("/api/clientes/")
    check("2g Grupo AM lista sus fichas → 200", r.status_code == 200, r.status_code)
    como("automotriz", "soporte@bigcode.cl")
    r = cliente.get("/api/clientes/")
    check("2h soporte @bigcode.cl lista las fichas → 200", r.status_code == 200, r.status_code)


def test_endurecimiento_medios():
    parte_1_limitador()
    parte_2_fichas()
    print()
    print("RESULTADO:", "TODO OK" if not _fails else f"{len(_fails)} fallas: {_fails}")
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-s"]))
