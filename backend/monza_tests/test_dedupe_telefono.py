"""El teléfono no puede fusionar a dos contribuyentes distintos.

Hallazgo CRÍTICO del equipo de testing (2026-08-27), reproducido contra el código vivo:
«EMPRESA A SpA» (76.111.222-3) y «EMPRESA B Ltda» (77.333.444-5) comparten el número de
la recepción. Al crear la segunda, el dedupe por teléfono devolvía la ficha de A y el RUT
recién tecleado se descartaba en silencio, así que la cotización, el cierre y la FACTURA
33 colgaban de la ficha equivocada: el DTE salía al RUT de otro contribuyente.

La misma llave abría con teléfonos de relleno: '-', '0' y '2342' —este último EXISTE hoy
en monza_clientes— fusionaban a cualquier par de clientes.

NOTA DE MÉTODO: los teléfonos de estas pruebas (977770xxx) son propios y no coinciden con
ningún abonado de la base. Una versión anterior de esta sonda usó un número real y el
dedupe —funcionando como debe— completó el RUT de una ficha de verdad. Los datos de
prueba se inventan; no se toman prestados de producción.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from database import SessionLocal
from monza_models import MonzaCliente, MonzaLead, MonzaLeadActividad, MonzaLog
from monza_router_clientes import router as clientes_router
from monza_router_leads import router as leads_router

MARK = "test-dedupe-tel"
EMAIL = f"{MARK}@test.invalid"
TEL_COMPARTIDO = "+56977770001"


class _Usuario:
    id, email, empresa, rol = 1, EMAIL, "automotriz", "admin"


@pytest.fixture()
def cli():
    app = FastAPI()
    app.include_router(clientes_router)
    app.include_router(leads_router)
    app.dependency_overrides[get_current_user] = lambda: _Usuario()
    return TestClient(app)


def _limpiar():
    db, S = SessionLocal(), "fetch"
    ids = [r[0] for r in db.query(MonzaCliente.id).filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
    lids = [r[0] for r in db.query(MonzaLead.id).filter(MonzaLead.cliente_id.in_(ids or [0])).all()]
    db.query(MonzaLeadActividad).filter(MonzaLeadActividad.lead_id.in_(lids or [0])).delete(synchronize_session=S)
    db.query(MonzaLead).filter(MonzaLead.id.in_(lids or [0])).delete(synchronize_session=S)
    db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
    db.query(MonzaCliente).filter(MonzaCliente.id.in_(ids or [0])).delete(synchronize_session=S)
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def limpieza():
    _limpiar()
    yield
    _limpiar()
    db = SessionLocal()
    assert db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count() == 0
    db.close()


def _crear(cli, nombre, rut=None, telefono=None):
    body = {"nombre": f"{MARK} {nombre}"}
    if rut:
        body["rut"] = rut
    if telefono:
        body["telefono"] = telefono
    return cli.post("/api/monza/clientes", json=body)


# ─────────────── El escenario del hallazgo ───────────────

def test_dos_contribuyentes_con_el_mismo_telefono_son_dos_fichas(cli):
    """SONDA: sin el guard, la segunda ficha devuelve el id de la primera."""
    a = _crear(cli, "EMPRESA A SpA", "76.111.222-3", TEL_COMPARTIDO)
    assert a.status_code == 201 and a.json()["reutilizado"] is False

    b = _crear(cli, "EMPRESA B Ltda", "77.333.444-5", TEL_COMPARTIDO)
    assert b.status_code == 201, b.text
    assert b.json()["reutilizado"] is False, "el teléfono fusionó a dos contribuyentes"
    assert b.json()["id"] != a.json()["id"]
    # Y su RUT queda guardado: antes se descartaba en silencio y el DTE salía al de A.
    assert b.json()["rut"] == "77.333.444-5"


@pytest.mark.parametrize("relleno", ["-", "0", "2342", "   ", "."])
def test_telefonos_de_relleno_no_identifican_a_nadie(cli, relleno):
    """'2342' existe hoy en monza_clientes: sin esta regla se comía a todo cliente nuevo."""
    uno = _crear(cli, f"UNO {relleno}", "82.111.222-3", relleno)
    dos = _crear(cli, f"DOS {relleno}", "83.111.222-4", relleno)
    assert uno.json()["id"] != dos.json()["id"], f"'{relleno}' fusionó dos clientes"


# ─────────────── Lo que el dedupe SÍ debe seguir haciendo ───────────────

def test_ficha_sin_rut_se_reconoce_por_el_abonado_en_cualquier_formato(cli):
    """El caso legítimo por el que este dedupe existe: mismo cliente, número tipeado
    como lo tiene en WhatsApp. Une, COMPLETA el RUT y no renombra."""
    vieja = _crear(cli, "TALLER VIEJO", telefono="977770002")
    assert vieja.json()["reutilizado"] is False

    otra = _crear(cli, "TALLER VIEJO tecleado distinto", "90.111.222-7", "+56 9 7777 0002")
    assert otra.json()["reutilizado"] is True
    assert otra.json()["id"] == vieja.json()["id"]
    assert otra.json()["rut"] == "90.111.222-7", "el RUT tecleado debe COMPLETAR la ficha"
    assert otra.json()["nombre"] == f"{MARK} TALLER VIEJO", "la ficha no se renombra"


# ─────────────── La otra puerta: el «cliente al vuelo» del lead ───────────────

def test_lead_al_vuelo_aplica_la_misma_regla(cli):
    """Las dos puertas que crean fichas deben decidir IGUAL: cuando cada una tenía su
    propia comparación, el mismo cliente terminaba con dos fichas según por dónde entrara."""
    a = cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} LEAD A", "rut": "91.111.222-8", "telefono": "977770003"},
        "marca": "Toyota"})
    b = cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} LEAD B", "rut": "92.111.222-9", "telefono": "977770003"},
        "marca": "Nissan"})
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["cliente"]["id"] != b.json()["cliente"]["id"]
    assert b.json()["cliente_reutilizado"] is False


def test_lead_al_vuelo_dedupea_por_rut_antes_que_por_telefono(cli):
    """El orden estaba invertido: el teléfono ganaba y el RUT tecleado se perdía."""
    a = cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} CLI RUT", "rut": "93.111.222-0", "telefono": "977770004"},
        "marca": "Kia"})
    b = cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} CLI RUT otra vez", "rut": "93.111.222-0"},
        "marca": "Kia"})
    assert b.json()["cliente"]["id"] == a.json()["cliente"]["id"]
    assert b.json()["cliente_reutilizado"] is True, "el operador debe enterarse de la fusión"


def test_lead_al_vuelo_completa_rut_y_email_de_la_ficha_vieja(cli):
    """Entrar por teléfono a una ficha sin RUT tiraba a la basura el RUT y el email que
    el operador acababa de teclear: los escribía y no quedaban en ninguna parte."""
    cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} SIN DATOS", "telefono": "977770005"}, "marca": "Ford"})
    r = cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} SIN DATOS", "telefono": "977770005",
                    "rut": "94.111.222-1", "email": "contacto@taller.invalid"},
        "marca": "Ford"})
    assert r.json()["cliente_reutilizado"] is True
    ficha = r.json()["cliente"]
    assert ficha["rut"] == "94.111.222-1"
    assert ficha["email"] == "contacto@taller.invalid"
