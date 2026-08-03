"""El registro de usuarios NO puede ser público. Regresión de un agujero REAL.

Verificado en vivo el 2026-07-22 contra el sistema del dueño: `POST /api/auth/register`
era ANÓNIMO. Un desconocido con solo la dirección del sistema podía:

    POST /api/auth/register            → 201, usuario ACTIVO creado
    POST /api/auth/login               → token válido
    GET  /api/contabilidad/facturas    → 200, TODAS las facturas de GRUPO AM
    GET  /api/contabilidad/kpis        → 200, los KPIs financieros

El usuario nacía sin `empresa`, o sea 'mineria' por el server_default, así que entraba
directo al lado de Grupo AM. Ningún punto del frontend llamaba al endpoint: era código
muerto abierto a internet.

Este test protege las dos mitades del arreglo:
  1. sin sesión (o con token inválido) NO se crea usuario;
  2. el usuario creado HEREDA la empresa de quien lo crea — para que una cuenta de una
     marca no fabrique cuentas de la otra y se salte el candado de empresa.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_auth_register_cerrado.py -q
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from routers.auth import router as auth_router  # noqa: E402

MARK = "test-register-cerrado"

# App SIN override de auth: reproduce al visitante anónimo tal cual llega de internet.
app_anonima = FastAPI()
app_anonima.include_router(auth_router, prefix="/api")
cliente_anonimo = TestClient(app_anonima)

# App CON sesión: el camino legítimo (un usuario ya autenticado crea a otro).
app_con_sesion = FastAPI()
app_con_sesion.include_router(auth_router, prefix="/api")
CURRENT = {"empresa": "automotriz"}
app_con_sesion.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, empresa=CURRENT["empresa"], email="admin@test.invalid")
cliente_con_sesion = TestClient(app_con_sesion)


def _emails_de_prueba():
    db = SessionLocal()
    try:
        return [r[0] for r in db.execute(
            text("SELECT email FROM users WHERE email LIKE :m"), {"m": f"%{MARK}%"})]
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM users WHERE email LIKE :m"), {"m": f"%{MARK}%"})
        db.commit()
    finally:
        db.close()


def test_register_no_es_publico():
    _limpiar()
    try:
        correo = f"anonimo-{uuid.uuid4().hex[:8]}-{MARK}@test.invalid"
        cuerpo = {"email": correo, "nombre": "INTRUSO", "password": "x123456"}

        # 1 · sin ninguna credencial
        r = cliente_anonimo.post("/api/auth/register", json=cuerpo)
        assert r.status_code in (401, 403), \
            f"El registro volvió a quedar ABIERTO: HTTP {r.status_code} — {r.text[:200]}"

        # 2 · con un token inventado
        r = cliente_anonimo.post("/api/auth/register", json=cuerpo,
                                 headers={"Authorization": "Bearer token-falso"})
        assert r.status_code in (401, 403), f"token inválido aceptado: {r.status_code}"

        # 3 · y lo que importa de verdad: NO quedó ningún usuario creado
        assert _emails_de_prueba() == [], \
            f"se creó un usuario sin sesión: {_emails_de_prueba()}"
    finally:
        _limpiar()


def test_usuario_creado_hereda_la_empresa_de_quien_lo_crea():
    """Una cuenta de MonzaParts no puede fabricar cuentas de Grupo AM (ni al revés)."""
    _limpiar()
    db = SessionLocal()
    try:
        correo = f"nuevo-{uuid.uuid4().hex[:8]}-{MARK}@test.invalid"
        CURRENT["empresa"] = "automotriz"
        r = cliente_con_sesion.post("/api/auth/register", json={
            "email": correo, "nombre": "Usuario Monza", "password": "x123456"})
        assert r.status_code == 201, f"el camino legítimo se rompió: {r.status_code} {r.text[:200]}"

        empresa = db.execute(text("SELECT empresa FROM users WHERE email=:e"),
                             {"e": correo}).scalar()
        assert empresa == "automotriz", \
            f"el usuario nuevo quedó en '{empresa}' en vez de heredar 'automotriz'"
    finally:
        db.close()
        _limpiar()


if __name__ == "__main__":
    test_register_no_es_publico()
    test_usuario_creado_hereda_la_empresa_de_quien_lo_crea()
    print("TODO OK — el registro exige sesión y la empresa se hereda")
