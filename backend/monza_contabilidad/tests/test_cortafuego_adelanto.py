"""Test del CORTAFUEGO de adelanto en Abastecimiento.

Regla: una venta con adelanto (pct_adelanto > 0) NO puede generar OC de proveedor
(POST /api/monza/abastecimiento/comprar) hasta que Contabilidad verifique el pago. Las
ventas sin adelanto compran normalmente.

Corre con:  cd backend && ./venv/bin/python monza_contabilidad/tests/test_cortafuego_adelanto.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_router_abastecimiento import router as abast_router  # noqa: E402
from monza_contabilidad.router import router as contab_router  # noqa: E402
from monza_contabilidad.models import MonzaContAdelanto  # noqa: E402

MARK = "__TEST_CF__"

app = FastAPI()
app.include_router(abast_router)
app.include_router(contab_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, empresa="automotriz", email="t@monzaparts.cl")
client = TestClient(app)

_fails = []
_seed = {"cli": None, "cot_adel": None, "item_adel": None, "cot_sin": None, "item_sin": None}
_ocp_ids = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _cot(db, cli_id, numero, pct):
    cot = mm.MonzaCotizacion(numero=numero, cliente_id=cli_id, estado="vendida",
                             total_neto=100000, iva_monto=19000, total_bruto=119000, iva_pct=19,
                             pct_adelanto=pct)
    db.add(cot); db.flush()
    item = mm.MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Repuesto", numero_parte="RP-1",
                                  cantidad=1, precio_unitario_clp=100000, subtotal_clp=100000,
                                  estado_linea="por_comprar")
    db.add(item); db.flush()
    return cot.id, item.id


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="22.222.222-2")
        db.add(cli); db.flush()
        cot_adel, item_adel = _cot(db, cli.id, f"{MARK}-ADEL", 50)
        cot_sin, item_sin = _cot(db, cli.id, f"{MARK}-SIN", 0)
        db.commit()
        _seed.update(cli=cli.id, cot_adel=cot_adel, item_adel=item_adel, cot_sin=cot_sin, item_sin=item_sin)
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        for oid in _ocp_ids:
            db.query(mm.MonzaOcProveedor).filter(mm.MonzaOcProveedor.id == oid).delete()
        cot_ids = [c for c in (_seed["cot_adel"], _seed["cot_sin"]) if c]
        if cot_ids:
            db.query(MonzaContAdelanto).filter(MonzaContAdelanto.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaCotizacionItem).filter(mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        if _seed["cli"]:
            db.query(mm.MonzaCliente).filter(mm.MonzaCliente.id == _seed["cli"]).delete()
        db.commit()
        print("Cleanup OK")
    except Exception as e:  # noqa: BLE001
        db.rollback(); print("Cleanup parcial:", e)
    finally:
        db.close()


def _comprar(*item_ids):
    return client.post("/api/monza/abastecimiento/comprar",
                       json={"item_ids": list(item_ids), "proveedor_nombre": "Proveedor Test"})


def run():
    # 1) Venta CON adelanto, sin verificar → comprar BLOQUEADO (409)
    r = _comprar(_seed["item_adel"])
    check("comprar adelanto NO verificado -> 409", r.status_code == 409, r.text)

    # 1b) Mezcla (ítem con adelanto no verificado + ítem sin adelanto) → bloquea TODO (409)
    rmix = _comprar(_seed["item_adel"], _seed["item_sin"])
    check("comprar mezcla con adelanto sin verificar -> 409", rmix.status_code == 409, rmix.text)

    # 2) Contabilidad verifica el adelanto
    rv = client.post(f"/api/monza/contabilidad/ventas/{_seed['cot_adel']}/adelanto/verificar",
                     json={"monto": 59500, "fecha_pago": "2026-06-29", "banco": "BancoX"})
    check("verificar adelanto -> 200", rv.status_code == 200, rv.text)

    # 3) Ahora SÍ se puede generar la OC de proveedor
    r2 = _comprar(_seed["item_adel"])
    check("comprar adelanto verificado -> 200", r2.status_code == 200, r2.text)
    if r2.status_code == 200:
        _ocp_ids.append(r2.json()["ocp_id"])

    # 4) Control: venta SIN adelanto compra directo (sin verificación)
    r3 = _comprar(_seed["item_sin"])
    check("comprar venta SIN adelanto -> 200", r3.status_code == 200, r3.text)
    if r3.status_code == 200:
        _ocp_ids.append(r3.json()["ocp_id"])

    print()
    if _fails:
        print(f"=== {len(_fails)} FALLO(S): {_fails} ===")
        return False
    print("=== TODO OK ===")
    return True


if __name__ == "__main__":
    seed()
    ok = False
    try:
        ok = run()
    finally:
        cleanup()
    sys.exit(0 if ok else 1)
