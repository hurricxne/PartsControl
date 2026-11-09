"""CORTE DE VISIBILIDAD POR FICHA (clientes.portal_visible_desde) — pedido de HEPI 2026-11-09:
«que solo le aparezcan desde la última orden».

LO QUE SE PROTEGE
  · Sin corte, el cliente ve todas sus ventas (lo de siempre).
  · Con corte, las ventas ingresadas ANTES no aparecen por ningún camino: listado, detalle
    por id (404, igual que una orden ajena), buscador por N° de OC, tablero y años.
  · Una cuenta con dos fichas: el corte de una no toca a la otra.
  · El script portal/visible_desde: sin --aplicar no escribe; --desde-oc toma el ingreso
    de esa OC (ella queda visible); una OC que no es de la ficha no fija nada; --quitar
    vuelve a mostrar todo.

Corre con:  ./venv/bin/python -m pytest portal/tests/test_visible_desde.py -q
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from models.models import Cliente  # noqa: E402
from portal import visible_desde  # noqa: E402
from portal.tests import _datos as D  # noqa: E402

MARK = "ZZPORTALCORTE"  # SIN guiones bajos: en LIKE el _ es comodín
client = TestClient(app)

VIEJA, NUEVA = datetime(2025, 8, 10, 12, 0), datetime(2026, 11, 6, 18, 26, 1)


@pytest.fixture(scope="module")
def datos():
    D.limpiar(MARK)
    with D.carpetas_de_prueba():
        db = SessionLocal()
        try:
            a = D.ficha(db, MARK, "A", 90031001)
            b = D.ficha(db, MARK, "B", 90031002)
            _, oc_vieja, _ = D.venta(db, MARK, "V1", a, [{}], numero_oc=f"{MARK}-OC-VIEJA")
            _, oc_nueva, _ = D.venta(db, MARK, "V2", a, [{}], numero_oc=f"{MARK}-OC-NUEVA")
            _, oc_b, _ = D.venta(db, MARK, "V3", b, [{}], numero_oc=f"{MARK}-OC-B")
            oc_vieja.created_at, oc_nueva.created_at, oc_b.created_at = VIEJA, NUEVA, VIEJA
            cuenta_a = D.portal(db, MARK, "a", [a.id])
            cuenta_ab = D.portal(db, MARK, "ab", [a.id, b.id])
            db.commit()
            yield {"a": a.id, "b": b.id, "vieja": oc_vieja.id, "nueva": oc_nueva.id, "oc_b": oc_b.id,
                   "cab_a": _cab(cuenta_a.email), "cab_ab": _cab(cuenta_ab.email)}
        finally:
            db.close()
            D.limpiar(MARK)


def _cab(email):
    r = client.post("/api/auth/login", json={"email": email, "password": D.CLAVE})
    assert r.status_code == 200, r.text[:120]
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _get(ruta, cab, **params):
    return client.get(f"/api/portal{ruta}", headers=cab, params=params or None)


def _ids(cab, **params):
    return {o["id"] for o in _get("/ordenes", cab, page_size=50, **params).json()["items"]}


def _corte(cliente_id):
    db = SessionLocal()
    try:
        return db.get(Cliente, cliente_id).portal_visible_desde
    finally:
        db.close()


def test_1_sin_corte_se_ve_todo(datos):
    assert _corte(datos["a"]) is None
    assert _ids(datos["cab_a"]) == {datos["vieja"], datos["nueva"]}


def test_2_script_sin_aplicar_no_escribe(datos):
    assert visible_desde.main(["--cliente", str(datos["a"]), "--desde-oc", f"{MARK}-OC-NUEVA"]) == 0
    assert _corte(datos["a"]) is None


def test_3_oc_de_otra_ficha_no_fija_nada(datos):
    assert visible_desde.main(["--cliente", str(datos["a"]), "--desde-oc", f"{MARK}-OC-B", "--aplicar"]) == 1
    assert _corte(datos["a"]) is None


def test_4_con_corte_lo_anterior_no_aparece_por_ningun_camino(datos):
    assert visible_desde.main(["--cliente", str(datos["a"]), "--desde-oc", f"{MARK}-OC-NUEVA", "--aplicar"]) == 0
    assert _corte(datos["a"]) == NUEVA, "el corte es el ingreso de esa OC: ella misma queda visible"
    cab = datos["cab_a"]
    assert _ids(cab) == {datos["nueva"]}
    assert _get(f"/ordenes/{datos['vieja']}", cab).status_code == 404, "por id, igual que una orden ajena"
    assert _get(f"/ordenes/{datos['nueva']}", cab).status_code == 200
    assert _ids(cab, q=f"{MARK}-OC-VIEJA") == set(), "el buscador tampoco la encuentra"
    tablero = _get("/resumen", cab).json()
    assert tablero["ordenes"]["total"] == 1, tablero["ordenes"]
    assert tablero["anios"] == [2026], "el año de la venta oculta no se ofrece"


def test_5_el_corte_de_una_ficha_no_toca_a_la_otra(datos):
    assert _ids(datos["cab_ab"]) == {datos["nueva"], datos["oc_b"]}


def test_6_quitar_vuelve_a_mostrar_todo(datos):
    assert visible_desde.main(["--cliente", str(datos["a"]), "--quitar", "--aplicar"]) == 0
    assert _corte(datos["a"]) is None
    assert _ids(datos["cab_a"]) == {datos["vieja"], datos["nueva"]}
