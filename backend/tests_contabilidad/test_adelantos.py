"""Tests de ADELANTOS de cliente (vía A: sin factura de anticipo) + Tesorería.

Cubre el ciclo completo: Comercial INFORMA (por cotizacion_id, como el Cierre de
Venta) → Tesorería APRUEBA (sin cartola) → conciliación abono↔adelanto → al emitir
la factura del despacho real se APLICA como cobranza medio='adelanto' (cap por saldo,
remanente a la siguiente factura) → reversión devuelve monto_aplicado. Además: topes
(Σ adelantos ≤ total venta), guardas (medio='adelanto' manual 400, anular aplicado
409, re-aprobar aplicado 409), exclusión de cobranzas 'adelanto' de la conciliación
de ingresos, y KPIs de Tesorería (resumen + flujo de caja).

Monta los routers en una app efímera (sin tocar main.py), simula auth minería, y
LIMPIA todo al terminar. Corre con:
    ./venv/bin/python -m pytest tests_contabilidad/test_adelantos.py -q
(también:  ./venv/bin/python tests_contabilidad/test_adelantos.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContAdelanto,
    ContFactoring,
)
from tesoreria.models import (  # noqa: E402
    CuentaBancaria, Cartola, MovimientoBancario, ConciliacionIngreso,
)
import routers.contabilidad as cont  # noqa: E402
from tesoreria.router import router as tes_router  # noqa: E402

MARK = "__TEST_ADEL__"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(cont.router, prefix="/api")
app.include_router(tes_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

# Precios deterministas (el pricing engine tiene sus propios tests). El total de la
# venta (tope de adelantos) sale de estos mismos precios, IVA half-up incluido.
PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0)} for i in items}
    neto = sum(cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0)) for i in items)
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear_venta(db, *, precio=10000.0, cantidad=10, despachar=None):
    """Venta simple de 1 ítem (total neto = precio×cantidad) con guía firmada por
    `despachar` unidades (default: todas)."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} HEPI", rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro", cantidad=cantidad, estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    qty = cantidad if despachar is None else despachar
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    estado="despachado", guia_firmada=1, numero_guia="G-TEST")
    db.add(desp); db.flush()
    db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id, qty_despachada=qty))
    db.commit()
    PRECIOS.clear()
    PRECIOS.update({it.id: precio})
    return cot, oc, desp, it


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    oc_ids = [oc.id for oc in db.query(OcCliente)
              .filter(OcCliente.cotizacion_id.in_([c.id for c in cots])).all()] if cots else []
    if oc_ids:
        adel_ids = [a.id for a in db.query(ContAdelanto)
                    .filter(ContAdelanto.oc_cliente_id.in_(oc_ids)).all()]
        fac_ids = [f.id for f in db.query(ContFacturaCliente)
                   .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
        # Orden FK-seguro: enlaces de conciliación → cobranzas (FK a adelanto) →
        # líneas de factura → adelantos (FK a factura de anticipo) → facturas →
        # despacho items → despachos → OCs.
        if adel_ids:
            db.query(ConciliacionIngreso).filter(
                ConciliacionIngreso.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            cob_ids = [c.id for c in db.query(ContCobranza)
                       .filter(ContCobranza.factura_id.in_(fac_ids)).all()]
            if cob_ids:
                db.query(ConciliacionIngreso).filter(
                    ConciliacionIngreso.cobranza_id.in_(cob_ids)).delete(synchronize_session=False)
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
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
    for cot in cots:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == cot.id).delete(synchronize_session=False)
    if cots:
        db.query(Cotizacion).filter(
            Cotizacion.id.in_([c.id for c in cots])).delete(synchronize_session=False)
    # Infra bancaria del test (cuentas/cartolas/movimientos MARK)
    ctas = db.query(CuentaBancaria).filter(CuentaBancaria.nombre.like(f"{MARK}%")).all()
    for cta in ctas:
        mov_ids = [m.id for m in db.query(MovimientoBancario)
                   .filter(MovimientoBancario.cuenta_id == cta.id).all()]
        if mov_ids:
            db.query(ConciliacionIngreso).filter(
                ConciliacionIngreso.movimiento_id.in_(mov_ids)).delete(synchronize_session=False)
            db.query(MovimientoBancario).filter(
                MovimientoBancario.id.in_(mov_ids)).delete(synchronize_session=False)
        db.query(Cartola).filter(Cartola.cuenta_id == cta.id).delete(synchronize_session=False)
        db.delete(cta)
    db.commit()


def _mov_abono(db, monto, fecha="2026-07-10"):
    cta = db.query(CuentaBancaria).filter(CuentaBancaria.nombre == f"{MARK}-CTA").first()
    if not cta:
        cta = CuentaBancaria(empresa="mineria", banco="Santander", nombre=f"{MARK}-CTA")
        db.add(cta); db.flush()
    mov = MovimientoBancario(empresa="mineria", cuenta_id=cta.id, tipo="abono",
                             monto=monto, fecha=cont._parse_date(fecha),
                             glosa=f"{MARK} transferencia recibida")
    db.add(mov)
    db.commit()
    db.refresh(mov)
    return mov


def run():
    cont._precios_de_cotizacion = _fake_precios
    db = SessionLocal()
    _limpiar(db)
    try:
        CURRENT["empresa"] = "mineria"

        # BASELINE de los KPIs globales de Tesorería: la BD local puede tener
        # adelantos reales de otras ventas — los checks de resumen/flujo miden
        # el DELTA que produce esta suite, no el total absoluto.
        base_res = client.get("/api/tesoreria/resumen").json()
        base_fj = client.get("/api/tesoreria/flujo-caja").json()

        # ═══ Informar (Comercial) ═══
        # Venta: 10 × $10.000 = $100.000 neto → $119.000 bruto
        cot, oc, desp, it = _crear_venta(db)
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"cotizacion_id": cot.id, "pct": 50})
        check("informar por cotizacion_id 200", r.status_code == 200, r.text)
        a = r.json()
        check("informado deriva monto del pct (50% de 119.000)",
              a["estado"] == "informado" and a["monto_esperado"] == 59500, a)
        adelanto_id = a["id"]

        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 70000})
        check("segundo adelanto que excede el total → 400",
              r.status_code == 400 and "excede" in r.json()["detail"].lower(), r.text)

        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 30000})
        check("segundo adelanto dentro del tope 200", r.status_code == 200, r.text)
        adelanto2_id = r.json()["id"]

        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        check("listar adelantos de la venta", r.status_code == 200 and len(r.json()) == 2, r.text)

        # ═══ Aprobar (Tesorería, sin cartola) ═══
        r = client.get("/api/tesoreria/aprobaciones")
        check("cola por_aprobar con los 2 informados",
              r.status_code == 200 and len(r.json()["por_aprobar"]) == 2, r.text)

        r = client.post(f"/api/tesoreria/adelantos/{adelanto_id}/aprobar",
                        json={"monto": 59500, "fecha_pago": "2026-07-10",
                              "banco": "Santander", "numero_operacion": "OP-1"})
        check("aprobar 200 (sin cartola subida)", r.status_code == 200, r.text)
        a = r.json()
        check("aprobado sin facturas: nada aplicado aún",
              a["estado"] == "aprobado" and a["monto"] == 59500
              and a["monto_aplicado"] == 0 and a["aplicado_ahora_clp"] == 0, a)

        # KPIs (DELTA vs baseline): +1 informado en cola, +1 aprobado sin conciliar
        r = client.get("/api/tesoreria/resumen")
        k = r.json()
        check("resumen: adelantos_por_aprobar +1 y sin_conciliar +1",
              k["adelantos_por_aprobar"] == base_res["adelantos_por_aprobar"] + 1
              and k["adelantos_sin_conciliar"] == base_res["adelantos_sin_conciliar"] + 1, k)
        r = client.get("/api/tesoreria/flujo-caja")
        fj = r.json()
        check("flujo de caja: informados aparte + recibidos sin aplicar (delta)",
              fj["adelantos_por_aprobar"]["monto"]
              == base_fj["adelantos_por_aprobar"]["monto"] + 30000
              and fj["adelantos_recibidos_sin_aplicar"]["monto"]
              == base_fj["adelantos_recibidos_sin_aplicar"]["monto"] + 59500, fj)

        # ═══ Conciliación abono ↔ adelanto ═══
        mov = _mov_abono(db, 59500)
        r = client.get(f"/api/tesoreria/movimientos/{mov.id}/sugerencias")
        sugs = r.json()["sugerencias"]
        check("sugerencia clase 'adelanto' para el abono",
              any(s["clase"] == "adelanto" and s["adelanto_id"] == adelanto_id for s in sugs), sugs)

        r = client.post(f"/api/tesoreria/movimientos/{mov.id}/conciliar",
                        json={"adelanto_id": adelanto2_id})
        check("conciliar adelanto NO aprobado → 409", r.status_code == 409, r.text)
        r = client.post(f"/api/tesoreria/movimientos/{mov.id}/conciliar",
                        json={"adelanto_id": adelanto_id})
        check("conciliar abono↔adelanto 200",
              r.status_code == 200 and r.json()["conciliado"] is True, r.text)
        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        check("conciliado_banco derivado en contabilidad",
              [x for x in r.json() if x["id"] == adelanto_id][0]["conciliado_banco"] is True, r.text)

        # aprobado+conciliado ya no se puede re-aprobar ni anular
        r = client.post(f"/api/tesoreria/adelantos/{adelanto_id}/aprobar", json={"monto": 1000})
        check("re-aprobar conciliado → 409", r.status_code == 409, r.text)
        r = client.post(f"/api/contabilidad/adelantos/{adelanto_id}/anular")
        check("anular conciliado → 409", r.status_code == 409, r.text)

        # ═══ Aplicación al emitir la factura del despacho real ═══
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-F1", "plazo_dias": 30})
        check("emitir factura 200", r.status_code == 200, r.text)
        f1 = r.json()
        cobs = [c for c in f1["cobranzas"] if c["es_adelanto"]]
        check("cobranza 'adelanto' automática por el monto del adelanto",
              len(cobs) == 1 and cobs[0]["monto"] == 59500
              and cobs[0]["adelanto_id"] == adelanto_id, f1["cobranzas"])
        check("factura nace con el adelanto descontado del saldo",
              f1["monto_bruto"] == 119000 and f1["saldo"] == 59500
              and f1["estado_pago"] == "parcial", f1)
        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        a1 = [x for x in r.json() if x["id"] == adelanto_id][0]
        check("monto_aplicado == monto (todo aplicado)",
              a1["monto_aplicado"] == 59500 and a1["pendiente_aplicar"] == 0, a1)

        # cobranza manual con medio='adelanto' → 400 (solo el sistema la genera)
        r = client.post(f"/api/contabilidad/facturas/{f1['id']}/cobranzas",
                        json={"monto": 1000, "medio": "adelanto"})
        check("cobranza manual medio='adelanto' → 400", r.status_code == 400, r.text)

        # la cobranza 'adelanto' NO aparece en la conciliación de ingresos
        mov2 = _mov_abono(db, 59500)
        r = client.get(f"/api/tesoreria/movimientos/{mov2.id}/sugerencias")
        sugs = r.json()["sugerencias"]
        check("cobranza 'adelanto' excluida de sugerencias",
              not any(s.get("clase") == "cobranza" for s in sugs), sugs)
        cob_adel_id = cobs[0]["id"]
        r = client.post(f"/api/tesoreria/movimientos/{mov2.id}/conciliar",
                        json={"cobranza_id": cob_adel_id})
        check("conciliar cobranza 'adelanto' → 409", r.status_code == 409, r.text)
        r = client.get("/api/tesoreria/cobranzas-pendientes")
        check("cobranza 'adelanto' fuera de pendientes",
              all(c["cobranza_id"] != cob_adel_id for c in r.json()["cobranzas"]), r.text)

        # ═══ Reversión: eliminar la cobranza 'adelanto' devuelve el monto ═══
        r = client.delete(f"/api/contabilidad/facturas/{f1['id']}/cobranzas/{cob_adel_id}")
        check("revertir cobranza 'adelanto' 200", r.status_code == 200, r.text)
        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        a1 = [x for x in r.json() if x["id"] == adelanto_id][0]
        check("reversión devuelve monto_aplicado a 0",
              a1["monto_aplicado"] == 0 and a1["pendiente_aplicar"] == 59500, a1)

        # ═══ Aprobar con factura YA emitida → aplica al tiro (cap por saldo) ═══
        # Se aprueba el 2° adelanto ($30.000). Además, el 1° quedó APROBADO con saldo
        # pendiente tras la reversión → la pasada de aplicación lo re-aplica también
        # (regla: un adelanto aprobado siempre termina aplicado si hay factura con
        # saldo). Total aplicado ahora: 59.500 + 30.000 = 89.500.
        r = client.post(f"/api/tesoreria/adelantos/{adelanto2_id}/aprobar",
                        json={"monto": 30000, "fecha_pago": "2026-07-12"})
        check("aprobar con factura existente aplica al tiro (incluye el re-pendiente)",
              r.status_code == 200 and r.json()["aplicado_ahora_clp"] == 89500, r.text)
        r = client.get("/api/contabilidad/facturas")
        f1n = [x for x in r.json()["facturas"] if x["id"] == f1["id"]][0]
        check("saldo refleja ambos adelantos aplicados",
              f1n["saldo"] == 29500 and f1n["monto_pagado"] == 89500, f1n)

        # ═══ Guard de factoring: con factoring VIGENTE el adelanto NO se aplica ═══
        # (mismo criterio que registrar_cobranza: el saldo de una factura factorizada
        # es la retención del factor, no deuda del cliente)
        r = client.post(f"/api/contabilidad/facturas/{f1['id']}/factoring",
                        json={"empresa_factoring": f"{MARK} FACTOR",
                              "monto_adelantado": 20000})
        check("factoring sobre la factura 200", r.status_code == 200, r.text)
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 20000})
        adelanto3_id = r.json()["id"]
        r = client.post(f"/api/tesoreria/adelantos/{adelanto3_id}/aprobar",
                        json={"monto": 20000, "fecha_pago": "2026-07-13"})
        check("aprobar con factoring vigente: NO aplica",
              r.status_code == 200 and r.json()["aplicado_ahora_clp"] == 0, r.text)
        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        a3 = [x for x in r.json() if x["id"] == adelanto3_id][0]
        check("adelanto queda pendiente completo (guard de factoring)",
              a3["monto_aplicado"] == 0 and a3["pendiente_aplicar"] == 20000, a3)

        # anular un adelanto aplicado → 409
        r = client.post(f"/api/contabilidad/adelantos/{adelanto2_id}/anular")
        check("anular aplicado → 409", r.status_code == 409, r.text)

        _limpiar(db)

        # ═══ Remanente: adelanto > 1ª factura parcial → resto a la 2ª ═══
        # Venta 10×10.000; solo 4 despachadas → F1 bruto 47.600. Adelanto 59.500.
        cot, oc, desp, it = _crear_venta(db, despachar=4)
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 59500})
        adel_id = r.json()["id"]
        client.post(f"/api/tesoreria/adelantos/{adel_id}/aprobar", json={"monto": 59500})
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-F2"})
        f = r.json()
        check("1ª factura parcial queda PAGADA por el adelanto (cap por su bruto)",
              f["monto_bruto"] == 47600 and f["saldo"] == 0 and f["estado_pago"] == "pagada", f)
        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        a1 = r.json()[0]
        check("remanente del adelanto queda pendiente de aplicar",
              a1["monto_aplicado"] == 47600 and a1["pendiente_aplicar"] == 11900, a1)
        # 2ª guía por el resto (6) → el remanente se aplica a la 2ª factura
        desp2 = Despacho(numero_despacho=f"{MARK}-DSP2-{oc.id}", oc_cliente_id=oc.id,
                         estado="despachado", guia_firmada=1, numero_guia="G-TEST2")
        db.add(desp2); db.flush()
        db.add(DespachoItem(despacho_id=desp2.id, item_cotizacion_id=it.id, qty_despachada=6))
        db.commit()
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp2.id,
                              "numero_factura": f"{MARK}-F3"})
        f = r.json()
        check("2ª factura recibe el remanente (11.900 de 71.400)",
              f["monto_bruto"] == 71400 and f["saldo"] == 59500, f)
        _limpiar(db)

        # ═══ Candado de empresa ═══
        cot, oc, desp, it = _crear_venta(db)
        CURRENT["empresa"] = "automotriz"
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 1000})
        check("candado automotriz contabilidad 403", r.status_code == 403, r.text)
        r = client.get("/api/tesoreria/aprobaciones")
        check("candado automotriz tesorería 403", r.status_code == 403, r.text)
        CURRENT["empresa"] = "mineria"
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        cont._precios_de_cotizacion = _orig_precios
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_adelantos_integration():
    run()


if __name__ == "__main__":
    run()
