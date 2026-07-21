"""Regresión del TOPE FÍSICO tras el UNION nacional en despachos.py.

Este es el ÚNICO toque a código compartido de Compras Nacionales: el UNION aditivo
en `routers/despachos.py::_qty_recibida_utilizable`. La suite confirma las dos reglas
de oro del diseño:
  (a) el ítem INTERNACIONAL queda EXACTAMENTE igual (con y sin recepción de embarque);
  (b) el ítem NACIONAL solo es despachable por lo RECIBIDO (nunca por lo vendido), y
      un nacional sin recepción cerrada NO es despachable.

Inserta las recepciones directamente en la BD (embarque y nacional) para ejercer el
dict combinado de `_qty_recibida_utilizable`, luego valida el tope contra el endpoint
real de detalle y creación de despachos. LIMPIA todo al final.

Corre con:  ./venv/bin/python routers/tests/test_recepcion_nacional_tope.py
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
)
from recepcion_nacional.models import RecepcionNacional, RecepcionNacionalItem  # noqa: E402
from routers.despachos import router as despachos_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_RN_TOPE__"
CURRENT = {"empresa": "mineria", "id": None}

app = FastAPI()
app.include_router(despachos_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _escenario(db, cantidades=(10,), estado_item="en_bodega"):
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} CLIENTE",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = ItemCotizacion(cotizacion_id=cot.id, item_num=n,
                            numero_parte=f"P-{MARK}-{n}", descripcion=f"Parte {n}",
                            cantidad=cant, estado_item=estado_item)
        db.add(it); items.append(it)
    db.flush()
    occ = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OCC", fecha_oc="2026-07-18")
    db.add(occ); db.commit()
    for obj in [cot, occ] + items:
        db.refresh(obj)
    return cot, occ, items


def _recepcion_embarque(db, item, estado_recep="completo", qty=8):
    """Inserta un embarque + su recepción CERRADA para un ítem internacional."""
    emb = Embarque(numero=f"{MARK}-EMB", estado="en_transito")
    db.add(emb); db.flush()
    ei = EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=item.id)
    db.add(ei); db.flush()
    rec = RecepcionEmbarque(embarque_id=emb.id, estado="cerrada")
    db.add(rec); db.flush()
    db.add(RecepcionEmbarqueItem(recepcion_id=rec.id, embarque_item_id=ei.id,
                                 estado_recepcion=estado_recep, qty_recibida=qty))
    db.commit()


def _recepcion_nacional(db, item, estado_recep="completo", qty=10, estado="cerrada"):
    rec = RecepcionNacional(numero_guia_proveedor=f"{MARK}-G", estado=estado)
    db.add(rec); db.flush()
    db.add(RecepcionNacionalItem(recepcion_id=rec.id, item_cotizacion_id=item.id,
                                 numero_parte=item.numero_parte, qty_recibida=qty,
                                 estado_recepcion=estado_recep))
    db.commit()
    return rec


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
    item_ids = ([i.id for i in db.query(ItemCotizacion)
                 .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all()] if cot_ids else [])
    occs = ([o.id for o in db.query(OcCliente)
             .filter(OcCliente.cotizacion_id.in_(cot_ids)).all()] if cot_ids else [])
    if item_ids:
        # Recepciones nacionales (CASCADE líneas)
        rn_ids = [r.recepcion_id for r in db.query(RecepcionNacionalItem)
                  .filter(RecepcionNacionalItem.item_cotizacion_id.in_(item_ids)).all()]
        if rn_ids:
            db.query(RecepcionNacionalItem).filter(
                RecepcionNacionalItem.recepcion_id.in_(rn_ids)).delete(synchronize_session=False)
            db.query(RecepcionNacional).filter(
                RecepcionNacional.id.in_(rn_ids)).delete(synchronize_session=False)
        # Embarques + recepciones de embarque
        ei_ids = [e.id for e in db.query(EmbarqueItem)
                  .filter(EmbarqueItem.item_cotizacion_id.in_(item_ids)).all()]
        emb_ids = list({e.embarque_id for e in db.query(EmbarqueItem)
                        .filter(EmbarqueItem.id.in_(ei_ids)).all()}) if ei_ids else []
        if ei_ids:
            re_ids = [r.recepcion_id for r in db.query(RecepcionEmbarqueItem)
                      .filter(RecepcionEmbarqueItem.embarque_item_id.in_(ei_ids)).all()]
            db.query(RecepcionEmbarqueItem).filter(
                RecepcionEmbarqueItem.embarque_item_id.in_(ei_ids)).delete(synchronize_session=False)
            if re_ids:
                db.query(RecepcionEmbarque).filter(
                    RecepcionEmbarque.id.in_(re_ids)).delete(synchronize_session=False)
            db.query(EmbarqueItem).filter(
                EmbarqueItem.id.in_(ei_ids)).delete(synchronize_session=False)
        if emb_ids:
            db.query(Embarque).filter(Embarque.id.in_(emb_ids)).delete(synchronize_session=False)
    if occs:
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(occs)).all()]
        if desp_ids:
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(occs)).delete(synchronize_session=False)
    if cot_ids:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    # Embarques rezagados del MARK
    for e in db.query(Embarque).filter(Embarque.numero.like(f"{MARK}%")).all():
        re_ids = [r.id for r in db.query(RecepcionEmbarque)
                  .filter(RecepcionEmbarque.embarque_id == e.id).all()]
        if re_ids:
            db.query(RecepcionEmbarqueItem).filter(
                RecepcionEmbarqueItem.recepcion_id.in_(re_ids)).delete(synchronize_session=False)
            db.query(RecepcionEmbarque).filter(
                RecepcionEmbarque.id.in_(re_ids)).delete(synchronize_session=False)
        db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == e.id).delete(synchronize_session=False)
        db.delete(e)
    db.commit()


def run():
    db = SessionLocal()
    try:
        _limpiar(db)

        # ═══ 1. INTERNACIONAL con recepción de embarque parcial (8/10) → intacto ═══
        cot, occ, (it,) = _escenario(db, (10,))
        _recepcion_embarque(db, it, "completo", 8)
        d = _detalle_item(occ.id, it.id)
        check("internacional con recepción 8/10: disponible 8 (UNION no altera)",
              d["qty_disponible"] == 8, d)
        check("internacional: despachar 9 → 400", _despachar(occ.id, it.id, 9).status_code == 400)
        check("internacional: despachar 8 → 200", _despachar(occ.id, it.id, 8).status_code == 200)
        _limpiar(db)

        # ═══ 2. INTERNACIONAL sin recepción (histórico) → tope = vendido ═══
        cot, occ, (it,) = _escenario(db, (10,))
        d = _detalle_item(occ.id, it.id)
        check("internacional sin recepción: disponible = 10 (histórico intacto)",
              d["qty_disponible"] == 10, d)
        check("internacional sin recepción: despachar 10 → 200",
              _despachar(occ.id, it.id, 10).status_code == 200)
        _limpiar(db)

        # ═══ 3. NACIONAL 'comprado' sin recepción → indespachable (gate en_bodega) ═══
        cot, occ, (it,) = _escenario(db, (10,), estado_item="comprado")
        d = _detalle_item(occ.id, it.id)
        check("nacional comprado sin recepción: disponible 0", d["qty_disponible"] == 0, d)
        check("nacional comprado sin recepción: despachar → 400",
              _despachar(occ.id, it.id, 5).status_code == 400)
        _limpiar(db)

        # ═══ 4. NACIONAL recepción cerrada completa (10/10) → despacha 100% ═══
        cot, occ, (it,) = _escenario(db, (10,))
        _recepcion_nacional(db, it, "completo", 10)
        d = _detalle_item(occ.id, it.id)
        check("nacional completo 10: disponible 10", d["qty_disponible"] == 10, d)
        check("nacional completo 10: despachar 10 → 200",
              _despachar(occ.id, it.id, 10).status_code == 200)
        _limpiar(db)

        # ═══ 5. NACIONAL recepción parcial (6/10) → tope por lo recibido ═══
        cot, occ, (it,) = _escenario(db, (10,))
        _recepcion_nacional(db, it, "completo", 6)
        d = _detalle_item(occ.id, it.id)
        check("nacional parcial 6: disponible 6", d["qty_disponible"] == 6, d)
        check("nacional parcial 6: despachar 7 → 400", _despachar(occ.id, it.id, 7).status_code == 400)
        check("nacional parcial 6: despachar 6 → 200", _despachar(occ.id, it.id, 6).status_code == 200)
        _limpiar(db)

        # ═══ 6. NACIONAL recepción ABIERTA (no cerrada) → no suma → indespachable ═══
        cot, occ, (it,) = _escenario(db, (10,), estado_item="comprado")
        _recepcion_nacional(db, it, "completo", 10, estado="abierta")
        d = _detalle_item(occ.id, it.id)
        check("nacional recepción ABIERTA: no suma (disponible 0)", d["qty_disponible"] == 0, d)
        _limpiar(db)

        # ═══ 7. NACIONAL 'no_llego' (qty 0) → no suma; indespachable ═══
        cot, occ, (it,) = _escenario(db, (10,), estado_item="comprado")
        _recepcion_nacional(db, it, "no_llego", 0)
        d = _detalle_item(occ.id, it.id)
        check("nacional no_llego: no suma (disponible 0)", d["qty_disponible"] == 0, d)
        _limpiar(db)

        # ═══ 8. NACIONAL 'sobrante' (12/10) → tope a lo vendido (10) ═══
        cot, occ, (it,) = _escenario(db, (10,))
        _recepcion_nacional(db, it, "sobrante", 12)
        d = _detalle_item(occ.id, it.id)
        check("nacional sobrante 12: disponible topeado a 10 (lo vendido)",
              d["qty_disponible"] == 10, d)
        _limpiar(db)

        # ═══ 9. NACIONAL entregas sucesivas (3 + 4 cerradas) → tope acumula = 7 ═══
        cot, occ, (it,) = _escenario(db, (10,))
        _recepcion_nacional(db, it, "faltante", 3)
        _recepcion_nacional(db, it, "completo", 4)
        d = _detalle_item(occ.id, it.id)
        check("nacional 3+4: tope acumula = 7 (min(10,7))", d["qty_disponible"] == 7, d)
        check("nacional 3+4: despachar 8 → 400", _despachar(occ.id, it.id, 8).status_code == 400)
        check("nacional 3+4: despachar 7 → 200", _despachar(occ.id, it.id, 7).status_code == 200)
        _limpiar(db)

        # ═══ 10. OC MIXTA (nacional + internacional) → cada ítem por su fuente ═══
        cot, occ, (nac, intl) = _escenario(db, (10, 10))
        _recepcion_nacional(db, nac, "completo", 6)      # nacional: tope 6
        _recepcion_embarque(db, intl, "completo", 9)     # internacional: tope 9
        dn = _detalle_item(occ.id, nac.id)
        di = _detalle_item(occ.id, intl.id)
        check("mixta: ítem nacional topeado a 6", dn["qty_disponible"] == 6, dn)
        check("mixta: ítem internacional topeado a 9", di["qty_disponible"] == 9, di)
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_recepcion_nacional_tope():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
