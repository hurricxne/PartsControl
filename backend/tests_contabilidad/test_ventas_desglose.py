"""Tests del DESGLOSE de la venta en Ventas—Contabilidad (por_facturar + facturas por OC).

Cubre la semántica nueva del resumen: `por_facturar_clp` (total OC − facturado, con
anticipo INCLUIDO en lo facturado) y `anticipo_por_descontar_clp` (bruto del anticipo
aún no descontado en facturas del despacho real), que alimentan la barra de avance,
el chip "Por facturar" de la tarjeta y la sección "Por facturar" del detalle.

Casos borde: OC sin facturas · solo anticipo cobrado (el caso que confundía: badge
"Cobrada" con el grueso sin facturar) · anticipo + factura final (cierra a 0) ·
tandas parciales (cantidad por factura en el payload del ítem) · sobrefacturación
(clamp a 0, nunca negativo) · payload de facturas[] completo · OC de 200 ítems
(anti-muro: el backend responde entero y con los campos nuevos).

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_ventas_desglose.py -q
(también:   ./venv/bin/python tests_contabilidad/test_ventas_desglose.py)
"""
import os
import sys
import time
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

MARK = "__TEST_VDSG__"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(cont.router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0),
                   "total_venta_clp": cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0))}
            for i in items}
    neto = sum(cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0)) for i in items)
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear_venta(db, *, precio=10000.0, cantidad=10, qty_desp=None, con_despacho=True,
                 sufijo="A"):
    """Venta de 1 ítem (neto = precio×cantidad). Con despacho firmado por qty_desp
    (default: la cantidad completa); sin despacho si con_despacho=False."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} HEPI",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro", cantidad=cantidad,
                        estado_item="en_bodega" if not con_despacho else "despachado")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = None
    if con_despacho:
        desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                        estado="despachado", guia_firmada=1, numero_guia=f"G-{oc.id}")
        db.add(desp); db.flush()
        db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id,
                            qty_despachada=qty_desp if qty_desp is not None else cantidad))
    db.commit()
    PRECIOS[it.id] = precio
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


def _resumen(oc_id):
    r = client.get(f"/api/contabilidad/ventas/{oc_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _fila_listado(oc_id):
    r = client.get("/api/contabilidad/ventas")
    assert r.status_code == 200, r.text
    return next((v for v in r.json() if v["oc_cliente_id"] == oc_id), None)


def run():
    db = SessionLocal()
    cont._precios_de_cotizacion = _fake_precios
    try:
        _limpiar(db)

        # ═══ E1 · OC sin facturas: por facturar == total; sin anticipo ═══
        cot, oc, desp, it = _crear_venta(db, sufijo="A")   # neto 100.000 / bruto 119.000
        d = _resumen(oc.id)
        r1 = d["resumen"]
        check("E1 sin facturas: por_facturar == total OC (119.000)",
              r1["por_facturar_clp"] == 119000 and r1["estado_cobranza"] == "sin_factura", r1)
        check("E1 anticipo_por_descontar == 0", r1["anticipo_por_descontar_clp"] == 0, r1)
        fila = _fila_listado(oc.id)
        check("E1 listado trae por_facturar_clp == 119.000",
              fila is not None and fila.get("por_facturar_clp") == 119000, fila)

        # ═══ E2 · Solo ANTICIPO cobrado (el caso que confundía a Aldo) ═══
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": f"{MARK}-ANT", "monto_neto_anticipo": 50000})
        check("E2 emitir anticipo 200", r.status_code == 200, r.text)
        fant = r.json()
        # pagar el anticipo (cobranza real; medio='adelanto' manual está prohibido)
        db.rollback()  # REPEATABLE READ: refrescar el snapshot para ver el commit del TestClient
        fdb = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fant["id"]).first()
        db.add(ContCobranza(factura_id=fdb.id, monto=59500, medio="transferencia",
                            fecha=fdb.fecha_emision))
        db.flush(); db.refresh(fdb)
        cont._recompute_factura(fdb)
        db.commit()
        d = _resumen(oc.id)
        r2 = d["resumen"]
        check("E2 caso Aldo: cobrada + por_cobrar 0 + POR FACTURAR 59.500",
              r2["estado_cobranza"] == "cobrada" and r2["por_cobrar_clp"] == 0
              and r2["por_facturar_clp"] == 59500, r2)
        check("E2 anticipo_por_descontar == 59.500 (bruto, nada descontado aún)",
              r2["anticipo_por_descontar_clp"] == 59500, r2)
        check("E2 identidad: por_facturar == mercadería (119.000) − anticipo (59.500)",
              r2["por_facturar_clp"] == 119000 - 59500, r2)
        check("E2 mercaderia_pendiente_clp autoritativa == 119.000 (para la nota de la UI)",
              r2["mercaderia_pendiente_clp"] == 119000, r2)
        facs = d["facturas"]
        check("E2 payload facturas[]: es_anticipo + items + dias_vencimiento presentes",
              len(facs) == 1 and facs[0]["es_anticipo"] is True
              and "items" in facs[0] and "dias_vencimiento" in facs[0], facs)

        # ═══ E3 · Factura final del despacho: el desglose cierra a 0 ═══
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-F1"})
        check("E3 emitir factura final 200", r.status_code == 200, r.text)
        d = _resumen(oc.id)
        r3 = d["resumen"]
        check("E3 por_facturar == 0 (Σ brutos == total de la venta)",
              r3["por_facturar_clp"] == 0 and r3["facturado_clp"] == 119000, r3)
        check("E3 anticipo_por_descontar == 0 (descuento aplicado en la final)",
              r3["anticipo_por_descontar_clp"] == 0, r3)
        check("E3 payload: 2 facturas, la final con guía y línea de descuento",
              len(d["facturas"]) == 2
              and any(f["numero_guia"] and any(i["total_neto"] < 0 for i in f["items"])
                      for f in d["facturas"] if not f["es_anticipo"]), d["facturas"])

        # ═══ E4 · Tandas parciales: cantidad por factura viaja en el ítem ═══
        cot4, oc4, desp4, it4 = _crear_venta(db, cantidad=10, qty_desp=4, sufijo="B")
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc4.id, "despacho_id": desp4.id,
                              "numero_factura": f"{MARK}-F4"})
        check("E4 emitir factura de tanda (4 de 10) 200", r.status_code == 200, r.text)
        d4 = _resumen(oc4.id)
        item4 = d4["items"][0]
        check("E4 ítem trae cantidad facturada 4 (remanente derivable: 6)",
              len(item4["facturas"]) == 1 and item4["facturas"][0]["cantidad"] == 4, item4)
        check("E4 por_facturar == total − bruto tanda (119.000 − 47.600 = 71.400)",
              d4["resumen"]["por_facturar_clp"] == 119000 - 47600, d4["resumen"])

        # ═══ E5 · La base es FÍSICA: dinero facturado sin ítems no borra el pendiente ═══
        # (hallazgo del enjambre G15: antes por_facturar = total_vivo − Σ brutos, y una
        # factura suelta "tapaba" mercadería pendiente; ahora el pendiente es físico)
        db.add(ContFacturaCliente(oc_cliente_id=oc4.id, cotizacion_id=cot4.id,
                                  empresa="mineria", numero_factura=f"{MARK}-EXTRA",
                                  monto_neto=200000, iva=38000, monto_bruto=238000,
                                  monto_pagado=0, saldo=238000))
        db.commit()
        d5 = _resumen(oc4.id)
        check("E5 factura sin ítems NO oculta la mercadería pendiente (sigue 71.400)",
              d5["resumen"]["por_facturar_clp"] == 71400, d5["resumen"])
        check("E5b clamp: por_facturar precalculado negativo → 0 (nunca negativo)",
              cont._resumen_cobranza([], por_facturar_clp=-5.0)["por_facturar_clp"] == 0, "")

        # ═══ E7 · Cambio del dólar/pricing NO inventa "por facturar" fantasma ═══
        # (hallazgo HIGH del enjambre: total vivo con TC del día vs facturas congeladas)
        PRECIOS[it.id] = 20000.0   # el pricing de la OC A "sube" (dólar nuevo)
        d7 = _resumen(oc.id)
        check("E7 OC totalmente facturada sigue en 0 aunque el pricing viva cambie",
              d7["resumen"]["por_facturar_clp"] == 0, d7["resumen"])
        PRECIOS[it.id] = 10000.0

        # ═══ E8 · Tandas con redondeo de IVA: cierra a 0 exacto, sin polvo de $1 ═══
        # neto 26 → IVA(26)=5 → total vivo 31; dos tandas de neto 13 → IVA(13)=2 c/u
        # → Σ brutos 30. La resta vieja dejaba $1 fantasma; la base física da 0.
        cot8, oc8, desp8, it8 = _crear_venta(db, precio=13.0, cantidad=2, qty_desp=1, sufijo="C")
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc8.id, "despacho_id": desp8.id,
                              "numero_factura": f"{MARK}-T1"})
        check("E8 tanda 1 (neto 13) 200", r.status_code == 200, r.text)
        desp8b = Despacho(numero_despacho=f"{MARK}-DSP-{oc8.id}-B", oc_cliente_id=oc8.id,
                          estado="despachado", guia_firmada=1, numero_guia=f"G-{oc8.id}-B")
        db.add(desp8b); db.flush()
        db.add(DespachoItem(despacho_id=desp8b.id, item_cotizacion_id=it8.id, qty_despachada=1))
        db.commit()
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc8.id, "despacho_id": desp8b.id,
                              "numero_factura": f"{MARK}-T2"})
        check("E8 tanda 2 (neto 13) 200", r.status_code == 200, r.text)
        d8 = _resumen(oc8.id)
        check("E8 por_facturar == 0 exacto pese al redondeo por tandas (Σ30 vs 31)",
              d8["resumen"]["por_facturar_clp"] == 0
              and d8["resumen"]["facturado_clp"] == 30, d8["resumen"])
        fila8 = _fila_listado(oc8.id)
        check("E8 listado también en 0 (misma base física que el detalle)",
              fila8 is not None and fila8.get("por_facturar_clp") == 0, fila8)

        # ═══ E6 · OC de 200 ítems: el backend responde entero (anti-muro) ═══
        cot6 = Cotizacion(numero=f"{MARK}-COT-XXL", cliente=f"{MARK} XXL",
                          rut_cliente="78.279.030-7")
        db.add(cot6); db.flush()
        for n in range(1, 201):
            it6 = ItemCotizacion(cotizacion_id=cot6.id, item_num=n,
                                 numero_parte=f"P-{n:04d}", descripcion=f"Pieza {n}",
                                 cantidad=1, estado_item="comprado")
            db.add(it6); db.flush()
            PRECIOS[it6.id] = 1000.0
        oc6 = OcCliente(cotizacion_id=cot6.id, numero_oc=f"{MARK}-OC-XXL", fecha_oc="2026-07-01")
        db.add(oc6); db.commit()
        t0 = time.time()
        d6 = _resumen(oc6.id)
        dt = time.time() - t0
        check("E6 detalle de 200 ítems: completo y con resumen nuevo",
              len(d6["items"]) == 200
              and d6["resumen"]["por_facturar_clp"] == d6["total_con_iva_clp"], len(d6["items"]))
        check(f"E6 responde en tiempo razonable ({dt:.1f}s < 15s)", dt < 15, f"{dt:.1f}s")
        check("E6 estados logísticos vienen por ítem (grupo 'Comprado' derivable)",
              all(i["estado_item"] == "comprado" for i in d6["items"][:5]), d6["items"][0])

    finally:
        cont._precios_de_cotizacion = _orig_precios
        _limpiar(db)
        db.close()

    print()
    if _fails:
        print(f"FALLARON {len(_fails)}: {_fails}")
        sys.exit(1)
    print("TODOS LOS CHECKS OK")


def test_ventas_desglose():
    run()
    assert not _fails, _fails


if __name__ == "__main__":
    run()
