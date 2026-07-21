"""Test de integración de Tesorería contra la DB local.

Monta los routers de tesorería + compras + contabilidad (sin tocar main.py) y ejerce:
  · flujo original de conciliación de CARGOS (cuenta → cartola → egreso → conciliar →
    desconciliar), incluido "un movimiento paga varios gastos" (egreso consolidado);
  · flujo NUEVO de ABONOS ↔ cobranzas (ingresos de caja de Facturas y Cobranzas);
  · POR PAGAR + aprobación de pago desde Tesorería (POST /tesoreria/pagos);
  · FLUJO DE CAJA NIC 7 (buckets, exclusión de factorizadas);
  · guard de integridad: no borrar una cobranza conciliada (409).
LIMPIA todo al final. Corre con:
    ./venv/bin/python tesoreria/tests/test_integration.py
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from tesoreria.router import router as tes_router  # noqa: E402
from tesoreria.models import CuentaBancaria, ConciliacionIngreso  # noqa: E402
from compras_contab.router import router as compras_router  # noqa: E402
from compras_contab.models import ContCompra, ContEgreso  # noqa: E402
from routers.contabilidad import router as contab_router  # noqa: E402
from models.models import ContFacturaCliente, ContCobranza, ContFactoring  # noqa: E402

MARK = "__TEST_TES__"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura conc_conciliacion_ingreso

app = FastAPI()
app.include_router(tes_router, prefix="/api")
app.include_router(compras_router, prefix="/api")
app.include_router(contab_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _compra(**kw):
    body = {"tipo_gasto": "otros", "condicion_pago": "credito", "monto_neto": 1000, **kw}
    return client.post("/api/compras-contab", json=body)


def _mk_factura_con_cobranza(db, *, folio, bruto, cobranza_monto, cobranza_fecha,
                             estado_pago=None, factoring=False):
    """Crea factura + cobranza directo en DB (el flujo real exige venta→guía firmada;
    acá probamos SOLO Tesorería). Devuelve (factura_id, cobranza_id) planos — ids, no
    instancias ORM, porque la sesión se cierra antes de usarlos."""
    f = ContFacturaCliente(
        empresa="mineria", numero_factura=folio, tipo_doc="factura",
        fecha_emision=date(2026, 6, 1), fecha_vencimiento=date(2026, 6, 30),
        monto_neto=round(bruto / 1.19, 2), iva=round(bruto - bruto / 1.19, 2),
        monto_bruto=bruto, monto_pagado=cobranza_monto,
        saldo=round(bruto - cobranza_monto, 2),
        estado_pago=estado_pago or ("pagada" if cobranza_monto >= bruto else "parcial"),
    )
    db.add(f)
    db.flush()
    c = ContCobranza(factura_id=f.id, fecha=cobranza_fecha, monto=cobranza_monto,
                     medio="transferencia", banco="Santander", numero_operacion=f"{MARK}-OP")
    db.add(c)
    db.flush()
    ids = (f.id, c.id)
    if factoring:
        db.add(ContFactoring(factura_id=f.id, empresa_factoring=f"{MARK} Factor",
                             monto_adelantado=bruto * 0.9, retencion=bruto * 0.1,
                             estado="vigente"))
    db.commit()
    return ids


def run():
    CURRENT["empresa"] = "mineria"

    # 1) Cuenta
    r = client.post("/api/tesoreria/cuentas", json={"banco": "Santander", "nombre": f"{MARK} Cta", "moneda": "CLP"})
    check("crear cuenta 200", r.status_code == 200, r.text)
    cuenta_id = r.json()["id"]

    # 2) Cartola CSV: cargo 119000 + cargo 8000 (consolidado) + abono 50000 + abono 77000
    csv = ("Fecha;Glosa;Cargo;Abono;Saldo\n"
           "01/06/2026;PAGO PROVEEDOR;119.000;;500.000\n"
           "03/06/2026;PAGO CONSOLIDADO;8.000;;492.000\n"
           "02/06/2026;DEPOSITO CLIENTE;;50.000;550.000\n"
           "04/06/2026;TRANSFERENCIA CLIENTE;;77.000;627.000\n")
    r = client.post("/api/tesoreria/cartolas/importar",
                    data={"cuenta_id": str(cuenta_id), "nombre": f"{MARK} cartola"},
                    files={"file": ("cartola.csv", csv.encode("utf-8"), "text/csv")})
    check("importar cartola (4 movs)", r.status_code == 200 and r.json()["n_importados"] == 4, r.text)

    # 3) Compra contado (genera egreso de 119000)
    r = _compra(numero_documento=f"{MARK}-A", acreedor=f"{MARK} ProvA", monto_neto=100000,
                afecto_iva=True, condicion_pago="contado")
    compra = r.json()
    pago = compra["pagos"][0]
    egreso_id = pago["egreso_id"]
    check("compra contado genera egreso 119000 no conciliado",
          pago["monto_clp"] == 119000.0 and pago["conciliado"] is False and egreso_id, pago)
    # La fecha valor propia del egreso (autollenada = fecha del pago) debe sobrevivir
    # a un ciclo conciliar→desconciliar: el snapshot del enlace la restaura (antes se
    # perdía: la cartola la pisaba al conciliar y la limpieza por igualdad la borraba).
    fecha_manual = pago["fecha_mov_bancario"]
    check("egreso de contado nace con fecha valor propia (≠ fecha de la cartola)",
          bool(fecha_manual) and fecha_manual != "2026-06-01", pago)

    # 4) Movimiento cargo 119000
    movs = client.get("/api/tesoreria/movimientos", params={"cuenta_id": cuenta_id, "tipo": "cargo"}).json()["movimientos"]
    cargo = next((m for m in movs if m["monto"] == 119000.0), None)
    check("existe movimiento cargo 119000", cargo is not None)

    # 5) Sugerencias → incluye el egreso (clase 'egreso')
    sugs = client.get(f"/api/tesoreria/movimientos/{cargo['id']}/sugerencias").json().get("sugerencias", [])
    check("sugerencia incluye el egreso", any(s.get("egreso_id") == egreso_id and s.get("clase") == "egreso" for s in sugs), sugs)

    # 6) Conciliar mov ↔ egreso
    r = client.post(f"/api/tesoreria/movimientos/{cargo['id']}/conciliar", json={"egreso_id": egreso_id})
    check("conciliar cargo 200 + conciliado", r.status_code == 200 and r.json()["conciliado"] is True, r.text)
    check("destino clase egreso", (r.json().get("destino") or {}).get("clase") == "egreso", r.json().get("destino"))

    # 7) La compra quedó con su pago conciliado + fecha del banco desde la cartola
    det = client.get(f"/api/compras-contab/{compra['id']}").json()
    pg = det["pagos"][0]
    check("pago conciliado + fecha banco de la cartola", pg["conciliado"] is True and pg["fecha_mov_bancario"] == "2026-06-01", pg)

    # 8) Guard: no revertir el pago de un egreso conciliado
    r = client.delete(f"/api/compras-contab/{compra['id']}/pagos/{pg['id']}")
    check("revertir pago conciliado 409", r.status_code == 409, r.text)

    # 9) Conciliar un movimiento ya conciliado → 409
    r2 = _compra(numero_documento=f"{MARK}-X", acreedor=f"{MARK} ProvX", monto_neto=999, afecto_iva=False, condicion_pago="contado")
    eg_x = r2.json()["pagos"][0]["egreso_id"]
    r = client.post(f"/api/tesoreria/movimientos/{cargo['id']}/conciliar", json={"egreso_id": eg_x})
    check("conciliar mov ya conciliado 409", r.status_code == 409, r.text)

    # 10) Desconciliar → libera y RESTAURA la fecha valor previa del egreso
    r = client.post(f"/api/tesoreria/movimientos/{cargo['id']}/desconciliar")
    check("desconciliar 200", r.status_code == 200 and r.json()["conciliado"] is False, r.text)
    det = client.get(f"/api/compras-contab/{compra['id']}").json()
    check("pago liberado", det["pagos"][0]["conciliado"] is False, det["pagos"][0])
    check("fecha valor del operador RESTAURADA tras desconciliar (snapshot del enlace)",
          det["pagos"][0]["fecha_mov_bancario"] == fecha_manual,
          (det["pagos"][0]["fecha_mov_bancario"], fecha_manual))

    # 11) CASO DEL DUEÑO: 1 movimiento (8000) paga 2 gastos (egreso consolidado)
    b = _compra(numero_documento=f"{MARK}-B", acreedor=f"{MARK} ProvB", monto_neto=5000, afecto_iva=False).json()
    c = _compra(numero_documento=f"{MARK}-C", acreedor=f"{MARK} ProvC", monto_neto=3000, afecto_iva=False).json()
    r = client.post("/api/compras-contab/egresos", json={
        "fecha": "2026-06-03", "medio": "transferencia", "beneficiario": f"{MARK} consolidado",
        "detalles": [{"compra_id": b["id"], "monto_clp": 5000}, {"compra_id": c["id"], "monto_clp": 3000}],
    })
    check("egreso consolidado 200 (paga 2 compras)", r.status_code == 200 and r.json()["monto_total_clp"] == 8000.0, r.text)
    eg_cons = r.json()["id"]
    cargo2 = next((m for m in client.get("/api/tesoreria/movimientos", params={"cuenta_id": cuenta_id, "tipo": "cargo"}).json()["movimientos"] if m["monto"] == 8000.0), None)
    r = client.post(f"/api/tesoreria/movimientos/{cargo2['id']}/conciliar", json={"egreso_id": eg_cons})
    check("conciliar 1 movimiento ↔ egreso de 2 gastos", r.status_code == 200 and r.json()["destino"]["n_compras"] == 2, r.text)

    # ══ NUEVO: abono ↔ cobranza (ingreso de caja de Facturas) ═══════════════════
    db = SessionLocal()
    try:
        fact_id, cob_id = _mk_factura_con_cobranza(db, folio=f"{MARK}-F1", bruto=77000.0,
                                                   cobranza_monto=77000.0, cobranza_fecha=date(2026, 6, 4))
        # cobranza 60000 ≠ abono 50000: sirve para probar el rechazo por monto distinto
        _fact2_id, cob2_id = _mk_factura_con_cobranza(db, folio=f"{MARK}-F2", bruto=200000.0,
                                                      cobranza_monto=60000.0, cobranza_fecha=date(2026, 6, 2))
        # factura factorizada (vigente) con saldo → NO debe aparecer en por_cobrar del flujo
        _mk_factura_con_cobranza(db, folio=f"{MARK}-F3", bruto=300000.0,
                                 cobranza_monto=1000.0, cobranza_fecha=date(2026, 6, 3),
                                 estado_pago="factorizada", factoring=True)
    finally:
        db.close()

    # 12) Abono 77000 → sugerencias incluyen la cobranza de F1 (clase 'cobranza')
    movs = client.get("/api/tesoreria/movimientos", params={"cuenta_id": cuenta_id, "tipo": "abono"}).json()["movimientos"]
    abono77 = next((m for m in movs if m["monto"] == 77000.0), None)
    abono50 = next((m for m in movs if m["monto"] == 50000.0), None)
    check("existen abonos 77000 y 50000", abono77 is not None and abono50 is not None)
    sugs = client.get(f"/api/tesoreria/movimientos/{abono77['id']}/sugerencias").json().get("sugerencias", [])
    check("sugerencia abono incluye cobranza F1",
          any(s.get("cobranza_id") == cob_id and s.get("clase") == "cobranza" for s in sugs), sugs)

    # 13) Conciliar abono ↔ cobranza
    r = client.post(f"/api/tesoreria/movimientos/{abono77['id']}/conciliar", json={"cobranza_id": cob_id})
    check("conciliar abono 200 + destino cobranza",
          r.status_code == 200 and (r.json().get("destino") or {}).get("clase") == "cobranza", r.text)

    # 14) La misma cobranza no se concilia dos veces
    r = client.post(f"/api/tesoreria/movimientos/{abono50['id']}/conciliar", json={"cobranza_id": cob_id})
    check("cobranza ya conciliada 409", r.status_code == 409, r.text)

    # 15) Montos distintos → 400
    r = client.post(f"/api/tesoreria/movimientos/{abono50['id']}/conciliar", json={"cobranza_id": cob2_id})
    check("montos distintos abono↔cobranza 400", r.status_code == 400, r.text)

    # 16) cobranzas-pendientes: excluye la conciliada, incluye la pendiente
    pend = client.get("/api/tesoreria/cobranzas-pendientes", params={"q": MARK}).json()["cobranzas"]
    ids = {p["cobranza_id"] for p in pend}
    check("cobranzas-pendientes excluye conciliada", cob_id not in ids and cob2_id in ids, ids)

    # 17) Guard de integridad: borrar cobranza conciliada → 409
    r = client.delete(f"/api/contabilidad/facturas/{fact_id}/cobranzas/{cob_id}")
    check("borrar cobranza conciliada 409", r.status_code == 409, r.text)

    # 18) Desconciliar el abono libera la cobranza (y ahora sí se puede borrar)
    r = client.post(f"/api/tesoreria/movimientos/{abono77['id']}/desconciliar")
    check("desconciliar abono 200", r.status_code == 200 and r.json()["conciliado"] is False, r.text)
    r = client.delete(f"/api/contabilidad/facturas/{fact_id}/cobranzas/{cob_id}")
    check("borrar cobranza liberada 200", r.status_code == 200, r.text)

    # ══ NUEVO: por pagar + aprobación de pago desde Tesorería ═══════════════════
    # 19) Compra a crédito vencida aparece en /por-pagar con bucket 'vencido'
    venc = (date.today() - timedelta(days=3)).isoformat()
    d = _compra(numero_documento=f"{MARK}-D", acreedor=f"{MARK} ProvD", monto_neto=40000,
                afecto_iva=False, condicion_pago="credito", fecha=venc, plazo_dias=0).json()
    pp = client.get("/api/tesoreria/por-pagar", params={"q": MARK}).json()
    fila = next((c for c in pp["compras"] if c["compra_id"] == d["id"]), None)
    check("por-pagar incluye compra D vencida", fila is not None and fila["bucket"] == "vencido", fila)
    check("por-pagar buckets suman", pp["buckets"]["vencido"]["n"] >= 1, pp["buckets"])

    # 20) Tesorería aprueba el pago (POST /pagos) → compra pagada
    r = client.post("/api/tesoreria/pagos", json={
        "fecha": date.today().isoformat(), "medio": "transferencia",
        "beneficiario": f"{MARK} ProvD",
        "detalles": [{"compra_id": d["id"], "monto_clp": 40000}],
    })
    check("aprobar pago 200", r.status_code == 200 and r.json()["monto_total_clp"] == 40000.0, r.text)
    check("compra D pagada tras aprobar", client.get(f"/api/compras-contab/{d['id']}").json()["estado_pago"] == "pagado")

    # 21) Sobre-pago rechazado
    r = client.post("/api/tesoreria/pagos", json={
        "detalles": [{"compra_id": b["id"], "monto_clp": 999999}],
    })
    check("sobre-pago 400", r.status_code == 400, r.text)

    # ══ NUEVO: flujo de caja NIC 7 ══════════════════════════════════════════════
    fc = client.get("/api/tesoreria/flujo-caja").json()
    check("flujo-caja shape", {"buckets", "por_pagar", "por_cobrar", "neto", "retenciones_factoring"}.issubset(fc), fc.keys())
    # F2 tiene saldo 140000 con vencimiento 2026-06-30 (pasado) → por_cobrar vencido
    check("por_cobrar vencido incluye F2", fc["por_cobrar"]["vencido"]["monto"] >= 140000, fc["por_cobrar"])
    # F3 factorizada (saldo 299000, vencida) NO suma a por_cobrar. Robusto ante datos
    # reales de la BD local: se compara el flujo con F3 factorizada vs no-factorizada.
    db = SessionLocal()
    try:
        f3 = db.query(ContFacturaCliente).filter(ContFacturaCliente.numero_factura == f"{MARK}-F3").first()
        f3.estado_pago = "parcial"
        db.commit()
    finally:
        db.close()
    fc2 = client.get("/api/tesoreria/flujo-caja").json()
    delta = fc2["por_cobrar"]["vencido"]["monto"] - fc["por_cobrar"]["vencido"]["monto"]
    check("factorizada excluida de por_cobrar (delta 299000)", abs(delta - 299000) <= 1, delta)
    check("retenciones factoring informadas", fc["retenciones_factoring"]["monto"] >= 30000, fc["retenciones_factoring"])

    # 22) Resumen extendido
    r = client.get("/api/tesoreria/resumen", params={"cuenta_id": cuenta_id})
    check("resumen shape", r.status_code == 200 and
          {"pagos_por_aprobar", "cargos_pendientes", "abonos_pendientes",
           "egresos_sin_conciliar", "cobranzas_sin_conciliar", "por_pagar_vencido_clp"}.issubset(r.json()), r.text)

    # 23) Cuenta con movimientos no se borra
    check("borrar cuenta con movimientos 409", client.delete(f"/api/tesoreria/cuentas/{cuenta_id}").status_code == 409)

    # 24) Scope: un movimiento manual no puede colgar de una cartola de OTRA cuenta
    cartolas = client.get("/api/tesoreria/cartolas", params={"cuenta_id": cuenta_id}).json()
    cartola_id = cartolas[0]["id"] if cartolas else None
    cuenta2_id = client.post("/api/tesoreria/cuentas", json={"banco": "BCI", "nombre": f"{MARK} Cta2", "moneda": "CLP"}).json()["id"]
    r = client.post("/api/tesoreria/movimientos", json={
        "cuenta_id": cuenta2_id, "fecha": "2026-06-05", "glosa": "X", "tipo": "cargo",
        "monto": 1000, "cartola_id": cartola_id})
    check("movimiento con cartola de otra cuenta 400", r.status_code == 400, r.text)

    # 25) Candado de empresa: usuario automotriz recibe 403
    CURRENT["empresa"] = "automotriz"
    r = client.get("/api/tesoreria/resumen")
    check("usuario automotriz 403", r.status_code == 403, r.text)
    CURRENT["empresa"] = "mineria"


def _cleanup():
    db = SessionLocal()
    try:
        for o in db.query(CuentaBancaria).filter(CuentaBancaria.nombre.like(f"{MARK}%")).all():
            db.delete(o)  # cascade movimientos + cartolas + conciliaciones (+ ingreso)
        db.commit()
        for o in db.query(ContEgreso).filter(ContEgreso.beneficiario.like(f"%{MARK}%")).all():
            db.delete(o)  # cascade detalles
        db.commit()
        for o in db.query(ContCompra).filter(ContCompra.numero_documento.like(f"{MARK}%")).all():
            db.delete(o)
        db.commit()
        facturas = db.query(ContFacturaCliente).filter(ContFacturaCliente.numero_factura.like(f"{MARK}%")).all()
        for f in facturas:
            for lk in db.query(ConciliacionIngreso).filter(
                    ConciliacionIngreso.cobranza_id.in_([c.id for c in f.cobranzas] or [0])).all():
                db.delete(lk)
            db.delete(f)  # cascade items + cobranzas + factoring
        db.commit()
        print("\nCleanup OK")
    finally:
        db.close()


def test_tesoreria_integration():
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
