"""Tests de la FACTURA DE ANTICIPO (vía B) y su descuento automático.

Cubre: emisión sin guía (única excepción a la regla rectora), tope contra el total de
la venta, vínculo con adelantos (y su aplicación al aprobar), descuento automático en
las facturas del despacho real (línea negativa referenciando el folio, neto/IVA/bruto
recalculados, Σ brutos de la OC == total venta), factura final en $0 con advertencia,
reversiones (borrar la final restaura el pendiente de descuento; la de anticipo
descontada no se borra), guardas (boleta 400, factoring 409), y el EXCEDENTE de un
adelanto mayor que su anticipo (fluye a la factura del despacho real, como vía A).

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_factura_anticipo.py -q
(también:   ./venv/bin/python tests_contabilidad/test_factura_anticipo.py)
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
)
from tesoreria.models import ConciliacionIngreso  # noqa: E402
import routers.contabilidad as cont  # noqa: E402
from tesoreria.router import router as tes_router  # noqa: E402

MARK = "__TEST_FANT__"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(cont.router, prefix="/api")
app.include_router(tes_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

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


def _crear_venta(db, *, precio=10000.0, cantidad=10):
    """Venta de 1 ítem con guía firmada completa (neto 100.000 / bruto 119.000)."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} HEPI", rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro", cantidad=cantidad, estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    estado="despachado", guia_firmada=1, numero_guia="G-TEST")
    db.add(desp); db.flush()
    db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id, qty_despachada=cantidad))
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
        if adel_ids:
            db.query(ConciliacionIngreso).filter(
                ConciliacionIngreso.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(ContCobranza).filter(
                ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
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
    db.commit()


def run():
    cont._precios_de_cotizacion = _fake_precios
    db = SessionLocal()
    _limpiar(db)
    try:
        CURRENT["empresa"] = "mineria"

        # ═══ Emisión de la factura de anticipo (sin guía) ═══
        cot, oc, desp, it = _crear_venta(db)
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 59500, "pct": 50})
        adel_id = r.json()["id"]

        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "monto_neto_anticipo": 50000, "adelanto_ids": [adel_id]})
        p = r.json()
        check("preview anticipo sin guía: puede emitir",
              r.status_code == 200 and p["puede_emitir"] is True, p)
        check("preview anticipo montos (neto 50.000 → bruto 59.500)",
              p["totales"] == {"neto": 50000.0, "iva": 9500.0, "bruto": 59500.0}, p["totales"])
        check("preview anticipo 1 línea 'ANTICIPO'",
              len(p["lineas"]) == 1 and p["lineas"][0]["numero_parte"] == "ANTICIPO", p["lineas"])

        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True, "tipo_doc": "boleta",
                              "monto_neto_anticipo": 50000})
        check("anticipo como boleta → 400", r.status_code == 400, r.text)

        # Razón social vacía → bloquea (mismo trato que la factura normal: la única
        # excepción de la vía B es la guía, no la completitud del receptor)
        cliente_original = cot.cliente
        cot.cliente = "   "
        db.commit()
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "monto_neto_anticipo": 50000})
        p = r.json()
        check("anticipo sin razón social: bloquea",
              p["puede_emitir"] is False
              and any("razón social" in x for x in p["problemas"]), p)
        # …y el campo por llenar del modal la desbloquea (razon_social_cliente)
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "monto_neto_anticipo": 50000,
                              "razon_social_cliente": "PRUEBA DEMO S.A."})
        p = r.json()
        check("anticipo con razón social digitada: desbloquea",
              not any("razón social" in x for x in p["problemas"])
              and p["receptor"]["razon_social"] == "PRUEBA DEMO S.A.", p)
        # …y al EMITIR se guarda en la venta (queda completa para las siguientes)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": f"{MARK}-ANT-RS",
                              "monto_neto_anticipo": 1000,
                              "razon_social_cliente": "PRUEBA DEMO S.A."})
        check("emitir con razón social digitada 200", r.status_code == 200, r.text)
        db.rollback()
        check("razón social PERSISTIDA en la venta",
              db.query(Cotizacion).filter(Cotizacion.id == cot.id).first().cliente
              == "PRUEBA DEMO S.A.")
        fid = r.json()["id"]
        db.query(ContCobranza).filter(ContCobranza.factura_id == fid).delete()
        db.query(ContFacturaClienteItem).filter(
            ContFacturaClienteItem.factura_id == fid).delete()
        db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fid).delete()
        cot.cliente = cliente_original
        db.commit()

        # Venta SIN RUT: bloquea; el RUT digitado en el modal (rut_cliente) desbloquea
        # (mismo contrato que la factura normal — el frontend lo manda con debounce)
        rut_original = cot.rut_cliente
        cot.rut_cliente = None
        db.commit()
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "monto_neto_anticipo": 50000})
        check("anticipo sin RUT: bloquea", r.json()["puede_emitir"] is False, r.text)
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "monto_neto_anticipo": 50000,
                              "rut_cliente": "78.279.030-7"})
        check("anticipo con RUT digitado: desbloquea",
              r.json()["puede_emitir"] is True, r.text)
        cot.rut_cliente = rut_original
        db.commit()

        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": f"{MARK}-ANT1",
                              "monto_neto_anticipo": 50000, "adelanto_ids": [adel_id]})
        check("emitir factura de anticipo 200", r.status_code == 200, r.text)
        fant = r.json()
        check("anticipo emitida: es_anticipo, sin guía, por cobrar",
              fant["es_anticipo"] is True and fant["despacho_id"] is None
              and fant["monto_bruto"] == 59500 and fant["saldo"] == 59500, fant)
        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        a = r.json()[0]
        check("adelanto ligado a la factura de anticipo (folio)",
              a["factura_anticipo_id"] == fant["id"]
              and a["factura_anticipo_folio"] == f"{MARK}-ANT1", a)

        # factoring sobre la factura de anticipo → 409
        r = client.post(f"/api/contabilidad/facturas/{fant['id']}/factoring",
                        json={"monto_adelantado": 10000})
        check("factoring sobre anticipo → 409", r.status_code == 409, r.text)

        # tope: un 2° anticipo que excede lo no facturado → 409
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": f"{MARK}-ANT2", "monto_neto_anticipo": 60000})
        check("2° anticipo que excede la venta → 409",
              r.status_code == 409 and "excede" in r.json()["detail"].lower(), r.text)

        # ═══ Aprobación del adelanto → se aplica a SU factura de anticipo ═══
        r = client.post(f"/api/tesoreria/adelantos/{adel_id}/aprobar",
                        json={"monto": 59500, "fecha_pago": "2026-07-10",
                              "banco": "Santander", "numero_operacion": "OP-9"})
        check("aprobar aplica a la factura de anticipo",
              r.status_code == 200 and r.json()["aplicado_ahora_clp"] == 59500, r.text)
        r = client.get("/api/contabilidad/facturas")
        fant_n = [x for x in r.json()["facturas"] if x["id"] == fant["id"]][0]
        check("factura de anticipo queda PAGADA",
              fant_n["saldo"] == 0 and fant_n["estado_pago"] == "pagada"
              and any(c["es_adelanto"] for c in fant_n["cobranzas"]), fant_n)

        # ═══ Factura del despacho real: descuento automático ═══
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        p = r.json()
        dsc = [ln for ln in p["lineas"] if ln["numero_parte"] == "DESCUENTO"]
        check("preview final trae línea de descuento con el folio",
              len(dsc) == 1 and dsc[0]["total_neto"] == -50000
              and f"{MARK}-ANT1" in dsc[0]["descripcion"], p["lineas"])
        check("preview final: neto 50.000 / bruto 59.500 (tras descuento)",
              p["totales"] == {"neto": 50000.0, "iva": 9500.0, "bruto": 59500.0}, p["totales"])

        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-F1"})
        check("emitir factura final 200", r.status_code == 200, r.text)
        ffin = r.json()
        check("final SIN cobranza 'adelanto' (la plata quedó en el anticipo)",
              not any(c["es_adelanto"] for c in ffin["cobranzas"]), ffin["cobranzas"])
        check("final descontada: bruto 59.500, saldo por cobrar del cliente",
              ffin["monto_bruto"] == 59500 and ffin["saldo"] == 59500, ffin)
        check("Σ brutos de la OC == total de la venta (119.000)",
              round(fant_n["monto_bruto"] + ffin["monto_bruto"]) == 119000,
              (fant_n["monto_bruto"], ffin["monto_bruto"]))
        linea_dsc = [i for i in ffin["items"] if i["anticipo_factura_id"] == fant["id"]]
        check("línea de descuento persistida referenciando el anticipo",
              len(linea_dsc) == 1 and linea_dsc[0]["total_neto"] == -50000, ffin["items"])

        # anticipo ya descontada → no se puede borrar
        r = client.delete(f"/api/contabilidad/facturas/{fant['id']}")
        check("borrar anticipo descontada → 409", r.status_code == 409, r.text)

        # ═══ Borrar la final restaura el pendiente de descuento (derivado) ═══
        r = client.delete(f"/api/contabilidad/facturas/{ffin['id']}")
        check("borrar factura final 200", r.status_code == 200, r.text)
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        p = r.json()
        check("descuento disponible de nuevo tras borrar la final",
              any(ln["numero_parte"] == "DESCUENTO" for ln in p["lineas"]), p["lineas"])

        # ═══ Borrar la factura de anticipo (sin descuentos): exige revertir pagos ═══
        r = client.delete(f"/api/contabilidad/facturas/{fant['id']}")
        check("borrar anticipo con cobranza → 409 (revertir primero)",
              r.status_code == 409 and "cobranza" in r.json()["detail"].lower(), r.text)
        cob_id = [c["id"] for c in fant_n["cobranzas"] if c["es_adelanto"]][0]
        client.delete(f"/api/contabilidad/facturas/{fant['id']}/cobranzas/{cob_id}")
        r = client.delete(f"/api/contabilidad/facturas/{fant['id']}")
        check("borrar anticipo sin pagos ni descuentos 200", r.status_code == 200, r.text)
        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        a = r.json()[0]
        check("adelanto vuelve a vía A (sin factura de anticipo) y con monto pendiente",
              a["factura_anticipo_id"] is None and a["monto_aplicado"] == 0
              and a["pendiente_aplicar"] == 59500, a)
        _limpiar(db)

        # ═══ Anticipo por el TOTAL → factura final en $0 con advertencia ═══
        cot, oc, desp, it = _crear_venta(db)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": f"{MARK}-ANT3", "monto_neto_anticipo": 100000})
        check("anticipo por el total 200", r.status_code == 200, r.text)
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        p = r.json()
        check("final en $0 permitida con ADVERTENCIA",
              p["puede_emitir"] is True and p["totales"]["bruto"] == 0
              and any("$0" in x for x in p["advertencias"]), p)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-F0"})
        f0 = r.json()
        check("final $0 emitida queda 'pagada' (nada por cobrar)",
              r.status_code == 200 and f0["monto_bruto"] == 0
              and f0["estado_pago"] == "pagada", f0)
        _limpiar(db)

        # ═══ No se puede ligar a un anticipo un adelanto YA aplicado (vía A) ═══
        cot, oc, desp, it = _crear_venta(db)
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 59500})
        adel_id = r.json()["id"]
        client.post(f"/api/tesoreria/adelantos/{adel_id}/aprobar", json={"monto": 59500})
        client.post("/api/contabilidad/facturas",
                    json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                          "numero_factura": f"{MARK}-F9"})  # vía A: aplica el adelanto
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": f"{MARK}-ANT9",
                              "monto_neto_anticipo": 10000, "adelanto_ids": [adel_id]})
        check("ligar adelanto ya aplicado → 409",
              r.status_code == 409 and "aplicado" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ EXCEDENTE vía B: adelanto MAYOR que su anticipo fluye al despacho real ═══
        # El cliente pagó 70.000 pero el anticipo se emitió por solo 59.500 → los
        # 10.500 restantes rebajan la factura final (misma regla que vía A: el resto
        # sigue a la próxima factura). Sin esto el excedente quedaba atrapado y la
        # deuda del cliente sobrestimada.
        cot, oc, desp, it = _crear_venta(db)
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 70000})
        adel_id = r.json()["id"]
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": f"{MARK}-ANT-EXC",
                              "monto_neto_anticipo": 50000, "adelanto_ids": [adel_id]})
        check("anticipo 59.500 ligado a adelanto de 70.000 emitido 200",
              r.status_code == 200, r.text)
        r = client.post(f"/api/tesoreria/adelantos/{adel_id}/aprobar",
                        json={"monto": 70000, "fecha_pago": "2026-07-10"})
        check("aprobar aplica solo el bruto del anticipo (59.500)",
              r.status_code == 200 and r.json()["aplicado_ahora_clp"] == 59500, r.text)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-F-EXC"})
        ffin = r.json()
        check("excedente (10.500) aplicado a la final: saldo 49.000",
              r.status_code == 200
              and any(c["es_adelanto"] and c["monto"] == 10500 for c in ffin["cobranzas"])
              and ffin["saldo"] == 49000, ffin)
        r = client.get(f"/api/contabilidad/ventas/{oc.id}/adelantos")
        a = r.json()[0]
        check("adelanto 70.000 aplicado COMPLETO (pendiente 0)",
              a["monto_aplicado"] == 70000 and a["pendiente_aplicar"] == 0, a)
        db.rollback()
        cobs = db.query(ContCobranza).filter(ContCobranza.adelanto_id == adel_id).all()
        check("INVARIANTE: monto_aplicado == Σ cobranzas medio='adelanto'",
              round(sum(float(c.monto) for c in cobs), 2) == 70000,
              [float(c.monto) for c in cobs])
        _limpiar(db)

        # Mismo excedente pero aprobando DESPUÉS de emitir ambas facturas:
        # aprobar_adelanto recorre los anticipos primero, así la misma pasada salda
        # el anticipo y deja el excedente en la final.
        cot, oc, desp, it = _crear_venta(db)
        r = client.post("/api/contabilidad/ventas/adelantos",
                        json={"oc_cliente_id": oc.id, "monto_esperado": 70000})
        adel_id = r.json()["id"]
        fant2 = client.post("/api/contabilidad/facturas",
                            json={"oc_cliente_id": oc.id, "es_anticipo": True,
                                  "numero_factura": f"{MARK}-ANT-EX2",
                                  "monto_neto_anticipo": 50000,
                                  "adelanto_ids": [adel_id]}).json()
        ffin2 = client.post("/api/contabilidad/facturas",
                            json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                                  "numero_factura": f"{MARK}-F-EX2"}).json()
        r = client.post(f"/api/tesoreria/adelantos/{adel_id}/aprobar", json={"monto": 70000})
        check("aprobar con ambas facturas emitidas: aplica 70.000 en una pasada",
              r.status_code == 200 and r.json()["aplicado_ahora_clp"] == 70000, r.text)
        r = client.get("/api/contabilidad/facturas")
        por_id = {x["id"]: x for x in r.json()["facturas"]}
        check("anticipo pagada y final con saldo 49.000 (excedente aplicado)",
              por_id[fant2["id"]]["saldo"] == 0
              and por_id[ffin2["id"]]["saldo"] == 49000,
              (por_id[fant2["id"]]["saldo"], por_id[ffin2["id"]]["saldo"]))
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        cont._precios_de_cotizacion = _orig_precios
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_factura_anticipo_integration():
    run()


if __name__ == "__main__":
    run()
