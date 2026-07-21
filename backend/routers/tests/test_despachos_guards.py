"""Test de integración de los guards de Despachos (endurecimiento post-revisión).

Monta el router de despachos en una app efímera (sin tocar main.py) y ejerce:
  · Candado de empresa: usuario automotriz → 403 en todo el router.
  · create_despacho: ítems repetidos → 400; sobredespacho vs disponible → 400;
    correlativo duplicado (carrera simulada) → reintenta y crea con N° único.
  · anular_despacho: con guía SII emitida (o en curso) → 409; con DTE fallido → anula.
  · update_despacho / firmar_despacho: pisar el folio SII → 409; mismo valor → no-op;
    la firma sin tocar el N° sigue funcionando.
LIMPIA todo al final. Corre con:
    ./venv/bin/python -m pytest routers/tests/test_despachos_guards.py -q
(también:  ./venv/bin/python routers/tests/test_despachos_guards.py)
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
)
from routers import despachos as despachos_mod  # noqa: E402
from routers.despachos import router, _DOCS_DIR  # noqa: E402
from wasabil_dte.models import WasabilDte  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_DSPG__"
CURRENT = {"empresa": "mineria", "id": None}

app = FastAPI()
app.include_router(router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear_datos(db):
    """OC con 2 ítems en bodega (10 y 20 unidades), sin despachos."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} CLIENTE",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it1 = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                         descripcion="Filtro de aceite motor", cantidad=10,
                         estado_item="en_bodega")
    it2 = ItemCotizacion(cotizacion_id=cot.id, item_num=2, numero_parte="6I-2503",
                         descripcion="Sello de polvo", cantidad=20,
                         estado_item="en_bodega")
    db.add_all([it1, it2]); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC", fecha_oc="2026-06-10")
    db.add(oc); db.commit()
    for obj in (cot, it1, it2, oc):
        db.refresh(obj)
    return cot, oc, it1, it2


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    if cot_ids:
        oc_ids = [o.id for o in db.query(OcCliente)
                  .filter(OcCliente.cotizacion_id.in_(cot_ids)).all()]
        if oc_ids:
            desp_ids = [d.id for d in db.query(Despacho)
                        .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
            if desp_ids:
                db.query(WasabilDte).filter(WasabilDte.despacho_id.in_(desp_ids)) \
                    .delete(synchronize_session=False)
                db.query(DespachoItem).filter(DespachoItem.despacho_id.in_(desp_ids)) \
                    .delete(synchronize_session=False)
                db.query(Despacho).filter(Despacho.id.in_(desp_ids)) \
                    .delete(synchronize_session=False)
            db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)) \
                .delete(synchronize_session=False)
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id.in_(cot_ids)) \
            .delete(synchronize_session=False)
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)) \
            .delete(synchronize_session=False)
    db.commit()


def run():
    db = SessionLocal()
    archivo_firma = f"{MARK.strip('_').lower()}_guia.pdf"
    ruta_firma = os.path.join(_DOCS_DIR, archivo_firma)
    try:
        _limpiar(db)
        os.makedirs(_DOCS_DIR, exist_ok=True)
        with open(ruta_firma, "wb") as f:
            f.write(b"%PDF-1.4 test")

        # ── Candado de empresa: automotriz no entra a despachos ──
        CURRENT["empresa"] = "automotriz"
        r = client.get("/api/despachos/counts")
        check("candado empresa: automotriz 403", r.status_code == 403, r.text)
        CURRENT["empresa"] = "mineria"
        r = client.get("/api/despachos/counts")
        check("candado empresa: mineria pasa", r.status_code == 200, r.text)

        # ── create: ítems repetidos → 400 (burlarían la validación de disponible) ──
        cot, oc, it1, it2 = _crear_datos(db)
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": oc.id,
            "items": [
                {"item_cotizacion_id": it1.id, "qty_despachada": 6},
                {"item_cotizacion_id": it1.id, "qty_despachada": 6},
            ],
        })
        check("ítems repetidos 400", r.status_code == 400
              and "repetidos" in r.json()["detail"], r.text)

        # ── create: el disponible descuenta lo ya despachado ──
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": oc.id,
            "items": [{"item_cotizacion_id": it1.id, "qty_despachada": 6}],
        })
        check("despacho parcial 6/10 ok", r.status_code == 200, r.text)
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": oc.id,
            "items": [{"item_cotizacion_id": it1.id, "qty_despachada": 5}],
        })
        check("sobredespacho 6+5>10 → 400", r.status_code == 400
              and "excede" in r.json()["detail"], r.text)
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": oc.id,
            "items": [{"item_cotizacion_id": it1.id, "qty_despachada": 4}],
        })
        check("remanente exacto 6+4=10 ok", r.status_code == 200, r.text)
        _limpiar(db)

        # ── create: choque de correlativo (carrera simulada) → reintenta ──
        cot, oc, it1, it2 = _crear_datos(db)
        orig_next = despachos_mod._next_numero_despacho
        numero_ocupado = orig_next(db)
        db.add(Despacho(numero_despacho=numero_ocupado, oc_cliente_id=oc.id,
                        estado="en_preparacion"))
        db.commit()
        llamadas = {"n": 0}

        def _next_con_choque(session):
            # 1ª llamada: devuelve el N° ya ocupado (simula al otro request que
            # ganó la carrera); siguientes: el cálculo real
            llamadas["n"] += 1
            return numero_ocupado if llamadas["n"] == 1 else orig_next(session)

        despachos_mod._next_numero_despacho = _next_con_choque
        try:
            r = client.post("/api/despachos/", json={
                "oc_cliente_id": oc.id,
                "items": [{"item_cotizacion_id": it2.id, "qty_despachada": 5}],
            })
        finally:
            despachos_mod._next_numero_despacho = orig_next
        check("correlativo duplicado: reintenta y crea", r.status_code == 200
              and r.json()["numero_despacho"] != numero_ocupado
              and llamadas["n"] >= 2, r.text)
        _limpiar(db)

        # ── anular: guía SII emitida bloquea (409); DTE fallido no bloquea ──
        cot, oc, it1, it2 = _crear_datos(db)
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": oc.id,
            "items": [{"item_cotizacion_id": it1.id, "qty_despachada": 10}],
        })
        desp_id = r.json()["id"]
        db.add(WasabilDte(tipo_dte=52, despacho_id=desp_id, uuid="uuid-t",
                          status_id=3, folio="777"))
        db.commit()
        r = client.delete(f"/api/despachos/{desp_id}")
        check("anular con guía emitida 409", r.status_code == 409
              and "777" in r.json()["detail"], r.text)
        # En curso (uuid + procesando) también bloquea
        dte = db.query(WasabilDte).filter(WasabilDte.despacho_id == desp_id).first()
        dte.status_id = 2; dte.folio = None
        db.commit()
        r = client.delete(f"/api/despachos/{desp_id}")
        check("anular con guía en curso 409", r.status_code == 409, r.text)
        # Fallida del SII: no hay documento legal → se puede anular
        dte.status_id = 4
        db.commit()
        r = client.delete(f"/api/despachos/{desp_id}")
        check("anular con DTE fallido ok", r.status_code == 200, r.text)
        _limpiar(db)

        # ── update / firmar: el folio SII no se pisa a mano ──
        cot, oc, it1, it2 = _crear_datos(db)
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": oc.id,
            "items": [{"item_cotizacion_id": it1.id, "qty_despachada": 10}],
        })
        desp_id = r.json()["id"]
        db.rollback()  # refresca el snapshot: el despacho lo creó la sesión del API
        db.add(WasabilDte(tipo_dte=52, despacho_id=desp_id, uuid="uuid-t2",
                          status_id=3, folio="888"))
        d = db.get(Despacho, desp_id)
        d.numero_guia = "888"  # como lo deja _actualizar_desde_wasabil al emitir
        db.commit()
        r = client.put(f"/api/despachos/{desp_id}",
                       json={"numero_guia": "GUIA-MANUAL"})
        check("update pisa folio 409", r.status_code == 409
              and "888" in r.json()["detail"], r.text)
        r = client.put(f"/api/despachos/{desp_id}",
                       json={"numero_guia": "888", "transportista": "Samex"})
        check("update mismo folio + transportista ok", r.status_code == 200, r.text)
        db.rollback()
        check("transportista actualizado y folio intacto",
              db.get(Despacho, desp_id).transportista == "Samex"
              and db.get(Despacho, desp_id).numero_guia == "888")
        # Firmar: cerrar primero (la firma exige estado despachado)
        r = client.post(f"/api/despachos/{desp_id}/cerrar")
        check("cerrar ok", r.status_code == 200, r.text)
        r = client.post(f"/api/despachos/{desp_id}/firmar",
                        json={"numero_guia": "GUIA-5", "archivo": archivo_firma})
        check("firmar pisando folio 409", r.status_code == 409, r.text)
        r = client.post(f"/api/despachos/{desp_id}/firmar",
                        json={"archivo": archivo_firma})
        check("firmar sin tocar N° ok", r.status_code == 200
              and r.json()["numero_guia"] == "888", r.text)
        _limpiar(db)

    finally:
        _limpiar(db)
        if os.path.isfile(ruta_firma):
            os.remove(ruta_firma)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_despachos_guards():
    run()


if __name__ == "__main__":
    run()
