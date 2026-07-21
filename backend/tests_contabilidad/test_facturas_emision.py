"""Tests de la EMISIÓN de facturas (contabilidad.py) tras la revisión de datos.

Cubre lo endurecido: cuadratura guía==factura (redondeo idéntico), RUT del cliente
obligatorio y válido, folio obligatorio para 'factura', bloqueo de precio $0,
preview == emisión, y el "campo por llenar" (RUT que completa la venta).

Monta el router en una app efímera (sin tocar main.py), simula la auth de un usuario
minería, arma un flujo real (cotización → OC → guía firmada) y LIMPIA todo al terminar.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_facturas_emision.py -q
(también:   ./venv/bin/python tests_contabilidad/test_facturas_emision.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContFactoring,
)
import routers.contabilidad as cont  # noqa: E402

MARK = "__TEST_FACT__"
CURRENT = {"empresa": "mineria", "id": None}

app = FastAPI()
app.include_router(cont.router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

# Precios deterministas CON decimales (para probar el redondeo): el pricing engine
# tiene sus propios tests; aquí forzamos precio_venta_clp para cuadrar guía vs factura.
PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0)} for i in items}
    # totales reales (los usa _total_bruto_venta para el tope Σ brutos ≤ venta)
    neto = sum(cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0)) for i in items)
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear_datos(db, rut="78.279.030-7", precio1=15990.4066, precio2=2500.0):
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} HEPI", rut_cliente=rut)
    db.add(cot); db.flush()
    it1 = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                         descripcion="Filtro de aceite", cantidad=10, estado_item="en_bodega")
    it2 = ItemCotizacion(cotizacion_id=cot.id, item_num=2, numero_parte="6I-2503",
                         descripcion="Sello de polvo", cantidad=20, estado_item="en_bodega")
    db.add_all([it1, it2]); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC", fecha_oc="2026-07-01",
                   cond_pago="45 días")
    db.add(oc); db.flush()
    # Despacho DESPACHADO y FIRMADO (requisito para facturar): 4 de 10 + 20 de 20
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    estado="despachado", guia_firmada=1, numero_guia="G-TEST")
    db.add(desp); db.flush()
    db.add_all([
        DespachoItem(despacho_id=desp.id, item_cotizacion_id=it1.id, qty_despachada=4),
        DespachoItem(despacho_id=desp.id, item_cotizacion_id=it2.id, qty_despachada=20),
    ])
    db.commit()
    PRECIOS.clear()
    PRECIOS.update({it1.id: precio1, it2.id: precio2})
    return cot, oc, desp, it1, it2


def _limpiar(db):
    db.rollback()  # cerrar cualquier transacción a medias de un run anterior
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    oc_ids = [oc.id for oc in db.query(OcCliente)
              .filter(OcCliente.cotizacion_id.in_([c.id for c in cots])).all()] if cots else []
    if oc_ids:
        fac_ids = [f.id for f in db.query(ContFacturaCliente)
                   .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
        # Orden FK-seguro: cobranzas/factoring → ítems de factura (referencian
        # despacho_items) → facturas → ítems de despacho → despachos → OCs.
        if fac_ids:
            db.query(ContCobranza).filter(
                ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFactoring).filter(
                ContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFacturaClienteItem).filter(
                ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFacturaCliente).filter(
                ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
        if desp_ids:
            try:
                from wasabil_dte.models import WasabilDte
                db.query(WasabilDte).filter(
                    WasabilDte.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            except Exception:
                pass
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


def test_cuadratura_guia_igual_factura():
    """El total de línea de la factura DEBE ser idéntico al de la guía electrónica:
    round(precio,2) ANTES de multiplicar, half-up a peso."""
    from wasabil_dte.service import armar_lineas, total_neto_lineas
    precio, qty = 15990.4066, 4
    di = SimpleNamespace(id=1, item_cotizacion_id=1, qty_despachada=qty,
                         item_cotizacion=SimpleNamespace(id=1, numero_parte="1R-0716",
                                                         descripcion="Filtro"))
    lineas_guia, _p = armar_lineas([di], {1: {"precio_venta_clp": precio}})
    total_guia = total_neto_lineas(lineas_guia)
    total_fact = cont._total_linea(precio, qty)
    assert total_fact == total_guia, (total_fact, total_guia)
    assert total_fact == round(round(precio, 2) * qty)  # 15990.41 × 4 = 63961.64 → 63962


def run():
    cont._precios_de_cotizacion = _fake_precios
    db = SessionLocal()
    _limpiar(db)
    try:
        CURRENT["empresa"] = "mineria"

        # ── Preview (líneas + montos + receptor) ──
        cot, oc, desp, it1, it2 = _crear_datos(db)
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("preview 200", r.status_code == 200, r.text)
        p = r.json()
        neto_esp = cont._total_linea(15990.4066, 4) + cont._total_linea(2500.0, 20)
        check("preview cuadra guía==factura", p["totales"]["neto"] == neto_esp, p["totales"])
        check("preview iva half-up", p["totales"]["iva"] == cont._iva_clp(neto_esp), p["totales"])
        check("preview 2 líneas", len(p["lineas"]) == 2, p["lineas"])
        check("preview receptor con RUT en venta", p["receptor"]["rut_en_venta"] is True, p["receptor"])
        check("preview puede_emitir", p["puede_emitir"] is True, p["problemas"])

        # ── Emisión sin folio → 400 ──
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id, "tipo_doc": "factura"})
        check("sin folio 400", r.status_code == 400 and "folio" in r.json()["detail"].lower(), r.text)

        # ── Emisión OK: montos == preview (preview == emisión) ──
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "tipo_doc": "factura", "numero_factura": f"{MARK}-100",
                              "plazo_dias": 45})
        check("emitir 200", r.status_code == 200, r.text)
        fac = r.json()
        check("emisión == preview (neto/iva)",
              fac["monto_neto"] == neto_esp and fac["iva"] == cont._iva_clp(neto_esp), fac)
        check("línea unit×cant == total (cuadra)",
              all(round(i["precio_unit_neto"] * i["cantidad"]) == i["total_neto"]
                  for i in fac["items"]), fac["items"])
        _limpiar(db)

        # ── RUT obligatorio: venta sin RUT y sin RUT en payload → bloquea ──
        cot, oc, desp, it1, it2 = _crear_datos(db, rut=None)
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("sin RUT preview bloquea", r.json()["puede_emitir"] is False
              and any("RUT" in x for x in r.json()["problemas"]), r.json()["problemas"])
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-101"})
        check("sin RUT emitir 409", r.status_code == 409 and "RUT" in r.json()["detail"], r.text)

        # ── RUT inválido (dígito verificador) → bloquea ──
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-101", "rut_cliente": "76.999.999-9"})
        check("RUT inválido 409", r.status_code == 409 and "válido" in r.json()["detail"], r.text)

        # ── Campo por llenar: el preview ACEPTA el RUT digitado (desbloquea el botón)
        #    y al emitir queda guardado en la venta ──
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "rut_cliente": "78.279.030-7"})
        check("preview con RUT digitado desbloquea", r.json()["puede_emitir"] is True,
              r.json()["problemas"])
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-101", "rut_cliente": "78.279.030-7"})
        check("RUT en payload emite 200", r.status_code == 200, r.text)
        db.rollback()
        check("RUT quedó guardado en la venta",
              db.get(Cotizacion, cot.id).rut_cliente == "78.279.030-7")
        _limpiar(db)

        # ── Venta con RUT INVÁLIDO guardado: el RUT corregido en el modal manda,
        #    emite, y CORRIGE el RUT de la venta ──
        cot, oc, desp, it1, it2 = _crear_datos(db, rut="76.999.999-9")  # DV malo
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("RUT inválido guardado bloquea preview", r.json()["puede_emitir"] is False,
              r.json()["problemas"])
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-102", "rut_cliente": "78.279.030-7"})
        check("RUT corregido emite 200", r.status_code == 200, r.text)
        db.rollback()
        check("RUT inválido de la venta quedó corregido",
              db.get(Cotizacion, cot.id).rut_cliente == "78.279.030-7")
        _limpiar(db)

        # ── RUT canónico: el MISMO RUT con y sin guión/puntos NO dispara la
        #    advertencia 'difiere', y el maestro de Clientes cruza cualquier formato ──
        from models.models import Cliente as _Cliente
        cot, oc, desp, it1, it2 = _crear_datos(db, rut="78.279.030-7")
        db.query(_Cliente).filter(_Cliente.rut == "782790307").delete()
        db.add(_Cliente(rut="782790307", nombre=f"{MARK} HEPI",  # maestro SIN puntos/guión
                        giro="VENTA DE REPUESTOS", direccion="Ruta 26 KM 15"))
        db.commit()
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "rut_cliente": "782790307"})  # mismo RUT, otro formato
        p = r.json()
        check("mismo RUT distinto formato: sin advertencia 'difiere'",
              not any("difiere" in a for a in p.get("advertencias", [])),
              p.get("advertencias"))
        check("maestro Cliente cruza formatos (giro/dirección en preview)",
              p["receptor"].get("giro") == "VENTA DE REPUESTOS"
              and "Ruta 26" in (p["receptor"].get("direccion") or ""), p["receptor"])
        db.query(_Cliente).filter(_Cliente.rut == "782790307").delete()
        db.commit()
        _limpiar(db)

        # ── Razón social vacía → bloquea (mismo trato que el RUT) ──
        cot, oc, desp, it1, it2 = _crear_datos(db)
        cot.cliente = ""
        db.commit()
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("razón social vacía bloquea", r.json()["puede_emitir"] is False
              and any("razón social" in x.lower() for x in r.json()["problemas"]),
              r.json()["problemas"])
        _limpiar(db)

        # ── Origen venta_clp SIN mockear el pricing: la línea que pasa 'origen' a
        #    calcular_cotizacion debe producir precio = CLP directo (margen 0) ──
        cont._precios_de_cotizacion = _orig_precios
        try:
            cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} CLP",
                             rut_cliente="78.279.030-7", origen="venta_clp")
            db.add(cot); db.flush()
            itc = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="CLP-1",
                                 descripcion="Servicio", cantidad=2, estado_item="despachado",
                                 precio_unit_cotizacion=150000.0, margen_pct=0.0)
            db.add(itc); db.flush()
            oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OCC", fecha_oc="2026-07-01")
            db.add(oc); db.flush()
            desp = Despacho(numero_despacho=f"{MARK}-DSPC-{oc.id}", oc_cliente_id=oc.id,
                            estado="despachado", guia_firmada=1, numero_guia="G-CLP")
            db.add(desp); db.flush()
            db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=itc.id, qty_despachada=2))
            db.commit()
            r = client.post("/api/contabilidad/facturas/preview",
                            json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
            p = r.json()
            check("venta_clp: neto = CLP directo × cant (pricing real)",
                  p["totales"]["neto"] == 300000 and p["puede_emitir"] is True,
                  p.get("totales"))
        finally:
            cont._precios_de_cotizacion = _fake_precios
        _limpiar(db)

        # ── Precio $0 → bloquea (antes emitía factura en $0 auto-'pagada') ──
        cot, oc, desp, it1, it2 = _crear_datos(db, precio1=0.0, precio2=0.0)
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("precio $0 bloquea", r.json()["puede_emitir"] is False
              and any("$0" in x or "precio" in x.lower() for x in r.json()["problemas"]),
              r.json()["problemas"])
        _limpiar(db)

        # ── Folio duplicado → 409 ──
        cot, oc, desp, it1, it2 = _crear_datos(db)
        client.post("/api/contabilidad/facturas",
                    json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                          "numero_factura": f"{MARK}-DUP"})
        # segunda guía firmada para la misma OC (para intentar reusar folio)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-DUP"})
        check("folio duplicado 409", r.status_code == 409 and "folio" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ── Precio CONGELADO de la guía electrónica emitida (cuadra con el SII aunque
        #    cambie el dólar): si existe wasabil_dte emitido, la factura usa SU precio ──
        from wasabil_dte.models import WasabilDte
        import json as _json
        cot, oc, desp, it1, it2 = _crear_datos(db, precio1=15990.4066, precio2=2500.0)
        di_ids = [di.id for di in db.query(DespachoItem)
                  .filter(DespachoItem.despacho_id == desp.id)
                  .order_by(DespachoItem.item_cotizacion_id).all()]
        # Guía 52 emitida con externalId=despacho_item_id y precio CONGELADO distinto del
        # que hoy daría el cotizador
        db.add(WasabilDte(
            tipo_dte=52, despacho_id=desp.id, status_id=3, folio="G-9",
            payload_json=_json.dumps({"details": [
                {"code": "1R-0716", "externalId": str(di_ids[0]), "price": 20000.0, "quantity": 4},
                {"code": "6I-2503", "externalId": str(di_ids[1]), "price": 3000.0, "quantity": 20},
            ]}),
        ))
        db.commit()
        # Ahora el cotizador daría OTRO precio (subió), pero debe MANDAR el de la guía
        PRECIOS.update({it1.id: 99999.0, it2.id: 88888.0})
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        p = r.json()
        neto_guia = cont._total_linea(20000.0, 4) + cont._total_linea(3000.0, 20)
        check("factura usa precio CONGELADO de la guía (por despacho_item)",
              p["totales"]["neto"] == neto_guia and p["precio_de_guia"] is True,
              {"neto": p["totales"]["neto"], "esperado": neto_guia})
        _limpiar(db)

        # ── Partes DUPLICADAS: el congelado por despacho_item cuadra 1:1 (el keyeo por
        #    n° de parte fallaba). Dos ítems con el MISMO numero_parte, precios distintos ──
        cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} DUP", rut_cliente="78.279.030-7")
        db.add(cot); db.flush()
        ia = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="DUP-1",
                            descripcion="A", cantidad=2, estado_item="despachado")
        ib = ItemCotizacion(cotizacion_id=cot.id, item_num=2, numero_parte="DUP-1",
                            descripcion="B", cantidad=3, estado_item="despachado")
        db.add_all([ia, ib]); db.flush()
        oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OCD", fecha_oc="2026-07-01")
        db.add(oc); db.flush()
        desp = Despacho(numero_despacho=f"{MARK}-DSPD-{oc.id}", oc_cliente_id=oc.id,
                        estado="despachado", guia_firmada=1, numero_guia="G-DUP")
        db.add(desp); db.flush()
        dia = DespachoItem(despacho_id=desp.id, item_cotizacion_id=ia.id, qty_despachada=2)
        dib = DespachoItem(despacho_id=desp.id, item_cotizacion_id=ib.id, qty_despachada=3)
        db.add_all([dia, dib]); db.flush()
        db.add(WasabilDte(
            tipo_dte=52, despacho_id=desp.id, status_id=3, folio="G-DUP",
            payload_json=_json.dumps({"details": [
                {"code": "DUP-1", "externalId": str(dia.id), "price": 1000.0, "quantity": 2},
                {"code": "DUP-1", "externalId": str(dib.id), "price": 2000.0, "quantity": 3},
            ]}),
        ))
        db.commit()
        PRECIOS.clear(); PRECIOS.update({ia.id: 55555.0, ib.id: 66666.0})
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        p = r.json()
        neto_dup = cont._total_linea(1000.0, 2) + cont._total_linea(2000.0, 3)
        check("partes duplicadas cuadran por despacho_item (no recalcula)",
              p["totales"]["neto"] == neto_dup and p["precio_de_guia"] is True
              and not p.get("advertencias"),
              {"neto": p["totales"]["neto"], "esperado": neto_dup, "adv": p.get("advertencias")})
        _limpiar(db)

        # ── Candado de empresa ──
        cot, oc, desp, it1, it2 = _crear_datos(db)
        CURRENT["empresa"] = "automotriz"
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("candado automotriz 403", r.status_code == 403, r.text)
        CURRENT["empresa"] = "mineria"
        _limpiar(db)

        # ── Tope Σ brutos ≤ venta con PRECIO EXPLÍCITO del payload (compuerta de
        #    API que la UI no usa; espejo del tope de la factura de anticipo) ──
        cot, oc, desp, it1, it2 = _crear_datos(db)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FX",
                              "items": [{"item_cotizacion_id": it1.id, "cantidad": 4,
                                         "precio_unit_neto": 99999999}]})
        check("precio explícito que excede la venta → 409",
              r.status_code == 409 and "excede el total de la venta" in r.json()["detail"],
              r.text)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FX2",
                              "items": [{"item_cotizacion_id": it1.id, "cantidad": 4,
                                         "precio_unit_neto": 1000}]})
        check("precio explícito dentro de la venta → 200 (correcciones legítimas)",
              r.status_code == 200, r.text)
        _limpiar(db)

        # ── eliminar_cobranza con factoring VIGENTE → 409 (espejo de registrar:
        #    la asignación de pagos de una factura cedida está congelada) ──
        cot, oc, desp, it1, it2 = _crear_datos(db)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FF"})
        fid = r.json()["id"]
        client.post(f"/api/contabilidad/facturas/{fid}/cobranzas",
                    json={"monto": 10000, "medio": "transferencia"})
        db.rollback()
        cob = db.query(ContCobranza).filter(ContCobranza.factura_id == fid).first()
        client.post(f"/api/contabilidad/facturas/{fid}/factoring",
                    json={"monto_adelantado": 5000})
        r = client.delete(f"/api/contabilidad/facturas/{fid}/cobranzas/{cob.id}")
        check("eliminar cobranza con factoring vigente → 409",
              r.status_code == 409 and "factoring vigente" in r.json()["detail"], r.text)
        r = client.post(f"/api/contabilidad/facturas/{fid}/factoring/liquidar")
        check("liquidar factoring 200", r.status_code == 200, r.text)
        r = client.delete(f"/api/contabilidad/facturas/{fid}/cobranzas/{cob.id}")
        check("eliminar cobranza tras liquidar → 200", r.status_code == 200, r.text)
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        cont._precios_de_cotizacion = _orig_precios
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_facturas_emision_integration():
    run()


if __name__ == "__main__":
    test_cuadratura_guia_igual_factura()
    print("OK  | cuadratura guía==factura (unit)")
    run()
