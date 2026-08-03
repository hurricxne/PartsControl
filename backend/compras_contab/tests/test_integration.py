"""Test de integración del módulo Compras / Cuentas por Pagar contra la DB local.

Monta el router en una app efímera (sin tocar main.py), sobreescribe la auth para
simular usuarios de distintas empresas, ejerce todos los flujos y LIMPIA todo lo
que creó al terminar (deja la DB intacta).

Cubre además el overlay de gastos de embarque de punta a punta (reflejo automático +
`compra_id` + registrar como compra + dedup 409 + punteros inexistentes 400), el candado
de egreso/pago CONCILIADO (409) y los topes de entrada del schema (plazo y longitudes).

Corre con:  ./venv/bin/python compras_contab/tests/test_integration.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import Embarque, EmbarqueItem  # noqa: E402
from embarques_pricing.models import (  # noqa: E402
    EmbarquePricing, EmbarquePricingGasto,
)
from compras_contab.router import router  # noqa: E402
from compras_contab.models import ContCompra, ContEgreso, ContEgresoDetalle  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

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
# Embarque + pricing + 1 gasto local de prueba (fuente del overlay /costos-embarque).
_seed = {"emb_id": None, "pricing_id": None, "gasto_id": None}


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _purge_embarque(db):
    """Borra el embarque de prueba con su pricing y gastos (idempotente). Las FK de
    cont_compra hacia el gasto/embarque son ON DELETE SET NULL → no bloquean."""
    for e in db.query(Embarque).filter(Embarque.numero.like(f"{MARK}%")).all():
        pr = db.query(EmbarquePricing).filter(EmbarquePricing.embarque_id == e.id).first()
        if pr:
            db.query(EmbarquePricingGasto).filter(
                EmbarquePricingGasto.pricing_id == pr.id).delete(synchronize_session=False)
            db.delete(pr)
        db.query(EmbarqueItem).filter(
            EmbarqueItem.embarque_id == e.id).delete(synchronize_session=False)
        db.delete(e)


def _seed_embarque():
    """Siembra un embarque con pricing y 1 gasto local (para el overlay)."""
    db = SessionLocal()
    try:
        _purge_embarque(db)
        db.commit()
        emb = Embarque(numero=f"{MARK}-EMB", estado="en_transito", forwarder="DHL")
        db.add(emb); db.flush()
        pr = EmbarquePricing(embarque_id=emb.id, tipo_embarque="courier", tc_valor=950,
                             moneda="USD", estado="borrador")
        db.add(pr); db.flush()
        g = EmbarquePricingGasto(pricing_id=pr.id, tipo="agencia", glosa="Agencia de Aduana",
                                 monto_neto=80000, iva=15200, capitaliza=True,
                                 nro_factura="F-778", orden=3)
        db.add(g); db.flush()
        _seed.update(emb_id=emb.id, pricing_id=pr.id, gasto_id=g.id)
        db.commit()
    finally:
        db.close()


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

    # ── Overlay embarque: el gasto anotado en Pricing se refleja solo y se puede pasar a CxP ──
    r = client.get("/api/compras-contab/costos-embarque")
    check("costos-embarque shape", r.status_code == 200 and set(["costos", "total_clp", "n"]).issubset(r.json()), r.text)
    fila = next((x for x in r.json()["costos"] if x["id"] == _seed["gasto_id"]), None)
    check("gasto de embarque aparece automáticamente", fila is not None, _seed)
    if fila:
        check("gasto trae neto+iva del pricing",
              fila["monto_neto"] == 80000.0 and fila["iva"] == 15200.0, fila)
        # compra_id es LA LLAVE que el front necesita para prefillar el botón «Registrar
        # como compra»: sin ella el operador re-digita, la compra nace con
        # emb_pricing_gasto_id NULL y el dedup queda inalcanzable (los NULL no colisionan).
        check("overlay declara compra_id (aún sin compra)",
              "compra_id" in fila and fila["compra_id"] is None, fila)
    # Registrarlo como compra pagable (origen EMBARQUE → cogs → cuenta 1.3.02)
    r = _crear(numero_documento=f"{MARK}-EMBG", origen="EMBARQUE", tipo_gasto="cogs",
               monto_neto=80000, iva=15200, emb_pricing_gasto_id=_seed["gasto_id"],
               embarque_id=_seed["emb_id"], referencia=f"{MARK}-EMB")
    check("registrar gasto de embarque como compra 200", r.status_code == 200, r.text)
    ce = r.json()
    check("compra EMBARQUE con cuenta 1.3.02", ce["cuenta_codigo"] == "1.3.02", ce["cuenta_codigo"])
    # Dedup: el mismo gasto NO puede convertirse en dos CxP activas (factura del
    # forwarder cargada 2 veces). Con el lock del gasto, el 2° intento ve la compra ya creada.
    r = _crear(numero_documento=f"{MARK}-EMBG2", origen="EMBARQUE", tipo_gasto="cogs",
               monto_neto=80000, emb_pricing_gasto_id=_seed["gasto_id"])
    check("mismo gasto de embarque otra vez → 409", r.status_code == 409, r.text)
    fila2 = next((x for x in client.get("/api/compras-contab/costos-embarque").json()["costos"]
                  if x["id"] == _seed["gasto_id"]), None)
    check("overlay marca el gasto como ya registrado (compra_id)",
          fila2 and fila2.get("compra_id") == ce["id"], fila2)
    # Anular la compra libera el gasto: el overlay vuelve a ofrecerlo y se puede re-registrar
    check("anular la compra del gasto 200",
          client.post(f"/api/compras-contab/{ce['id']}/anular",
                      json={"motivo": "carga errada"}).status_code == 200)
    fila3 = next((x for x in client.get("/api/compras-contab/costos-embarque").json()["costos"]
                  if x["id"] == _seed["gasto_id"]), None)
    check("tras anular: el overlay vuelve a ofrecer el gasto (compra_id None)",
          fila3 is not None and "compra_id" in fila3 and fila3["compra_id"] is None, fila3)
    # Carrera del dedup: 2 registros SIMULTÁNEOS del mismo gasto. El lock de la fila del
    # gasto los serializa → el 2° ve la compra ya creada y da 409. Sin el lock ambos leen
    # "no hay duplicado" y la factura del forwarder queda cargada DOS veces (models.py:102
    # es index=True, NO unique: en la BD no hay red que lo atrape).
    import threading
    rondas_malas, codigos_vistos = 0, []
    for ronda in range(4):
        codigos: list = []

        def _reg(n, _r=ronda):
            codigos.append(_crear(
                numero_documento=f"{MARK}-RACE{_r}{n}", origen="EMBARQUE", tipo_gasto="cogs",
                monto_neto=80000, emb_pricing_gasto_id=_seed["gasto_id"]).status_code)

        t1 = threading.Thread(target=_reg, args=(1,))
        t2 = threading.Thread(target=_reg, args=(2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        codigos_vistos.append(sorted(codigos))
        db_r = SessionLocal()
        try:
            n_act = (db_r.query(ContCompra)
                     .filter(ContCompra.emb_pricing_gasto_id == _seed["gasto_id"],
                             ContCompra.anulado.is_(False)).count())
            if n_act != 1:
                rondas_malas += 1
            # libera el gasto para la ronda siguiente
            db_r.query(ContCompra).filter(
                ContCompra.emb_pricing_gasto_id == _seed["gasto_id"]).update(
                {"anulado": True}, synchronize_session=False)
            db_r.commit()
        finally:
            db_r.close()
    check("carrera ×4: 2 registros simultáneos del mismo gasto → 1 sola CxP activa",
          rondas_malas == 0, f"{rondas_malas} rondas con CxP duplicada, códigos {codigos_vistos}")
    # Punteros inexistentes → 400 (antes se guardaban y la trazabilidad nacía rota)
    r = _crear(numero_documento=f"{MARK}-EMBGX", emb_pricing_gasto_id=99999999)
    check("gasto de embarque inexistente → 400", r.status_code == 400, r.text)
    r = _crear(numero_documento=f"{MARK}-EMBGY", embarque_id=99999999)
    check("embarque inexistente → 400", r.status_code == 400, r.text)

    # ── Egreso/pago CONCILIADO: la cartola es la fuente de verdad → PATCH rechaza 409 ──
    r = _crear(numero_documento=f"{MARK}-CONC", proveedor_rut="76.444.444-4",
               monto_neto=2000, condicion_pago="contado")
    conc = r.json()
    egreso_conc_id = conc["pagos"][0]["egreso_id"]
    db = SessionLocal()
    try:
        db.query(ContEgreso).filter(ContEgreso.id == egreso_conc_id).update({"conciliado": True})
        db.commit()
    finally:
        db.close()
    r = client.patch(f"/api/compras-contab/egresos/{egreso_conc_id}",
                     json={"referencia_bancaria": "X"})
    check("PATCH egreso conciliado → 409", r.status_code == 409, r.text)
    r = client.patch(f"/api/compras-contab/{conc['id']}/pagos/{conc['pagos'][0]['id']}",
                     json={"fecha_mov_bancario": hoy.isoformat()})
    check("PATCH pago conciliado → 409", r.status_code == 409, r.text)

    # ── TOCTOU: Tesorería concilia MIENTRAS el PATCH está en vuelo ──
    # Se toma el lock de la fila desde fuera, se lanza el PATCH (que queda esperando ese
    # lock) y solo entonces se concilia y commitea. Con el lock en el endpoint, el PATCH
    # relee y da 409. Sin el lock, el guard corre contra la lectura vieja y la escritura
    # PISA la referencia que la cartola acaba de fijar (cartola y libro discrepan).
    import time
    r = _crear(numero_documento=f"{MARK}-TOCTOU", proveedor_rut="76.555.555-5",
               monto_neto=3000, condicion_pago="contado")
    toc = r.json()
    eg_toc = toc["pagos"][0]["egreso_id"]
    res: dict = {}
    db_lock = SessionLocal()
    try:
        db_lock.query(ContEgreso).filter(ContEgreso.id == eg_toc).with_for_update().first()

        def _patch():
            res["r"] = client.patch(f"/api/compras-contab/egresos/{eg_toc}",
                                    json={"referencia_bancaria": f"{MARK}-PISADA"})

        th = threading.Thread(target=_patch)
        th.start()
        time.sleep(0.8)   # el PATCH ya está esperando el lock de la fila
        db_lock.query(ContEgreso).filter(ContEgreso.id == eg_toc).update(
            {"conciliado": True, "referencia_bancaria": f"{MARK}-CARTOLA"},
            synchronize_session=False)
        db_lock.commit()
        th.join(timeout=30)
    finally:
        db_lock.close()
    check("TOCTOU: conciliar mientras el PATCH está en vuelo → 409",
          res.get("r") is not None and res["r"].status_code == 409,
          res.get("r") is not None and res["r"].status_code)
    db_chk = SessionLocal()
    try:
        ref_final = db_chk.query(ContEgreso.referencia_bancaria).filter(
            ContEgreso.id == eg_toc).scalar()
    finally:
        db_chk.close()
    check("TOCTOU: la referencia de la cartola NO fue pisada",
          ref_final == f"{MARK}-CARTOLA", ref_final)

    # ── N+1 en el detalle: las queries NO deben crecer con la cantidad de pagos ──
    from sqlalchemy import event as _sa_event
    _q = {"n": 0}

    def _cuenta_queries(conn, cursor, statement, parameters, context, executemany):
        # solo las tablas donde vive el N+1 (pagos → egreso → detalles, e items)
        if "cont_egreso" in statement or "cont_compra_item" in statement:
            _q["n"] += 1

    r = _crear(numero_documento=f"{MARK}-N1", proveedor_rut="76.666.666-6", monto_neto=30000)
    c_1pago = r.json()["id"]
    client.post(f"/api/compras-contab/{c_1pago}/pagos", json={"monto_clp": 1000})
    r = _crear(numero_documento=f"{MARK}-N3", proveedor_rut="76.666.666-6", monto_neto=30000)
    c_3pagos = r.json()["id"]
    for _ in range(3):
        client.post(f"/api/compras-contab/{c_3pagos}/pagos", json={"monto_clp": 1000})
    _sa_event.listen(engine, "before_cursor_execute", _cuenta_queries)
    try:
        _q["n"] = 0
        client.get(f"/api/compras-contab/{c_1pago}")
        q1 = _q["n"]
        _q["n"] = 0
        client.get(f"/api/compras-contab/{c_3pagos}")
        q3 = _q["n"]
    finally:
        _sa_event.remove(engine, "before_cursor_execute", _cuenta_queries)
    check("detalle sin N+1: mismas queries con 1 pago y con 3 pagos",
          q1 == q3, f"1 pago={q1} queries, 3 pagos={q3} queries")


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

    # ── Topes de entrada del schema (422 limpio en la API, no 500 desde la BD) ──
    # VAN AL FINAL a propósito: sin el tope el request revienta con OverflowError /
    # DataError 1406 y el TestClient re-lanza la excepción (raise_server_exceptions),
    # así que una regresión acá cortaría la suite y taparía los checks siguientes.
    r = _crear(numero_documento=f"{MARK}-PLAZO", plazo_dias=99999999)
    check("plazo_dias fuera de rango → 422 (no OverflowError 500)", r.status_code == 422, r.status_code)
    r = _crear(numero_documento=f"{MARK}-LARGO", referencia="X" * 200)
    check("referencia más larga que la columna (120) → 422", r.status_code == 422, r.status_code)
    r = _crear(numero_documento="D" * 150)
    check("numero_documento más largo que la columna (100) → 422", r.status_code == 422, r.status_code)


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
        # Embarque + pricing + gasto sembrados para el overlay (después de las compras:
        # sus FK son SET NULL, así que el orden no bloquea, pero deja la BD prolija).
        _purge_embarque(db)
        db.commit()
        print(f"\nCleanup: {len(ids)} compras + {len(egreso_ids) + len(huerfanos)} egresos de prueba eliminados")
    finally:
        db.close()


def test_compras_contab_integration():
    """Wrapper para pytest: corre todo y falla si hubo alguna aserción rota."""
    _seed_embarque()
    try:
        run()
    finally:
        _cleanup()
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    _seed_embarque()
    try:
        run()
    finally:
        _cleanup()
    print("\n=== RESULTADO:", "TODO OK" if not _fails else f"{len(_fails)} FALLAS: {_fails}", "===")
    sys.exit(1 if _fails else 0)
