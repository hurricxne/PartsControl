"""Textos de MonzaParts más largos que su columna: 422 claro, no un 500 (monza_largos).

Casos reales de PROD que hoy terminaban en 500 y en el registro perdido:
  · VIN escrito en el campo Año (10 máx.) al crear un lead (2026-10-09, 3 intentos) y
    una cotización (2026-08-20, 3 intentos);
  · enlace del proveedor en el N° de parte desde la calculadora (2026-10-27).
Los ítems de lead ya validaban desde 2026-09-25 (test_lead_items_largo.py).

Lo que se fija: el mensaje es texto (el frontend solo muestra `detail` si es string),
dice qué hacer, y el rechazo no deja nada creado ni modificado. «XXXX» (año desconocido)
y los espacios sobrantes en el año siguen entrando.

Sin red. Datos con MARK propio y limpieza al inicio y al final.
Corre con:  ./venv/bin/python -m pytest monza_tests/test_monza_largos_cabecera.py -q
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from database import SessionLocal
from monza_largos import validar_largos
from monza_models import (MonzaCliente, MonzaCotizacion, MonzaLead, MonzaLeadActividad,
                          MonzaLeadItem, MonzaLog)
from monza_router_cotizaciones import router as cot_router
from monza_router_cotizador import router as cotizador_router
from monza_router_leads import router as leads_router

MARK = "test-monza-largos"
EMAIL = f"{MARK}@test.invalid"
VIN = "WDB907633LP121734"
ENLACE = "https://www.euro-amp.com/collections/bmw/products/" + "x" * 80


class _Usuario:
    id, email, empresa, rol = 1, EMAIL, "automotriz", "admin"


@pytest.fixture()
def cli():
    app = FastAPI()
    for r in (leads_router, cot_router, cotizador_router):
        app.include_router(r)
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


def _contar(modelo, *filtros):
    db = SessionLocal()
    try:
        return db.query(modelo).filter(*filtros).count()
    finally:
        db.close()


def _lead(cli, sufijo, **extra):
    return cli.post("/api/monza/leads", json={
        "cliente": {"nombre": f"{MARK} {sufijo}", "telefono": "912345671"},
        "marca": "Mercedes", "modelo": "Sprinter", **extra})


def test_vin_en_el_ano_al_crear_lead_da_422_que_orienta_y_no_crea_nada(cli):
    r = _lead(cli, "A", anio=VIN, vin="")
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "Eso parece un VIN: va en el campo VIN, no en Año."
    assert _contar(MonzaCliente, MonzaCliente.nombre == f"{MARK} A") == 0, \
        "la validación corre antes de crear el cliente al vuelo y el lead"


def test_ano_con_espacios_o_desconocido_sigue_entrando(cli):
    r = _lead(cli, "B", anio=" 2015\t", vin=VIN)
    assert r.status_code == 201, r.text
    db = SessionLocal()
    try:
        assert db.get(MonzaLead, r.json()["id"]).anio == "2015", "los espacios a los lados se quitan"
    finally:
        db.close()
    assert _lead(cli, "C", anio="XXXX", vin=VIN).status_code == 201, "«XXXX» = año desconocido, se acepta"


def test_otro_texto_largo_en_la_cabecera_dice_campo_y_largo(cli):
    r = _lead(cli, "D", anio="dos mil quince", vin=VIN)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "El año admite hasta 10 caracteres y tiene 14."


def test_vin_en_el_ano_al_crear_cotizacion_da_422_sin_crearla(cli):
    db = SessionLocal()
    try:
        cliente = MonzaCliente(nombre=f"{MARK} E", telefono="912345672")
        db.add(cliente)
        db.commit()
        cid = cliente.id
    finally:
        db.close()
    item = {"descripcion": "Parachoque", "cantidad": 1, "precio_unitario_clp": 1000}
    r = cli.post("/api/monza/cotizaciones", json={"cliente_id": cid, "anio": VIN, "items": [item]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "Eso parece un VIN: va en el campo VIN, no en Año."

    r = cli.post("/api/monza/cotizaciones", json={
        "cliente_id": cid, "anio": "2018", "items": [item, {**item, "numero_parte": ENLACE}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("Repuesto 2: El N° de parte admite hasta 100"), r.json()["detail"]
    assert _contar(MonzaCotizacion, MonzaCotizacion.cliente_id == cid) == 0


def test_enlace_en_numero_parte_desde_la_calculadora_da_422_y_no_toca_el_item(cli):
    r = _lead(cli, "F", anio="2018", vin=VIN,
              items=[{"descripcion": "Faro", "numero_parte": "A9068200161"}])
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    db = SessionLocal()
    try:
        item = db.query(MonzaLeadItem).filter(MonzaLeadItem.lead_id == lid).one()
        iid = item.id
    finally:
        db.close()
    r = cli.post("/api/monza/cotizador/aplicar", json={"lead_id": lid, "items": [
        {"item_id": iid, "calidad": "oem", "precio_clp": 50000, "numero_parte": ENLACE}]})
    assert r.status_code == 422, r.text
    detalle = r.json()["detail"]
    assert detalle.startswith("Repuesto 1: El N° de parte admite hasta 100") and "enlace" in detalle, detalle
    db = SessionLocal()
    try:
        item = db.get(MonzaLeadItem, iid)
        assert (item.numero_parte, item.precio_clp) == ("A9068200161", None), \
            "el rechazo no puede dejar el ítem a medio aplicar"
    finally:
        db.close()


def test_el_validador_solo_mira_campos_conocidos_de_la_tabla():
    validar_largos({"comentario": "x" * 5000, "anio": None, "otro": "y" * 999}, MonzaLead.__table__)
