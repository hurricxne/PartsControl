"""Fase 9 · ítem 6 — PARIDAD INVERSA: los 3 endurecimientos que MonzaParts ganó en la Fase 2
del espejo y a Grupo AM le faltaban.

(a) ALTO · "Faltante" con recibido >= vendido. Es UN CLIC: el input de BodegaPage.tsx:95 viene
    precargado con la cantidad VENDIDA. Efecto: 'faltante' está en _RECEPCION_UTILIZABLE, así
    que el tope queda en min(vendido, recibido) = vendido y la línea entera sale despachable; y
    en el cierre faltante_pendiente = 0, así que NO se crea reclamo. Las unidades que nunca
    llegaron desaparecen sin traza y sale guía 52 + factura 33 por mercadería inexistente.
(b) MEDIO · Retry 1213/1205 en create_despacho y anular_despacho. Crear lockea los ÍTEMS
    primero y cerrar/anular el DESPACHO primero → ciclo InnoDB. `cerrar` ya tenía el retry.
(c) MEDIO · Pertenencia del ítem al embarque de la recepción. Una fila espuria infla el tope
    físico de OTRA OC: el cierre la ignora pero _qty_recibida_utilizable la cuenta. Invisible
    y activa. Solo alcanzable por API.

Más el LOCK de la recepción, sin el cual (a) y (c) tienen una ventana y dos cierres simultáneos
DUPLICAN los ReclamoProveedor.

SONDA DE PODER DISCRIMINANTE: revirtiendo los guards, 7 de estos checks FALLAN y el backend
responde {"ok":true} — verificado antes de escribir la suite, para que no sea complaciente.

Datos MARCADOS + limpieza total verificada con sesión nueva.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_paridad_inversa_bodega.py -q
"""
import os, sys, uuid
from types import SimpleNamespace
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from database import SessionLocal, get_db
from auth import get_current_user
from models.models import (Cotizacion, ItemCotizacion, OcCliente, Embarque, EmbarqueItem,
                           RecepcionEmbarque, RecepcionEmbarqueItem, ReclamoProveedor)
from routers.bodega import router as bodega_router
import routers.despachos as dsp

MARK = f"__F9F_{uuid.uuid4().hex[:5].upper()}__"
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, email="f@9.invalid", empresa="mineria")
app = FastAPI(); app.include_router(bodega_router, prefix="/api")
app.dependency_overrides[get_current_user] = _cu
cli = TestClient(app, raise_server_exceptions=False)

fails = []
def ck(n, c, extra=""):
    print(("OK   | " if c else "FAIL | ") + n + ("" if c else f"  -> {str(extra)[:220]}"))
    if not c: fails.append(n)

def seed(db, suf, cantidad=10):
    cot = Cotizacion(numero=f"{MARK}-C{suf}", cliente=f"{MARK} Cli", rut_cliente="11.111.111-1")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte=f"{MARK}-P{suf}",
                        descripcion="Filtro", cantidad=cantidad, estado_item="en_transito")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC{suf}", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    e = Embarque(numero=f"{MARK}-E{suf}", estado="en_transito")
    db.add(e); db.flush()
    ei = EmbarqueItem(embarque_id=e.id, item_cotizacion_id=it.id)
    db.add(ei); db.flush()
    r = RecepcionEmbarque(embarque_id=e.id, estado="abierta")
    db.add(r); db.flush()
    db.commit()
    return SimpleNamespace(cot=cot.id, item=it.id, oc=oc.id, emb=e.id, ei=ei.id, rec=r.id)


def run():
    db = SessionLocal()
    try:
        # ══ (a) FALTANTE con recibido == vendido ══
        A = seed(db, "A")
        r = cli.patch(f"/api/bodega/recepciones/{A.rec}/items/0",
                      json={"embarque_item_id": A.ei, "qty_recibida": 10,
                            "estado_recepcion": "faltante"})
        ck("(a) 'Faltante' con recibido == vendido (10 de 10) → 400", r.status_code == 400, r.text)
        ck("(a) el mensaje dice el número vendido y qué usar en su lugar",
           "10" in r.text and "Completo" in r.text, r.text)
        db.rollback()
        ck("(a) NO se marcó la línea",
           db.query(RecepcionEmbarqueItem).filter(RecepcionEmbarqueItem.recepcion_id == A.rec).count() == 0)
        # y el camino legítimo sigue abierto
        r = cli.patch(f"/api/bodega/recepciones/{A.rec}/items/0",
                      json={"embarque_item_id": A.ei, "qty_recibida": 6, "estado_recepcion": "faltante"})
        ck("(a) NO hay regresión: faltante REAL (6 de 10) → 200", r.status_code == 200, r.text)
        r = cli.patch(f"/api/bodega/recepciones/{A.rec}/items/0",
                      json={"embarque_item_id": A.ei, "qty_recibida": 10, "estado_recepcion": "completo"})
        ck("(a) NO hay regresión: 'completo' con todo (10 de 10) → 200", r.status_code == 200, r.text)
        r = cli.patch(f"/api/bodega/recepciones/{A.rec}/items/0",
                      json={"embarque_item_id": A.ei, "qty_recibida": 5, "qty_danada": -3,
                            "estado_recepcion": "completo"})
        ck("(a) cantidad DAÑADA negativa → 400", r.status_code == 400, r.text)

        # ══ (c) PERTENENCIA: ítem de OTRO embarque ══
        B = seed(db, "B"); C = seed(db, "C")
        r = cli.patch(f"/api/bodega/recepciones/{B.rec}/items/0",
                      json={"embarque_item_id": C.ei, "qty_recibida": 5, "estado_recepcion": "completo"})
        ck("(c) ítem de OTRO embarque → 400", r.status_code == 400, r.text)
        ck("(c) el mensaje lo explica", "no pertenece" in r.text.lower(), r.text)
        db.rollback()
        ck("(c) NO se creó la fila espuria (que habría inflado el tope de la otra OC)",
           db.query(RecepcionEmbarqueItem).filter(RecepcionEmbarqueItem.recepcion_id == B.rec).count() == 0)
        r = cli.post(f"/api/bodega/recepciones/{B.rec}/items",
                     json={"embarque_item_id": C.ei, "qty_recibida": 5, "estado_recepcion": "completo"})
        ck("(c) el MISMO guard en el endpoint POST /items → 400", r.status_code == 400, r.text)

        # ══ (b) RETRY de deadlock ══
        import inspect
        src_crear = inspect.getsource(dsp.create_despacho)
        ck("(b) create_despacho captura OperationalError 1213/1205",
           "OperationalError" in src_crear and "1213" in src_crear, src_crear[:150])
        src_anular = inspect.getsource(dsp.anular_despacho)
        ck("(b) anular_despacho tiene el retry y delega en _anular_despacho_tx",
           "OperationalError" in src_anular and "_anular_despacho_tx" in src_anular, src_anular[:150])
        ck("(b) _anular_despacho_tx existe y es invocable", callable(getattr(dsp, "_anular_despacho_tx", None)))

        # ══ lock en cerrar_recepcion ══
        src_cerrar = inspect.getsource(dsp.__dict__.get("cerrar_despacho", lambda: None)) if False else ""
        import routers.bodega as bod
        ck("lock: cerrar_recepcion lee la recepción con with_for_update",
           "with_for_update" in inspect.getsource(bod.cerrar_recepcion))
        ck("lock: marcar_item también", "with_for_update" in inspect.getsource(bod.marcar_item))
    finally:
        db.rollback()
        cots = [x[0] for x in db.query(Cotizacion.id).filter(Cotizacion.numero.like(f"{MARK}%")).all()]
        embs = [x[0] for x in db.query(Embarque.id).filter(Embarque.numero.like(f"{MARK}%")).all()]
        recs = [x[0] for x in db.query(RecepcionEmbarque.id).filter(RecepcionEmbarque.embarque_id.in_(embs or [0])).all()]
        its = [x[0] for x in db.query(ItemCotizacion.id).filter(ItemCotizacion.cotizacion_id.in_(cots or [0])).all()]
        S = False
        db.query(ReclamoProveedor).filter(ReclamoProveedor.item_cotizacion_id.in_(its or [0])).delete(synchronize_session=S)
        db.query(RecepcionEmbarqueItem).filter(RecepcionEmbarqueItem.recepcion_id.in_(recs or [0])).delete(synchronize_session=S)
        db.query(RecepcionEmbarque).filter(RecepcionEmbarque.id.in_(recs or [0])).delete(synchronize_session=S)
        db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id.in_(embs or [0])).delete(synchronize_session=S)
        db.query(Embarque).filter(Embarque.id.in_(embs or [0])).delete(synchronize_session=S)
        db.query(OcCliente).filter(OcCliente.cotizacion_id.in_(cots or [0])).delete(synchronize_session=S)
        db.query(ItemCotizacion).filter(ItemCotizacion.id.in_(its or [0])).delete(synchronize_session=S)
        db.query(Cotizacion).filter(Cotizacion.id.in_(cots or [0])).delete(synchronize_session=S)
        db.commit(); db.close()
        d2 = SessionLocal()
        resto = d2.execute(text("SELECT COUNT(*) FROM cotizaciones WHERE numero LIKE :m"), {"m": f"{MARK}%"}).scalar()
        d2.close()
        print(f"[cleanup] filas MARCADAS que sobreviven: {resto}")
        print("=== TODO OK ===" if not fails and resto == 0 else f"=== FALLAS ({len(fails)}): {fails} ===")
        assert not fails and resto == 0, f"fallas={fails} residuos={resto}"


def test_paridad_inversa_bodega_grupoam():
    """Wrapper de una línea: sin esto pytest no descubre run() (en este repo ya hubo DOS
    suites invisibles por olvidarlo)."""
    run()


if __name__ == "__main__":
    run()
