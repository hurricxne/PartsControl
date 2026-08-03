"""Test de integración de la COMPRA NACIONAL Monza con detalle por ítem
(monza_cont_compra_item). Espejo de compras_contab/tests/test_nacional.py (GA).

Cubre el costeo por ítem y sus guards (doble costeo internacional/nacional, Σ líneas
≤ neto, Σ cantidad ≤ recibido, pertenencia vía oc_proveedor_id directo — adaptación
Monza sin tabla de asignación), la imputación a Existencias (1.3.01), el IVA que NO
capitaliza, la anulación que libera el costeo, y el CIRCUITO CxP → Tesorería →
conciliación de punta a punta (por-pagar con vencimiento, egreso contado automático,
pago vía /tesoreria/pagos con anti sobre-pago, conciliación cargo↔egreso), más las
carreras G13 (costear ‖ anular recepción; compras concurrentes sin 500; cierre tardío
que no revierte 'despachado').

Monta monza_compras_contab + monza_tesoreria + monza_recepcion_nacional en una app
efímera (sin tocar main.py); siembra recepciones nacionales CERRADAS directamente en
la BD (el tope de cantidad costeable). LIMPIA todo al final (deja la BD intacta).

Corre con:  cd backend && ./venv/bin/python monza_compras_contab/tests/test_nacional.py
"""
import os
import sys
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, Base, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
    MonzaDespacho, MonzaDespachoItem,
)
from monza_embarques_pricing.models import MonzaEmbPricingItem  # noqa: E402
from monza_compras_contab.models import (  # noqa: E402
    MonzaContCompra, MonzaContEgreso, MonzaContEgresoDetalle, MonzaContCompraItem,
)
from monza_recepcion_nacional.models import (  # noqa: E402
    MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
)
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesCuentaBancaria, MonzaTesConciliacion,
)
from monza_compras_contab.router import router as mcc_router  # noqa: E402
from monza_tesoreria.router import router as tes_router  # noqa: E402
from monza_recepcion_nacional.router import router as mrn_router  # noqa: E402  (carreras G13)

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_MCCNAC__"
CURRENT = {"empresa": "automotriz", "id": None}

app = FastAPI()
# Los routers monza ya traen su prefijo /api/monza/... — se montan SIN prefix.
app.include_router(mcc_router)
app.include_router(tes_router)
app.include_router(mrn_router)


# Auth REALISTA: además de devolver el usuario, hace una lectura en la MISMA sesión
# del request, igual que auth.get_current_user en producción. Ese SELECT abre el
# read view de MySQL (REPEATABLE READ) ANTES de cualquier with_for_update(), que es
# la condición real bajo la que corren los endpoints. Con un lambda "seco", el lock
# terminaba siendo la PRIMERA sentencia y el snapshot nacía DESPUÉS del lock: las
# carreras de plata quedaban invisibles para los tests (lección G13).
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _setup(db, cantidades=(10, 10), recibir=None):
    """Cliente + cotización + ítems + OC proveedor NACIONAL (vínculo directo
    oc_proveedor_id — adaptación Monza). `recibir` = dict {indice_item: qty} de
    recepción nacional CERRADA (default: recibe todo)."""
    suf = uuid.uuid4().hex[:6].upper()
    cli = MonzaCliente(nombre=MARK)
    db.add(cli); db.flush()
    # numero corto (la columna es String(20)); el ancla de limpieza es el cliente MARK.
    cot = MonzaCotizacion(numero=f"CT-MCN-{suf}", cliente_id=cli.id, estado="vendida")
    db.add(cot); db.flush()
    ocp = MonzaOcProveedor(numero=f"{MARK}-OCP-{suf}", numero_oc=f"{MARK}-PROV-DOC",
                           proveedor_nombre=f"{MARK} PROV", moneda="CLP",
                           tipo_origen="nacional")
    db.add(ocp); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = MonzaCotizacionItem(cotizacion_id=cot.id,
                                 numero_parte=f"P-{MARK}-{n}", descripcion=f"Parte {n}",
                                 cantidad=cant, estado_linea="comprado",
                                 oc_proveedor_id=ocp.id)
        db.add(it); items.append(it)
    db.flush()
    # Recepción nacional CERRADA (siembra el recibido utilizable)
    recibir = {i: (cantidades[i]) for i in range(len(items))} if recibir is None else recibir
    if recibir:
        rec = MonzaRecepcionNacional(oc_proveedor_id=ocp.id,
                                     numero_guia_proveedor=f"{MARK}-G", estado="cerrada")
        db.add(rec); db.flush()
        for idx, qty in recibir.items():
            it = items[idx]
            it.estado_linea = "en_bodega"
            db.add(MonzaRecepcionNacionalItem(recepcion_id=rec.id, item_cotizacion_id=it.id,
                                              numero_parte=it.numero_parte, qty_recibida=qty,
                                              estado_recepcion="completo"))
    db.commit()
    for obj in [cot, ocp] + items:
        db.refresh(obj)
    return cot, ocp, items


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
    return client.post("/api/monza/compras-contab", json=body)


def _limpiar(db):
    db.rollback()
    # Cuentas bancarias de prueba (cascade ORM borra movimientos + conciliaciones)
    for cta in db.query(MonzaTesCuentaBancaria).filter(
            MonzaTesCuentaBancaria.banco.like(f"{MARK}%")).all():
        db.delete(cta)
    db.flush()
    # Compras de prueba + sus egresos + monza_cont_compra_item (cascade en la compra)
    compras = db.query(MonzaContCompra).filter(
        MonzaContCompra.numero_documento.like(f"{MARK}%")).all()
    compra_ids = [c.id for c in compras]
    egreso_ids = ({d.egreso_id for d in db.query(MonzaContEgresoDetalle)
                   .filter(MonzaContEgresoDetalle.compra_id.in_(compra_ids)).all()}
                  if compra_ids else set())
    # Conciliaciones que apunten a esos egresos (por si quedaron sin cuenta MARK)
    if egreso_ids:
        db.query(MonzaTesConciliacion).filter(
            MonzaTesConciliacion.egreso_id.in_(egreso_ids)).delete(synchronize_session=False)
    for c in compras:
        db.delete(c)
    db.flush()
    for eid in egreso_ids:
        e = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == eid).first()
        if e:
            db.delete(e)
    db.flush()
    # Escenario de ventas/compras. Ancla por el cliente MARK (el numero de la
    # cotización es corto, no cabe la marca — patrón test_bodega_despachos_flujo).
    cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
               .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
               .filter(MonzaCliente.nombre == MARK).all()]
    item_ids = ([i.id for i in db.query(MonzaCotizacionItem)
                 .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).all()]
                if cot_ids else [])
    ocps = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.numero.like(f"{MARK}%")).all()
    ocp_ids = [o.id for o in ocps]
    if ocp_ids:
        for rec in db.query(MonzaRecepcionNacional).filter(
                MonzaRecepcionNacional.oc_proveedor_id.in_(ocp_ids)).all():
            db.delete(rec)   # CASCADE borra sus líneas
        db.flush()
    if item_ids:
        db.query(MonzaEmbPricingItem).filter(
            MonzaEmbPricingItem.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=False)
    # Despachos sembrados por el escenario G13 (orden FK: ítems → cabecera)
    desp_ids = [d.id for d in db.query(MonzaDespacho)
                .filter(MonzaDespacho.numero.like(f"{MARK}%")).all()]
    if desp_ids:
        db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.id.in_(desp_ids)).delete(synchronize_session=False)
    if ocp_ids:
        db.query(MonzaOcProveedor).filter(
            MonzaOcProveedor.id.in_(ocp_ids)).delete(synchronize_session=False)
    if cot_ids:
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=False)
    db.commit()


def run():
    db = SessionLocal()
    try:
        _limpiar(db)
        CURRENT["empresa"] = "automotriz"

        # ═══ 1. Compra nacional CLP con detalle: tc=1, imputa 1.3.01 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
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
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-IVA", ocp.id, [(a.id, 10, 1000)], monto_neto=10000, afecto_iva=True)
        cc = r.json()
        check("afecto_iva: iva=1900 (19%)", cc.get("iva") == 1900.0, cc.get("iva"))
        suma_costo = sum(it["costo_total_clp"] for it in cc.get("items", []))
        check("IVA no capitaliza: Σ costo_total_clp = neto 10000 (sin IVA)", suma_costo == 10000.0, suma_costo)
        _limpiar(db)

        # ═══ 3. Guard A — doble costeo internacional (monza_emb_pricing_item) → 409 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        db.add(MonzaEmbPricingItem(pricing_id=None, item_cotizacion_id=a.id,
                                   numero_parte=a.numero_parte, costo_total_clp=999))
        db.commit()
        r = _crear(f"{MARK}-A", ocp.id, [(a.id, 5, 1000)])
        check("guard A: ítem con costo internacional → 409",
              r.status_code == 409 and "internacional" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 4. Guard C — costear sin recepción (recibido 0) → 409 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10), recibir={0: 10})  # b sin recibir
        r = _crear(f"{MARK}-C0", ocp.id, [(b.id, 1, 1000)])
        check("guard C: costear ítem sin recepción → 409",
              r.status_code == 409 and "recib" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 5. Guard C — Σ cantidad > recibido → 409 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-C1", ocp.id, [(a.id, 11, 1000)], monto_neto=11000)
        check("guard C: cantidad 11 > recibido 10 → 409", r.status_code == 409, r.text)
        _limpiar(db)

        # ═══ 6. Guard B — doble costeo nacional acumulado > recibido → 409 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r1 = _crear(f"{MARK}-B1", ocp.id, [(a.id, 6, 1000)])
        check("guard B: primera compra (6 de 10) → 200", r1.status_code == 200, r1.text)
        r2 = _crear(f"{MARK}-B2", ocp.id, [(a.id, 5, 1000)])  # 6+5=11 > 10
        check("guard B: segunda compra 6+5=11 > recibido 10 → 409", r2.status_code == 409, r2.text)
        _limpiar(db)

        # ═══ 7. Guard D — Σ líneas > neto → 400 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-D", ocp.id, [(a.id, 5, 1000)], monto_neto=4000)  # 5000 > 4000
        check("guard D: Σ líneas 5000 > neto 4000 → 400", r.status_code == 400, r.text)
        _limpiar(db)

        # ═══ 8. Ítem repetido en el detalle → 400 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-DUP", ocp.id, [(a.id, 3, 1000), (a.id, 2, 1000)], monto_neto=100000)
        check("ítem repetido en items → 400", r.status_code == 400, r.text)
        _limpiar(db)

        # ═══ 8b. Guard E — ítem de OTRA OC → 400 'no pertenece' (vínculo directo) ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        cot2, ocp2, (c,) = _setup(db, (5,))
        r = _crear(f"{MARK}-E", ocp.id, [(c.id, 1, 1000)])
        check("guard E: ítem de otra OC → 400 'no pertenece'",
              r.status_code == 400 and "pertenece" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 9. Cobertura PARCIAL: costear 8 de 10 → OK, quedan 2 sin costear ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-PARC", ocp.id, [(a.id, 8, 1000)], monto_neto=8000)
        check("cobertura parcial 8/10 → 200", r.status_code == 200, r.text)
        onac = client.get("/api/monza/compras-contab/oc-nacionales").json()
        fila = None
        for oc in onac["ocs"]:
            if oc["oc_proveedor_id"] == ocp.id:
                fila = next((x for x in oc["items"] if x["item_cotizacion_id"] == a.id), None)
        check("parcial: disponible_costear = 2 (10 recibidas − 8 costeadas)",
              fila and fila["ya_costeado"] == 8 and fila["disponible_costear"] == 2, fila)
        _limpiar(db)

        # ═══ 10. Compra anulada LIBERA su costeo → re-costear en compra nueva ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r1 = _crear(f"{MARK}-AN1", ocp.id, [(a.id, 10, 1000)])
        cid = r1.json()["id"]
        check("anulada-libera: costeo inicial 10 → 200", r1.status_code == 200, r1.text)
        r2 = _crear(f"{MARK}-AN2", ocp.id, [(a.id, 5, 1000)])   # 10+5 > 10 → 409 mientras activa
        check("anulada-libera: mientras activa, re-costear → 409", r2.status_code == 409, r2.text)
        ra = client.post(f"/api/monza/compras-contab/{cid}/anular", json={"motivo": "err"})
        check("anulada-libera: anular la compra → 200", ra.status_code == 200, ra.text)
        r3 = _crear(f"{MARK}-AN3", ocp.id, [(a.id, 10, 1000)])  # ahora libre
        check("anulada-libera: re-costear 10 tras anular → 200", r3.status_code == 200, r3.text)
        _limpiar(db)

        # ═══ 11. Circuito — crédito 30: aparece en /por-pagar con venc=fecha+30 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        hoy = date.today()
        r = _crear(f"{MARK}-CRED", ocp.id, [(a.id, 10, 1000)], condicion="credito",
                   plazo=30, fecha=hoy.isoformat())
        cc = r.json()
        check("crédito 30: venc = fecha+30", cc["fecha_vencimiento"] == (hoy + timedelta(days=30)).isoformat(),
              cc["fecha_vencimiento"])
        pp = client.get("/api/monza/tesoreria/por-pagar", params={"q": f"{MARK}", "page_size": 200}).json()
        fila = next((x for x in pp["compras"] if x["compra_id"] == cc["id"]), None)
        check("crédito 30: aparece en Tesorería /por-pagar", fila is not None, pp.get("total"))
        check("crédito 30: /por-pagar trae el vencimiento",
              fila and fila["fecha_vencimiento"] == (hoy + timedelta(days=30)).isoformat(), fila)
        _limpiar(db)

        # ═══ 12. Circuito — contado: egreso automático el mismo día (pagado) ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-CONT", ocp.id, [(a.id, 10, 1000)], condicion="contado")
        cc = r.json()
        check("contado: estado pagado + saldo 0",
              cc["estado_pago"] == "pagado" and cc["saldo_clp"] == 0, cc)
        check("contado: 1 pago (egreso automático)", len(cc.get("pagos", [])) == 1, cc.get("pagos"))
        _limpiar(db)

        # ═══ 13. Circuito — pago vía /tesoreria/pagos + anti sobre-pago ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-PAG", ocp.id, [(a.id, 10, 1000)], condicion="credito", plazo=30)
        cc = r.json(); total = cc["monto_total_clp"]
        rp = client.post("/api/monza/tesoreria/pagos", json={
            "medio": "transferencia", "detalles": [{"compra_id": cc["id"], "monto_clp": total}]})
        check("pago vía Tesorería (reusa _crear_egreso) → 200", rp.status_code == 200, rp.text)
        det = client.get(f"/api/monza/compras-contab/{cc['id']}").json()
        check("tras pago Tesorería: compra pagada", det["estado_pago"] == "pagado", det["estado_pago"])
        ro = client.post("/api/monza/tesoreria/pagos", json={
            "detalles": [{"compra_id": cc["id"], "monto_clp": 100}]})  # saldo 0 → sobre-pago
        check("sobre-pago vía Tesorería → 400", ro.status_code == 400, ro.text)
        _limpiar(db)

        # ═══ 14. Circuito — conciliación cargo↔egreso de la compra nacional ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-CONC", ocp.id, [(a.id, 10, 1000)], condicion="contado")
        cc = r.json(); total = cc["monto_total_clp"]
        # egreso automático del contado
        db.rollback()
        egd = db.query(MonzaContEgresoDetalle).filter(
            MonzaContEgresoDetalle.compra_id == cc["id"]).first()
        egreso_id = egd.egreso_id if egd else None
        rc = client.post("/api/monza/tesoreria/cuentas", json={"banco": f"{MARK} Banco", "moneda": "CLP"})
        cuenta_id = rc.json()["id"]
        rm = client.post("/api/monza/tesoreria/movimientos", json={
            "cuenta_id": cuenta_id, "tipo": "cargo", "monto": total,
            "glosa": f"{MARK} cargo", "fecha": date.today().isoformat()})
        mov_id = rm.json()["id"]
        rcon = client.post(f"/api/monza/tesoreria/movimientos/{mov_id}/conciliar",
                           json={"egreso_id": egreso_id})
        check("conciliar cargo↔egreso nacional → 200", rcon.status_code == 200, rcon.text)
        db.rollback()
        eg = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == egreso_id).first()
        check("egreso de compra nacional queda conciliado", eg and bool(eg.conciliado), eg and eg.conciliado)
        _limpiar(db)

        # ═══ 15. Anular compra nacional CON pagos → 409 ═══
        cot, ocp, (a, b) = _setup(db, (10, 10))
        r = _crear(f"{MARK}-ANP", ocp.id, [(a.id, 10, 1000)], condicion="contado")
        cid = r.json()["id"]
        ra = client.post(f"/api/monza/compras-contab/{cid}/anular", json={"motivo": "x"})
        check("anular compra nacional con pagos → 409", ra.status_code == 409, ra.text)
        _limpiar(db)

        # ═══ 16. G13: carrera costear ‖ anular recepción — jamás Σ costeado > recibido ═══
        # (write-skew cerrado: ambos caminos se serializan en el lock de
        # MonzaCotizacionItem y los guards leen con lecturas BLOQUEANTES)
        import threading
        inconsistencias = 0
        for _ronda in range(6):
            cot, ocp, (a,) = _setup(db, (10,))
            db.rollback()
            rec_row = (db.query(MonzaRecepcionNacional)
                       .filter(MonzaRecepcionNacional.oc_proveedor_id == ocp.id).first())
            resultados = {}

            def _costear():
                resultados["c"] = _crear(f"{MARK}-RACE", ocp.id, [(a.id, 10, 1000)]).status_code

            def _anular():
                resultados["a"] = client.delete(
                    f"/api/monza/recepcion-nacional/{rec_row.id}").status_code

            t1 = threading.Thread(target=_costear)
            t2 = threading.Thread(target=_anular)
            t1.start(); t2.start(); t1.join(); t2.join()
            db.rollback()
            costeado = sum(float(c or 0) for (c,) in db.query(MonzaContCompraItem.cantidad)
                           .join(MonzaContCompra, MonzaContCompra.id == MonzaContCompraItem.compra_id)
                           .filter(MonzaContCompraItem.item_cotizacion_id == a.id,
                                   MonzaContCompra.anulado.is_(False)).all())
            recibido = sum(float(q or 0) for (q,) in db.query(MonzaRecepcionNacionalItem.qty_recibida)
                           .join(MonzaRecepcionNacional,
                                 MonzaRecepcionNacional.id == MonzaRecepcionNacionalItem.recepcion_id)
                           .filter(MonzaRecepcionNacionalItem.item_cotizacion_id == a.id,
                                   MonzaRecepcionNacional.estado == "cerrada").all())
            if costeado > recibido + 0.001:
                inconsistencias += 1
            _limpiar(db)
        check("G13 carrera costear‖anular ×6: Σ costeado nunca supera recibido",
              inconsistencias == 0, f"{inconsistencias} rondas inconsistentes")

        # ═══ 17. G13: 2 compras concurrentes de ítems DISTINTOS — sin 500 (retry deadlock) ═══
        # (los gap locks de InnoDB deadlockean incluso ítems distintos; el retry 1213 absorbe)
        con_500 = 0
        for _ronda in range(6):
            cot, ocp, (a, b) = _setup(db, (10, 10))
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
        cot, ocp, (a,) = _setup(db, (10,))
        r2 = client.post("/api/monza/recepcion-nacional", json={
            "oc_proveedor_id": ocp.id, "numero_guia_proveedor": f"{MARK}-G-TARDIA",
            "cerrar": False,
            "items": [{"item_cotizacion_id": a.id, "qty_recibida": 2,
                       "estado_recepcion": "sobrante"}],
        })
        check("recepción tardía abierta creada 200", r2.status_code == 200, r2.text)
        rec2_id = r2.json()["id"]
        db.rollback()
        # El ítem ya se despachó completo (siembra directa: despacho cerrado + estado)
        desp = MonzaDespacho(numero=f"{MARK}-DSP", cotizacion_id=cot.id,
                             cliente_nombre=MARK, estado="despachado")
        db.add(desp); db.flush()
        db.add(MonzaDespachoItem(despacho_id=desp.id, item_id=a.id, qty_despachada=10))
        item_row = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == a.id).first()
        item_row.estado_linea = "despachado"
        db.commit()
        rc = client.post(f"/api/monza/recepcion-nacional/{rec2_id}/cerrar")
        check("cerrar recepción tardía 200", rc.status_code == 200, rc.text)
        db.rollback()
        estado_final = db.query(MonzaCotizacionItem.estado_linea).filter(
            MonzaCotizacionItem.id == a.id).scalar()
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


def test_monza_compras_contab_nacional():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
