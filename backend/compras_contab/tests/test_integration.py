"""Test de integración del módulo Compras / Cuentas por Pagar contra la DB local.

Monta el router en una app efímera (sin tocar main.py), sobreescribe la auth para
simular usuarios de distintas empresas, ejerce todos los flujos y LIMPIA todo lo
que creó al terminar (deja la DB intacta).

Corre con:  ./venv/bin/python compras_contab/tests/test_integration.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from compras_contab.router import router  # noqa: E402
from compras_contab.models import ContCompra, ContEgreso, ContEgresoDetalle  # noqa: E402

MARK = "__TEST_CC__"

# Usuario actual mutable (para cambiar de empresa en el test de scope).
CURRENT = {"empresa": "mineria", "id": None}

from fastapi import Depends  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from database import get_db  # noqa: E402

app = FastAPI()
app.include_router(router, prefix="/api")
# Auth REALISTA: además de devolver el usuario, hace una lectura en la MISMA sesión del
# request, igual que auth.get_current_user en producción. Ese SELECT abre el read view de
# MySQL (REPEATABLE READ) ANTES de cualquier with_for_update(), que es la condición real
# bajo la que corren los endpoints. Con un lambda "seco", el lock terminaba siendo la
# PRIMERA sentencia y el snapshot nacía DESPUÉS del lock: las carreras de plata quedaban
# invisibles para los tests.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_created_ids: list[int] = []
_fails: list[str] = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear(**kw):
    body = {"tipo_gasto": "otros", "condicion_pago": "credito", "monto_neto": 1000, **kw}
    r = client.post("/api/compras-contab", json=body)
    if r.status_code == 200:
        _created_ids.append(r.json()["id"])
    return r


def run():
    CURRENT["empresa"] = "mineria"

    # ── Flujos base ──
    r = _crear(numero_documento=f"{MARK}-CONTADO", categoria="Arriendo", acreedor="K",
               afecto_iva=True, condicion_pago="contado", monto_neto=100000)
    check("contado 200", r.status_code == 200, r.text)
    c = r.json()
    check("contado pagado/saldo0", c["estado_pago"] == "pagado" and c["saldo_clp"] == 0, c)
    check("contado iva 19%", c["iva"] == 19000.0, c["iva"])
    check("contado 1 pago con fecha banco", len(c["pagos"]) == 1 and c["pagos"][0]["fecha_mov_bancario"], c["pagos"])

    # Fechas DINÁMICAS (hoy + 30): con fechas fijas el test se volvía 'vencido' con el
    # paso del tiempo (el estado ahora se calcula en vivo al servir).
    from datetime import date as _date, timedelta as _td
    hoy = _date.today()
    r = _crear(numero_documento=f"{MARK}-CRED", proveedor_rut="76.111.111-1",
               tipo_gasto="cogs", fecha=hoy.isoformat(), plazo_dias=30, monto_neto=200000)
    cc = r.json()
    check("credito venc=fecha+30", cc["fecha_vencimiento"] == (hoy + _td(days=30)).isoformat(), cc["fecha_vencimiento"])
    total_cred = cc["monto_total_clp"]
    r = client.post(f"/api/compras-contab/{cc['id']}/pagos", json={"monto_clp": 100000, "fecha_mov_bancario": "2026-06-10"})
    check("pago parcial", r.status_code == 200 and r.json()["estado_pago"] == "parcial", r.text)
    r = client.post(f"/api/compras-contab/{cc['id']}/pagos", json={"monto_clp": total_cred - 100000})
    check("pago completa", r.status_code == 200 and r.json()["estado_pago"] == "pagado", r.text)
    r = client.post(f"/api/compras-contab/{cc['id']}/pagos", json={"monto_clp": 100})  # > TOL_PAGO sobre saldo 0
    check("sobre-pago 400", r.status_code == 400, r.text)

    # PATCH fecha banco (conciliación posterior) + limpiar
    pago_id = cc and client.get(f"/api/compras-contab/{cc['id']}").json()["pagos"][0]["id"]
    r = client.patch(f"/api/compras-contab/{cc['id']}/pagos/{pago_id}", json={"fecha_mov_bancario": "2026-06-20"})
    check("PATCH fecha banco", r.status_code == 200 and r.json()["pagos"][0]["fecha_mov_bancario"] == "2026-06-20", r.text)
    r = client.patch(f"/api/compras-contab/{cc['id']}/pagos/{pago_id}", json={"fecha_mov_bancario": ""})
    check("PATCH limpia fecha banco", r.status_code == 200 and r.json()["pagos"][0]["fecha_mov_bancario"] is None, r.text)

    # Revertir un pago (DELETE) → la compra recalcula saldo y vuelve a 'parcial'
    r = client.delete(f"/api/compras-contab/{cc['id']}/pagos/{pago_id}")
    check("DELETE pago (revertir) 200", r.status_code == 200, r.text)
    rev = client.get(f"/api/compras-contab/{cc['id']}").json()
    check("tras revertir: estado parcial", rev["estado_pago"] == "parcial", rev["estado_pago"])
    check("tras revertir: saldo = 100.000", abs(float(rev["saldo_clp"]) - 100000) < 1, rev["saldo_clp"])

    # ── Unicidad / re-registro tras anular ──
    base_doc = dict(numero_documento=f"{MARK}-REREG", proveedor_rut="77.222.222-2", monto_neto=1000)
    r = _crear(**base_doc); a_id = r.json()["id"]
    check("dup activa 409", _crear(**base_doc).status_code == 409)
    check("anula A", client.post(f"/api/compras-contab/{a_id}/anular", json={"motivo": "err"}).status_code == 200)
    check("re-registro tras anular 200", _crear(**base_doc).status_code == 200)

    # ── USD spot ──
    r = _crear(numero_documento=f"{MARK}-USD", moneda="USD", tc=950, monto_neto=100,
               afecto_iva=False, condicion_pago="contado", pago={"medio": "tarjeta"})
    u = r.json()
    check("USD total_clp=95000", r.status_code == 200 and u["monto_total_clp"] == 95000.0, u)

    # ── Validación robusta (Fase 3) ──
    check("monto negativo 422", _crear(numero_documento=f"{MARK}-NEG", monto_neto=-5).status_code == 422)
    check("tipo_gasto inválido 422", _crear(numero_documento=f"{MARK}-TIPO", tipo_gasto="xxx").status_code == 422)
    check("USD tc=0 422", _crear(numero_documento=f"{MARK}-TC0", moneda="USD", tc=0).status_code == 422)
    check("fecha inválida 400", _crear(numero_documento=f"{MARK}-FECHA", fecha="ayer-pasado").status_code == 400)

    # ── KPIs + listado + antigüedad (forma nueva) ──
    r = client.get("/api/compras-contab/kpis")
    k = r.json()
    check("kpis shape", r.status_code == 200 and set(["n_compras", "total_comprado_clp", "por_pagar_clp", "por_tipo"]).issubset(k), k)
    check("kpis por_tipo 4", set(k["por_tipo"].keys()) == {"cogs", "gasto_operacional", "gasto_no_operacional", "otros"}, k["por_tipo"])
    r = client.get("/api/compras-contab")
    d = r.json()
    check("list shape paginado", set(["compras", "total", "page", "page_size", "antiguedad"]).issubset(d), list(d.keys()))

    # ── Paginación (SQL) ──
    for i in range(3):
        _crear(numero_documento=f"{MARK}-PAG-{i}", monto_neto=1000 + i)
    r1 = client.get("/api/compras-contab", params={"q": f"{MARK}-PAG", "page_size": 2, "page": 1}).json()
    r2 = client.get("/api/compras-contab", params={"q": f"{MARK}-PAG", "page_size": 2, "page": 2}).json()
    check("paginación total=3", r1["total"] == 3, r1["total"])
    check("paginación page1=2", len(r1["compras"]) == 2, len(r1["compras"]))
    check("paginación page2=1", len(r2["compras"]) == 1, len(r2["compras"]))

    # ── Overlay embarque (solo lectura) ──
    r = client.get("/api/compras-contab/costos-embarque")
    check("costos-embarque shape", r.status_code == 200 and set(["costos", "total_clp", "n"]).issubset(r.json()), r.text)

    # ── SCOPE POR EMPRESA (Fase 1, crítico) ──
    # Módulo SOLO MachParts: el guard de router (require_empresa("mineria")) deniega con 403
    # a cualquier usuario no-mineria ANTES de llegar al endpoint (ni siquiera puede listar).
    r = _crear(numero_documento=f"{MARK}-SCOPE", monto_neto=1000)
    scope_id = r.json()["id"]
    ids_mineria = {x["id"] for x in client.get("/api/compras-contab", params={"page_size": 200}).json()["compras"]}
    check("mineria ve su compra", scope_id in ids_mineria)
    CURRENT["empresa"] = "automotriz"  # cambia de marca → bloqueado por el guard
    check("automotriz NO ve detalle (403)", client.get(f"/api/compras-contab/{scope_id}").status_code == 403)
    check("automotriz NO puede pagar (403)", client.post(f"/api/compras-contab/{scope_id}/pagos", json={"monto_clp": 10}).status_code == 403)
    check("automotriz NO puede anular (403)", client.post(f"/api/compras-contab/{scope_id}/anular", json={"motivo": "x"}).status_code == 403)
    check("automotriz NO puede ni listar (403)", client.get("/api/compras-contab", params={"page_size": 200}).status_code == 403)
    CURRENT["empresa"] = "mineria"

    # F1 — imputación a cuenta contable
    cat = client.get("/api/compras-contab/catalogos").json()
    check("catalogos trae plan_cuentas (>=50)", len(cat.get("plan_cuentas", [])) >= 50, len(cat.get("plan_cuentas", [])))
    r = _crear(numero_documento=f"{MARK}-CTADEF", tipo_gasto="otros", monto_neto=1000)
    check("compra toma cuenta default (otros→6.4.01)", r.json().get("cuenta_codigo") == "6.4.01", r.json().get("cuenta_codigo"))
    arriendo = next((x for x in cat.get("plan_cuentas", []) if x["codigo"] == "6.2.02"), None)
    check("plan_cuentas incluye 6.2.02 (Arriendo)", arriendo is not None)
    if arriendo:
        # con RUT: si la cuenta exige auxiliar (requiere_auxiliar), el backend ahora lo valida
        r = _crear(numero_documento=f"{MARK}-CTAEXP", tipo_gasto="gasto_operacional", monto_neto=1000,
                   cuenta_contable_id=arriendo["id"], proveedor_rut="77.333.333-3")
        check("compra acepta cuenta elegida", r.json().get("cuenta_codigo") == "6.2.02", r.json().get("cuenta_codigo"))
        if arriendo.get("requiere_auxiliar"):
            r = _crear(numero_documento=f"{MARK}-CTAEXP2", tipo_gasto="gasto_operacional", monto_neto=1000,
                       cuenta_contable_id=arriendo["id"])
            check("cuenta con auxiliar exige RUT 400", r.status_code == 400, r.text)


def _cleanup():
    db = SessionLocal()
    try:
        # Por id creado + red de seguridad por MARK (cubre filas de cualquier empresa)
        ids = set(_created_ids)
        for o in db.query(ContCompra).filter(ContCompra.numero_documento.like(f"{MARK}%")).all():
            ids.add(o.id)
        # Egresos generados por esas compras (contado/pagos): borrarlos para no dejar
        # egresos huérfanos que contaminan otras pruebas (p.ej. Conciliación).
        egreso_ids = {
            d.egreso_id for d in db.query(ContEgresoDetalle.egreso_id)
            .filter(ContEgresoDetalle.compra_id.in_(ids)).all()
        } if ids else set()
        for cid in ids:
            o = db.query(ContCompra).filter(ContCompra.id == cid).first()
            if o:
                db.delete(o)
        db.flush()
        for eid in egreso_ids:
            e = db.query(ContEgreso).filter(ContEgreso.id == eid).first()
            if e:
                db.delete(e)
        # Red de seguridad: egresos huérfanos (sin ningún detalle) son siempre basura de test.
        huerfanos = (
            db.query(ContEgreso)
            .filter(~db.query(ContEgresoDetalle.id)
                    .filter(ContEgresoDetalle.egreso_id == ContEgreso.id).exists())
            .all()
        )
        for e in huerfanos:
            db.delete(e)
        db.commit()
        print(f"\nCleanup: {len(ids)} compras + {len(egreso_ids) + len(huerfanos)} egresos de prueba eliminados")
    finally:
        db.close()


def test_compras_contab_integration():
    """Wrapper para pytest: corre todo y falla si hubo alguna aserción rota."""
    try:
        run()
    finally:
        _cleanup()
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    try:
        run()
    finally:
        _cleanup()
    print("\n=== RESULTADO:", "TODO OK" if not _fails else f"{len(_fails)} FALLAS: {_fails}", "===")
    sys.exit(1 if _fails else 0)
