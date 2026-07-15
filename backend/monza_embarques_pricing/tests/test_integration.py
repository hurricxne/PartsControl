"""Test de integración del módulo Embarques Pricing MonzaParts contra la BD local.

Monta el router en apps efímeras (sin tocar main.py), simula un usuario automotriz,
siembra un embarque con 2 ítems de cotización y ejerce: listado, detalle (auto-crea
pricing + 6 gastos), guardar (header + gastos + override FOB manual), cuadre de
prorrateos, cerrar/reabrir y candado de empresa (mineria → 403). Limpia todo al final.

Corre con:  cd backend && ./venv/bin/python monza_embarques_pricing/tests/test_integration.py
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
from monza_embarques_pricing.router import router  # noqa: E402
from monza_embarques_pricing.models import (  # noqa: E402
    MonzaEmbPricing, MonzaEmbPricingGasto, MonzaEmbPricingItem,
)

MARK = "__TEST_MEP__"

app = FastAPI()
app.include_router(router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, empresa="automotriz")
client = TestClient(app)

app_min = FastAPI()
app_min.include_router(router)
app_min.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2, empresa="mineria")
client_min = TestClient(app_min)

_fails = []
_seed = {"cli_id": None, "cot_id": None, "item1_id": None, "item2_id": None, "item3_id": None,
         "emb_id": None, "ei1_id": None, "ei2_id": None, "ei3_id": None}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def approx(a, b, tol=1.0):
    return abs(float(a) - float(b)) <= tol


def _purge(db):
    """Borra cualquier residuo del MARK en orden seguro de FK (idempotente)."""
    pr_ids = [
        p.id for p in db.query(MonzaEmbPricing)
        .join(mm.MonzaEmbarque, MonzaEmbPricing.embarque_id == mm.MonzaEmbarque.id)
        .filter(mm.MonzaEmbarque.numero.like(MARK + "%")).all()
    ]
    if pr_ids:
        db.query(MonzaEmbPricingItem).filter(MonzaEmbPricingItem.pricing_id.in_(pr_ids)).delete(synchronize_session=False)
        db.query(MonzaEmbPricingGasto).filter(MonzaEmbPricingGasto.pricing_id.in_(pr_ids)).delete(synchronize_session=False)
        db.query(MonzaEmbPricing).filter(MonzaEmbPricing.id.in_(pr_ids)).delete(synchronize_session=False)
    emb_ids = [e.id for e in db.query(mm.MonzaEmbarque).filter(mm.MonzaEmbarque.numero.like(MARK + "%")).all()]
    if emb_ids:
        db.query(mm.MonzaEmbarqueItem).filter(mm.MonzaEmbarqueItem.embarque_id.in_(emb_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaEmbarque).filter(mm.MonzaEmbarque.id.in_(emb_ids)).delete(synchronize_session=False)
    cot_ids = [c.id for c in db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.numero.like(MARK + "%")).all()]
    if cot_ids:
        db.query(mm.MonzaCotizacionItem).filter(mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.flush()
    db.query(mm.MonzaCliente).filter(mm.MonzaCliente.nombre.like(MARK + "%")).delete(synchronize_session=False)


def seed():
    db = SessionLocal()
    try:
        _purge(db)  # idempotente: limpia residuos de corridas previas
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="22.222.222-2")
        db.add(cli); db.flush()
        cot = mm.MonzaCotizacion(
            numero=f"{MARK}-COT", cliente_id=cli.id, estado="vendida",
            total_neto=0, iva_monto=0, total_bruto=0, iva_pct=19, forma_pago="credito",
        )
        db.add(cot); db.flush()
        # item1: costo 100 USD, 10 kg, cant 2 → fob_clp 200.000 @ tc 1000
        it1 = mm.MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion="Pieza A", numero_parte="PA-1",
            cantidad=2, costo=100, moneda="USD", peso_kg=10, estado_linea="en_transito",
        )
        # item2: costo 50 USD, 30 kg, cant 1 → fob_clp 50.000 @ tc 1000
        it2 = mm.MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion="Pieza B", numero_parte="PB-1",
            cantidad=1, costo=50, moneda="USD", peso_kg=30, estado_linea="en_transito",
        )
        # item3: costo 0 → FOB sin dato (origen "auto"); peso 0 para no alterar el cuadre
        it3 = mm.MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion="Pieza C", numero_parte="PC-1",
            cantidad=1, costo=0, moneda="USD", peso_kg=0, estado_linea="en_transito",
        )
        db.add_all([it1, it2, it3]); db.flush()
        emb = mm.MonzaEmbarque(numero=f"{MARK}-EMB", estado="en_transito", forwarder="LATAM Cargo")
        db.add(emb); db.flush()
        ei1 = mm.MonzaEmbarqueItem(embarque_id=emb.id, item_id=it1.id)
        ei2 = mm.MonzaEmbarqueItem(embarque_id=emb.id, item_id=it2.id)
        ei3 = mm.MonzaEmbarqueItem(embarque_id=emb.id, item_id=it3.id)
        db.add_all([ei1, ei2, ei3]); db.flush()
        _seed.update(cli_id=cli.id, cot_id=cot.id, item1_id=it1.id, item2_id=it2.id, item3_id=it3.id,
                     emb_id=emb.id, ei1_id=ei1.id, ei2_id=ei2.id, ei3_id=ei3.id)
        db.commit()
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        _purge(db)
        db.commit()
    finally:
        db.close()


def run():
    emb_id = _seed["emb_id"]

    # 1) Listado: el embarque aparece, aún sin pricing
    r = client.get("/api/monza/embarques-pricing")
    check("GET list 200", r.status_code == 200, r.text)
    fila = next((e for e in r.json() if e["embarque_id"] == emb_id), None)
    check("embarque aparece en listado", fila is not None)
    if fila:
        check("pricing_estado inicial = sin_pricing", fila["pricing_estado"] == "sin_pricing", fila["pricing_estado"])
        check("n_items = 3", fila["n_items"] == 3, fila["n_items"])

    # 2) Detalle: auto-crea pricing + 6 gastos + 2 ítems con FOB default (costo cotización)
    r = client.get(f"/api/monza/embarques-pricing/{emb_id}")
    check("GET detalle 200", r.status_code == 200, r.text)
    d = r.json()
    check("pricing estado borrador", d["pricing"]["estado"] == "borrador", d["pricing"]["estado"])
    check("6 gastos seed", len(d["gastos"]) == 6, len(d["gastos"]))
    check("3 ítems", len(d["items"]) == 3, len(d["items"]))
    it1 = next((x for x in d["items"] if x["item_cotizacion_id"] == _seed["item1_id"]), None)
    check("item1 FOB default = costo cotización (100)", it1 and approx(it1["fob_unit"], 100), it1)
    check("item1 fob_origen = cotizacion", it1 and it1["fob_origen"] == "cotizacion", it1)
    check("item1 peso_unit_kg = 10", it1 and approx(it1["peso_unit_kg"], 10), it1)
    it3 = next((x for x in d["items"] if x["item_cotizacion_id"] == _seed["item3_id"]), None)
    check("item3 (costo 0) fob_origen = auto", it3 and it3["fob_origen"] == "auto", it3)

    # 3) Guardar header + gastos (sin override) → debe cuadrar como el test de servicio
    payload = {
        "tipo_embarque": "normal", "tc_tipo": "manual", "tc_valor": 1000,
        "moneda": "USD", "flete_en_me": False, "shipping_clp": 50000,
        "gastos": [
            {"tipo": "desconsolidacion", "monto_neto": 25000, "iva": 4750, "capitaliza": True},
            {"tipo": "almacenaje", "monto_neto": 0, "iva": 0, "capitaliza": True},
            {"tipo": "agencia", "monto_neto": 0, "iva": 0, "capitaliza": True},
            {"tipo": "arancel", "monto_neto": 0, "iva": 0, "capitaliza": True},
            {"tipo": "otros", "monto_neto": 0, "iva": 0, "capitaliza": True},
            {"tipo": "iva_importacion", "monto_neto": 0, "iva": 0, "capitaliza": False},
        ],
    }
    r = client.put(f"/api/monza/embarques-pricing/{emb_id}", json=payload)
    check("PUT guardar 200", r.status_code == 200, r.text)
    d = r.json()
    check("estado calculado", d["pricing"]["estado"] == "calculado", d["pricing"]["estado"])
    check("Σ shipping = 50.000 (cuadre)", approx(d["totales"]["shipping_clp"], 50000), d["totales"])
    check("Σ gastos = 25.000 (cuadre)", approx(d["totales"]["gastos_clp"], 25000), d["totales"])
    check("Σ costo_total = 325.000", approx(d["totales"]["costo_total_clp"], 325000), d["totales"])
    check("total_capitaliza = 25.000", approx(d["totales_gastos"]["total_capitaliza"], 25000), d["totales_gastos"])
    # arancel debe quedar con IVA 0 aunque se mande (regla iva_exento) — acá vino 0, validamos desconsolidación
    g_des = next((g for g in d["gastos"] if g["tipo"] == "desconsolidacion"), None)
    check("desconsolidación IVA respetado (4.750)", g_des and approx(g_des["iva"], 4750), g_des)

    # 4) Override FOB manual en item2 (de 50 → 80)
    payload2 = {"items": [{"embarque_item_id": _seed["ei2_id"], "fob_unit": 80, "fob_manual": True}]}
    r = client.put(f"/api/monza/embarques-pricing/{emb_id}", json=payload2)
    check("PUT override 200", r.status_code == 200, r.text)
    d = r.json()
    it2 = next((x for x in d["items"] if x["embarque_item_id"] == _seed["ei2_id"]), None)
    check("item2 fob_origen = manual", it2 and it2["fob_origen"] == "manual", it2)
    check("item2 fob_unit = 80", it2 and approx(it2["fob_unit"], 80), it2)

    # 5) Detalle persiste override + estado
    r = client.get(f"/api/monza/embarques-pricing/{emb_id}")
    d = r.json()
    it2 = next((x for x in d["items"] if x["embarque_item_id"] == _seed["ei2_id"]), None)
    check("override persiste tras releer", it2 and it2["fob_origen"] == "manual" and approx(it2["fob_unit"], 80), it2)

    # 6) Listado ahora trae costo total y estado calculado
    r = client.get("/api/monza/embarques-pricing")
    fila = next((e for e in r.json() if e["embarque_id"] == emb_id), None)
    check("listado pricing_estado = calculado", fila and fila["pricing_estado"] == "calculado", fila)
    check("listado costo_total_clp > 0", fila and (fila["costo_total_clp"] or 0) > 0, fila)

    # 6b) Validaciones de entrada (rechazo en origen)
    r = client.put(f"/api/monza/embarques-pricing/{emb_id}", json={"tc_valor": -5})
    check("tc_valor negativo → 422", r.status_code == 422, r.status_code)
    r = client.put(f"/api/monza/embarques-pricing/{emb_id}", json={"gastos": [{"tipo": "otros", "monto_neto": -100}]})
    check("gasto monto_neto negativo → 422", r.status_code == 422, r.status_code)
    r = client.put(f"/api/monza/embarques-pricing/{emb_id}", json={"items": [{"embarque_item_id": _seed["ei1_id"], "fob_unit": -1, "fob_manual": True}]})
    check("fob_unit negativo → 422", r.status_code == 422, r.status_code)
    r = client.put(f"/api/monza/embarques-pricing/{emb_id}", json={"items": [{"embarque_item_id": 99999999, "fob_unit": 10, "fob_manual": True}]})
    check("override de embarque_item ajeno → 400", r.status_code == 400, r.status_code)

    # 7) Cerrar → bloquea edición
    r = client.post(f"/api/monza/embarques-pricing/{emb_id}/cerrar")
    check("POST cerrar 200", r.status_code == 200, r.text)
    check("estado cerrado", r.json()["pricing"]["estado"] == "cerrado", r.json()["pricing"]["estado"])
    r = client.put(f"/api/monza/embarques-pricing/{emb_id}", json={"observaciones": "x"})
    check("PUT tras cerrar → 409", r.status_code == 409, r.text)

    # 8) Reabrir → vuelve a calculado
    r = client.post(f"/api/monza/embarques-pricing/{emb_id}/reabrir")
    check("POST reabrir 200", r.status_code == 200, r.text)
    check("estado calculado tras reabrir", r.json()["pricing"]["estado"] == "calculado", r.json()["pricing"]["estado"])

    # 9) Candado de empresa: mineria → 403
    r = client_min.get("/api/monza/embarques-pricing")
    check("candado: mineria GET list → 403", r.status_code == 403, r.status_code)
    r = client_min.get(f"/api/monza/embarques-pricing/{emb_id}")
    check("candado: mineria GET detalle → 403", r.status_code == 403, r.status_code)

    # 10) 404 embarque inexistente
    r = client.get("/api/monza/embarques-pricing/99999999")
    check("embarque inexistente → 404", r.status_code == 404, r.status_code)


if __name__ == "__main__":
    seed()
    try:
        run()
    finally:
        cleanup()
    print()
    if _fails:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
