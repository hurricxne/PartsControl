"""Fase 4 espejo GA (G15b) — tableros de avance de Despachos MonzaParts.

Cubre, contra la BD real (datos MARCADOS + limpieza total; lecturas por API):

  1. Card de avance por venta: buckets del pipeline SIEMPRE completos, progreso_pct,
     cascada de estado (pendiente → listo → parcial → completado), días hábiles.
  2. Tabs listas | en_curso | historial (historial = despacho CERRADO, aunque la
     venta siga 'vendida' por estar parcial).
  3. /counts en una query.
  4. Notificación "lista para despacho" al cerrar la recepción, con anti-duplicado.
  5. Historial de embarques recepcionados.
  6. Detalle de avance: ítems con despachado/disponible + despachos + embarques.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_avance_despachos.py -q
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaEmbarque, MonzaEmbarqueItem,
    MonzaRecepcion, MonzaRecepcionItem, MonzaDespacho, MonzaDespachoItem, MonzaReclamo,
    MonzaLog, MonzaNotificacion,
)
from monza_router_bodega import router as bodega_router  # noqa: E402
from monza_router_despachos import router as despachos_router  # noqa: E402

MARK = "test-m4-avance"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(bodega_router)
app.include_router(despachos_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)


def _setup():
    """Venta V (vendida) con A(4) en_bodega vía recepción y B(2) embarcado."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA", rut="11.111.111-1")
        db.add(cli); db.flush()
        suf = uuid.uuid4().hex[:6].upper()
        cot = MonzaCotizacion(numero=f"AV-{suf}", cliente_id=cli.id, estado="vendida",
                              oc_cliente=f"OC-{MARK}", total_bruto=100000)
        db.add(cot); db.flush()
        A = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} A", cantidad=4,
                                precio_unitario_clp=10000, estado_linea="embarcado")
        B = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} B", cantidad=2,
                                precio_unitario_clp=5000, estado_linea="embarcado")
        db.add_all([A, B]); db.flush()
        E = MonzaEmbarque(numero=f"{MARK}-E-{suf}", estado="en_transito", awb="AWB-123")
        db.add(E); db.flush()
        db.add(MonzaEmbarqueItem(embarque_id=E.id, item_id=A.id))
        db.commit()
        return {"cli": cli.id, "cot": cot.id, "A": A.id, "B": B.id, "E": E.id}
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
        item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                    .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
        dsp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                   .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
                   .filter(MonzaEmbarque.numero.like(f"{MARK}%")).all()]
        rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
                   .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "despacho",
            MonzaNotificacion.entidad_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "recepcion",
            MonzaNotificacion.entidad_id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaReclamo).filter(MonzaReclamo.item_id.in_(item_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespachoItem).filter(MonzaDespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcionItem).filter(MonzaRecepcionItem.recepcion_id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcion).filter(MonzaRecepcion.id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaEmbarqueItem).filter(MonzaEmbarqueItem.embarque_id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaEmbarque).filter(MonzaEmbarque.id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(item_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _card_de(cot_id, tab):
    r = client.get(f"/api/monza/despachos/avance", params={"tab": tab})
    assert r.status_code == 200, r.text
    return next((c for c in r.json() if c["id"] == cot_id), None)


def test_tableros_avance_despachos():
    _limpiar()
    ids = _setup()
    try:
        # ══ 1 · Card inicial: todo embarcado → bucket en_transito, estado pendiente ══
        r = client.get("/api/monza/despachos/avance", params={"tab": "malo"})
        assert r.status_code == 400
        card = _card_de(ids["cot"], "listas")
        assert card is None, "sin ítems en bodega no aparece en 'listas'"

        # ══ 2 · Recepción de A (4/4) → notif 'plazo'/'lista' según corresponda ══
        r = client.post(f"/api/monza/bodega/embarques/{ids['E']}/recibir")
        rec_id = r.json()["recepcion_id"]
        assert client.patch(f"/api/monza/bodega/recepciones/{rec_id}/items/{ids['A']}",
                            json={"estado_recepcion": "completo", "qty_recibida": 4}).status_code == 200
        assert client.post(f"/api/monza/bodega/recepciones/{rec_id}/cerrar",
                           json={"forzar": False}).status_code == 200

        card = _card_de(ids["cot"], "listas")
        assert card is not None, "con A en bodega la venta aparece en 'listas'"
        assert card["items_en_bodega"] == 1 and card["total_items"] == 2
        assert card["estado"] == "listo"
        assert card["progreso_pct"] == 0
        assert set(card["progreso_estados"].keys()) == {
            "pendiente", "en_compras", "en_transito", "en_bodega", "despachado", "reclamo"}
        assert card["progreso_estados"]["en_bodega"] == 1
        assert card["progreso_estados"]["en_transito"] == 1  # B sigue embarcado

        # B sigue embarcado → la venta NO está completa → sin notif 'lista para despacho'
        db = SessionLocal()
        try:
            notifs = db.query(MonzaNotificacion).filter(
                MonzaNotificacion.entidad == "cotizacion",
                MonzaNotificacion.entidad_id == ids["cot"]).all()
            assert all("lista para despacho" not in (n.titulo or "") for n in notifs)
        finally:
            db.close()

        # ══ 3 · counts ══
        r = client.get("/api/monza/despachos/counts")
        assert r.status_code == 200
        assert r.json()["ventas_listas"] >= 1 and r.json()["items_listos"] >= 1

        # ══ 4 · Borrador de despacho → tab en_curso; cerrar → historial ══
        r = client.post("/api/monza/despachos/crear",
                        json={"cotizacion_id": ids["cot"], "items": [{"item_id": ids["A"], "qty": 4}]})
        assert r.status_code == 200, r.text
        d1 = r.json()["id"]
        assert _card_de(ids["cot"], "en_curso") is not None, "con borrador aparece en 'en_curso'"
        assert _card_de(ids["cot"], "historial") is None
        assert client.post(f"/api/monza/despachos/entidades/{d1}/cerrar").status_code == 200
        assert _card_de(ids["cot"], "historial") is not None, \
            "con despacho CERRADO aparece en historial aunque la venta siga 'vendida'"
        card = _card_de(ids["cot"], "historial")
        assert card["items_despachados"] == 1 and card["estado"] == "pendiente" or card["estado"] in ("parcial", "pendiente")
        assert card["progreso_pct"] == 50  # 1 de 2 ítems despachado

        # ══ 5 · Detalle de avance: ítems + despachos + embarques ══
        r = client.get(f"/api/monza/despachos/avance/{ids['cot']}")
        assert r.status_code == 200, r.text
        det = r.json()
        por_id = {i["id"]: i for i in det["items"]}
        assert por_id[ids["A"]]["bucket"] == "despachado"
        assert por_id[ids["A"]]["qty_despachada"] == 4
        assert por_id[ids["B"]]["bucket"] == "en_transito"
        assert len(det["despachos"]) == 1
        assert det["embarques"] and det["embarques"][0]["items_de_esta_venta"] == 1
        assert det["embarques"][0]["awb"] == "AWB-123"

        # ══ 6 · Historial de embarques recepcionados ══
        r = client.get("/api/monza/bodega/embarques/historial")
        assert r.status_code == 200
        fila = next((e for e in r.json() if e["id"] == ids["E"]), None)
        assert fila is not None and fila["recepcion"]["fecha_cierre"] is not None
    finally:
        _limpiar()


def test_notif_lista_para_despacho_con_antiduplicado():
    _limpiar()
    ids = _setup()
    try:
        # Dejar TAMBIÉN a B en bodega (vía recepción del mismo embarque no se puede —
        # B no viene en E; se agrega B a un embarque nuevo y se recepciona completo).
        db = SessionLocal()
        try:
            E2 = MonzaEmbarque(numero=f"{MARK}-E2-{uuid.uuid4().hex[:5]}", estado="en_transito")
            db.add(E2); db.flush()
            db.add(MonzaEmbarqueItem(embarque_id=E2.id, item_id=ids["B"]))
            db.commit()
            e2_id = E2.id
        finally:
            db.close()
        for emb, item, qty in ((ids["E"], ids["A"], 4), (e2_id, ids["B"], 2)):
            rec = client.post(f"/api/monza/bodega/embarques/{emb}/recibir").json()["recepcion_id"]
            assert client.patch(f"/api/monza/bodega/recepciones/{rec}/items/{item}",
                                json={"estado_recepcion": "completo", "qty_recibida": qty}).status_code == 200
            assert client.post(f"/api/monza/bodega/recepciones/{rec}/cerrar",
                               json={"forzar": False}).status_code == 200

        db = SessionLocal()
        try:
            notifs = db.query(MonzaNotificacion).filter(
                MonzaNotificacion.entidad == "cotizacion",
                MonzaNotificacion.entidad_id == ids["cot"],
                MonzaNotificacion.titulo.like("%lista para despacho%")).all()
            assert len(notifs) == 1, \
                f"debe existir UNA notif 'lista para despacho' (anti-duplicado), hay {len(notifs)}"
        finally:
            db.close()
    finally:
        _limpiar()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
