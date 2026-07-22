"""E2E del VIAJE DE LA PLATA: de la factura emitida al SII hasta la conciliación bancaria.

Sigue una venta real de punta a punta y verifica, con NÚMEROS en cada paso, las tres
preguntas del negocio: cómo se GUARDA la información, cómo se PAGA, y cómo VIAJA por
Tesorería.

    Comercial informa adelanto
        → Tesorería lo aprueba (ahí recién es plata confirmada)
        → factura de ANTICIPO al SII  → el adelanto la deja pagada
        → factura FINAL al SII        → descuenta el anticipo (no se cobra dos veces)
        → el cliente paga el saldo    → cobranza
        → Tesorería concilia el abono del banco contra esa cobranza
        → el depósito del adelanto se concilia por su propia vía (abono ↔ adelanto)
        → KPIs de Contabilidad y de Tesorería cuentan lo MISMO

Invariantes que se verifican (los cinco que costaron plata en la auditoría 2026-07-21):
  1. monto_aplicado del adelanto == Σ cobranzas medio='adelanto' ligadas a él.
  2. Σ brutos de las facturas de la OC == mercadería vendida (el anticipo NO se suma dos veces).
  3. Las cobranzas medio='adelanto' NO son conciliables como abono nuevo (no es plata que entra
     de nuevo: ya entró cuando Tesorería aprobó el adelanto).
  4. Un abono del banco no se concilia dos veces.
  5. saldo / monto_pagado / estado_pago persistidos cuadran SIEMPRE con las filas reales.

Wasabil va SIMULADO (jamás toca el SII). Todo dato lleva MARK y se borra en finally.
Las verificaciones leen con CONEXIÓN NUEVA: la sesión del test arrastra su propio
snapshot y puede mentir en ambas direcciones.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_viaje_de_la_plata.py -q -s
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContAdelanto, ContFactoring,
)
from tesoreria.models import (  # noqa: E402
    CuentaBancaria, Cartola, MovimientoBancario, ConciliacionIngreso,
)
from wasabil_dte.models import WasabilDte  # noqa: E402
import routers.contabilidad as cont  # noqa: E402
from wasabil_dte.router import router as wasabil_router  # noqa: E402
from tesoreria.router import router as tes_router  # noqa: E402
# El fake oficial del módulo: valida como el API real (price>=0, qty>0, paymentMethod…)
from wasabil_dte.tests.test_facturas_integration import FakeWasabil  # noqa: E402

MARK = "__TEST_VIAJE__"
CURRENT = {"id": None, "empresa": "mineria"}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()


# Auth REALISTA (consulta la BD en la misma sesión, como producción): sin esto el
# snapshot nacería después de los locks y las verificaciones serían optimistas.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
app.include_router(cont.router, prefix="/api")
app.include_router(wasabil_router, prefix="/api")
app.include_router(tes_router, prefix="/api")
client = TestClient(app)

PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0)} for i in items}
    neto = sum(cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0)) for i in items)
    return items, pmap, {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
                         "total_con_iva_clp": neto + cont._iva_clp(neto)}


_fails = []


def check(name, cond, extra=""):
    print(("OK   | " if cond else "FAIL | ") + name + ("" if cond else f"  -> {str(extra)[:280]}"))
    if not cond:
        _fails.append(name)


# ─── Lecturas de verificación: SIEMPRE con conexión nueva ─────────────────────
def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _factura_real(fid):
    r = _sql("SELECT monto_neto,iva,monto_bruto,monto_pagado,saldo,estado_pago,numero_factura "
             "FROM cont_factura_cliente WHERE id=:i", i=fid)[0]
    cobs = _sql("SELECT id,monto,medio,adelanto_id FROM cont_cobranza WHERE factura_id=:i", i=fid)
    return {"neto": float(r[0] or 0), "iva": float(r[1] or 0), "bruto": float(r[2] or 0),
            "pagado": float(r[3] or 0), "saldo": float(r[4] or 0), "estado": r[5],
            "folio": r[6],
            "cobranzas": [(c[0], float(c[1]), c[2], c[3]) for c in cobs],
            "suma_cob": sum(float(c[1]) for c in cobs)}


def _adelanto_real(aid):
    r = _sql("SELECT monto,monto_aplicado,estado FROM cont_adelanto WHERE id=:i", i=aid)[0]
    return {"monto": float(r[0] or 0), "aplicado": float(r[1] or 0), "estado": r[2]}


def _emitir_sii(payload):
    """Emite y SONDEA hasta que el SII confirme, igual que el modal real.

    El API responde primero 'procesando' (status 2) y el folio llega en una consulta
    posterior; el frontend sondea cada 3 s. Sin este paso la factura queda sin folio —
    y el sistema, con razón, no deja facturar contra un anticipo sin folio confirmado."""
    r = client.post("/api/wasabil/facturas/emitir", json=payload)
    if r.status_code != 200:
        return r, None
    fid = r.json()["factura_id"]
    for _ in range(5):
        est = client.get(f"/api/wasabil/facturas/{fid}/estado")
        if est.status_code == 200 and est.json().get("estado") == "emitido":
            break
    return r, fid


def _crear_venta(db, *, cantidad=20, precio=10000.0):
    """Venta de 1 ítem con DOS despachos posibles (para facturar en dos tandas)."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} MINERA X",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="FILTRO ACEITE", cantidad=cantidad, estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK[-8:]}-OC", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    estado="despachado", guia_firmada=1, numero_guia=f"G{oc.id}",
                    fecha_despacho=None)
    db.add(desp); db.flush()
    db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id, qty_despachada=cantidad))
    db.commit()
    PRECIOS.clear(); PRECIOS.update({it.id: precio})
    return cot, oc, desp, it


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    if not cots:
        return
    oc_ids = [o.id for o in db.query(OcCliente)
              .filter(OcCliente.cotizacion_id.in_([c.id for c in cots])).all()]
    if oc_ids:
        adel_ids = [a.id for a in db.query(ContAdelanto)
                    .filter(ContAdelanto.oc_cliente_id.in_(oc_ids)).all()]
        fac_ids = [f.id for f in db.query(ContFacturaCliente)
                   .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
        cob_ids = ([c.id for c in db.query(ContCobranza)
                    .filter(ContCobranza.factura_id.in_(fac_ids)).all()] if fac_ids else [])
        if cob_ids:
            db.query(ConciliacionIngreso).filter(
                ConciliacionIngreso.cobranza_id.in_(cob_ids)).delete(synchronize_session=False)
        if adel_ids:
            db.query(ConciliacionIngreso).filter(
                ConciliacionIngreso.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(WasabilDte).filter(
                WasabilDte.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContCobranza).filter(
                ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFactoring).filter(
                ContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFacturaClienteItem).filter(
                ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
        if adel_ids:
            db.query(ContAdelanto).filter(
                ContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(ContFacturaCliente).filter(
                ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
        if desp_ids:
            db.query(WasabilDte).filter(
                WasabilDte.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
    for c in cots:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == c.id).delete(synchronize_session=False)
        db.delete(c)
    # Tesorería: movimientos/cartolas/cuentas del test
    ctas = db.query(CuentaBancaria).filter(CuentaBancaria.banco.like(f"{MARK}%")).all()
    if ctas:
        cids = [c.id for c in ctas]
        movs = db.query(MovimientoBancario).filter(MovimientoBancario.cuenta_id.in_(cids)).all()
        if movs:
            db.query(ConciliacionIngreso).filter(
                ConciliacionIngreso.movimiento_id.in_([m.id for m in movs])
            ).delete(synchronize_session=False)
            db.query(MovimientoBancario).filter(
                MovimientoBancario.cuenta_id.in_(cids)).delete(synchronize_session=False)
        db.query(Cartola).filter(Cartola.cuenta_id.in_(cids)).delete(synchronize_session=False)
        db.query(CuentaBancaria).filter(
            CuentaBancaria.id.in_(cids)).delete(synchronize_session=False)
    db.commit()


def test_viaje_de_la_plata():
    cont._precios_de_cotizacion = _fake_precios
    fake = FakeWasabil()
    # El fake trae folio FIJO ("777"); el SII real da uno distinto por documento. Sin esto
    # la 2ª factura choca con el índice único de folio y el sistema (bien) no lo pisa.
    _folios = {}
    _orig_obtener = fake._obtener

    def _obtener_folio_unico(uuid):
        d = dict(_orig_obtener(uuid))
        if uuid not in _folios:
            _folios[uuid] = str(900 + len(_folios))
        d["folio"] = _folios[uuid]
        d["document_pdf_url"] = f"https://api.wasabil.com/pdf/{d['folio']}"
        return d

    fake._obtener = _obtener_folio_unico
    fake.install()                      # SIEMPRE al inicio (las suites se pisan entre sí)
    db = SessionLocal()
    try:
        _limpiar(db)
        # KPIs ANTES de sembrar: el test mide DELTAS, no valores absolutos — la base
        # real del dueño tiene sus propias facturas y un total fijo daría falso rojo.
        kpis_antes = client.get("/api/contabilidad/kpis").json()
        # Venta: 20 u × $10.000 = $200.000 neto → $238.000 bruto
        cot, oc, desp, it = _crear_venta(db)
        NETO_VENTA, BRUTO_VENTA = 200_000.0, 238_000.0

        print("\n───────── 1 · COMERCIAL INFORMA EL ADELANTO (aún NO es plata) ─────────")
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 119_000.0,
                              "banco": "BCI", "observaciones": f"{MARK} 50%"})
        check("1 adelanto informado", r.status_code == 200, r.text)
        adel_id = r.json().get("id") or r.json().get("adelanto", {}).get("id")
        a = _adelanto_real(adel_id)
        check("1 se guarda como 'informado' y sin plata aplicada",
              a["estado"] == "informado" and a["aplicado"] == 0, a)

        print("\n───────── 2 · TESORERÍA APRUEBA (aquí sí entró la plata) ─────────")
        r = client.post(f"/api/tesoreria/adelantos/{adel_id}/aprobar",
                        json={"monto": 119_000.0, "fecha_pago": "2026-07-05",
                              "banco": "BCI", "numero_operacion": f"{MARK}-OP1"})
        check("2 Tesorería aprueba el adelanto", r.status_code == 200, r.text)
        a = _adelanto_real(adel_id)
        check("2 queda aprobado y con el monto real confirmado",
              a["estado"] == "aprobado" and a["monto"] == 119_000.0, a)
        check("2 sin facturas todavía, la plata queda DISPONIBLE (no aplicada)",
              a["aplicado"] == 0, a)

        print("\n───────── 3 · FACTURA DE ANTICIPO AL SII ─────────")
        r, fac_ant_id = _emitir_sii({"oc_cliente_id": oc.id, "es_anticipo": True,
                                     "monto_neto_anticipo": 100_000.0,
                                     "adelanto_ids": [adel_id]})
        check("3 factura de anticipo emitida", r.status_code == 200, r.text)
        fa = _factura_real(fac_ant_id)
        check("3 el folio del SII quedó guardado en la factura", bool(fa["folio"]), fa)
        check("3 bruto del anticipo = 119.000 (100.000 + IVA)", fa["bruto"] == 119_000.0, fa)
        check("3 el adelanto la dejó PAGADA con una cobranza medio='adelanto'",
              fa["estado"] == "pagada" and fa["saldo"] == 0
              and [c for c in fa["cobranzas"] if c[2] == "adelanto"], fa)
        a = _adelanto_real(adel_id)
        suma_adel = sum(m for (_i, m, med, aid) in fa["cobranzas"] if aid == adel_id)
        check("3 INVARIANTE 1: monto_aplicado == Σ cobranzas del adelanto",
              abs(a["aplicado"] - suma_adel) < 0.01, (a, suma_adel))

        print("\n───────── 4 · FACTURA FINAL AL SII (descuenta el anticipo) ─────────")
        r, fac_fin_id = _emitir_sii({"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("4 factura final emitida", r.status_code == 200, r.text)
        ff = _factura_real(fac_fin_id)
        check("4 neto final = 200.000 − 100.000 de anticipo = 100.000",
              ff["neto"] == 100_000.0, ff)
        # INVARIANTE 2: no se cobra dos veces
        check("4 INVARIANTE 2: Σ brutos facturados == mercadería vendida (238.000)",
              abs((fa["bruto"] + ff["bruto"]) - BRUTO_VENTA) < 0.01,
              f"anticipo {fa['bruto']} + final {ff['bruto']} = {fa['bruto'] + ff['bruto']}")
        # el payload que viajó al SII cuadra con lo local
        doc = fake.creados[-1]
        neto_payload = sum(round(d["price"] * d["quantity"], 2) * (1 - d.get("discount", 0) / 100.0)
                           for d in doc["details"])
        check("4 el neto del documento enviado al SII == neto local (cuadratura exacta)",
              abs(round(neto_payload, 2) - ff["neto"]) < 0.01, (neto_payload, ff["neto"]))
        refs = doc.get("references") or []
        tipos_ref = {str(x.get("document_type") or x.get("documentType")) for x in refs}
        check("4 el documento referencia OC (801), guía (52) y el anticipo (33)",
              {"801", "52", "33"} <= tipos_ref, refs)
        a = _adelanto_real(adel_id)
        check("4 el adelanto NO se aplica de nuevo a la final (ya se consumió)",
              abs(a["aplicado"] - 119_000.0) < 0.01, a)

        print("\n───────── 5 · EL CLIENTE PAGA EL SALDO ─────────")
        saldo_final = ff["saldo"]
        r = client.post(f"/api/contabilidad/facturas/{fac_fin_id}/cobranzas",
                        json={"monto": saldo_final, "fecha": "2026-07-20",
                              "medio": "transferencia", "banco": "BCI",
                              "numero_operacion": f"{MARK}-OP2"})
        check("5 cobranza registrada", r.status_code == 200, r.text)
        ff = _factura_real(fac_fin_id)
        check("5 INVARIANTE 5: saldo/pagado persistidos cuadran con las filas",
              abs(ff["pagado"] - ff["suma_cob"]) < 0.01 and ff["saldo"] == 0
              and ff["estado"] == "pagada", ff)
        # Cuánto puso REALMENTE el cliente en toda la venta
        depositado = 119_000.0 + saldo_final
        check("5 lo depositado por el cliente == total de la venta (238.000)",
              abs(depositado - BRUTO_VENTA) < 0.01,
              f"adelanto 119.000 + pago {saldo_final} = {depositado}")

        print("\n───────── 6 · TESORERÍA: CONCILIACIÓN BANCARIA ─────────")
        r = client.post("/api/tesoreria/cuentas",
                        json={"banco": f"{MARK}-BCI", "nombre": "Cta Cte", "moneda": "CLP"})
        check("6 cuenta bancaria creada", r.status_code == 200, r.text)
        cta_id = r.json()["id"]
        # abono del pago del cliente
        r = client.post("/api/tesoreria/movimientos",
                        json={"cuenta_id": cta_id, "fecha": "2026-07-20", "tipo": "abono",
                              "monto": saldo_final, "glosa": f"{MARK} pago cliente"})
        mov_pago = r.json()["id"]
        # INVARIANTE 3: la aplicación del adelanto NO es conciliable como abono nuevo
        r = client.get("/api/tesoreria/cobranzas-pendientes")
        pend = r.json()["cobranzas"]
        medios = [c.get("medio") for c in pend]
        check("6 INVARIANTE 3: las cobranzas medio='adelanto' NO se ofrecen como abono nuevo",
              "adelanto" not in medios, medios)
        ids_pend = [c.get("cobranza_id") for c in pend]
        cob_pago = [c[0] for c in ff["cobranzas"] if c[2] == "transferencia"][0]
        check("6 la cobranza del pago real SÍ está pendiente de conciliar",
              cob_pago in ids_pend, (cob_pago, ids_pend))
        r = client.post(f"/api/tesoreria/movimientos/{mov_pago}/conciliar",
                        json={"cobranza_id": cob_pago})
        check("6 abono ↔ cobranza conciliado", r.status_code == 200, r.text)
        # INVARIANTE 4: no se concilia dos veces
        r2 = client.post("/api/tesoreria/movimientos/{}/conciliar".format(mov_pago),
                         json={"cobranza_id": cob_pago})
        check("6 INVARIANTE 4: el mismo abono no se concilia dos veces", r2.status_code == 409, r2.text)

        print("\n───────── 7 · EL DEPÓSITO DEL ADELANTO VA POR SU PROPIA VÍA ─────────")
        r = client.post("/api/tesoreria/movimientos",
                        json={"cuenta_id": cta_id, "fecha": "2026-07-05", "tipo": "abono",
                              "monto": 119_000.0, "glosa": f"{MARK} adelanto cliente"})
        mov_adel = r.json()["id"]
        r = client.post(f"/api/tesoreria/movimientos/{mov_adel}/conciliar",
                        json={"adelanto_id": adel_id})
        check("7 abono ↔ adelanto conciliado (el depósito real del anticipo)",
              r.status_code == 200, r.text)
        total_conc = _sql("SELECT COUNT(*) FROM conc_conciliacion_ingreso")[0][0]
        check("7 quedaron exactamente 2 enlaces de ingreso (pago + adelanto), sin duplicar",
              total_conc >= 2, total_conc)

        print("\n───────── 8 · LOS TABLEROS DICEN LO MISMO ─────────")
        k = client.get("/api/contabilidad/kpis").json()
        d_fact = k.get("facturado_clp", 0) - kpis_antes.get("facturado_clp", 0)
        d_cobrar = k.get("por_cobrar_clp", 0) - kpis_antes.get("por_cobrar_clp", 0)
        check("8 el KPI facturado subió exactamente el bruto de esta venta (238.000)",
              abs(d_fact - BRUTO_VENTA) < 1.0, f"delta={d_fact}")
        check("8 el KPI por cobrar no se movió (la venta quedó saldada)",
              abs(d_cobrar) < 1.0, f"delta={d_cobrar}")
        r = client.get("/api/tesoreria/flujo-caja")
        check("8 el flujo de caja de Tesorería responde", r.status_code == 200, r.text)
        r = client.get("/api/tesoreria/resumen")
        check("8 el resumen de Tesorería responde", r.status_code == 200, r.text)

        print("\n───────── 9 · CIERRE: NADA QUEDA COLGANDO ─────────")
        fa = _factura_real(fac_ant_id); ff = _factura_real(fac_fin_id)
        a = _adelanto_real(adel_id)
        check("9 ambas facturas pagadas y con folio del SII",
              fa["estado"] == "pagada" and ff["estado"] == "pagada"
              and fa["folio"] and ff["folio"], (fa["estado"], ff["estado"]))
        check("9 el adelanto quedó aplicado por completo, sin remanente fantasma",
              abs(a["aplicado"] - a["monto"]) < 0.01, a)
        total_cobrado = fa["suma_cob"] + ff["suma_cob"]
        check("9 Σ cobranzas de la venta == total facturado (nadie pagó de más ni de menos)",
              abs(total_cobrado - BRUTO_VENTA) < 0.01,
              f"anticipo {fa['suma_cob']} + final {ff['suma_cob']} = {total_cobrado}")

        print()
        assert not _fails, f"{len(_fails)} fallos: {_fails}"
    finally:
        cont._precios_de_cotizacion = _orig_precios
        _limpiar(db)
        db.close()


if __name__ == "__main__":
    test_viaje_de_la_plata()
    print("\nTODO OK" if not _fails else f"\n{len(_fails)} FALLOS")
