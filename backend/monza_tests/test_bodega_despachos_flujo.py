"""Flujo Bodega→Despachos MonzaParts — Fase 2 del espejo Grupo AM.

Prueba de integración contra la BD real (datos MARCADOS + limpieza total en orden
FK-seguro; nunca toca datos reales) del contrato completo:

  1. Recepción PARTICIONADA: una llegada parcial deja lo recibido en bodega y
     reclama SOLO el faltante real (antes la línea entera caía a reclamo).
  2. TOPE FÍSICO: no se despacha más de lo recibido; los borradores consumen cupo.
  3. Ciclo de vida: en_preparacion → cerrar (la línea voltea solo con cobertura
     completa de CERRADOS) → anular reversible solo en borrador.
  4. Cierre FORZADO: lo no marcado queda como reclamo 'no llegó' trazable.
  5. Reposición: completar la línea en una recepción posterior NO genera reclamo
     fantasma y reabre el cupo de despacho.

Espejo de routers/tests/test_bodega_despachos_flujo.py (Grupo AM), adaptado al
modelo Monza: la cotización ES la venta y la recepción apunta directo al ítem.
Verificaciones de estado con SESIÓN NUEVA (la sesión del cliente puede mentir).

Corre con:  ./venv/bin/python -m pytest monza_tests/test_bodega_despachos_flujo.py -q
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

MARK = "test-m2-flujo"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(bodega_router)
app.include_router(despachos_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)


# ── Fixtures marcadas ──────────────────────────────────────────────────────────
def _setup():
    """cot1: A(10), B(4) · cot2: C(5), X(2) · E1:{A,B} E2:{C} E3:{A}."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=MARK)
        db.add(cli); db.flush()
        suf = uuid.uuid4().hex[:6].upper()
        cot1 = MonzaCotizacion(numero=f"CT-{MARK[-4:]}-{suf}-1", cliente_id=cli.id, estado="vendida")
        cot2 = MonzaCotizacion(numero=f"CT-{MARK[-4:]}-{suf}-2", cliente_id=cli.id, estado="vendida")
        db.add_all([cot1, cot2]); db.flush()
        A = MonzaCotizacionItem(cotizacion_id=cot1.id, descripcion=f"{MARK} A", cantidad=10, estado_linea="embarcado")
        B = MonzaCotizacionItem(cotizacion_id=cot1.id, descripcion=f"{MARK} B", cantidad=4, estado_linea="embarcado")
        C = MonzaCotizacionItem(cotizacion_id=cot2.id, descripcion=f"{MARK} C", cantidad=5, estado_linea="embarcado")
        X = MonzaCotizacionItem(cotizacion_id=cot2.id, descripcion=f"{MARK} X", cantidad=2, estado_linea="en_bodega")
        db.add_all([A, B, C, X]); db.flush()
        E1 = MonzaEmbarque(numero=f"{MARK}-E1-{suf}", estado="en_transito")
        E2 = MonzaEmbarque(numero=f"{MARK}-E2-{suf}", estado="en_transito")
        E3 = MonzaEmbarque(numero=f"{MARK}-E3-{suf}", estado="en_transito")
        db.add_all([E1, E2, E3]); db.flush()
        db.add_all([
            MonzaEmbarqueItem(embarque_id=E1.id, item_id=A.id),
            MonzaEmbarqueItem(embarque_id=E1.id, item_id=B.id),
            MonzaEmbarqueItem(embarque_id=E2.id, item_id=C.id),
            MonzaEmbarqueItem(embarque_id=E3.id, item_id=A.id),
        ])
        db.commit()
        return {"cli": cli.id, "cot1": cot1.id, "cot2": cot2.id,
                "A": A.id, "B": B.id, "C": C.id, "X": X.id,
                "E1": E1.id, "E2": E2.id, "E3": E3.id}
    finally:
        db.close()


def _cleanup():
    db = SessionLocal()
    try:
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre == MARK).all()]
        item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                    .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
        dsp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                   .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
                   .filter(MonzaEmbarque.numero.like(f"{MARK}%")).all()]
        rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
                   .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
        S = "fetch"
        # Scope por entidad (no producto cruzado): un id de despacho ajeno que
        # coincida con un id de recepción nuestro no debe borrar notifs reales.
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
        db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


# ── Lecturas con sesión NUEVA (regla de la casa: la sesión del test miente) ────
def _linea(item_id):
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacionItem.estado_linea).filter(MonzaCotizacionItem.id == item_id).scalar()
    finally:
        db.close()


def _cot_estado(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacion.estado).filter(MonzaCotizacion.id == cot_id).scalar()
    finally:
        db.close()


def _despacho(dsp_id):
    db = SessionLocal()
    try:
        d = db.query(MonzaDespacho).filter(MonzaDespacho.id == dsp_id).first()
        return (d.estado, d.fecha_despacho) if d else (None, None)
    finally:
        db.close()


def _reclamos_de(item_id):
    db = SessionLocal()
    try:
        return [(r.motivo, r.qty_afectada) for r in
                db.query(MonzaReclamo).filter(MonzaReclamo.item_id == item_id).all()]
    finally:
        db.close()


def _abrir_recepcion(emb_id):
    r = client.post(f"/api/monza/bodega/embarques/{emb_id}/recibir")
    assert r.status_code == 200, r.text
    return r.json()["recepcion_id"]


def _marcar(rec_id, item_id, estado, qty=None, danada=0):
    return client.patch(f"/api/monza/bodega/recepciones/{rec_id}/items/{item_id}",
                        json={"estado_recepcion": estado, "qty_recibida": qty, "qty_danada": danada})


def _disponibles():
    """{item_id: qty_disponible} desde /listos (lo que la UI ofrece despachar)."""
    r = client.get("/api/monza/despachos/listos")
    assert r.status_code == 200, r.text
    out = {}
    for c in r.json():
        for i in c["items"]:
            out[i["id"]] = i["qty_disponible"]
    return out


def test_flujo_bodega_despachos_monza():
    _cleanup()
    ids = _setup()
    try:
        # ══ 1 · RECEPCIÓN PARTICIONADA (E1: A faltante 6/10, B completo 4/4) ══
        rec1 = _abrir_recepcion(ids["E1"])
        # guard de coherencia: 'faltante' con recibido >= vendido inflaría el tope
        assert _marcar(rec1, ids["A"], "faltante", qty=10).status_code == 400
        assert _marcar(rec1, ids["A"], "faltante", qty=6).status_code == 200
        assert _marcar(rec1, ids["B"], "completo", qty=4).status_code == 200
        # guard: cantidad negativa
        assert _marcar(rec1, ids["B"], "completo", qty=-1).status_code == 400
        r = client.post(f"/api/monza/bodega/recepciones/{rec1}/cerrar", json={"forzar": False})
        assert r.status_code == 200, r.text
        assert r.json()["en_bodega"] == 2, "lo recibido de A debe quedar EN BODEGA (partición)"
        assert r.json()["reclamos"] == 1
        assert _linea(ids["A"]) == "en_bodega", "llegada parcial: lo recibido queda despachable"
        assert _linea(ids["B"]) == "en_bodega"
        assert _reclamos_de(ids["A"]) == [("faltante", 4)], "solo el faltante REAL va a reclamo"
        assert _reclamos_de(ids["B"]) == []
        # guard: recepción cerrada no se re-marca
        assert _marcar(rec1, ids["A"], "completo", qty=10).status_code == 400
        # guard: el embarque YA recepcionado no se re-recepciona (una segunda
        # recepción sumaría sus cantidades DE NUEVO al tope → unidades fantasma)
        r = client.post(f"/api/monza/bodega/embarques/{ids['E1']}/recibir")
        assert r.status_code == 400, "re-recepcionar un embarque cerrado debe rechazarse"

        # ══ 2 · TOPE FÍSICO en el despacho ══
        disp = _disponibles()
        assert disp.get(ids["A"]) == 6, f"disponible A = recibido 6, no vendido 10 (dio {disp.get(ids['A'])})"
        assert disp.get(ids["B"]) == 4
        crear = lambda cot, items, **extra: client.post(  # noqa: E731
            "/api/monza/despachos/crear", json={"cotizacion_id": cot, "items": items, **extra})
        r = crear(ids["cot1"], [{"item_id": ids["A"], "qty": 7}])
        assert r.status_code == 400 and "RECIBIDO" in r.json()["detail"], "no se despacha más de lo recibido"
        # qty explícita inválida en la vía nueva → 400 (no "todo el disponible")
        assert crear(ids["cot1"], [{"item_id": ids["A"], "qty": 0}]).status_code == 400
        assert crear(ids["cot1"], [{"item_id": ids["A"], "qty": -2}]).status_code == 400
        # qty fraccionaria → 422 (la columna es Integer: no se redondea en silencio)
        assert crear(ids["cot1"], [{"item_id": ids["A"], "qty": 2.5}]).status_code == 422
        # ítems duplicados
        r = crear(ids["cot1"], [{"item_id": ids["A"], "qty": 2}, {"item_id": ids["A"], "qty": 2}])
        assert r.status_code == 400
        # ítem de OTRA cotización
        r = crear(ids["cot1"], [{"item_id": ids["X"], "qty": 1}])
        assert r.status_code == 400
        # borrador válido A×4 (parcial)
        r = crear(ids["cot1"], [{"item_id": ids["A"], "qty": 4}])
        assert r.status_code == 200, r.text
        d1 = r.json()["id"]
        assert r.json()["estado"] == "en_preparacion"
        assert _linea(ids["A"]) == "en_bodega", "el borrador NO voltea la línea"
        assert _disponibles().get(ids["A"]) == 2, "el borrador consume cupo (6−4)"
        r = crear(ids["cot1"], [{"item_id": ids["A"], "qty": 3}])
        assert r.status_code == 400, "no puede exceder el cupo restante (2)"

        # ══ 3 · CICLO DE VIDA: anular borrador devuelve el cupo ══
        assert client.delete(f"/api/monza/despachos/entidades/{d1}").status_code == 200
        assert _despacho(d1)[0] == "anulado"
        assert _disponibles().get(ids["A"]) == 6, "anular el borrador devuelve el cupo"
        # VÍA LEGADA (item_ids sin cantidades): despacha el DISPONIBLE (6, lo
        # recibido), NUNCA la cantidad vendida (10) — ese era el bug original.
        r = client.post("/api/monza/despachos/crear",
                        json={"cotizacion_id": ids["cot1"], "item_ids": [ids["A"]]})
        assert r.status_code == 200, r.text
        d2 = r.json()["id"]
        assert r.json()["estado"] == "en_preparacion"
        det = client.get(f"/api/monza/despachos/entidades/{d2}").json()
        assert [(x["item_id"], x["qty_despachada"]) for x in det["items"]] == [(ids["A"], 6)], \
            "la vía legada debe despachar el disponible físico (6), no lo vendido (10)"
        r = client.post(f"/api/monza/despachos/entidades/{d2}/cerrar")
        assert r.status_code == 200, r.text
        estado, fdesp = _despacho(d2)
        assert estado == "despachado" and fdesp is not None
        assert _linea(ids["A"]) == "en_bodega", \
            "cobertura 6 < vendido 10: la línea NO voltea (el resto está en reclamo)"
        assert _disponibles().get(ids["A"]) is None, "sin cupo → no se ofrece en /listos"
        # cerrar/anular un cerrado → 400
        assert client.post(f"/api/monza/despachos/entidades/{d2}/cerrar").status_code == 400
        assert client.delete(f"/api/monza/despachos/entidades/{d2}").status_code == 400
        # PUT cabecera sobre cerrado (transportista/expedición llegan después) → ok
        r = client.put(f"/api/monza/despachos/entidades/{d2}",
                       json={"transportista": "Chilexpress", "numero_expedicion": "EXP-99"})
        assert r.status_code == 200
        # B completo: crear + cerrar → línea voltea
        r = crear(ids["cot1"], [{"item_id": ids["B"], "qty": 4}])
        assert r.status_code == 200
        d3 = r.json()["id"]
        assert client.post(f"/api/monza/despachos/entidades/{d3}/cerrar").status_code == 200
        assert _linea(ids["B"]) == "despachado", "cobertura completa (4/4) voltea la línea"
        assert _cot_estado(ids["cot1"]) == "vendida", "la venta no se cierra con A incompleta"

        # ══ 4 · CIERRE FORZADO (E2: C sin marcar) ══
        rec2 = _abrir_recepcion(ids["E2"])
        # guard de pertenencia: A no viene en E2 — marcarla aquí inflaría el tope
        # físico de A con una fila espuria al cerrar
        assert _marcar(rec2, ids["A"], "completo", qty=4).status_code == 400, \
            "marcar un ítem ajeno al embarque debe rechazarse"
        r = client.post(f"/api/monza/bodega/recepciones/{rec2}/cerrar", json={"forzar": False})
        assert r.status_code == 400 and "forzar" in r.json()["detail"]
        r = client.post(f"/api/monza/bodega/recepciones/{rec2}/cerrar", json={"forzar": True})
        assert r.status_code == 200
        assert _linea(ids["C"]) == "reclamo"
        assert _reclamos_de(ids["C"]) == [("no_llego", 5)], "lo no marcado queda trazable, no atascado"

        # ══ 5 · REPOSICIÓN sin reclamo fantasma (E3: llegan los 4 de A) ══
        rec3 = _abrir_recepcion(ids["E3"])
        assert _marcar(rec3, ids["A"], "completo", qty=4).status_code == 200
        r = client.post(f"/api/monza/bodega/recepciones/{rec3}/cerrar", json={"forzar": False})
        assert r.status_code == 200
        assert _reclamos_de(ids["A"]) == [("faltante", 4)], \
            "la reposición que completa la línea NO genera reclamo nuevo (fantasma)"
        assert _disponibles().get(ids["A"]) == 4, "cupo reabierto: min(10, 6+4) − 6 despachado = 4"
        # despachar el resto → cobertura 10/10 → línea y venta despachadas
        r = crear(ids["cot1"], [{"item_id": ids["A"], "qty": 4}])
        assert r.status_code == 200
        d4 = r.json()["id"]
        assert client.post(f"/api/monza/despachos/entidades/{d4}/cerrar").status_code == 200
        assert _linea(ids["A"]) == "despachado"
        assert _cot_estado(ids["cot1"]) == "despachado", "todas las líneas cubiertas → venta despachada"

        # PUT sobre anulado → 400
        assert client.put(f"/api/monza/despachos/entidades/{d1}",
                          json={"transportista": "x"}).status_code == 400
    finally:
        _cleanup()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
