"""Test de integración del módulo Embarques Pricing MonzaParts contra la BD local.

Monta el router en apps efímeras (sin tocar main.py), simula un usuario automotriz,
siembra un embarque con 3 ítems de cotización y ejerce: listado, detalle (auto-crea
pricing + 6 gastos), guardar (header + gastos + override FOB manual), cuadre de
prorrateos, cerrar/reabrir y candado de empresa (mineria → 403).

El paso 11 (espejo del paso 12 de Grupo AM) usa 2 embarques extra para el PESO
EDITABLE por ítem: re-prorrateo del flete, reseteo al peso de la cotización, manual
<= 0, independencia FOB↔peso (editar uno no revierte el otro), congelado al cerrar y
fallback por FOB cuando la cotización trae todos los pesos en 0.

El paso 12 usa un embarque extra para el ORIGEN DEL FOB (FOB real del proveedor):
marcado como 'factura' vs corrección 'manual', compatibilidad sin el flag, reseteo a
'cotizacion'/'auto', valor 0 que no bloquea, negativo → 422, la trampa del tri-estado
(editar solo el peso NO revierte el origen del FOB) y congelado/reapertura.

El paso 13 usa un embarque con monedas MEZCLADAS: el aviso defensivo tiene que ser
visible en el detalle (y NO bloquear el costeo).

Limpia todo lo que crea al final (todo va marcado con MARK).

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
         "emb_id": None, "ei1_id": None, "ei2_id": None, "ei3_id": None,
         # Embarque dedicado al peso editable (paso 11): 2 ítems mismo FOB, pesos 1 y 3.
         "embP_id": None, "eiA_id": None, "eiB_id": None,
         # Embarque con TODOS los pesos de cotización en 0 (paso 11f: fallback por FOB).
         "embZ_id": None, "eiZA_id": None, "eiZB_id": None,
         # Embarque dedicado al ORIGEN DEL FOB (paso 12): F1/F2 con costo 100, F3 costo 0.
         "embF_id": None, "eiF1_id": None, "eiF2_id": None, "eiF3_id": None,
         # Embarque con monedas MEZCLADAS USD+EUR (paso 13: aviso defensivo).
         "embM_id": None}


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

        # ── Embarque P: peso editable (paso 11). A y B con el MISMO FOB (100) y
        # pesos 1.0 / 3.0 kg → el shipping se reparte SOLO por peso (1:3).
        cotP = mm.MonzaCotizacion(
            numero=f"{MARK}-COTP", cliente_id=cli.id, estado="vendida",
            total_neto=0, iva_monto=0, total_bruto=0, iva_pct=19, forma_pago="credito",
        )
        db.add(cotP); db.flush()
        itA = mm.MonzaCotizacionItem(cotizacion_id=cotP.id, descripcion="A", numero_parte="PA-1",
                                     cantidad=1, costo=100, moneda="USD", peso_kg=1.0)
        itB = mm.MonzaCotizacionItem(cotizacion_id=cotP.id, descripcion="B", numero_parte="PB-1",
                                     cantidad=1, costo=100, moneda="USD", peso_kg=3.0)
        db.add_all([itA, itB]); db.flush()
        embP = mm.MonzaEmbarque(numero=f"{MARK}-EMB-P", estado="en_transito", forwarder="LATAM Cargo")
        db.add(embP); db.flush()
        eiA = mm.MonzaEmbarqueItem(embarque_id=embP.id, item_id=itA.id)
        eiB = mm.MonzaEmbarqueItem(embarque_id=embP.id, item_id=itB.id)
        db.add_all([eiA, eiB]); db.flush()

        # ── Embarque Z: TODOS los pesos de cotización en 0 (paso 11f) → el flete cae
        # al fallback por FOB; un override de peso > 0 debe dominar el prorrateo.
        cotZ = mm.MonzaCotizacion(
            numero=f"{MARK}-COTZ", cliente_id=cli.id, estado="vendida",
            total_neto=0, iva_monto=0, total_bruto=0, iva_pct=19, forma_pago="credito",
        )
        db.add(cotZ); db.flush()
        izA = mm.MonzaCotizacionItem(cotizacion_id=cotZ.id, descripcion="ZA", numero_parte="ZA-1",
                                     cantidad=1, costo=100, moneda="USD", peso_kg=0)
        izB = mm.MonzaCotizacionItem(cotizacion_id=cotZ.id, descripcion="ZB", numero_parte="ZB-1",
                                     cantidad=1, costo=100, moneda="USD", peso_kg=0)
        db.add_all([izA, izB]); db.flush()
        embZ = mm.MonzaEmbarque(numero=f"{MARK}-EMB-Z", estado="en_transito", forwarder="LATAM Cargo")
        db.add(embZ); db.flush()
        eiZA = mm.MonzaEmbarqueItem(embarque_id=embZ.id, item_id=izA.id)
        eiZB = mm.MonzaEmbarqueItem(embarque_id=embZ.id, item_id=izB.id)
        db.add_all([eiZA, eiZB]); db.flush()

        # ── Embarque F: ORIGEN DEL FOB (paso 12). F1/F2 con costo estimado 100 y peso 1;
        # F3 con costo 0 (para el reseteo a 'auto': la cotización no trae precio).
        cotF = mm.MonzaCotizacion(
            numero=f"{MARK}-COTF", cliente_id=cli.id, estado="vendida",
            total_neto=0, iva_monto=0, total_bruto=0, iva_pct=19, forma_pago="credito",
        )
        db.add(cotF); db.flush()
        itF1 = mm.MonzaCotizacionItem(cotizacion_id=cotF.id, descripcion="F1", numero_parte="F1-1",
                                      cantidad=1, costo=100, moneda="USD", peso_kg=1.0)
        itF2 = mm.MonzaCotizacionItem(cotizacion_id=cotF.id, descripcion="F2", numero_parte="F2-1",
                                      cantidad=1, costo=100, moneda="USD", peso_kg=1.0)
        itF3 = mm.MonzaCotizacionItem(cotizacion_id=cotF.id, descripcion="F3", numero_parte="F3-1",
                                      cantidad=1, costo=0, moneda="USD", peso_kg=0)
        db.add_all([itF1, itF2, itF3]); db.flush()
        embF = mm.MonzaEmbarque(numero=f"{MARK}-EMB-F", estado="en_transito", forwarder="LATAM Cargo")
        db.add(embF); db.flush()
        eiF1 = mm.MonzaEmbarqueItem(embarque_id=embF.id, item_id=itF1.id)
        eiF2 = mm.MonzaEmbarqueItem(embarque_id=embF.id, item_id=itF2.id)
        eiF3 = mm.MonzaEmbarqueItem(embarque_id=embF.id, item_id=itF3.id)
        db.add_all([eiF1, eiF2, eiF3]); db.flush()

        # ── Embarque M: monedas MEZCLADAS (paso 13). El 1er ítem manda la moneda del
        # pricing (USD); el 2º viene en EUR → el aviso defensivo debe dispararse.
        cotM = mm.MonzaCotizacion(
            numero=f"{MARK}-COTM", cliente_id=cli.id, estado="vendida",
            total_neto=0, iva_monto=0, total_bruto=0, iva_pct=19, forma_pago="credito",
        )
        db.add(cotM); db.flush()
        itM1 = mm.MonzaCotizacionItem(cotizacion_id=cotM.id, descripcion="M-USD", numero_parte="MU-1",
                                      cantidad=1, costo=100, moneda="USD", peso_kg=1.0)
        itM2 = mm.MonzaCotizacionItem(cotizacion_id=cotM.id, descripcion="M-EUR", numero_parte="ME-1",
                                      cantidad=1, costo=100, moneda="EUR", peso_kg=1.0)
        db.add_all([itM1, itM2]); db.flush()
        embM = mm.MonzaEmbarque(numero=f"{MARK}-EMB-M", estado="en_transito", forwarder="LATAM Cargo")
        db.add(embM); db.flush()
        db.add_all([mm.MonzaEmbarqueItem(embarque_id=embM.id, item_id=itM1.id),
                    mm.MonzaEmbarqueItem(embarque_id=embM.id, item_id=itM2.id)])
        db.flush()

        _seed.update(embP_id=embP.id, eiA_id=eiA.id, eiB_id=eiB.id,
                     embZ_id=embZ.id, eiZA_id=eiZA.id, eiZB_id=eiZB.id,
                     embF_id=embF.id, eiF1_id=eiF1.id, eiF2_id=eiF2.id, eiF3_id=eiF3.id,
                     embM_id=embM.id)
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

    # ── 11) PESO EDITABLE POR ÍTEM (espejo del paso 12 de Grupo AM) ─────────────
    # El peso gobierna el prorrateo del flete: si vino mal en la cotización,
    # Contabilidad lo corrige y el flete se re-prorratea sin que se pierda plata.
    embP = _seed["embP_id"]
    eiA, eiB = _seed["eiA_id"], _seed["eiB_id"]
    base_url = f"/api/monza/embarques-pricing/{embP}"

    # Base: TC 1000, shipping 40.000 CLP, sin gastos → shipping 1:3 = 10.000 / 30.000
    r = client.put(base_url, json={"tc_valor": 1000, "flete_en_me": False, "shipping_clp": 40000})
    check("11) PUT base embarque-peso 200", r.status_code == 200, r.text)
    d = r.json()
    A = next((x for x in d["items"] if x["embarque_item_id"] == eiA), None)
    B = next((x for x in d["items"] if x["embarque_item_id"] == eiB), None)
    check("11) shipping por peso 1:3 = 10.000 / 30.000",
          A and B and approx(A["shipping_clp"], 10000) and approx(B["shipping_clp"], 30000),
          (A and A["shipping_clp"], B and B["shipping_clp"]))
    check("11) peso_origen inicial = auto y peso = 1.0 (de la cotización)",
          A and A["peso_origen"] == "auto" and approx(A["peso_unit_kg"], 1.0), A)
    check("11) peso_default expuesto = 1.0", A and approx(A["peso_default"], 1.0), A)

    # 11a) Override peso A → 3.0: ahora 3:3 = 20.000 / 20.000; Σ shipping intacta (40.000)
    r = client.put(base_url, json={"items": [{"embarque_item_id": eiA, "peso_unit_kg": 3.0, "peso_manual": True}]})
    check("11a) PUT override peso 200", r.status_code == 200, r.text)
    d = r.json()
    A = next((x for x in d["items"] if x["embarque_item_id"] == eiA), None)
    B = next((x for x in d["items"] if x["embarque_item_id"] == eiB), None)
    check("11a) peso A = 3.0 y peso_origen = manual",
          A and approx(A["peso_unit_kg"], 3.0) and A["peso_origen"] == "manual", A)
    check("11a) el flete se re-prorratea 20.000 / 20.000",
          A and B and approx(A["shipping_clp"], 20000) and approx(B["shipping_clp"], 20000),
          (A and A["shipping_clp"], B and B["shipping_clp"]))
    check("11a) Σ shipping sigue intacta (40.000)",
          approx(d["totales"]["shipping_clp"], 40000), d["totales"])
    check("11a) peso_default sigue siendo el de la cotización (1.0)",
          A and approx(A["peso_default"], 1.0), A)

    # 11b) Quitar el override (peso_manual=False) → vuelve al peso de la cotización (1.0)
    r = client.put(base_url, json={"items": [{"embarque_item_id": eiA, "peso_manual": False}]})
    d = r.json()
    A = next((x for x in d["items"] if x["embarque_item_id"] == eiA), None)
    check("11b) quitar override vuelve al peso de la cotización (1.0, auto)",
          A and approx(A["peso_unit_kg"], 1.0) and A["peso_origen"] == "auto", A)
    check("11b) shipping vuelve a 10.000", A and approx(A["shipping_clp"], 10000), A)

    # 11c) Manual <= 0 → se ignora (un peso 0 no es real) y cae al de la cotización
    r = client.put(base_url, json={"items": [{"embarque_item_id": eiA, "peso_unit_kg": 0, "peso_manual": True}]})
    d = r.json()
    A = next((x for x in d["items"] if x["embarque_item_id"] == eiA), None)
    check("11c) peso manual = 0 se ignora (cae al peso de la cotización)",
          A and approx(A["peso_unit_kg"], 1.0) and A["peso_origen"] == "auto", A)
    r = client.put(base_url, json={"items": [{"embarque_item_id": eiA, "peso_unit_kg": -3, "peso_manual": True}]})
    check("11c) peso manual negativo → 422 (rechazo en la API)", r.status_code == 422, r.status_code)

    # 11d) LA TRAMPA: FOB manual + editar SOLO el peso NO revierte el FOB (tri-estado)
    r = client.put(base_url, json={"items": [{"embarque_item_id": eiA, "fob_unit": 555, "fob_manual": True}]})
    check("11d) PUT FOB manual 200", r.status_code == 200, r.text)
    r = client.put(base_url, json={"items": [{"embarque_item_id": eiA, "peso_unit_kg": 2.0, "peso_manual": True}]})
    d = r.json()
    A = next((x for x in d["items"] if x["embarque_item_id"] == eiA), None)
    check("11d) editar solo el peso NO revierte el FOB manual (555)",
          A and approx(A["fob_unit"], 555) and A["fob_origen"] == "manual", A)
    check("11d) el peso sí queda en 2.0 manual",
          A and approx(A["peso_unit_kg"], 2.0) and A["peso_origen"] == "manual", A)
    # …y el espejo: editar solo el FOB no revierte el peso manual
    r = client.put(base_url, json={"items": [{"embarque_item_id": eiA, "fob_unit": 777, "fob_manual": True}]})
    d = r.json()
    A = next((x for x in d["items"] if x["embarque_item_id"] == eiA), None)
    check("11d) editar solo el FOB NO revierte el peso manual (2.0)",
          A and approx(A["peso_unit_kg"], 2.0) and A["peso_origen"] == "manual", A)

    # 11e) Cerrado congela el peso; peso_default == el valor congelado
    r = client.post(f"{base_url}/cerrar")
    check("11e) POST cerrar 200", r.status_code == 200, r.text)
    r = client.get(base_url)
    d = r.json()
    A = next((x for x in d["items"] if x["embarque_item_id"] == eiA), None)
    check("11e) el snapshot congela el peso manual (2.0)",
          A and A["peso_origen"] == "manual" and approx(A["peso_unit_kg"], 2.0), A)
    check("11e) cerrado: peso_default == valor congelado",
          A and approx(A["peso_default"], A["peso_unit_kg"]), A)
    r = client.post(f"{base_url}/reabrir")
    d = r.json()
    A = next((x for x in d["items"] if x["embarque_item_id"] == eiA), None)
    check("11e) reabrir mantiene el peso manual (2.0)",
          A and A["peso_origen"] == "manual" and approx(A["peso_unit_kg"], 2.0), A)

    # 11f) Pesos de cotización TODOS en 0 → fallback por FOB; un override > 0 domina
    embZ, eiZA = _seed["embZ_id"], _seed["eiZA_id"]
    z_url = f"/api/monza/embarques-pricing/{embZ}"
    r = client.put(z_url, json={"tc_valor": 1000, "flete_en_me": False, "shipping_clp": 40000})
    check("11f) PUT base embarque pesos-0 200", r.status_code == 200, r.text)
    d = r.json()
    check("11f) sin pesos → shipping por FOB (iguales): 20.000 / 20.000",
          all(approx(x["shipping_clp"], 20000) for x in d["items"]), [x["shipping_clp"] for x in d["items"]])
    check("11f) Σ shipping = 40.000 (nada se pierde)", approx(d["totales"]["shipping_clp"], 40000), d["totales"])
    r = client.put(z_url, json={"items": [{"embarque_item_id": eiZA, "peso_unit_kg": 5, "peso_manual": True}]})
    d = r.json()
    za = next((x for x in d["items"] if x["embarque_item_id"] == eiZA), None)
    check("11f) un override de peso > 0 se lleva TODO el flete (40.000)",
          za and approx(za["shipping_clp"], 40000), za)
    check("11f) Σ shipping sigue en 40.000", approx(d["totales"]["shipping_clp"], 40000), d["totales"])

    # ── 12) ORIGEN DEL FOB: factura real del proveedor vs corrección a mano ─────
    # Monza no tiene tabla de facturas de proveedor: el FOB real se carga acá, al
    # costear, y se MARCA para saber si el costo landed usa el precio real o el estimado.
    embF = _seed["embF_id"]
    f1, f3 = _seed["eiF1_id"], _seed["eiF3_id"]
    f_url = f"/api/monza/embarques-pricing/{embF}"

    def _F(dd, eiid):
        return next((x for x in dd["items"] if x["embarque_item_id"] == eiid), None)

    # Base: TC 1000, sin flete ni gastos → FOB CLP = FOB unit × 1.000 (aislado y legible)
    r = client.put(f_url, json={"tc_valor": 1000, "flete_en_me": False, "shipping_clp": 0})
    check("12) PUT base embarque-FOB 200", r.status_code == 200, r.text)
    d = r.json()
    check("12) sin override: fob_origen = cotizacion y FOB = 100 (estimado)",
          _F(d, f1) and _F(d, f1)["fob_origen"] == "cotizacion" and approx(_F(d, f1)["fob_unit"], 100), _F(d, f1))
    check("12) el ítem sin costo en la cotización queda 'auto' (sin dato)",
          _F(d, f3) and _F(d, f3)["fob_origen"] == "auto", _F(d, f3))

    # 12a) FOB de la FACTURA REAL → fob_origen 'factura' y el costo landed lo usa
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f1, "fob_unit": 120, "fob_manual": True, "fob_es_factura": True}]})
    check("12a) PUT FOB de factura 200", r.status_code == 200, r.text)
    d = r.json()
    check("12a) fob_origen = factura", _F(d, f1) and _F(d, f1)["fob_origen"] == "factura", _F(d, f1))
    check("12a) el costo landed usa el FOB de la factura (120 × 1.000 = 120.000)",
          _F(d, f1) and approx(_F(d, f1)["fob_unit"], 120) and approx(_F(d, f1)["fob_clp"], 120000), _F(d, f1))
    r = client.get(f_url)
    check("12a) 'factura' persiste tras releer",
          _F(r.json(), f1) and _F(r.json(), f1)["fob_origen"] == "factura", _F(r.json(), f1))

    # 12b) El mismo ítem marcado como CORRECCIÓN A MANO → 'manual' (sin regresión)
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f1, "fob_unit": 130, "fob_manual": True, "fob_es_factura": False}]})
    d = r.json()
    check("12b) fob_es_factura=False → fob_origen = manual",
          _F(d, f1) and _F(d, f1)["fob_origen"] == "manual" and approx(_F(d, f1)["fob_unit"], 130), _F(d, f1))
    check("12b) el costo landed usa el FOB corregido (130.000)",
          _F(d, f1) and approx(_F(d, f1)["fob_clp"], 130000), _F(d, f1))

    # 12c) Compatibilidad: sin el flag nuevo el comportamiento es el de hoy ('manual')
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f1, "fob_unit": 140, "fob_manual": True}]})
    d = r.json()
    check("12c) sin fob_es_factura → 'manual' (compatible con el payload viejo)",
          _F(d, f1) and _F(d, f1)["fob_origen"] == "manual" and approx(_F(d, f1)["fob_unit"], 140), _F(d, f1))

    # 12d) Quitar el override vuelve al dato de la cotización: 'cotizacion' si trae costo,
    #      'auto' si no. Se prueba desde un override marcado como 'factura' (el reseteo
    #      tiene que soltar también ese origen, no solo 'manual').
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f1, "fob_unit": 150, "fob_manual": True, "fob_es_factura": True}]})
    check("12d) preparado: F1 en 'factura'", _F(r.json(), f1)["fob_origen"] == "factura", r.text)
    r = client.put(f_url, json={"items": [{"embarque_item_id": f1, "fob_manual": False}]})
    d = r.json()
    check("12d) quitar el override 'factura' vuelve a 'cotizacion' (100)",
          _F(d, f1) and _F(d, f1)["fob_origen"] == "cotizacion" and approx(_F(d, f1)["fob_unit"], 100), _F(d, f1))
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f3, "fob_unit": 70, "fob_manual": True, "fob_es_factura": True}]})
    check("12d) F3 (cotización sin costo) acepta FOB de factura",
          _F(r.json(), f3)["fob_origen"] == "factura", r.text)
    r = client.put(f_url, json={"items": [{"embarque_item_id": f3, "fob_manual": False}]})
    check("12d) quitarlo en un ítem sin costo de cotización vuelve a 'auto'",
          _F(r.json(), f3)["fob_origen"] == "auto", _F(r.json(), f3))

    # 12e) Valor 0 NO bloquea el FOB de la cotización (aunque venga marcado como factura)
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f1, "fob_unit": 0, "fob_manual": True, "fob_es_factura": True}]})
    d = r.json()
    check("12e) FOB 0 marcado 'factura' no bloquea: cae al estimado de la cotización (100)",
          _F(d, f1) and _F(d, f1)["fob_origen"] == "cotizacion" and approx(_F(d, f1)["fob_unit"], 100), _F(d, f1))

    # 12f) Negativo → 422 (rechazo en la API, igual que hoy)
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f1, "fob_unit": -1, "fob_manual": True, "fob_es_factura": True}]})
    check("12f) FOB negativo marcado 'factura' → 422", r.status_code == 422, r.status_code)

    # 12g) LA TRAMPA DEL TRI-ESTADO: editar SOLO el peso no revierte el ORIGEN del FOB
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f1, "fob_unit": 160, "fob_manual": True, "fob_es_factura": True}]})
    check("12g) preparado: F1 en 'factura' con 160", _F(r.json(), f1)["fob_origen"] == "factura", r.text)
    r = client.put(f_url, json={"items": [{"embarque_item_id": f1, "peso_unit_kg": 4.0, "peso_manual": True}]})
    d = r.json()
    check("12g) editar solo el peso NO degrada 'factura' a 'manual' (y conserva 160)",
          _F(d, f1) and _F(d, f1)["fob_origen"] == "factura" and approx(_F(d, f1)["fob_unit"], 160), _F(d, f1))
    check("12g) el peso sí queda manual en 4.0",
          _F(d, f1) and _F(d, f1)["peso_origen"] == "manual" and approx(_F(d, f1)["peso_unit_kg"], 4.0), _F(d, f1))
    # …y el espejo: reeditar el FOB (marcado factura) no revierte el peso manual
    r = client.put(f_url, json={"items": [
        {"embarque_item_id": f1, "fob_unit": 170, "fob_manual": True, "fob_es_factura": True}]})
    d = r.json()
    check("12g) editar solo el FOB no revierte el peso manual (4.0)",
          _F(d, f1) and approx(_F(d, f1)["peso_unit_kg"], 4.0) and _F(d, f1)["peso_origen"] == "manual", _F(d, f1))

    # 12h) El cierre CONGELA el origen del FOB y reabrir lo conserva
    r = client.post(f"{f_url}/cerrar")
    check("12h) POST cerrar 200", r.status_code == 200, r.text)
    r = client.get(f_url)
    d = r.json()
    check("12h) el snapshot congela fob_origen = factura (170)",
          _F(d, f1) and _F(d, f1)["fob_origen"] == "factura" and approx(_F(d, f1)["fob_unit"], 170), _F(d, f1))
    r = client.post(f"{f_url}/reabrir")
    check("12h) reabrir conserva 'factura'",
          _F(r.json(), f1) and _F(r.json(), f1)["fob_origen"] == "factura", _F(r.json(), f1))

    # ── 13) Aviso defensivo de MONEDA MEZCLADA (no bloquea el costeo) ────────────
    check("13) embarque de una sola moneda: sin advertencias",
          client.get(f_url).json().get("advertencias") == [], client.get(f_url).json().get("advertencias"))
    embM = _seed["embM_id"]
    m_url = f"/api/monza/embarques-pricing/{embM}"
    r = client.get(m_url)
    check("13) GET detalle embarque mezclado 200", r.status_code == 200, r.text)
    advs = r.json().get("advertencias") or []
    # Este embarque dispara DOS avisos y los dos son correctos: (1) el costeo va en USD y
    # uno de sus ítems está en EUR, y (2) los ítems no se ponen de acuerdo entre ellos.
    # Antes solo existía el (2), que es justo el caso que el dueño dice que no pasa.
    check("13) monedas mezcladas → aparecen los avisos", len(advs) >= 1, advs)
    check("13) los avisos nombran las DOS monedas y la que usa el costo",
          any("EUR" in a and "USD" in a for a in advs), advs)
    check("13) avisa que el costeo va en OTRA moneda que el ítem (el caso que SÍ pasa)",
          any("los ítems de este embarque están en" in a for a in advs), advs)
    check("13) y avisa que los ítems traen más de una moneda entre ellos",
          any("más de una moneda" in a for a in advs), advs)
    # …y NO bloquea: el embarque ya llegó, hay que poder costearlo igual
    r = client.put(m_url, json={"tc_valor": 1000, "flete_en_me": False, "shipping_clp": 10000})
    check("13) el aviso NO bloquea el guardado (200)", r.status_code == 200, r.text)
    check("13) el aviso sigue visible después de guardar",
          len(r.json().get("advertencias") or []) >= 1, r.json().get("advertencias"))
    r = client.post(f"{m_url}/cerrar")
    check("13) el aviso NO bloquea el cierre (200)", r.status_code == 200, r.text)
    check("13) el aviso sigue visible con el pricing cerrado",
          len(r.json().get("advertencias") or []) >= 1, r.json().get("advertencias"))


def test_monza_embarques_pricing_integration():
    """Wrapper para pytest (espejo de embarques_pricing/tests/test_integration.py:403 de GA):
    sin él la suite era INVISIBLE al gate rutinario 'pytest verde'."""
    seed()
    try:
        run()
    finally:
        cleanup()
    assert not _fails, f"fallas: {_fails}"


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
