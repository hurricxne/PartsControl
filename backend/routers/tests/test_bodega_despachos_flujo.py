"""Test de integración del flujo BODEGA → DESPACHOS → estados posteriores.

Nace del enjambre de casos borde del 2026-07-16 (el dueño reportó que la guía
salía por la cantidad de la OC y no por lo recibido en bodega). Cubre:
  · Recepción completa → en_bodega, disponible = cantidad.
  · Recepción PARCIAL marcada 'completo' (8 de 10) → disponible = 8 (tope por lo
    RECIBIDO), despachar 9/10 → 400, despachar 8 → 200, y reclamo trazable por 2.
  · 'faltante' parcial (8 de 10) → lo recibido queda EN BODEGA y despachable;
    solo la diferencia va a reclamo (antes la línea entera quedaba bloqueada).
  · 'no_llego' → línea a reclamo con qty_afectada = TODO lo esperado.
  · 'sobrante' (12 de 10) → disponible topeado a la cantidad vendida (10).
  · Cierre con forzar=true e ítems sin marcar → van a reclamo trazable
    (antes quedaban atascados en 'embarcado', invisibles).
  · Ciclo en tandas: 4+3+3 → línea despachada; anular la del medio → vuelve a
    en_bodega con remanente exacto; re-despachar → coherente.
  · Ítem en_bodega SIN recepción registrada (flujo antiguo) → disponible =
    cantidad (sin tope, compatibilidad histórica).
  · G16 (mesa redonda 2026-07-19): la cobertura que voltea línea/embarque cuenta
    SOLO despachos CERRADOS (tandas abiertas ya no marcan 'despachado' prematuro),
    la última tanda sí cuenta (anti off-by-one), el embarque espera a TODAS sus
    líneas, la reversa defensiva de anular auto-sana embarques atascados legados,
    y el cierre parcial firmado sigue facturable aunque la línea esté en_bodega.
LIMPIA todo al final. Corre con:
    ./venv/bin/python routers/tests/test_bodega_despachos_flujo.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    Embarque, EmbarqueItem, RecepcionEmbarque, RecepcionEmbarqueItem,
    ReclamoProveedor,
)
from routers.bodega import router as bodega_router  # noqa: E402
from routers.despachos import router as despachos_router  # noqa: E402
from routers.contabilidad import router as contabilidad_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_BODFLUJO__"
CURRENT = {"empresa": "mineria", "id": None}

app = FastAPI()
app.include_router(bodega_router, prefix="/api")
app.include_router(despachos_router, prefix="/api")
app.include_router(contabilidad_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear_escenario(db, cantidades=(10,), estado_item="embarcado"):
    """OC + cotización con N líneas (cantidades) + embarque con sus EmbarqueItems."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} CLIENTE",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = ItemCotizacion(cotizacion_id=cot.id, item_num=n,
                            numero_parte=f"P-{MARK}-{n}",
                            descripcion=f"Parte demo {n}", cantidad=cant,
                            estado_item=estado_item)
        db.add(it); items.append(it)
    db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC",
                   fecha_oc="2026-07-10")
    db.add(oc); db.flush()
    emb = Embarque(numero=f"{MARK}-EMB", estado="en_transito")
    db.add(emb); db.flush()
    eis = []
    for it in items:
        ei = EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=it.id)
        db.add(ei); eis.append(ei)
    db.commit()
    for obj in [cot, oc, emb] + items + eis:
        db.refresh(obj)
    return cot, oc, emb, items, eis


def _recibir(emb_id, marcas, forzar=False):
    """Abre la recepción del embarque, marca cada ítem y la cierra.
    marcas: [(embarque_item_id, estado_recepcion, qty_recibida), ...]"""
    r = client.post(f"/api/bodega/embarques/{emb_id}/recibir")
    assert r.status_code in (200, 201), r.text
    rec_id = r.json()["recepcion_id"] if "recepcion_id" in r.json() else r.json()["id"]
    for ei_id, estado, qty in marcas:
        r = client.patch(f"/api/bodega/recepciones/{rec_id}/items/0",
                         json={"embarque_item_id": ei_id,
                               "estado_recepcion": estado, "qty_recibida": qty})
        assert r.status_code == 200, r.text
    r = client.post(f"/api/bodega/recepciones/{rec_id}/cerrar",
                    json={"forzar": forzar})
    assert r.status_code == 200, r.text
    return rec_id


def _detalle_item(oc_id, item_id):
    r = client.get(f"/api/despachos/oc-clientes/{oc_id}")
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["items"] if x["id"] == item_id)


def _despachar(oc_id, item_id, qty):
    return client.post("/api/despachos/", json={
        "oc_cliente_id": oc_id,
        "items": [{"item_cotizacion_id": item_id, "qty_despachada": qty}],
    })


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    if cot_ids:
        item_ids = [i.id for i in db.query(ItemCotizacion)
                    .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all()]
        oc_ids = [o.id for o in db.query(OcCliente)
                  .filter(OcCliente.cotizacion_id.in_(cot_ids)).all()]
        if item_ids:
            db.query(ReclamoProveedor).filter(
                ReclamoProveedor.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=False)
            ei_ids = [e.id for e in db.query(EmbarqueItem)
                      .filter(EmbarqueItem.item_cotizacion_id.in_(item_ids)).all()]
            emb_ids = list({e.embarque_id for e in db.query(EmbarqueItem)
                            .filter(EmbarqueItem.id.in_(ei_ids)).all()}) if ei_ids else []
            if ei_ids:
                db.query(RecepcionEmbarqueItem).filter(
                    RecepcionEmbarqueItem.embarque_item_id.in_(ei_ids)).delete(synchronize_session=False)
                db.query(EmbarqueItem).filter(
                    EmbarqueItem.id.in_(ei_ids)).delete(synchronize_session=False)
            if emb_ids:
                db.query(RecepcionEmbarque).filter(
                    RecepcionEmbarque.embarque_id.in_(emb_ids)).delete(synchronize_session=False)
                db.query(Embarque).filter(Embarque.id.in_(emb_ids)).delete(synchronize_session=False)
        if oc_ids:
            desp_ids = [d.id for d in db.query(Despacho)
                        .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
            if desp_ids:
                db.query(DespachoItem).filter(
                    DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
                db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    # Embarques rezagados del MARK (si un fallo dejó el escenario a medias)
    for e in db.query(Embarque).filter(Embarque.numero.like(f"{MARK}%")).all():
        rec_ids = [r.id for r in db.query(RecepcionEmbarque)
                   .filter(RecepcionEmbarque.embarque_id == e.id).all()]
        if rec_ids:
            db.query(RecepcionEmbarqueItem).filter(
                RecepcionEmbarqueItem.recepcion_id.in_(rec_ids)).delete(synchronize_session=False)
            db.query(RecepcionEmbarque).filter(
                RecepcionEmbarque.id.in_(rec_ids)).delete(synchronize_session=False)
        db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == e.id).delete(synchronize_session=False)
        db.delete(e)
    db.commit()


def run():
    db = SessionLocal()
    try:
        _limpiar(db)

        # ═══ 1. Recepción COMPLETA → en_bodega, disponible = cantidad ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "completo", 10)])
        db.rollback()
        check("recepción completa: línea en_bodega",
              db.get(ItemCotizacion, it.id).estado_item == "en_bodega")
        d = _detalle_item(oc.id, it.id)
        check("recepción completa: disponible 10", d["qty_disponible"] == 10, d)
        _limpiar(db)

        # ═══ 2. PARCIAL 'completo' 8/10 → tope por lo RECIBIDO + reclamo por 2 ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "completo", 8)])
        d = _detalle_item(oc.id, it.id)
        check("parcial: disponible = 8 (lo recibido, NO la cantidad de la OC)",
              d["qty_disponible"] == 8, d)
        r = _despachar(oc.id, it.id, 10)
        check("parcial: despachar 10 → 400 con mensaje de RECIBIDO",
              r.status_code == 400 and "RECIBIDO" in r.json()["detail"], r.text)
        r = _despachar(oc.id, it.id, 9)
        check("parcial: despachar 9 → 400", r.status_code == 400, r.text)
        r = _despachar(oc.id, it.id, 8)
        check("parcial: despachar 8 (lo recibido) → 200", r.status_code == 200, r.text)
        db.rollback()
        recl = db.query(ReclamoProveedor).filter(
            ReclamoProveedor.item_cotizacion_id == it.id).all()
        check("parcial: reclamo trazable por las 2 faltantes",
              len(recl) == 1 and float(recl[0].qty_afectada) == 2
              and recl[0].motivo == "faltante", [(x.motivo, float(x.qty_afectada)) for x in recl])
        _limpiar(db)

        # ═══ 3. 'faltante' 8/10 → lo recibido DESPACHABLE, reclamo por 2 ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "faltante", 8)])
        db.rollback()
        check("faltante parcial: línea queda EN BODEGA (no bloqueada entera)",
              db.get(ItemCotizacion, it.id).estado_item == "en_bodega")
        d = _detalle_item(oc.id, it.id)
        check("faltante parcial: disponible = 8", d["qty_disponible"] == 8, d)
        r = _despachar(oc.id, it.id, 8)
        check("faltante parcial: despachar las 8 recibidas → 200", r.status_code == 200, r.text)
        db.rollback()
        recl = db.query(ReclamoProveedor).filter(
            ReclamoProveedor.item_cotizacion_id == it.id).all()
        check("faltante parcial: reclamo por 2 (lo FALTANTE, no lo recibido)",
              len(recl) == 1 and float(recl[0].qty_afectada) == 2, [(x.motivo, float(x.qty_afectada)) for x in recl])
        _limpiar(db)

        # ═══ 4. 'no_llego' → reclamo por TODO lo esperado ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "no_llego", 0)])
        db.rollback()
        check("no_llego: línea a reclamo_proveedor",
              db.get(ItemCotizacion, it.id).estado_item == "reclamo_proveedor")
        d = _detalle_item(oc.id, it.id)
        check("no_llego: disponible 0", d["qty_disponible"] == 0, d)
        recl = db.query(ReclamoProveedor).filter(
            ReclamoProveedor.item_cotizacion_id == it.id).first()
        check("no_llego: reclamo por las 10 esperadas (antes registraba 0)",
              recl and float(recl.qty_afectada) == 10, recl and float(recl.qty_afectada))
        _limpiar(db)

        # ═══ 5. 'sobrante' 12/10 → despachable topeado a lo VENDIDO (10) ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "sobrante", 12)])
        d = _detalle_item(oc.id, it.id)
        check("sobrante: disponible topeado a 10 (lo vendido)",
              d["qty_disponible"] == 10, d)
        _limpiar(db)

        # ═══ 6. Cierre FORZADO con ítem sin marcar → reclamo trazable ═══
        cot, oc, emb, (it1, it2), (ei1, ei2) = _crear_escenario(db, (10, 5))
        _recibir(emb.id, [(ei1.id, "completo", 10)], forzar=True)  # ei2 sin marcar
        db.rollback()
        check("forzado: el marcado queda en_bodega",
              db.get(ItemCotizacion, it1.id).estado_item == "en_bodega")
        check("forzado: el SIN marcar va a reclamo (no queda atascado en 'embarcado')",
              db.get(ItemCotizacion, it2.id).estado_item == "reclamo_proveedor")
        recl = db.query(ReclamoProveedor).filter(
            ReclamoProveedor.item_cotizacion_id == it2.id).first()
        check("forzado: reclamo trazable por la cantidad completa",
              recl and recl.motivo == "no_llego" and float(recl.qty_afectada) == 5,
              recl and (recl.motivo, float(recl.qty_afectada)))
        _limpiar(db)

        # ═══ 7. Ciclo en tandas 4+3+3, anular la del medio, re-despachar ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "completo", 10)])
        ids = []
        for q in (4, 3, 3):
            r = _despachar(oc.id, it.id, q)
            check(f"tanda de {q} → 200", r.status_code == 200, r.text)
            ids.append(r.json()["id"])
        for did in ids:
            r = client.post(f"/api/despachos/{did}/cerrar")
            check(f"cerrar despacho {did} → 200", r.status_code == 200, r.text)
        db.rollback()
        check("cobertura completa: línea 'despachado'",
              db.get(ItemCotizacion, it.id).estado_item == "despachado")
        r = client.delete(f"/api/despachos/{ids[1]}")  # anular la tanda del medio
        check("anular tanda del medio → 400 (ya cerrada, no anulable)",
              r.status_code == 400, r.text)
        _limpiar(db)

        # ═══ 7b. REPOSICIÓN: el proveedor repone el faltante en un 2° embarque ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "faltante", 8)])       # llegan 8, reclamo por 2
        r = _despachar(oc.id, it.id, 8)
        check("reposición: despachar las 8 primeras → 200", r.status_code == 200, r.text)
        emb2 = Embarque(numero=f"{MARK}-EMB2", estado="en_transito")
        db.add(emb2); db.flush()
        ei2 = EmbarqueItem(embarque_id=emb2.id, item_cotizacion_id=it.id)
        db.add(ei2); db.commit(); db.refresh(emb2); db.refresh(ei2)
        _recibir(emb2.id, [(ei2.id, "completo", 2)])     # reposición de las 2
        db.rollback()
        recl = db.query(ReclamoProveedor).filter(
            ReclamoProveedor.item_cotizacion_id == it.id).all()
        check("reposición: SIN reclamo fantasma (solo el original por 2)",
              len(recl) == 1 and float(recl[0].qty_afectada) == 2,
              [(x.motivo, float(x.qty_afectada)) for x in recl])
        d = _detalle_item(oc.id, it.id)
        check("reposición: el tope crece — disponible = 2 (10 recibidas − 8 despachadas)",
              d["qty_disponible"] == 2, d)
        r = _despachar(oc.id, it.id, 2)
        check("reposición: despachar el saldo de 2 → 200", r.status_code == 200, r.text)
        _limpiar(db)

        # ═══ 7c. Línea repartida en 2 embarques (5+5, ambos completos) ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "completo", 5)])
        emb2 = Embarque(numero=f"{MARK}-EMB2", estado="en_transito")
        db.add(emb2); db.flush()
        ei2 = EmbarqueItem(embarque_id=emb2.id, item_cotizacion_id=it.id)
        db.add(ei2); db.commit(); db.refresh(emb2); db.refresh(ei2)
        _recibir(emb2.id, [(ei2.id, "completo", 5)])
        db.rollback()
        recl2 = db.query(ReclamoProveedor).filter(
            ReclamoProveedor.item_cotizacion_id == it.id).all()
        # La 1ª recepción deja reclamo por las 5 que faltaban EN ESE MOMENTO
        # (trazable, se resuelve a mano al llegar el resto); la 2ª, que completa
        # la línea, NO debe agregar ningún reclamo fantasma.
        check("split 5+5: solo el reclamo de la 1ª recepción (sin fantasma de la 2ª)",
              len(recl2) == 1 and float(recl2[0].qty_afectada) == 5,
              [(x.motivo, float(x.qty_afectada)) for x in recl2])
        d = _detalle_item(oc.id, it.id)
        check("split 5+5: disponible = 10", d["qty_disponible"] == 10, d)
        _limpiar(db)

        # ═══ 8. Ítem en_bodega SIN recepción (flujo antiguo) → sin tope ═══
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,), estado_item="en_bodega")
        d = _detalle_item(oc.id, it.id)
        check("sin recepción registrada: disponible = cantidad (histórico intacto)",
              d["qty_disponible"] == 10, d)
        r = _despachar(oc.id, it.id, 10)
        check("sin recepción registrada: despachar 10 → 200", r.status_code == 200, r.text)
        _limpiar(db)

        # ═══ 9. G16: cobertura por CERRADOS + reversa del embarque ═══
        def _estado(obj_q, obj_id, campo="estado"):
            db.rollback()
            return getattr(db.query(obj_q).filter(obj_q.id == obj_id).first(), campo)

        # 9a. Tandas 4+3+3 ABIERTAS: cerrar solo la 1ª NO voltea línea ni embarque
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "completo", 10)])
        d1 = _despachar(oc.id, it.id, 4).json()
        d2 = _despachar(oc.id, it.id, 3).json()
        d3 = _despachar(oc.id, it.id, 3).json()
        r = client.post(f"/api/despachos/{d1['id']}/cerrar")
        check("9a cerrar 1ª tanda (4/10) → 200", r.status_code == 200, r.text)
        check("9a línea sigue en_bodega (tandas abiertas NO cuentan)",
              _estado(ItemCotizacion, it.id, "estado_item") == "en_bodega", "")
        check("9a embarque sigue en_bodega (sin flip prematuro)",
              _estado(Embarque, emb.id) == "en_bodega", _estado(Embarque, emb.id))
        client.post(f"/api/despachos/{d2['id']}/cerrar")
        check("9a con 7/10 cerrado: línea y embarque siguen en_bodega",
              _estado(ItemCotizacion, it.id, "estado_item") == "en_bodega"
              and _estado(Embarque, emb.id) == "en_bodega", "")
        client.post(f"/api/despachos/{d3['id']}/cerrar")
        check("9a última tanda (10/10) SÍ voltea (anti off-by-one): línea+embarque despachados",
              _estado(ItemCotizacion, it.id, "estado_item") == "despachado"
              and _estado(Embarque, emb.id) == "despachado", "")
        _limpiar(db)

        # 9b. Multi-línea: el embarque espera a TODAS sus líneas
        cot, oc, emb, (itA, itB), (eiA, eiB) = _crear_escenario(db, (4, 6))
        _recibir(emb.id, [(eiA.id, "completo", 4), (eiB.id, "completo", 6)])
        dA = _despachar(oc.id, itA.id, 4).json()
        client.post(f"/api/despachos/{dA['id']}/cerrar")
        check("9b línea A despachada, embarque ESPERA a la línea B",
              _estado(ItemCotizacion, itA.id, "estado_item") == "despachado"
              and _estado(Embarque, emb.id) == "en_bodega", _estado(Embarque, emb.id))
        dB = _despachar(oc.id, itB.id, 6).json()
        client.post(f"/api/despachos/{dB['id']}/cerrar")
        check("9b con B cerrada el embarque completa a despachado",
              _estado(Embarque, emb.id) == "despachado", "")
        _limpiar(db)

        # 9c. DEFENSA: embarque atascado legado se auto-sana al anular
        cot, oc, emb, (itA, itB), (eiA, eiB) = _crear_escenario(db, (4, 6))
        _recibir(emb.id, [(eiA.id, "completo", 4), (eiB.id, "completo", 6)])
        dA = _despachar(oc.id, itA.id, 4).json()
        client.post(f"/api/despachos/{dA['id']}/cerrar")
        db.rollback()
        db.query(Embarque).filter(Embarque.id == emb.id).update({"estado": "despachado"})
        db.commit()   # simula el atasco legado: embarque 'despachado' con B en bodega
        dB = _despachar(oc.id, itB.id, 6).json()
        r = client.delete(f"/api/despachos/{dB['id']}")
        check("9c anular el despacho abierto → 200", r.status_code == 200, r.text)
        check("9c reversa defensiva: embarque atascado vuelve a en_bodega",
              _estado(Embarque, emb.id) == "en_bodega", _estado(Embarque, emb.id))
        check("9c la línea A legítimamente despachada queda intacta",
              _estado(ItemCotizacion, itA.id, "estado_item") == "despachado", "")
        _limpiar(db)

        # 9d. Cierre parcial CERRADO+FIRMADO sigue facturable con la línea en_bodega
        cot, oc, emb, (it,), (ei,) = _crear_escenario(db, (10,))
        _recibir(emb.id, [(ei.id, "completo", 10)])
        d1 = _despachar(oc.id, it.id, 4).json()
        client.post(f"/api/despachos/{d1['id']}/cerrar")
        # firmar exige el archivo de la guía firmada en uploads/docs: crear uno dummy
        docs_dir = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "uploads", "docs"))
        os.makedirs(docs_dir, exist_ok=True)
        archivo_firma = f"{MARK.strip('_')}-g9d.pdf"
        with open(os.path.join(docs_dir, archivo_firma), "wb") as fh:
            fh.write(b"%PDF-1.4 test")
        try:
            r = client.post(f"/api/despachos/{d1['id']}/firmar",
                            json={"numero_guia": f"{MARK}-G9d", "archivo": archivo_firma})
            check("9d firmar el parcial cerrado → 200", r.status_code == 200, r.text)
            check("9d la línea sigue en_bodega (remanente 6 despachable)",
                  _estado(ItemCotizacion, it.id, "estado_item") == "en_bodega", "")
            r = client.get(f"/api/contabilidad/ventas/{oc.id}/despachos-facturables")
            facs = r.json() if r.status_code == 200 else []
            fila = next((x for x in facs if x.get("id") == d1["id"]), None)
            check("9d el parcial firmado APARECE facturable (estado_item no lo excluye)",
                  r.status_code == 200 and fila is not None
                  and float(fila.get("facturable") or 0) == 4, (r.status_code, facs))
        finally:
            try:
                os.remove(os.path.join(docs_dir, archivo_firma))
            except OSError:
                pass
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_bodega_despachos_flujo():
    run()


if __name__ == "__main__":
    run()
