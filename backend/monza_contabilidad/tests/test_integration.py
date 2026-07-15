"""Test de integración del módulo Contabilidad MonzaParts contra la BD local.

Monta el router en una app efímera (sin tocar main.py), simula un usuario automotriz,
siembra una venta (cotización + ítem + despacho) y ejerce los flujos: facturación
parcial, doble tope, cobranzas, factoring, borrado seguro, candado de empresa y KPIs.
Limpia todo lo que creó al terminar (deja la BD intacta).

Corre con:  cd backend && ./venv/bin/python monza_contabilidad/tests/test_integration.py
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
from monza_contabilidad.router import router  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContCobranza, MonzaContFactoring, MonzaContFacturaClienteItem,
    MonzaContAdelanto,
)

MARK = "__TEST_MC__"
CURRENT = {"empresa": "automotriz", "id": 1}

app = FastAPI()
app.include_router(router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails = []
_seed = {"cot_id": None, "cli_id": None, "desp_id": None, "item_id": None, "desp_item_id": None, "orphan_id": None,
         "cot_adel_id": None, "item2_id": None, "desp2_id": None, "desp_item2_id": None,
         "cot_ret_id": None, "item_ret_id": None}
_factura_ids = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
        cot = mm.MonzaCotizacion(
            numero=f"{MARK}-COT", cliente_id=cli.id, estado="vendida",
            total_neto=1000000, iva_monto=190000, total_bruto=1190000, iva_pct=19,
            forma_pago="credito", oc_cliente="OC-TEST",
        )
        db.add(cot); db.flush()
        item = mm.MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion="Filtro", numero_parte="FP-1",
            cantidad=10, precio_unitario_clp=100000, subtotal_clp=1000000,
            estado_linea="despachado",
        )
        db.add(item); db.flush()
        desp = mm.MonzaDespacho(
            numero=f"{MARK}-DSP", cotizacion_id=cot.id, estado="despachado",
            numero_guia="G-001", cliente_nombre=cli.nombre,
        )
        db.add(desp); db.flush()
        di = mm.MonzaDespachoItem(despacho_id=desp.id, item_id=item.id, qty_despachada=10)
        db.add(di); db.flush()
        # Segunda venta para el flujo de ADELANTO 50% (aislada de la principal).
        cot2 = mm.MonzaCotizacion(
            numero=f"{MARK}-ADEL", cliente_id=cli.id, estado="vendida",
            total_neto=100000, iva_monto=19000, total_bruto=119000, iva_pct=19,
            forma_pago="50% adelanto", pct_adelanto=50,
        )
        db.add(cot2); db.flush()
        item2 = mm.MonzaCotizacionItem(
            cotizacion_id=cot2.id, descripcion="Bujía", numero_parte="BJ-1",
            cantidad=1, precio_unitario_clp=100000, subtotal_clp=100000, estado_linea="despachado",
        )
        db.add(item2); db.flush()
        desp2 = mm.MonzaDespacho(numero=f"{MARK}-DSP2", cotizacion_id=cot2.id, estado="despachado", numero_guia="G-002")
        db.add(desp2); db.flush()
        di2 = mm.MonzaDespachoItem(despacho_id=desp2.id, item_id=item2.id, qty_despachada=1)
        db.add(di2); db.flush()
        # Tercera venta para RETIRO EN OFICINA (sin guía): SIN despacho.
        cot3 = mm.MonzaCotizacion(
            numero=f"{MARK}-RET", cliente_id=cli.id, estado="vendida",
            total_neto=250000, iva_monto=47500, total_bruto=297500, iva_pct=19, forma_pago="Contado",
        )
        db.add(cot3); db.flush()
        item3 = mm.MonzaCotizacionItem(
            cotizacion_id=cot3.id, descripcion="Pastillas", numero_parte="PF-1",
            cantidad=5, precio_unitario_clp=50000, subtotal_clp=250000, estado_linea="en_bodega",
        )
        db.add(item3); db.flush()
        db.commit()
        _seed.update(cot_id=cot.id, cli_id=cli.id, desp_id=desp.id, item_id=item.id, desp_item_id=di.id,
                     cot_adel_id=cot2.id, item2_id=item2.id, desp2_id=desp2.id, desp_item2_id=di2.id,
                     cot_ret_id=cot3.id, item_ret_id=item3.id)
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        cot_ids = [i for i in (_seed["cot_id"], _seed["cot_adel_id"], _seed["cot_ret_id"]) if i]
        # Facturas de ambas ventas (cascade borra items/cobranzas/factoring) + adelantos
        if cot_ids:
            for f in db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)
            ).all():
                db.delete(f)
            db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.cotizacion_id.in_(cot_ids)
            ).delete(synchronize_session=False)
            db.flush()
        # Datos sembrados (ambas ventas)
        for did in (_seed["desp_item_id"], _seed["desp_item2_id"]):
            if did:
                db.query(mm.MonzaDespachoItem).filter(mm.MonzaDespachoItem.id == did).delete()
        for did in (_seed["desp_id"], _seed["desp2_id"], _seed["orphan_id"]):
            if did:
                db.query(mm.MonzaDespacho).filter(mm.MonzaDespacho.id == did).delete()
        for iid in (_seed["item_id"], _seed["item2_id"], _seed["item_ret_id"]):
            if iid:
                db.query(mm.MonzaCotizacionItem).filter(mm.MonzaCotizacionItem.id == iid).delete()
        for cid in cot_ids:
            db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id == cid).delete()
        if _seed["cli_id"]:
            db.query(mm.MonzaCliente).filter(mm.MonzaCliente.id == _seed["cli_id"]).delete()
        db.commit()
        print("Cleanup OK")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print("Cleanup parcial:", e)
    finally:
        db.close()


def run():
    CURRENT["empresa"] = "automotriz"
    cot_id = _seed["cot_id"]
    desp_id = _seed["desp_id"]
    item_id = _seed["item_id"]
    desp_item_id = _seed["desp_item_id"]

    # ── Ventas ──
    ventas = client.get("/api/monza/contabilidad/ventas").json()
    mine = [v for v in ventas if v["cotizacion_id"] == cot_id]
    check("venta aparece en listado", len(mine) == 1, ventas)
    check("venta total con iva", mine and mine[0]["total_con_iva_clp"] == 1190000, mine)

    # ── Despachos facturables ──
    facturables = client.get(f"/api/monza/contabilidad/ventas/{cot_id}/despachos-facturables").json()
    check("1 guia facturable", len(facturables) == 1, facturables)
    check("facturable = 10", facturables and facturables[0]["facturable"] == 10, facturables)

    # ── Marca guía firmada (opcional) — no debe afectar facturación ──
    rf = client.patch(f"/api/monza/contabilidad/ventas/despachos/{desp_id}/guia-firmada", json={"firmada": True})
    check("marcar guia firmada 200", rf.status_code == 200 and rf.json()["guia_firmada"] is True, rf.text)
    # IDOR: un despacho huérfano (sin venta válida) NO se puede marcar
    dbx = SessionLocal()
    orphan = mm.MonzaDespacho(numero=f"{MARK}-ORPH", cotizacion_id=999999999, estado="despachado", numero_guia="G-ORPH")
    dbx.add(orphan); dbx.commit(); _seed["orphan_id"] = orphan.id; dbx.close()
    rorph = client.patch(f"/api/monza/contabilidad/ventas/despachos/{_seed['orphan_id']}/guia-firmada", json={"firmada": True})
    check("IDOR: despacho sin venta -> 404", rorph.status_code == 404, rorph.text)

    # ── Facturación PARCIAL por ítems (4 de 10) ──
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": cot_id,
        "items": [{"item_cotizacion_id": item_id, "despacho_item_id": desp_item_id, "cantidad": 4}],
        "numero_factura": f"{MARK}-F1", "plazo_dias": 30,
    })
    check("factura parcial 200", r.status_code == 200, r.text)
    f1 = r.json(); _factura_ids.append(f1["id"])
    check("f1 neto 400.000", f1["monto_neto"] == 400000, f1)
    check("f1 iva 76.000", f1["iva"] == 76000, f1)
    check("f1 bruto 476.000", f1["monto_bruto"] == 476000, f1)
    check("f1 guia snapshot", f1["numero_guia"] == "G-001", f1)

    # ── Tope: no facturar más de lo disponible (quedan 6, pedir 7) ──
    rover = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": cot_id,
        "items": [{"item_cotizacion_id": item_id, "despacho_item_id": desp_item_id, "cantidad": 7}],
    })
    check("sobre-facturación 409", rover.status_code == 409, rover.text)

    # ── Facturar el resto por despacho (deriva 6) ──
    r2 = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": cot_id, "despacho_id": desp_id, "numero_factura": f"{MARK}-F2", "plazo_dias": 30,
    })
    check("factura resto 200", r2.status_code == 200, r2.text)
    f2 = r2.json(); _factura_ids.append(f2["id"])
    check("f2 neto 600.000", f2["monto_neto"] == 600000, f2)

    # ── Ya no quedan guías facturables ──
    fac2 = client.get(f"/api/monza/contabilidad/ventas/{cot_id}/despachos-facturables").json()
    check("sin guías facturables tras facturar todo", len(fac2) == 0, fac2)
    r3 = client.post("/api/monza/contabilidad/facturas", json={"cotizacion_id": cot_id, "despacho_id": desp_id})
    check("re-facturar despacho 409", r3.status_code == 409, r3.text)

    # ── Cobranzas sobre f1 (476.000) ──
    rc = client.post(f"/api/monza/contabilidad/facturas/{f1['id']}/cobranzas", json={"monto": 200000})
    check("cobranza parcial 200", rc.status_code == 200 and rc.json()["estado_pago"] == "parcial", rc.text)
    rc2 = client.post(f"/api/monza/contabilidad/facturas/{f1['id']}/cobranzas", json={"monto": 276000})
    check("cobranza completa -> pagada", rc2.status_code == 200 and rc2.json()["estado_pago"] == "pagada", rc2.text)
    check("f1 saldo 0", rc2.json()["saldo"] == 0, rc2.json())
    rover2 = client.post(f"/api/monza/contabilidad/facturas/{f1['id']}/cobranzas", json={"monto": 1000})
    check("sobre-pago 400", rover2.status_code == 400, rover2.text)

    # ── Reversión de cobranza (recalcula saldo/estado) ──
    cob_id = rc2.json()["cobranzas"][-1]["id"]
    rdc = client.delete(f"/api/monza/contabilidad/facturas/{f1['id']}/cobranzas/{cob_id}")
    check("eliminar cobranza 200", rdc.status_code == 200, rdc.text)
    f1_after = next((x for x in client.get("/api/monza/contabilidad/facturas").json()["facturas"] if x["id"] == f1["id"]), None)
    check("saldo recomputado tras revertir cobranza", bool(f1_after) and f1_after["saldo"] > 0 and f1_after["estado_pago"] != "pagada", f1_after)

    # ── Factoring sobre f2 (714.000) ──
    rfac = client.post(f"/api/monza/contabilidad/facturas/{f2['id']}/factoring", json={"monto_adelantado": 500000})
    check("factoring 200 + estado factorizada", rfac.status_code == 200 and rfac.json()["estado_pago"] == "factorizada", rfac.text)
    check("factoring genera cobranza adelanto", any(c["medio"] == "factoring_adelanto" for c in rfac.json()["cobranzas"]), rfac.json())
    rblock = client.post(f"/api/monza/contabilidad/facturas/{f2['id']}/cobranzas", json={"monto": 1000})
    check("cobranza con factoring vigente 409", rblock.status_code == 409, rblock.text)
    rliq = client.post(f"/api/monza/contabilidad/facturas/{f2['id']}/factoring/liquidar")
    check("liquidar factoring -> saldo 0", rliq.status_code == 200 and rliq.json()["saldo"] == 0, rliq.text)

    # ── Borrado seguro ──
    rdel = client.delete(f"/api/monza/contabilidad/facturas/{f2['id']}")
    check("borrar factura con factoring 409", rdel.status_code == 409, rdel.text)
    rdel1 = client.delete(f"/api/monza/contabilidad/facturas/{f1['id']}")
    check("borrar factura con cobranzas 409", rdel1.status_code == 409, rdel1.text)

    # ── KPIs ──
    kpis = client.get("/api/monza/contabilidad/kpis").json()
    check("kpis shape", set(["facturado_clp", "por_cobrar_clp", "en_factoring_clp"]).issubset(kpis), kpis)

    # ── Adelanto 50%: estado, verificación y aplicación automática a la factura ──
    cot_adel = _seed["cot_adel_id"]
    ventas2 = client.get("/api/monza/contabilidad/ventas").json()
    va = next((v for v in ventas2 if v["cotizacion_id"] == cot_adel), None)
    check("venta con adelanto: requiere y por_verificar",
          bool(va) and va.get("requiere_adelanto") and va.get("estado_adelanto") == "por_verificar", va)
    rv = client.post(f"/api/monza/contabilidad/ventas/{cot_adel}/adelanto/verificar",
                     json={"monto": 59500, "fecha_pago": "2026-06-29", "banco": "BancoX"})
    check("verificar adelanto -> verificado", rv.status_code == 200 and rv.json().get("estado_adelanto") == "verificado", rv.text)
    rfa = client.post("/api/monza/contabilidad/facturas",
                      json={"cotizacion_id": cot_adel, "despacho_id": _seed["desp2_id"], "numero_factura": f"{MARK}-FAD"})
    check("factura de venta con adelanto 200", rfa.status_code == 200, rfa.text)
    fa = rfa.json()
    check("factura trae cobranza 'adelanto'", any(c["medio"] == "adelanto" for c in fa["cobranzas"]), fa.get("cobranzas"))
    check("adelanto descuenta saldo (119.000 - 59.500)", fa["saldo"] == fa["monto_bruto"] - 59500, fa)
    rrev = client.post(f"/api/monza/contabilidad/ventas/{cot_adel}/adelanto/verificar", json={"monto": 1})
    check("re-verificar adelanto ya aplicado -> 409", rrev.status_code == 409, rrev.text)
    # Revertir la cobranza de adelanto devuelve monto_aplicado → re-verificar se permite
    adel_cob = next((c for c in fa["cobranzas"] if c["medio"] == "adelanto"), None)
    check("factura adelanto tiene cobranza reversible", adel_cob is not None, fa["cobranzas"])
    if adel_cob:
        rda = client.delete(f"/api/monza/contabilidad/facturas/{fa['id']}/cobranzas/{adel_cob['id']}")
        check("revertir cobranza adelanto 200", rda.status_code == 200, rda.text)
        rrev2 = client.post(f"/api/monza/contabilidad/ventas/{cot_adel}/adelanto/verificar", json={"monto": 59500})
        check("re-verificar tras revertir adelanto -> 200", rrev2.status_code == 200, rrev2.text)

    # ── Retiro en oficina (factura SIN guía, tope por lo vendido) ──
    cot_ret = _seed["cot_ret_id"]
    rret = client.post("/api/monza/contabilidad/facturas",
                       json={"cotizacion_id": cot_ret, "sin_guia": True, "numero_factura": f"{MARK}-FRET"})
    check("retiro sin guía 200", rret.status_code == 200, rret.text)
    fr = rret.json()
    check("retiro neto = 5×50.000", fr.get("monto_neto") == 250000, fr)
    check("retiro sin guía -> numero_guia None", fr.get("numero_guia") is None, fr)
    rret2 = client.post("/api/monza/contabilidad/facturas", json={"cotizacion_id": cot_ret, "sin_guia": True})
    check("re-facturar retiro completo -> 409", rret2.status_code == 409, rret2.text)
    # sin_guia es excluyente: combinarlo con despacho → 400
    rmix = client.post("/api/monza/contabilidad/facturas",
                       json={"cotizacion_id": cot_id, "sin_guia": True, "despacho_id": desp_id})
    check("sin_guia + despacho -> 400 (excluyente)", rmix.status_code == 400, rmix.text)

    # ── CANDADO de empresa: un usuario mineria NO entra ──
    CURRENT["empresa"] = "mineria"
    rg = client.get("/api/monza/contabilidad/ventas")
    check("mineria bloqueada (403)", rg.status_code == 403, rg.text)
    rgk = client.get("/api/monza/contabilidad/kpis")
    check("mineria bloqueada en kpis (403)", rgk.status_code == 403, rgk.text)
    CURRENT["empresa"] = "automotriz"

    # ── AUTH: usuario anónimo (sin token) debe ser rechazado (no 200) ──
    anon_app = FastAPI(); anon_app.include_router(router)
    anon = TestClient(anon_app, raise_server_exceptions=False)
    check("sin token -> rechazado en ventas", anon.get("/api/monza/contabilidad/ventas").status_code in (401, 403))
    check("sin token -> rechazado en kpis", anon.get("/api/monza/contabilidad/kpis").status_code in (401, 403))

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
