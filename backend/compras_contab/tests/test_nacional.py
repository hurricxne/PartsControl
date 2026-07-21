"""Test de integración de la COMPRA NACIONAL con detalle por ítem (cont_compra_item).

Cubre el costeo por ítem y sus 4 guards (doble costeo internacional/nacional, Σ líneas
≤ neto, Σ cantidad ≤ recibido), la imputación a Existencias (1.3.01), el IVA que NO
capitaliza, la anulación que libera el costeo, y el CIRCUITO CxP → Tesorería →
conciliación de punta a punta (por-pagar con vencimiento, egreso contado automático,
pago vía /tesoreria/pagos con anti sobre-pago, conciliación cargo↔egreso).

Monta compras_contab + tesoreria en una app efímera; siembra recepciones nacionales
CERRADAS directamente en la BD (el tope de cantidad costeable). LIMPIA todo al final.

Corre con:  ./venv/bin/python compras_contab/tests/test_nacional.py
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, OcProveedor, OcProveedorItem,
    Despacho, DespachoItem,
)
import embarques_pricing.models  # noqa: E402,F401  (emb_pricing_gasto: FK destino de cont_compra)
from embarques_pricing.models import EmbarquePricingItem  # noqa: E402
from compras_contab.models import ContCompra, ContEgreso, ContEgresoDetalle, ContCompraItem  # noqa: E402
from recepcion_nacional.models import RecepcionNacional, RecepcionNacionalItem  # noqa: E402
from tesoreria.models import CuentaBancaria, MovimientoBancario, Conciliacion  # noqa: E402
from compras_contab.router import router as cc_router  # noqa: E402
from tesoreria.router import router as tes_router  # noqa: E402
from recepcion_nacional.router import router as rn_router  # noqa: E402  (carreras G13)

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_CCNAC__"
CURRENT = {"empresa": "mineria", "id": None}

app = FastAPI()
app.include_router(cc_router, prefix="/api")
app.include_router(tes_router, prefix="/api")
app.include_router(rn_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _setup(db, cantidades=(10, 10), recibir=None):
    """cot + ítems + OC cliente + OC proveedor NACIONAL + asignaciones. `recibir` =
    dict {indice_item: qty} de recepción nacional CERRADA (default: recibe todo)."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} CLIENTE", rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = ItemCotizacion(cotizacion_id=cot.id, item_num=n, numero_parte=f"P-{MARK}-{n}",
                            descripcion=f"Parte {n}", cantidad=cant, estado_item="comprado")
        db.add(it); items.append(it)
    db.flush()
    occ = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OCC", fecha_oc="2026-07-18")
    db.add(occ); db.flush()
    ocp = OcProveedor(numero=f"{MARK}-OCP", numero_oc=f"{MARK}-PROV", proveedor=f"{MARK} PROV",
                      moneda="CLP", tipo_origen="nacional")
    db.add(ocp); db.flush()
    for it in items:
        db.add(OcProveedorItem(oc_proveedor_id=ocp.id, oc_cliente_id=occ.id,
                               item_cotizacion_id=it.id))
    # Recepción nacional CERRADA (siembra el recibido utilizable)
    recibir = {i: (cantidades[i]) for i in range(len(items))} if recibir is None else recibir
    if recibir:
        rec = RecepcionNacional(oc_proveedor_id=ocp.id, numero_guia_proveedor=f"{MARK}-G",
                                estado="cerrada")
        db.add(rec); db.flush()
        for idx, qty in recibir.items():
            it = items[idx]
            it.estado_item = "en_bodega"
            db.add(RecepcionNacionalItem(recepcion_id=rec.id, item_cotizacion_id=it.id,
                                         numero_parte=it.numero_parte, qty_recibida=qty,
                                         estado_recepcion="completo"))
    db.commit()
    for obj in [cot, occ, ocp] + items:
        db.refresh(obj)
    return cot, occ, ocp, items


def _crear(numero_doc, ocp_id, lineas, *, condicion="credito", plazo=None, monto_neto=None,
           afecto_iva=False, pago=None, fecha=None):
    """lineas: [(item_id, cantidad, precio_unit), ...]. monto_neto default = Σ líneas."""
    items = [{"item_cotizacion_id": i, "cantidad": c, "precio_unit": p} for i, c, p in lineas]
    neto = monto_neto if monto_neto is not None else sum(c * p for _i, c, p in lineas)
    body = {
        "tipo_gasto": "cogs", "origen": "NACIONAL", "moneda": "CLP",
        "numero_documento": numero_doc, "oc_proveedor_id": ocp_id,
        "condicion_pago": condicion, "monto_neto": neto, "afecto_iva": afecto_iva,
        "items": items,
    }
    if plazo is not None:
        body["plazo_dias"] = plazo
    if fecha:
        body["fecha"] = fecha
    if pago is not None:
        body["pago"] = pago
    return client.post("/api/compras-contab", json=body)


def _limpiar(db):
    db.rollback()
    # Cuentas bancarias de prueba (CASCADE borra movimientos + conciliaciones)
    for cta in db.query(CuentaBancaria).filter(CuentaBancaria.banco.like(f"{MARK}%")).all():
        db.delete(cta)
    db.flush()
    # Compras de prueba + sus egresos + cont_compra_item (cascade en la compra)
    compras = db.query(ContCompra).filter(ContCompra.numero_documento.like(f"{MARK}%")).all()
    compra_ids = [c.id for c in compras]
    egreso_ids = ({d.egreso_id for d in db.query(ContEgresoDetalle.egreso_id)
                   .filter(ContEgresoDetalle.compra_id.in_(compra_ids)).all()}
                  if compra_ids else set())
    # Conciliaciones que apunten a esos egresos (por si quedaron)
    if egreso_ids:
        db.query(Conciliacion).filter(
            Conciliacion.egreso_id.in_(egreso_ids)).delete(synchronize_session=False)
    for c in compras:
        db.delete(c)
    db.flush()
    for eid in egreso_ids:
        e = db.query(ContEgreso).filter(ContEgreso.id == eid).first()
        if e:
            db.delete(e)
    db.flush()
    # Escenario de ventas/compras
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    item_ids = ([i.id for i in db.query(ItemCotizacion)
                 .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all()] if cot_ids else [])
    ocps = db.query(OcProveedor).filter(OcProveedor.numero.like(f"{MARK}%")).all()
    ocp_ids = [o.id for o in ocps]
    occs = ([o.id for o in db.query(OcCliente)
             .filter(OcCliente.cotizacion_id.in_(cot_ids)).all()] if cot_ids else [])
    if ocp_ids:
        for rec in db.query(RecepcionNacional).filter(
                RecepcionNacional.oc_proveedor_id.in_(ocp_ids)).all():
            db.delete(rec)
        db.flush()
    if item_ids:
        db.query(EmbarquePricingItem).filter(
            EmbarquePricingItem.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=False)
    if ocp_ids:
        db.query(OcProveedorItem).filter(
            OcProveedorItem.oc_proveedor_id.in_(ocp_ids)).delete(synchronize_session=False)
        db.query(OcProveedor).filter(OcProveedor.id.in_(ocp_ids)).delete(synchronize_session=False)
    if occs:
        # Despachos sembrados por los escenarios G13 (orden FK: ítems → cabecera)
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(occs)).all()]
        if desp_ids:
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(occs)).delete(synchronize_session=False)
    if cot_ids:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.commit()


def run():
    db = SessionLocal()
    try:
        _limpiar(db)
        CURRENT["empresa"] = "mineria"

        # ═══ 1. Compra nacional CLP con detalle: tc=1, imputa 1.3.01 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-1", ocp.id, [(a.id, 6, 1000), (b.id, 10, 500)])
        check("nacional con detalle → 200", r.status_code == 200, r.text)
        cc = r.json()
        check("nacional imputa Existencias 1.3.01", cc.get("cuenta_codigo") == "1.3.01", cc.get("cuenta_codigo"))
        check("nacional guarda oc_proveedor_id", cc.get("oc_proveedor_id") == ocp.id, cc.get("oc_proveedor_id"))
        items = {it["item_cotizacion_id"]: it for it in cc.get("items", [])}
        check("nacional: 2 líneas costeadas", len(cc.get("items", [])) == 2, cc.get("items"))
        la = items.get(a.id)
        check("línea A: costo_unit_clp=1000 (tc=1)", la and la["costo_unit_clp"] == 1000.0, la)
        check("línea A: costo_total_clp=6000 (6×1000)", la and la["costo_total_clp"] == 6000.0, la)
        _limpiar(db)

        # ═══ 2. IVA no capitaliza: costo = NETO ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-IVA", ocp.id, [(a.id, 10, 1000)], monto_neto=10000, afecto_iva=True)
        cc = r.json()
        check("afecto_iva: iva=1900 (19%)", cc.get("iva") == 1900.0, cc.get("iva"))
        suma_costo = sum(it["costo_total_clp"] for it in cc.get("items", []))
        check("IVA no capitaliza: Σ costo_total_clp = neto 10000 (sin IVA)", suma_costo == 10000.0, suma_costo)
        _limpiar(db)

        # ═══ 3. Guard A — doble costeo internacional (emb_pricing_item) → 409 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        db.add(EmbarquePricingItem(pricing_id=None, item_cotizacion_id=a.id,
                                   numero_parte=a.numero_parte, costo_total_clp=999))
        db.commit()
        r = _crear(f"{MARK}-A", ocp.id, [(a.id, 5, 1000)])
        check("guard A: ítem con costo internacional → 409",
              r.status_code == 409 and "internacional" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 4. Guard C — costear sin recepción (recibido 0) → 409 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10), recibir={0: 10})  # b sin recibir
        r = _crear(f"{MARK}-C0", ocp.id, [(b.id, 1, 1000)])
        check("guard C: costear ítem sin recepción → 409",
              r.status_code == 409 and "recib" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 5. Guard C — Σ cantidad > recibido → 409 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-C1", ocp.id, [(a.id, 11, 1000)], monto_neto=11000)
        check("guard C: cantidad 11 > recibido 10 → 409", r.status_code == 409, r.text)
        _limpiar(db)

        # ═══ 6. Guard B — doble costeo nacional acumulado > recibido → 409 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r1 = _crear(f"{MARK}-B1", ocp.id, [(a.id, 6, 1000)])
        check("guard B: primera compra (6 de 10) → 200", r1.status_code == 200, r1.text)
        r2 = _crear(f"{MARK}-B2", ocp.id, [(a.id, 5, 1000)])  # 6+5=11 > 10
        check("guard B: segunda compra 6+5=11 > recibido 10 → 409", r2.status_code == 409, r2.text)
        _limpiar(db)

        # ═══ 7. Guard D — Σ líneas > neto → 400 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-D", ocp.id, [(a.id, 5, 1000)], monto_neto=4000)  # 5000 > 4000
        check("guard D: Σ líneas 5000 > neto 4000 → 400", r.status_code == 400, r.text)
        _limpiar(db)

        # ═══ 8. Ítem repetido en el detalle → 400 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-DUP", ocp.id, [(a.id, 3, 1000), (a.id, 2, 1000)], monto_neto=100000)
        check("ítem repetido en items → 400", r.status_code == 400, r.text)
        _limpiar(db)

        # ═══ 9. Cobertura PARCIAL: costear 8 de 10 → OK, quedan 2 sin costear ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-PARC", ocp.id, [(a.id, 8, 1000)], monto_neto=8000)
        check("cobertura parcial 8/10 → 200", r.status_code == 200, r.text)
        onac = client.get("/api/compras-contab/oc-nacionales").json()
        fila = None
        for oc in onac["ocs"]:
            if oc["oc_proveedor_id"] == ocp.id:
                fila = next((x for x in oc["items"] if x["item_cotizacion_id"] == a.id), None)
        check("parcial: disponible_costear = 2 (10 recibidas − 8 costeadas)",
              fila and fila["ya_costeado"] == 8 and fila["disponible_costear"] == 2, fila)
        _limpiar(db)

        # ═══ 10. Compra anulada LIBERA su costeo → re-costear en compra nueva ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r1 = _crear(f"{MARK}-AN1", ocp.id, [(a.id, 10, 1000)])
        cid = r1.json()["id"]
        check("anulada-libera: costeo inicial 10 → 200", r1.status_code == 200, r1.text)
        r2 = _crear(f"{MARK}-AN2", ocp.id, [(a.id, 5, 1000)])   # 10+5 > 10 → 409 mientras activa
        check("anulada-libera: mientras activa, re-costear → 409", r2.status_code == 409, r2.text)
        ra = client.post(f"/api/compras-contab/{cid}/anular", json={"motivo": "err"})
        check("anulada-libera: anular la compra → 200", ra.status_code == 200, ra.text)
        r3 = _crear(f"{MARK}-AN3", ocp.id, [(a.id, 10, 1000)])  # ahora libre
        check("anulada-libera: re-costear 10 tras anular → 200", r3.status_code == 200, r3.text)
        _limpiar(db)

        # ═══ 11. Circuito — crédito 30: aparece en /por-pagar con venc=fecha+30 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        hoy = date.today()
        r = _crear(f"{MARK}-CRED", ocp.id, [(a.id, 10, 1000)], condicion="credito",
                   plazo=30, fecha=hoy.isoformat())
        cc = r.json()
        check("crédito 30: venc = fecha+30", cc["fecha_vencimiento"] == (hoy + timedelta(days=30)).isoformat(),
              cc["fecha_vencimiento"])
        pp = client.get("/api/tesoreria/por-pagar", params={"q": f"{MARK}", "page_size": 200}).json()
        fila = next((x for x in pp["compras"] if x["compra_id"] == cc["id"]), None)
        check("crédito 30: aparece en Tesorería /por-pagar", fila is not None, pp.get("total"))
        check("crédito 30: /por-pagar trae el vencimiento",
              fila and fila["fecha_vencimiento"] == (hoy + timedelta(days=30)).isoformat(), fila)
        _limpiar(db)

        # ═══ 12. Circuito — contado: egreso automático el mismo día (pagado) ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-CONT", ocp.id, [(a.id, 10, 1000)], condicion="contado")
        cc = r.json()
        check("contado: estado pagado + saldo 0",
              cc["estado_pago"] == "pagado" and cc["saldo_clp"] == 0, cc)
        check("contado: 1 pago (egreso automático)", len(cc.get("pagos", [])) == 1, cc.get("pagos"))
        _limpiar(db)

        # ═══ 13. Circuito — pago vía /tesoreria/pagos + anti sobre-pago ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-PAG", ocp.id, [(a.id, 10, 1000)], condicion="credito", plazo=30)
        cc = r.json(); total = cc["monto_total_clp"]
        rp = client.post("/api/tesoreria/pagos", json={
            "medio": "transferencia", "detalles": [{"compra_id": cc["id"], "monto_clp": total}]})
        check("pago vía Tesorería (reusa _crear_egreso) → 200", rp.status_code == 200, rp.text)
        det = client.get(f"/api/compras-contab/{cc['id']}").json()
        check("tras pago Tesorería: compra pagada", det["estado_pago"] == "pagado", det["estado_pago"])
        ro = client.post("/api/tesoreria/pagos", json={
            "detalles": [{"compra_id": cc["id"], "monto_clp": 100}]})  # saldo 0 → sobre-pago
        check("sobre-pago vía Tesorería → 400", ro.status_code == 400, ro.text)
        _limpiar(db)

        # ═══ 14. Circuito — conciliación cargo↔egreso de la compra nacional ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-CONC", ocp.id, [(a.id, 10, 1000)], condicion="contado")
        cc = r.json(); total = cc["monto_total_clp"]
        # egreso automático del contado
        db.rollback()
        egd = db.query(ContEgresoDetalle).filter(ContEgresoDetalle.compra_id == cc["id"]).first()
        egreso_id = egd.egreso_id if egd else None
        rc = client.post("/api/tesoreria/cuentas", json={"banco": f"{MARK} Banco", "moneda": "CLP"})
        cuenta_id = rc.json()["id"]
        rm = client.post("/api/tesoreria/movimientos", json={
            "cuenta_id": cuenta_id, "tipo": "cargo", "monto": total,
            "glosa": f"{MARK} cargo", "fecha": date.today().isoformat()})
        mov_id = rm.json()["id"]
        rcon = client.post(f"/api/tesoreria/movimientos/{mov_id}/conciliar",
                           json={"egreso_id": egreso_id})
        check("conciliar cargo↔egreso nacional → 200", rcon.status_code == 200, rcon.text)
        db.rollback()
        eg = db.query(ContEgreso).filter(ContEgreso.id == egreso_id).first()
        check("egreso de compra nacional queda conciliado", eg and bool(eg.conciliado), eg and eg.conciliado)
        _limpiar(db)

        # ═══ 15. Anular compra nacional CON pagos → 409 ═══
        cot, occ, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-ANP", ocp.id, [(a.id, 10, 1000)], condicion="contado")
        cid = r.json()["id"]
        ra = client.post(f"/api/compras-contab/{cid}/anular", json={"motivo": "x"})
        check("anular compra nacional con pagos → 409", ra.status_code == 409, ra.text)
        _limpiar(db)

        # ═══ 16. G13: carrera costear ‖ anular recepción — jamás Σ costeado > recibido ═══
        # (write-skew cerrado: ambos caminos se serializan en el lock de ItemCotizacion
        # y los guards leen con lecturas BLOQUEANTES; antes 60/60 carreras inconsistentes)
        import threading
        inconsistencias = 0
        for _ronda in range(6):
            cot, occ, ocp, (a,) = _setup(db, (10,))
            db.rollback()
            rec_row = (db.query(RecepcionNacional)
                       .filter(RecepcionNacional.oc_proveedor_id == ocp.id).first())
            resultados = {}

            def _costear():
                resultados["c"] = _crear(f"{MARK}-RACE", ocp.id, [(a.id, 10, 1000)]).status_code

            def _anular():
                resultados["a"] = client.delete(
                    f"/api/recepcion-nacional/{rec_row.id}").status_code

            t1 = threading.Thread(target=_costear)
            t2 = threading.Thread(target=_anular)
            t1.start(); t2.start(); t1.join(); t2.join()
            db.rollback()
            costeado = sum(float(c or 0) for (c,) in db.query(ContCompraItem.cantidad)
                           .join(ContCompra, ContCompra.id == ContCompraItem.compra_id)
                           .filter(ContCompraItem.item_cotizacion_id == a.id,
                                   ContCompra.anulado.is_(False)).all())
            recibido = sum(float(q or 0) for (q,) in db.query(RecepcionNacionalItem.qty_recibida)
                           .join(RecepcionNacional,
                                 RecepcionNacional.id == RecepcionNacionalItem.recepcion_id)
                           .filter(RecepcionNacionalItem.item_cotizacion_id == a.id,
                                   RecepcionNacional.estado == "cerrada").all())
            if costeado > recibido + 0.001:
                inconsistencias += 1
            _limpiar(db)
        check("G13 carrera costear‖anular ×6: Σ costeado nunca supera recibido",
              inconsistencias == 0, f"{inconsistencias} rondas inconsistentes")

        # ═══ 17. G13: 2 compras concurrentes de ítems DISTINTOS — sin 500 (retry deadlock) ═══
        # (los gap locks de InnoDB deadlockeaban ~50% de las rondas; el retry 1213 lo absorbe)
        con_500 = 0
        for _ronda in range(6):
            cot, occ, ocp, (a, b) = _setup(db, (10, 10))
            codigos = []

            def _c1():
                codigos.append(_crear(f"{MARK}-DL1", ocp.id, [(a.id, 5, 1000)]).status_code)

            def _c2():
                codigos.append(_crear(f"{MARK}-DL2", ocp.id, [(b.id, 5, 1000)]).status_code)

            t1 = threading.Thread(target=_c1)
            t2 = threading.Thread(target=_c2)
            t1.start(); t2.start(); t1.join(); t2.join()
            con_500 += sum(1 for s in codigos if s >= 500)
            _limpiar(db)
        check("G13 compras concurrentes ítems distintos ×6: ningún 500 (retry deadlock)",
              con_500 == 0, f"{con_500} respuestas 500")

        # ═══ 18. G13: cerrar recepción TARDÍA no revierte 'despachado' → 'en_bodega' ═══
        cot, occ, ocp, (a,) = _setup(db, (10,))
        r2 = client.post("/api/recepcion-nacional", json={
            "oc_proveedor_id": ocp.id, "numero_guia_proveedor": f"{MARK}-G-TARDIA",
            "cerrar": False,
            "items": [{"item_cotizacion_id": a.id, "qty_recibida": 2,
                       "estado_recepcion": "sobrante"}],
        })
        rec2_id = r2.json()["id"]
        db.rollback()
        # El ítem ya se despachó completo (siembra directa: despacho cerrado + estado)
        desp = Despacho(numero_despacho=f"{MARK}-DSP", oc_cliente_id=occ.id,
                        estado="despachado", guia_firmada=0)
        db.add(desp); db.flush()
        db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=a.id, qty_despachada=10))
        item_row = db.query(ItemCotizacion).filter(ItemCotizacion.id == a.id).first()
        item_row.estado_item = "despachado"
        db.commit()
        rc = client.post(f"/api/recepcion-nacional/{rec2_id}/cerrar")
        check("cerrar recepción tardía 200", rc.status_code == 200, rc.text)
        db.rollback()
        estado_final = db.query(ItemCotizacion.estado_item).filter(
            ItemCotizacion.id == a.id).scalar()
        check("G13 el ítem despachado NO revierte a en_bodega",
              estado_final == "despachado", estado_final)
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_compras_contab_nacional():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
