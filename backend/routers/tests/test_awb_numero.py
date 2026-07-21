"""Test de integración del N° AWB/BL escribible (embarques.awb_numero).

POR QUÉ (negocio): la columna `awb` guarda el NOMBRE DEL ARCHIVO del adjunto
AWB/BL; el dueño necesita un N° universal, escribible a mano y BUSCABLE. Se agregó
la columna nueva `awb_numero`, independiente del adjunto. Este test verifica el
ciclo completo SIN tocar main.py: montar el router de compras con TestClient +
stub de usuario, y ejercer:
  · CIERRE de pre-embarque con awb_numero  -> persiste; NO contamina `awb`.
  · listado/detalle de embarques exponen awb_numero.
  · edición ex-post (PUT) sobreescribe el número.
  · buscador de Embarques Pricing (haystack) matchea por awb_numero.
  · embarque histórico sin número no rompe (awb_numero == "" en la API).
LIMPIA todo al final. Corre con:
    ./venv/bin/python routers/tests/test_awb_numero.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from routers.compras import router as compras_router  # noqa: E402
from embarques_pricing import router as EP  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, PreEmbarque, PreEmbarqueItem,
    Embarque, EmbarqueItem, User,
)
from embarques_pricing.models import EmbarquePricing  # noqa: E402

MARK = "__TEST_AWB__"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)


def _current_user():
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app = FastAPI()
app.include_router(compras_router, prefix="/api")
app.dependency_overrides[get_current_user] = _current_user
client = TestClient(app)

# Usuario stub para llamar EP.listar_embarques_pricing directo (sólo lo recibe por
# Depends; no lo usa para filtrar → un SimpleNamespace basta).
EP_USER = SimpleNamespace(id=1, empresa="mineria")

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def main():
    db = SessionLocal()
    # Semilla: cotización + 2 ítems + pre-embarque con 2 líneas.
    cot = Cotizacion(numero=MARK, cliente=MARK)
    db.add(cot)
    db.commit()
    db.refresh(cot)

    it1 = ItemCotizacion(cotizacion_id=cot.id, numero_parte=f"{MARK}-P1", cantidad=1)
    it2 = ItemCotizacion(cotizacion_id=cot.id, numero_parte=f"{MARK}-P2", cantidad=1)
    db.add(it1); db.add(it2)
    pre = PreEmbarque(numero=f"{MARK}-PRE", estado="en_preparacion")
    db.add(pre)
    db.commit()
    db.refresh(it1); db.refresh(it2); db.refresh(pre)

    db.add(PreEmbarqueItem(pre_embarque_id=pre.id, item_cotizacion_id=it1.id))
    db.add(PreEmbarqueItem(pre_embarque_id=pre.id, item_cotizacion_id=it2.id))
    db.commit()

    emb_ids = []  # para limpieza (el N° del embarque lo genera el server, no es MARK)

    try:
        # 2) CIERRE con N° AWB escrito a mano → 201
        r = client.post(f"/api/compras/pre-embarques/{pre.id}/cerrar",
                        json={"awb_numero": "176-99887766"})
        check("cerrar con awb_numero -> 201", r.status_code == 201, r.text)
        emb_id = r.json().get("id")
        if emb_id:
            emb_ids.append(emb_id)

        # 3) Persistencia + NO contaminación del adjunto `awb`
        db.rollback()  # refresca el snapshot REPEATABLE READ para ver el commit del API
        emb = db.query(Embarque).filter(Embarque.id == emb_id).first()
        check("awb_numero persistido", emb and emb.awb_numero == "176-99887766",
              emb.awb_numero if emb else None)
        check("adjunto awb NO contaminado", emb and not emb.awb, emb.awb if emb else None)

        # 4) Listado expone awb_numero
        r = client.get("/api/compras/embarques-list")
        row = next((x for x in r.json() if x["id"] == emb_id), None)
        check("embarques-list trae awb_numero", row and row["awb_numero"] == "176-99887766",
              row)

        # 5) Detalle expone awb_numero
        r = client.get(f"/api/compras/embarques-list/{emb_id}")
        check("detalle trae awb_numero", r.status_code == 200
              and r.json().get("awb_numero") == "176-99887766", r.text)

        # 6) Edición ex-post: el PUT sobreescribe el número
        r = client.put(f"/api/compras/embarques-list/{emb_id}",
                       json={"awb_numero": "235-00011122"})
        check("PUT awb_numero -> 200", r.status_code == 200, r.text)
        db.rollback()
        emb = db.query(Embarque).filter(Embarque.id == emb_id).first()
        check("PUT persiste nuevo awb_numero", emb and emb.awb_numero == "235-00011122",
              emb.awb_numero if emb else None)

        # 6b) N° AWB > 100 chars: la columna es VARCHAR(100). El schema lo acota
        # (max_length=100), así que debe rebotar como 422 de validación y NO como
        # 500 (DataError 1406) al intentar persistirlo. El valor previo no cambia.
        r = client.put(f"/api/compras/embarques-list/{emb_id}",
                       json={"awb_numero": "X" * 101})
        check("PUT awb_numero >100 -> 422 (no 500)", r.status_code == 422, r.text)
        db.rollback()
        emb = db.query(Embarque).filter(Embarque.id == emb_id).first()
        check("awb_numero intacto tras 422", emb and emb.awb_numero == "235-00011122",
              emb.awb_numero if emb else None)

        # 7) Buscador de Pricing matchea por N° AWB (haystack incluye awb_numero)
        db.rollback()
        res = EP.listar_embarques_pricing(q="235-00011122", db=db, current_user=EP_USER)
        ids = {x["embarque_id"] for x in res}
        check("pricing search por N° AWB incluye el embarque", emb_id in ids, ids)

        db.rollback()
        res_none = EP.listar_embarques_pricing(q="zzz-inexistente", db=db, current_user=EP_USER)
        ids_none = {x["embarque_id"] for x in res_none}
        check("pricing search inexistente NO lo incluye", emb_id not in ids_none, ids_none)

        # 8) Embarque histórico SIN número: no rompe (awb_numero == "" en la API)
        emb2 = Embarque(numero=f"{MARK}-EMB2", estado="en_bodega")
        db.add(emb2)
        db.commit()
        db.refresh(emb2)
        emb_ids.append(emb2.id)

        r = client.get("/api/compras/embarques-list")
        check("embarques-list responde 200", r.status_code == 200, r.text)
        row2 = next((x for x in r.json() if x["id"] == emb2.id), None)
        check("embarque sin número -> awb_numero == ''",
              row2 and row2["awb_numero"] == "", row2)

        db.rollback()
        res = EP.listar_embarques_pricing(q="235-00011122", db=db, current_user=EP_USER)
        ids = {x["embarque_id"] for x in res}
        check("pricing search NO incluye el histórico sin número", emb2.id not in ids, ids)

    finally:
        db.rollback()
        # Orden FK: items de embarque → pricing → embarques → pre-embarque (cascade
        # borra sus líneas) → ítems de cotización (cubre posibles clones) → cotización.
        if emb_ids:
            db.query(EmbarqueItem).filter(
                EmbarqueItem.embarque_id.in_(emb_ids)).delete(synchronize_session=False)
            db.query(EmbarquePricing).filter(
                EmbarquePricing.embarque_id.in_(emb_ids)).delete(synchronize_session=False)
            db.query(Embarque).filter(
                Embarque.id.in_(emb_ids)).delete(synchronize_session=False)
        db.query(PreEmbarque).filter(PreEmbarque.numero.like(f"{MARK}%")).delete(
            synchronize_session=False)
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == cot.id).delete(synchronize_session=False)
        db.query(Cotizacion).filter(Cotizacion.numero == MARK).delete(
            synchronize_session=False)
        db.commit()
        db.close()

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\nTODOS OK")


def test_awb_numero():
    main()


if __name__ == "__main__":
    main()
