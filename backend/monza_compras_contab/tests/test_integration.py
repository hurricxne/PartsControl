"""Test de integración del módulo Compras/CxP MonzaParts contra la BD local.

Monta el router en apps efímeras (sin tocar main.py), simula usuarios automotriz y
mineria, y ejerce: crear compra contado (auto-egreso) / crédito, plazo 0 (vence = fecha),
pagos parcial/total/sobre-pago, revertir pago, PATCH fecha banco, candado de egreso
conciliado (409), pago consolidado, unicidad de documento y re-registro tras anular,
USD con TC, validaciones 422, overlay de costos de embarque (reflejo automático +
registrar como compra + dedup 409) y candado de empresa (403).
Limpia todo lo que creó (deja la BD intacta).

Corre con:  cd backend && ./venv/bin/python monza_compras_contab/tests/test_integration.py
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
from monza_embarques_pricing.models import MonzaEmbPricing, MonzaEmbPricingGasto  # noqa: E402
from monza_compras_contab.router import router  # noqa: E402
from monza_compras_contab.models import (  # noqa: E402
    MonzaContCompra, MonzaContEgreso, MonzaContEgresoDetalle,
)

MARK = "__TEST_MCC__"

from fastapi import Depends  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from database import get_db  # noqa: E402

app = FastAPI()
app.include_router(router)
# Auth REALISTA: además de devolver el usuario, hace una lectura en la MISMA sesión del
# request, igual que auth.get_current_user en producción. Ese SELECT abre el read view de
# MySQL (REPEATABLE READ) ANTES de cualquier with_for_update(), que es la condición real
# bajo la que corren los endpoints. Con un lambda "seco", el lock terminaba siendo la
# PRIMERA sentencia y el snapshot nacía DESPUÉS del lock: las carreras de plata quedaban
# invisibles para los tests.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

app_min = FastAPI()
app_min.include_router(router)
app_min.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2, empresa="mineria")
client_min = TestClient(app_min)

_fails = []
_seed = {"emb_id": None, "pricing_id": None, "gasto_id": None}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _purge(db):
    """Borra cualquier residuo del MARK en orden seguro de FK (idempotente)."""
    # Compras del MARK (por acreedor o documento) y sus egresos.
    compras = db.query(MonzaContCompra).filter(
        MonzaContCompra.acreedor.like(MARK + "%")).all()
    compra_ids = [c.id for c in compras]
    if compra_ids:
        egreso_ids = [d.egreso_id for d in db.query(MonzaContEgresoDetalle)
                      .filter(MonzaContEgresoDetalle.compra_id.in_(compra_ids)).all()]
        if egreso_ids:
            db.query(MonzaContEgresoDetalle).filter(
                MonzaContEgresoDetalle.egreso_id.in_(egreso_ids)).delete(synchronize_session=False)
            db.query(MonzaContEgreso).filter(
                MonzaContEgreso.id.in_(egreso_ids)).delete(synchronize_session=False)
        db.query(MonzaContCompra).filter(
            MonzaContCompra.id.in_(compra_ids)).delete(synchronize_session=False)
    # Egresos consolidados huérfanos del MARK (beneficiario).
    orfanos = [e.id for e in db.query(MonzaContEgreso)
               .filter(MonzaContEgreso.beneficiario.like(MARK + "%")).all()]
    if orfanos:
        db.query(MonzaContEgresoDetalle).filter(
            MonzaContEgresoDetalle.egreso_id.in_(orfanos)).delete(synchronize_session=False)
        db.query(MonzaContEgreso).filter(MonzaContEgreso.id.in_(orfanos)).delete(synchronize_session=False)
    # Embarque de prueba + pricing + gastos + ítems.
    embs = db.query(mm.MonzaEmbarque).filter(mm.MonzaEmbarque.numero.like(MARK + "%")).all()
    for e in embs:
        pr = db.query(MonzaEmbPricing).filter(MonzaEmbPricing.embarque_id == e.id).first()
        if pr:
            db.query(MonzaEmbPricingGasto).filter(MonzaEmbPricingGasto.pricing_id == pr.id).delete(synchronize_session=False)
            db.delete(pr)
        db.query(mm.MonzaEmbarqueItem).filter(mm.MonzaEmbarqueItem.embarque_id == e.id).delete(synchronize_session=False)
        db.delete(e)


def seed():
    """Siembra un embarque con pricing y 1 gasto local (para el overlay)."""
    db = SessionLocal()
    try:
        _purge(db)
        db.commit()
        emb = mm.MonzaEmbarque(numero=f"{MARK}-EMB", estado="en_transito", forwarder="DHL")
        db.add(emb); db.flush()
        pr = MonzaEmbPricing(embarque_id=emb.id, tipo_embarque="courier", tc_valor=950,
                             moneda="USD", estado="borrador")
        db.add(pr); db.flush()
        g = MonzaEmbPricingGasto(pricing_id=pr.id, tipo="agencia", glosa="Agencia de Aduana",
                                 monto_neto=80000, iva=15200, capitaliza=True,
                                 nro_factura="F-778", orden=3)
        db.add(g); db.flush()
        _seed.update(emb_id=emb.id, pricing_id=pr.id, gasto_id=g.id)
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


def _crear(**kw):
    body = {"tipo_gasto": "gasto_operacional", "acreedor": f"{MARK} Proveedor",
            "monto_neto": 100000, "condicion_pago": "credito", **kw}
    return client.post("/api/monza/compras-contab", json=body)


def run():
    # 1) Catálogos: plan de cuentas importado + tipos de gasto
    r = client.get("/api/monza/compras-contab/catalogos")
    check("catalogos 200", r.status_code == 200, r.text)
    cat = r.json()
    check("plan de cuentas cargado (>0 imputables)", len(cat["plan_cuentas"]) > 0, len(cat.get("plan_cuentas", [])))
    check("4 tipos de gasto", len(cat["tipos_gasto"]) == 4)
    check("cuenta default MANUAL|cogs asignada", cat["cuenta_default_por_tipo"].get("MANUAL|cogs") is not None, cat["cuenta_default_por_tipo"])

    # 2) CONTADO sin pago explícito → egreso automático por el total (pagado)
    r = _crear(numero_documento=f"{MARK}-CONT", proveedor_rut="76.000.111-2",
               condicion_pago="contado", monto_neto=100000)
    check("crear contado 200", r.status_code == 200, r.text)
    c = r.json()
    check("contado queda pagado", c["estado_pago"] == "pagado", c["estado_pago"])
    check("contado iva 19%", c["iva"] == 19000.0, c["iva"])
    check("contado 1 pago auto", len(c["pagos"]) == 1, c["pagos"])
    check("cuenta default imputada", c["cuenta_codigo"] is not None, c)

    # 3) CRÉDITO 30 días → pendiente; pagos parcial → total; sobre-pago 400
    # Fechas DINÁMICAS (hoy + 30): con fechas fijas el test se volvía 'vencido' con el
    # paso del tiempo (el estado ahora se calcula en vivo al servir).
    from datetime import date as _date, timedelta as _td
    hoy = _date.today()
    r = _crear(numero_documento=f"{MARK}-CRED", proveedor_rut="76.000.111-2",
               tipo_gasto="cogs", fecha=hoy.isoformat(), plazo_dias=30, monto_neto=200000)
    check("crear crédito 200", r.status_code == 200, r.text)
    cc = r.json()
    check("crédito pendiente", cc["estado_pago"] == "pendiente", cc["estado_pago"])
    check("venc = fecha+30", cc["fecha_vencimiento"] == (hoy + _td(days=30)).isoformat(), cc["fecha_vencimiento"])
    # plazo 0 días también genera vencimiento (= fecha); antes `elif payload.plazo_dias`
    # (falsy) lo dejaba sin fecha de vencimiento
    r = _crear(numero_documento=f"{MARK}-P0", proveedor_rut="76.000.111-2",
               plazo_dias=0, fecha=hoy.isoformat(), monto_neto=1000)
    check("plazo 0 → vence = fecha", r.status_code == 200 and r.json()["fecha_vencimiento"] == hoy.isoformat(),
          r.json().get("fecha_vencimiento"))
    total_cred = cc["monto_total_clp"]
    r = client.post(f"/api/monza/compras-contab/{cc['id']}/pagos", json={"monto_clp": 100000, "fecha_mov_bancario": "2026-06-10"})
    check("pago parcial", r.status_code == 200 and r.json()["estado_pago"] == "parcial", r.text)
    r = client.post(f"/api/monza/compras-contab/{cc['id']}/pagos", json={"monto_clp": total_cred - 100000})
    check("pago completa → pagado", r.status_code == 200 and r.json()["estado_pago"] == "pagado", r.text)
    r = client.post(f"/api/monza/compras-contab/{cc['id']}/pagos", json={"monto_clp": 100})
    check("sobre-pago 400", r.status_code == 400, r.status_code)

    # 4) PATCH fecha banco + revertir pago (recalcula saldo)
    det = client.get(f"/api/monza/compras-contab/{cc['id']}").json()
    pago_id = det["pagos"][0]["id"]
    r = client.patch(f"/api/monza/compras-contab/{cc['id']}/pagos/{pago_id}", json={"fecha_mov_bancario": "2026-06-20"})
    check("PATCH fecha banco", r.status_code == 200 and r.json()["pagos"][0]["fecha_mov_bancario"] == "2026-06-20", r.text)
    r = client.delete(f"/api/monza/compras-contab/{cc['id']}/pagos/{pago_id}")
    check("DELETE pago (revertir) 200", r.status_code == 200, r.text)
    rev = client.get(f"/api/monza/compras-contab/{cc['id']}").json()
    check("tras revertir: parcial", rev["estado_pago"] == "parcial", rev["estado_pago"])
    check("tras revertir: saldo 100.000", abs(float(rev["saldo_clp"]) - 100000) < 1, rev["saldo_clp"])

    # 4b) Egreso conciliado: la cartola es la fuente de verdad → PATCH rechaza 409
    r = _crear(numero_documento=f"{MARK}-CONC", proveedor_rut="76.000.111-2",
               monto_neto=2000, condicion_pago="contado")
    conc = r.json()
    egreso_conc_id = conc["pagos"][0]["egreso_id"]
    db = SessionLocal()
    try:
        db.query(MonzaContEgreso).filter(MonzaContEgreso.id == egreso_conc_id).update({"conciliado": True})
        db.commit()
    finally:
        db.close()
    r = client.patch(f"/api/monza/compras-contab/egresos/{egreso_conc_id}", json={"referencia_bancaria": "X"})
    check("PATCH egreso conciliado → 409", r.status_code == 409, r.status_code)
    r = client.patch(f"/api/monza/compras-contab/{conc['id']}/pagos/{conc['pagos'][0]['id']}",
                     json={"fecha_mov_bancario": hoy.isoformat()})
    check("PATCH pago conciliado → 409", r.status_code == 409, r.status_code)

    # 5) Unicidad de documento + re-registro tras anular
    base_doc = dict(numero_documento=f"{MARK}-REREG", proveedor_rut="77.111.222-3", monto_neto=1000)
    r = _crear(**base_doc); a_id = r.json()["id"]
    check("dup activa 409", _crear(**base_doc).status_code == 409)
    check("anular A 200", client.post(f"/api/monza/compras-contab/{a_id}/anular", json={"motivo": "err"}).status_code == 200)
    check("re-registro tras anular 200", _crear(**base_doc).status_code == 200)

    # 6) USD con TC
    r = _crear(numero_documento=f"{MARK}-USD", moneda="USD", tc=950, monto_neto=100,
               afecto_iva=False, condicion_pago="contado", pago={"medio": "tarjeta"})
    u = r.json()
    check("USD total_clp = 95.000", r.status_code == 200 and u["monto_total_clp"] == 95000.0, u)
    r = _crear(numero_documento=f"{MARK}-EURX", moneda="EUR", tc=0, monto_neto=100)
    check("ME sin TC → 4xx", r.status_code in (400, 422), r.status_code)

    # 7) Validaciones de entrada (rechazo en origen)
    r = _crear(monto_neto=-5)
    check("neto negativo → 422", r.status_code == 422, r.status_code)
    r = client.post("/api/monza/compras-contab", json={"tipo_gasto": "invalido", "monto_neto": 1})
    check("tipo_gasto inválido → 4xx", r.status_code in (400, 422), r.status_code)

    # 8) Pago consolidado: 1 egreso paga 2 compras
    r1 = _crear(numero_documento=f"{MARK}-CONS1", proveedor_rut="78.222.333-4", monto_neto=10000)
    r2 = _crear(numero_documento=f"{MARK}-CONS2", proveedor_rut="78.222.333-4", monto_neto=20000)
    id1, id2 = r1.json()["id"], r2.json()["id"]
    t1, t2 = r1.json()["monto_total_clp"], r2.json()["monto_total_clp"]
    r = client.post("/api/monza/compras-contab/egresos", json={
        "beneficiario": f"{MARK} Consolidado", "fecha": "2026-06-15",
        "detalles": [{"compra_id": id1, "monto_clp": t1}, {"compra_id": id2, "monto_clp": t2}],
    })
    check("egreso consolidado 200", r.status_code == 200, r.text)
    egr = r.json()
    check("consolidado suma ok", abs(float(egr["monto_total_clp"]) - (t1 + t2)) < 1, egr["monto_total_clp"])
    check("ambas quedan pagadas",
          client.get(f"/api/monza/compras-contab/{id1}").json()["estado_pago"] == "pagado"
          and client.get(f"/api/monza/compras-contab/{id2}").json()["estado_pago"] == "pagado")
    r = client.delete(f"/api/monza/compras-contab/egresos/{egr['id']}")
    check("revertir consolidado 200", r.status_code == 200, r.text)
    check("ambas vuelven a pendiente",
          client.get(f"/api/monza/compras-contab/{id1}").json()["estado_pago"] == "pendiente"
          and client.get(f"/api/monza/compras-contab/{id2}").json()["estado_pago"] == "pendiente")

    # 9) Overlay costos de embarque: el gasto anotado en Embarques Pricing se refleja solo
    r = client.get("/api/monza/compras-contab/costos-embarque")
    check("costos-embarque 200", r.status_code == 200, r.text)
    costos = r.json()["costos"]
    fila = next((x for x in costos if x["id"] == _seed["gasto_id"]), None)
    check("gasto de embarque aparece automáticamente", fila is not None)
    if fila:
        check("gasto trae neto+iva del pricing", fila["monto_neto"] == 80000.0 and fila["iva"] == 15200.0, fila)
        check("gasto aún sin compra asociada", fila["compra_id"] is None, fila)
    # Registrarlo como compra pagable (origen EMBARQUE → cogs, cuenta 1.3.02)
    r = _crear(numero_documento=f"{MARK}-EMBG", origen="EMBARQUE", tipo_gasto="cogs",
               monto_neto=80000, iva=15200, emb_pricing_gasto_id=_seed["gasto_id"],
               embarque_id=_seed["emb_id"], referencia=f"{MARK}-EMB")
    check("registrar gasto de embarque como compra 200", r.status_code == 200, r.text)
    ce = r.json()
    check("compra EMBARQUE con cuenta 1.3.02", ce["cuenta_codigo"] == "1.3.02", ce["cuenta_codigo"])
    r = _crear(numero_documento=f"{MARK}-EMBG2", origen="EMBARQUE", tipo_gasto="cogs",
               monto_neto=80000, emb_pricing_gasto_id=_seed["gasto_id"])
    check("mismo gasto de embarque otra vez → 409", r.status_code == 409, r.status_code)
    fila2 = next((x for x in client.get("/api/monza/compras-contab/costos-embarque").json()["costos"]
                  if x["id"] == _seed["gasto_id"]), None)
    check("overlay marca el gasto como ya registrado", fila2 and fila2["compra_id"] == ce["id"], fila2)
    r = _crear(numero_documento=f"{MARK}-EMBGX", emb_pricing_gasto_id=99999999)
    check("gasto de embarque inexistente → 400", r.status_code == 400, r.status_code)

    # 10) Listado + KPIs + antigüedad
    r = client.get("/api/monza/compras-contab", params={"q": MARK})
    check("listado 200 + filtra por q", r.status_code == 200 and r.json()["total"] >= 4, r.json().get("total"))
    check("listado trae antigüedad", "antiguedad" in r.json())
    r = client.get("/api/monza/compras-contab/kpis")
    k = r.json()
    check("kpis 200 y consistentes", r.status_code == 200 and k["total_comprado_clp"] >= k["pagado_clp"], k)

    # 11) Candado de empresa: mineria → 403 en todo
    for ep in ["", "/kpis", "/catalogos", "/costos-embarque", "/egresos"]:
        r = client_min.get(f"/api/monza/compras-contab{ep}")
        check(f"candado: mineria GET {ep or '/'} → 403", r.status_code == 403, r.status_code)
    r = client_min.post("/api/monza/compras-contab", json={"tipo_gasto": "otros", "monto_neto": 1})
    check("candado: mineria POST → 403", r.status_code == 403, r.status_code)

    # 12) Eliminar compra sin pagos OK / con pagos 409
    r = _crear(numero_documento=f"{MARK}-DEL", monto_neto=500)
    del_id = r.json()["id"]
    check("eliminar sin pagos 200", client.delete(f"/api/monza/compras-contab/{del_id}").status_code == 200)
    r = _crear(numero_documento=f"{MARK}-DEL2", monto_neto=500, condicion_pago="contado")
    check("eliminar con pagos 409", client.delete(f"/api/monza/compras-contab/{r.json()['id']}").status_code == 409)


def test_monza_compras_contab_integration():
    """Wrapper para pytest (espejo de compras_contab/tests/test_integration.py:219 de GA):
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
