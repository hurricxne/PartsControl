"""Test de integración de la OC-Cliente (obligatoriedad al crear + edición ex-post + guard de rol).

Monta el router de compras (sin tocar main.py) y ejerce:
  · POST /compras/oc-cliente SIN numero_oc  -> 400 (bloqueo duro).
  · POST /compras/oc-cliente CON numero_oc  -> 201.
  · PUT  /compras/oc-cliente/{id}           -> 200 y persiste los 5 campos.
  · require_rol: permisivo sin rol (hoy) y 403 con un rol no permitido.
LIMPIA todo al final. Corre con:
    ./venv/bin/python routers/tests/test_oc_cliente.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from role_guard import require_rol  # noqa: E402
from routers.compras import router as compras_router  # noqa: E402
from models.models import Cotizacion, OcCliente, User  # noqa: E402

MARK = "__TEST_OC__"
CURRENT = {"empresa": "mineria", "id": None, "rol": None}

Base.metadata.create_all(bind=engine, checkfirst=True)


def _current_user():
    ns = SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])
    if CURRENT["rol"] is not None:      # sin rol -> getattr(...,'rol',None) es None -> permisivo
        ns.rol = CURRENT["rol"]
    return ns


app = FastAPI()
app.include_router(compras_router, prefix="/api")
app.dependency_overrides[get_current_user] = _current_user
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def main():
    db = SessionLocal()
    asesor = User(email=f"{MARK}@t.cl", nombre=MARK, hashed_password="x", is_active=1, empresa="mineria")
    cot = Cotizacion(numero=MARK, cliente=MARK)
    db.add(asesor)
    db.add(cot)
    db.commit()
    db.refresh(asesor)
    db.refresh(cot)
    CURRENT["id"] = asesor.id
    oc_id = None

    try:
        # 1) Bloqueo duro: sin numero_oc -> 400
        r = client.post("/api/compras/oc-cliente", json={"cotizacion_id": cot.id})
        check("POST sin numero_oc -> 400", r.status_code == 400, r.text)

        # 2) Con numero_oc -> 201
        r = client.post("/api/compras/oc-cliente", json={"cotizacion_id": cot.id, "numero_oc": "OC-INI"})
        check("POST con numero_oc -> 201", r.status_code == 201, r.text)
        oc_id = r.json().get("id")

        # 2b) Idempotente por cotización: reintentar el cierre (p.ej. porque falló el
        #     avance de fase) devuelve la MISMA OC, no una duplicada
        r = client.post("/api/compras/oc-cliente",
                        json={"cotizacion_id": cot.id, "numero_oc": "OC-RETRY"})
        check("POST reintentado devuelve la misma OC", r.status_code == 201
              and r.json().get("id") == oc_id, r.text)
        db.rollback()
        n_ocs = db.query(OcCliente).filter(OcCliente.cotizacion_id == cot.id).count()
        check("sin OC duplicada", n_ocs == 1, n_ocs)

        # 3) PUT actualiza los 5 campos
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={
            "numero_oc": "OC-9999",
            "fecha_oc": "2026-07-16",
            "cond_pago": "60 días contra factura",
            "fecha_entrega": "2026-09-01",
            "asesor_id": asesor.id,
        })
        check("PUT -> 200", r.status_code == 200, r.text)

        # rollback y no expire_all: bajo REPEATABLE READ de MySQL, expire_all no
        # refresca el SNAPSHOT de la transacción y no se vería el commit del API
        db.rollback()
        oc = db.query(OcCliente).filter(OcCliente.id == oc_id).first()
        check("PUT persiste numero_oc", oc and oc.numero_oc == "OC-9999")
        check("PUT persiste cond_pago", oc and oc.cond_pago == "60 días contra factura")
        check("PUT persiste asesor_id", oc and oc.asesor_id == asesor.id)
        check("PUT persiste fecha_entrega", oc and oc.fecha_entrega is not None)

        # 3b) Desasignar asesor: el null EXPLÍCITO limpia el campo (no es un no-op
        #     silencioso — el modal manda null al elegir "— sin asesor —")
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={"asesor_id": None})
        check("PUT asesor null -> 200", r.status_code == 200, r.text)
        db.rollback()
        oc = db.query(OcCliente).filter(OcCliente.id == oc_id).first()
        check("asesor desasignado (NULL persistido)", oc and oc.asesor_id is None)
        client.put(f"/api/compras/oc-cliente/{oc_id}", json={"asesor_id": asesor.id})

        # 4) PUT con numero_oc vacío -> 400 (no se puede vaciar el N° OC)
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={"numero_oc": "  "})
        check("PUT numero_oc vacío -> 400", r.status_code == 400, r.text)

        # 4b) asesor_id inválido (inexistente o de otra empresa) -> 400
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={"asesor_id": 99999999})
        check("PUT asesor inexistente -> 400", r.status_code == 400, r.text)
        monza = User(email=f"{MARK}mz@t.cl", nombre=f"{MARK}MZ", hashed_password="x",
                     is_active=1, empresa="automotriz")
        db.add(monza); db.commit(); db.refresh(monza)
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={"asesor_id": monza.id})
        check("PUT asesor de otra empresa -> 400", r.status_code == 400, r.text)

        # 4c) Con guía SII 52 EMITIDA referenciando la OC, el N°/fecha no se editan (409);
        #     los demás campos sí. (La guía 801 lleva el N° y la fecha de la OC.)
        from models.models import Despacho
        from wasabil_dte.models import WasabilDte
        desp = Despacho(numero_despacho=f"{MARK}-DSP", oc_cliente_id=oc_id,
                        estado="en_preparacion")
        db.add(desp); db.flush()
        dte = WasabilDte(tipo_dte=52, despacho_id=desp.id, uuid="uuid-oc-t",
                         status_id=3, folio="999")
        db.add(dte); db.commit()
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={"numero_oc": "OC-CAMBIADA"})
        check("PUT numero_oc con guía emitida -> 409", r.status_code == 409
              and "999" in r.json()["detail"], r.text)
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={"fecha_oc": "2026-01-01"})
        check("PUT fecha_oc con guía emitida -> 409", r.status_code == 409, r.text)
        r = client.put(f"/api/compras/oc-cliente/{oc_id}",
                       json={"cond_pago": "Contado", "numero_oc": "OC-9999"})
        check("PUT mismo N° + cond_pago con guía -> 200", r.status_code == 200, r.text)
        db.query(WasabilDte).filter(WasabilDte.despacho_id == desp.id).delete()
        db.query(Despacho).filter(Despacho.id == desp.id).delete()
        db.query(User).filter(User.id == monza.id).delete()
        db.commit()

        # 5) Guard de rol: rol no permitido -> 403; rol permitido -> 200
        CURRENT["rol"] = "logistica"
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={"cond_pago": "Contado"})
        check("PUT rol logistica -> 403", r.status_code == 403, r.text)
        CURRENT["rol"] = "comercial"
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", json={"cond_pago": "Contado"})
        check("PUT rol comercial -> 200", r.status_code == 200, r.text)
        CURRENT["rol"] = None

        # 6) require_rol como unidad (sin DB): None permite, rol malo 403, rol bueno pasa
        guard = require_rol("comercial", "admin")
        check("require_rol sin rol -> permite", guard(SimpleNamespace(id=1)).id == 1)
        try:
            guard(SimpleNamespace(id=1, rol="bodega"))
            check("require_rol rol no permitido -> 403", False, "no lanzó")
        except Exception as e:
            check("require_rol rol no permitido -> 403", getattr(e, "status_code", None) == 403)
        check("require_rol rol permitido -> pasa", guard(SimpleNamespace(id=1, rol="admin")).id == 1)

    finally:
        # Limpieza (robusta: también los datos de la guía SII si un check falló a medias)
        db.rollback()
        from models.models import Despacho as _D
        from wasabil_dte.models import WasabilDte as _W
        desp_ids = [d.id for d in db.query(_D)
                    .filter(_D.numero_despacho.like(f"{MARK}%")).all()]
        if desp_ids:
            db.query(_W).filter(_W.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(_D).filter(_D.id.in_(desp_ids)).delete(synchronize_session=False)
        if oc_id:
            db.query(OcCliente).filter(OcCliente.id == oc_id).delete()
        db.query(Cotizacion).filter(Cotizacion.numero == MARK).delete()
        db.query(User).filter(User.email.in_([f"{MARK}@t.cl", f"{MARK}mz@t.cl"])).delete(
            synchronize_session=False)
        db.commit()
        db.close()

    # Mismo patrón que las demás suites (test_despachos_guards, wasabil_dte/tests):
    # AssertionError para que pytest lo recolecte; ejecutable directo también.
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\nTODOS OK")


def test_oc_cliente():
    main()


if __name__ == "__main__":
    main()
