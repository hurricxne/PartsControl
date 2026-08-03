"""Fase 9 — el N° de TRACKING es buscable en Embarques Pricing.

Era el ÚNICO identificador del embarque por el que no se podía buscar acá: el dueño tiene el
tracking a mano en el correo del forwarder y tenía que ir a Logística a traducirlo a N° de
embarque para poder costear. Monza NO necesita la columna `awb_numero` que sí necesitó Grupo
AM (acá `awb` ya es texto libre y los adjuntos viven aparte en monza_documentos).

SONDA DE PODER DISCRIMINANTE: con el buscador viejo (sin `tracking`) estos checks fallan 4 de
8 — verificado antes de escribir la suite, para que no sea complaciente.

Datos MARCADOS + limpieza total verificada con sesión nueva. Solo lecturas del listado.

Corre con:  ./venv/bin/python -m pytest monza_embarques_pricing/tests/test_tracking_buscable.py -q
(también:   ./venv/bin/python monza_embarques_pricing/tests/test_tracking_buscable.py)
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
import monza_models as mm
from monza_embarques_pricing.router import router as pr

MARK = "__VERIF_TRK__"
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, email="v@t.invalid", empresa="automotriz")
app = FastAPI(); app.include_router(pr)
app.dependency_overrides[get_current_user] = _cu
cli = TestClient(app)

fails = []
def ck(n, c, extra=""):
    print(("OK   | " if c else "FAIL | ") + n + ("" if c else f"  -> {extra}"))
    if not c: fails.append(n)


def run():
    TRK = f"TRK-{uuid.uuid4().hex[:8].upper()}"
    AWB = f"AWB-{uuid.uuid4().hex[:6].upper()}"
    db = SessionLocal()
    try:
        e = mm.MonzaEmbarque(numero=f"{MARK}-E1", estado="en_transito",
                             forwarder="Fastmark", awb=AWB, tracking=TRK)
        db.add(e); db.commit(); eid = e.id
        print(f"[seed] embarque {eid} · awb={AWB} · tracking={TRK}")

        r = cli.get("/api/monza/embarques-pricing", params={"q": TRK})
        ck("buscar por TRACKING lo encuentra (antes NO se podía)",
           r.status_code == 200 and any(x["embarque_id"] == eid for x in r.json()), r.text[:200])
        fila = next((x for x in r.json() if x["embarque_id"] == eid), None) if r.status_code == 200 else None
        ck("la fila EXPONE tracking (para ver por qué calzó)",
           fila is not None and fila.get("tracking") == TRK, fila)
        ck("y sigue exponiendo el AWB", fila is not None and fila.get("awb") == AWB, fila)

        r = cli.get("/api/monza/embarques-pricing", params={"q": TRK[5:12].lower()})
        ck("búsqueda PARCIAL y en minúsculas también calza",
           r.status_code == 200 and any(x["embarque_id"] == eid for x in r.json()), r.text[:200])

        for campo, val in (("AWB", AWB), ("forwarder", "Fastmark"), ("N° embarque", f"{MARK}-E1")):
            r = cli.get("/api/monza/embarques-pricing", params={"q": val})
            ck(f"NO hay regresión: sigue buscando por {campo}",
               r.status_code == 200 and any(x["embarque_id"] == eid for x in r.json()), r.text[:150])

        r = cli.get("/api/monza/embarques-pricing", params={"q": "ZZZ-NO-EXISTE-9999"})
        ck("un término que no calza NO lo devuelve",
           r.status_code == 200 and not any(x["embarque_id"] == eid for x in r.json()))
    finally:
        db.rollback()
        ids = [x[0] for x in db.query(mm.MonzaEmbarque.id).filter(mm.MonzaEmbarque.numero.like(f"{MARK}%")).all()]
        if ids:
            from monza_embarques_pricing.models import MonzaEmbPricing, MonzaEmbPricingItem, MonzaEmbPricingGasto
            pids = [x[0] for x in db.query(MonzaEmbPricing.id).filter(MonzaEmbPricing.embarque_id.in_(ids)).all()]
            if pids:
                db.query(MonzaEmbPricingItem).filter(MonzaEmbPricingItem.pricing_id.in_(pids)).delete(synchronize_session=False)
                db.query(MonzaEmbPricingGasto).filter(MonzaEmbPricingGasto.pricing_id.in_(pids)).delete(synchronize_session=False)
                db.query(MonzaEmbPricing).filter(MonzaEmbPricing.id.in_(pids)).delete(synchronize_session=False)
            db.query(mm.MonzaEmbarqueItem).filter(mm.MonzaEmbarqueItem.embarque_id.in_(ids)).delete(synchronize_session=False)
            db.query(mm.MonzaEmbarque).filter(mm.MonzaEmbarque.id.in_(ids)).delete(synchronize_session=False)
        db.commit(); db.close()
        d2 = SessionLocal()
        resto = d2.execute(text("SELECT COUNT(*) FROM monza_embarques WHERE numero LIKE :m"), {"m": f"{MARK}%"}).scalar()
        d2.close()
        print(f"[cleanup] filas MARCADAS que sobreviven: {resto}")
        print("=== TODO OK ===" if not fails and resto == 0 else f"=== FALLAS: {fails} ===")
        assert not fails and resto == 0, f"fallas={fails} residuos={resto}"


def test_monza_tracking_buscable_en_pricing():
    """Wrapper de una línea: sin esto pytest no descubre run() (patrón de la casa; ya hubo
    DOS suites invisibles por olvidarlo)."""
    run()


if __name__ == "__main__":
    run()
