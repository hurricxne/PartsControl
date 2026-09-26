"""Textos de un ítem de lead más largos que su columna: 422 claro, no un 500 crudo.

Incidente 2026-09-25 (PROD): dos veces ese día, al agregar un repuesto a un lead con el
N° de parte de más de 100 caracteres (la descripción pegada en el campo equivocado), la
base lo rechazó con "Data too long for column 'numero_parte'" y el usuario vio un 500
sin explicación. Los tres caminos que escriben ítems de lead validan ahora el largo
contra la columna del modelo: agregar ítem, editar ítem y crear lead con ítems.

Lo que se fija:
  · el límite exacto (100 pasa, 101 no) y que cuente CARACTERES, no bytes;
  · el mensaje dice qué campo, cuánto admite y cuánto tiene;
  · el rechazo no deja nada a medias: ni ítem nuevo, ni ítem editado, ni lead ni
    cliente creados al vuelo.

Sin red. Datos con MARK propio y limpieza al inicio y al final.
Corre con:  ./venv/bin/python -m pytest monza_tests/test_lead_items_largo.py -q
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from database import SessionLocal
from monza_models import MonzaCliente, MonzaLead, MonzaLeadActividad, MonzaLeadItem, MonzaLog
from monza_router_leads import router as leads_router

MARK = "test-lead-largo"
EMAIL = f"{MARK}@test.invalid"


class _Usuario:
    id, email, empresa, rol = 1, EMAIL, "automotriz", "admin"


@pytest.fixture()
def cli():
    app = FastAPI()
    app.include_router(leads_router)
    app.dependency_overrides[get_current_user] = lambda: _Usuario()
    return TestClient(app)


def _limpiar():
    db, S = SessionLocal(), "fetch"
    ids = [r[0] for r in db.query(MonzaCliente.id).filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
    lids = [r[0] for r in db.query(MonzaLead.id).filter(MonzaLead.cliente_id.in_(ids or [0])).all()]
    db.query(MonzaLeadActividad).filter(MonzaLeadActividad.lead_id.in_(lids or [0])).delete(synchronize_session=S)
    db.query(MonzaLeadItem).filter(MonzaLeadItem.lead_id.in_(lids or [0])).delete(synchronize_session=S)
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


def _lead(cli, sufijo="A"):
    r = cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} {sufijo}", "telefono": "912345678"}, "marca": "Toyota"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _items(lead_id):
    db = SessionLocal()
    try:
        return [(i.id, i.numero_parte, i.descripcion)
                for i in db.query(MonzaLeadItem).filter(MonzaLeadItem.lead_id == lead_id).all()]
    finally:
        db.close()


def test_agregar_item_con_numero_parte_largo_da_422_claro_y_no_guarda(cli):
    lid = _lead(cli)
    r = cli.post(f"/api/monza/leads/{lid}/items",
                 json={"descripcion": "Parachoque", "numero_parte": "X" * 101})
    assert r.status_code == 422, r.text
    detalle = r.json()["detail"]
    assert isinstance(detalle, str), "el frontend muestra el detalle solo si es texto"
    assert "N° de parte" in detalle and "100" in detalle and "101" in detalle, detalle
    assert "Descripción" in detalle, "debe orientar: la descripción va en su campo"
    assert _items(lid) == [], "el rechazo no puede dejar un ítem guardado"


def test_el_limite_es_exacto_y_cuenta_caracteres_no_bytes(cli):
    lid = _lead(cli)
    # 100 caracteres con tildes y ñ: en bytes son más de 100, en caracteres son 100
    np = ("ÑANDÚ-" * 20)[:100]
    assert len(np) == 100 and len(np.encode("utf-8")) > 100
    r = cli.post(f"/api/monza/leads/{lid}/items", json={"descripcion": "Parachoque", "numero_parte": np})
    assert r.status_code == 201, r.text
    assert _items(lid)[0][1] == np


def test_descripcion_larga_tambien_da_422_con_su_nombre(cli):
    lid = _lead(cli)
    r = cli.post(f"/api/monza/leads/{lid}/items", json={"descripcion": "D" * 301})
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("La descripción admite hasta 300 caracteres y tiene 301")


def test_editar_item_con_numero_parte_largo_da_422_y_no_lo_cambia(cli):
    lid = _lead(cli)
    iid = cli.post(f"/api/monza/leads/{lid}/items",
                   json={"descripcion": "Parachoque", "numero_parte": "52119-0K010"}).json()["id"]
    r = cli.put(f"/api/monza/leads/{lid}/items/{iid}", json={"numero_parte": "X" * 101})
    assert r.status_code == 422, r.text
    assert "N° de parte" in r.json()["detail"]
    assert _items(lid)[0][1] == "52119-0K010", "el ítem no puede quedar modificado"


def test_crear_lead_con_item_largo_no_deja_lead_ni_cliente_a_medias(cli):
    r = cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} B", "telefono": "912345679"}, "marca": "Toyota",
        "items": [{"descripcion": "Mascara", "numero_parte": "OK-1"},
                  {"descripcion": "Parachoque", "numero_parte": "X" * 150}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("Repuesto 2: El N° de parte admite hasta 100"), r.json()["detail"]
    db = SessionLocal()
    try:
        clientes = db.query(MonzaCliente).filter(MonzaCliente.nombre == f"{MARK} B").count()
    finally:
        db.close()
    assert clientes == 0, "la validación corre ANTES de crear el cliente al vuelo y el lead"


def test_crear_lead_ignora_items_vacios_igual_que_antes(cli):
    """Un ítem sin descripción se descarta en la creación (regla previa): su texto no se
    valida, porque no se va a guardar."""
    r = cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} C", "telefono": "912345670"}, "marca": "Toyota",
        "items": [{"descripcion": "   ", "numero_parte": "X" * 150},
                  {"descripcion": "Mascara", "numero_parte": "OK-1"}]})
    assert r.status_code == 201, r.text
    assert [np for _i, np, _d in _items(r.json()["id"])] == ["OK-1"]
