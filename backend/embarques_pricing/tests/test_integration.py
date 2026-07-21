"""Test de integración del módulo Embarques Pricing contra la DB real (local).

Siembra un embarque completo (con factura de proveedor para el FOB), ejerce el
flujo de pricing y LIMPIA todo lo que creó al terminar (deja la DB intacta).

Corre con:  ./venv/bin/python embarques_pricing/tests/test_integration.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import SessionLocal  # noqa: E402
from models.models import (  # noqa: E402
    User, Cotizacion, ItemCotizacion, OcProveedor, OcProveedorItem,
    FacturaProveedor, FacturaProveedorItem, Embarque, EmbarqueItem,
)
from embarques_pricing.models import (  # noqa: E402
    EmbarquePricing, EmbarquePricingGasto, EmbarquePricingItem,
)
from embarques_pricing import router as R  # noqa: E402
from embarques_pricing.integration import ensure_pricing_for_embarque  # noqa: E402
from embarques_pricing.service import GASTOS_CATALOGO  # noqa: E402

TOL = 1.0
MARK = "__TEST_EMB_PRICING__"


def _aprox(a, b, tol=TOL):
    return abs(float(a) - float(b)) <= tol


def _seed(db):
    """Crea cotización + 3 ítems + OCP + factura proveedor + embarque. Devuelve ids."""
    user = User(email=f"{MARK}@test.local", nombre=MARK, hashed_password="x", empresa="mineria")
    db.add(user); db.flush()

    cot = Cotizacion(numero=f"{MARK}-COT", cliente="Cliente Test")
    db.add(cot); db.flush()

    # 3 ítems con peso y cantidad (FOB vendrá de la factura del proveedor)
    specs = [("7T-1997", 0.3, 1, 124), ("4W-0259", 1.2, 1, 890), ("1R-0749", 2.1, 1, 1420)]
    items = []
    for parte, peso, cant, _fob in specs:
        it = ItemCotizacion(
            cotizacion_id=cot.id, numero_parte=parte, descripcion=f"DESC {parte}",
            cantidad=cant, peso_unit_lbs=peso, precio_unit_cotizacion=0,
        )
        db.add(it); db.flush()
        items.append(it)

    ocp = OcProveedor(numero=f"{MARK}-OCP", proveedor="FLORIDA ENGINE", moneda="USD")
    db.add(ocp); db.flush()

    factura = FacturaProveedor(ocp_id=ocp.id, invoice_no=f"{MARK}-INV", total_usd=2434)
    db.add(factura); db.flush()

    embarque = Embarque(numero=f"{MARK}-EMB", estado="en_bodega", forwarder="LATAM Cargo")
    db.add(embarque); db.flush()

    for it, (_p, _peso, _cant, fob) in zip(items, specs):
        ocpi = OcProveedorItem(oc_proveedor_id=ocp.id, item_cotizacion_id=it.id)
        db.add(ocpi); db.flush()
        db.add(FacturaProveedorItem(
            factura_id=factura.id, ocp_item_id=ocpi.id, descripcion=it.descripcion,
            qty_facturada=1, weight_lbs=_peso, unit_price_usd=fob,
        ))
        db.add(EmbarqueItem(
            embarque_id=embarque.id, item_cotizacion_id=it.id, oc_proveedor_id=ocp.id,
        ))
    db.commit()
    return SimpleNamespace(user=user, cot=cot, items=items, ocp=ocp,
                           factura=factura, embarque=embarque)


def _cleanup(db):
    """Borra TODO lo marcado con MARK, en orden seguro de FKs (idempotente)."""
    like = f"%{MARK}%"
    from models.models import PreEmbarque
    # 1) Pricing primero (cascade borra gastos + snapshot, que apuntan a embarque_items/items)
    for emb in db.query(Embarque).filter(Embarque.numero.like(like)).all():
        pr = db.query(EmbarquePricing).filter(EmbarquePricing.embarque_id == emb.id).first()
        if pr:
            db.delete(pr); db.flush()
        db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == emb.id).delete(); db.flush()
        db.delete(emb); db.flush()
    # 1.5) Pre-embarques de prueba (cascade borra sus items, que apuntan a items_cotizacion)
    for pre in db.query(PreEmbarque).filter(PreEmbarque.numero.like(like)).all():
        db.delete(pre); db.flush()
    # 2) Facturas de proveedor (items → cabecera)
    for f in db.query(FacturaProveedor).filter(FacturaProveedor.invoice_no.like(like)).all():
        db.query(FacturaProveedorItem).filter(FacturaProveedorItem.factura_id == f.id).delete()
        db.delete(f); db.flush()
    # 3) OC proveedor (items → cabecera) — antes de items_cotizacion
    for o in db.query(OcProveedor).filter(OcProveedor.numero.like(like)).all():
        db.query(OcProveedorItem).filter(OcProveedorItem.oc_proveedor_id == o.id).delete()
        db.delete(o); db.flush()
    # 4) Cotización + sus ítems
    for c in db.query(Cotizacion).filter(Cotizacion.numero.like(like)).all():
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == c.id).delete()
        db.delete(c); db.flush()
    # 5) Usuario de prueba
    db.query(User).filter(User.nombre == MARK).delete()
    db.commit()


def run():
    db = SessionLocal()
    try:
        _cleanup(db)  # limpia cualquier resto de corridas anteriores
        ids = _seed(db)
        user = ids.user
        emb_id = ids.embarque.id

        # 1) Detalle: crea pricing diferido + toma FOB de la factura del proveedor
        detail = R.detalle_embarque_pricing(emb_id, db=db, current_user=user)
        assert detail["pricing"]["estado"] == "borrador"
        assert len(detail["gastos"]) == 6, "deben sembrarse 6 líneas de gastos"
        assert len(detail["items"]) == 3
        for r in detail["items"]:
            assert r["fob_origen"] == "factura", r["fob_origen"]
        fobs = {r["numero_parte"]: r["fob_unit"] for r in detail["items"]}
        assert _aprox(fobs["7T-1997"], 124) and _aprox(fobs["1R-0749"], 1420), fobs
        print("OK 1) FOB tomado de la factura del proveedor + 6 gastos sembrados")

        # 2) Guardar: TC 962, shipping 450.000 CLP, gastos que capitalizan = 340.000
        gastos_payload = [
            {"tipo": "desconsolidacion", "glosa": "Desconsolidación", "monto_neto": 90_000, "iva": 17_100, "capitaliza": True, "orden": 1},
            {"tipo": "almacenaje", "glosa": "Almacenaje", "monto_neto": 90_000, "iva": 17_100, "capitaliza": True, "orden": 2},
            {"tipo": "agencia", "glosa": "Agencia de Aduana", "monto_neto": 160_000, "iva": 30_400, "capitaliza": True, "orden": 3},
            {"tipo": "arancel", "glosa": "Arancel / Derechos", "monto_neto": 0, "iva": 0, "capitaliza": True, "orden": 4},
            {"tipo": "otros", "glosa": "Otros", "monto_neto": 0, "iva": 0, "capitaliza": True, "orden": 5},
            {"tipo": "iva_importacion", "glosa": "IVA Importación", "monto_neto": 500_000, "iva": 0, "capitaliza": False, "orden": 6},
        ]
        payload = R.PricingSaveIn(
            tc_tipo="manual", tc_valor=962, flete_en_me=False, shipping_clp=450_000,
            gastos=[R.GastoIn(**g) for g in gastos_payload],
        )
        detail = R.guardar_embarque_pricing(emb_id, payload, db=db, current_user=user)

        assert _aprox(detail["totales_gastos"]["total_capitaliza"], 340_000), detail["totales_gastos"]
        assert _aprox(detail["totales_gastos"]["iva_importacion"], 500_000)
        assert _aprox(detail["totales"]["shipping_clp"], 450_000)
        assert _aprox(detail["totales"]["gastos_clp"], 340_000)
        # Shipping por peso: 7T-1997 (0.3/3.6 de 450k) = 37.500
        row0 = next(r for r in detail["items"] if r["numero_parte"] == "7T-1997")
        assert _aprox(row0["shipping_clp"], 37_500), row0["shipping_clp"]
        # FOB CLP = 124 × 962
        assert _aprox(row0["fob_clp"], 124 * 962)
        assert detail["pricing"]["estado"] == "calculado"
        print("OK 2) Guardar calcula landed (shipping por peso, gastos por CIF) y cuadra")

        # 3) Snapshot persistido en DB
        pricing = db.query(EmbarquePricing).filter(EmbarquePricing.embarque_id == emb_id).first()
        snap = db.query(EmbarquePricingItem).filter(EmbarquePricingItem.pricing_id == pricing.id).all()
        assert len(snap) == 3
        suma = sum(float(s.costo_total_clp) for s in snap)
        assert _aprox(suma, detail["totales"]["costo_total_clp"]), (suma, detail["totales"]["costo_total_clp"])
        print("OK 3) Snapshot por ítem persistido y cuadra con el total")

        # 3b) Cuadre POR ÍTEM: costo_total = fob_clp + shipping_clp + gastos_clp
        for it in detail["items"]:
            assert _aprox(it["costo_total_clp"], it["fob_clp"] + it["shipping_clp"] + it["gastos_clp"]), it
        print("OK 3b) Cuadre por ítem (costo_total = FOB + shipping + gastos)")

        # 4) Override de FOB manual en un ítem
        eiid = row0["embarque_item_id"]
        payload2 = R.PricingSaveIn(items=[R.ItemOverrideIn(embarque_item_id=eiid, fob_unit=200, fob_manual=True)])
        detail = R.guardar_embarque_pricing(emb_id, payload2, db=db, current_user=user)
        row0b = next(r for r in detail["items"] if r["embarque_item_id"] == eiid)
        assert _aprox(row0b["fob_unit"], 200) and row0b["fob_origen"] == "manual", row0b
        # Quitar el override → vuelve al FOB de la factura (124)
        payload3 = R.PricingSaveIn(items=[R.ItemOverrideIn(embarque_item_id=eiid, fob_manual=False)])
        detail = R.guardar_embarque_pricing(emb_id, payload3, db=db, current_user=user)
        row0c = next(r for r in detail["items"] if r["embarque_item_id"] == eiid)
        assert _aprox(row0c["fob_unit"], 124), row0c
        print("OK 4) Override de FOB manual se aplica y se revierte al default")

        # 5) Cerrar bloquea la edición; reabrir la habilita
        R.cerrar_pricing(emb_id, db=db, current_user=user)
        try:
            R.guardar_embarque_pricing(emb_id, R.PricingSaveIn(tc_valor=1000), db=db, current_user=user)
            raise AssertionError("debía rechazar edición con pricing cerrado")
        except Exception as e:
            assert getattr(e, "status_code", None) == 409, e
        R.reabrir_pricing(emb_id, db=db, current_user=user)
        d = R.detalle_embarque_pricing(emb_id, db=db, current_user=user)
        assert d["pricing"]["estado"] in ("calculado", "borrador")
        print("OK 5) Cerrar bloquea edición (409) y reabrir la habilita")

        # 6) La lista incluye el embarque con su costo
        lst = R.listar_embarques_pricing(db=db, current_user=user)
        mine = [x for x in lst if x["embarque_id"] == emb_id]
        assert mine and mine[0]["costo_total_clp"] and mine[0]["costo_total_clp"] > 0, mine
        print("OK 6) La lista muestra el embarque con su costo total")

        # 7) Multi-OC: el mismo ítem en 2 OCs con precios distintos → el embarque
        #    debe usar el FOB de SU orden, no el de la otra (regresión HIGH-1).
        cot2 = Cotizacion(numero=f"{MARK}-COT2", cliente="Cliente Test 2"); db.add(cot2); db.flush()
        it2 = ItemCotizacion(cotizacion_id=cot2.id, numero_parte="ZZ-9999",
                             descripcion="DESC ZZ", cantidad=1, peso_unit_lbs=1, precio_unit_cotizacion=0)
        db.add(it2); db.flush()
        ocpA = OcProveedor(numero=f"{MARK}-OCP-A", proveedor="PROV A", moneda="USD"); db.add(ocpA)
        ocpB = OcProveedor(numero=f"{MARK}-OCP-B", proveedor="PROV B", moneda="USD"); db.add(ocpB); db.flush()
        oiA = OcProveedorItem(oc_proveedor_id=ocpA.id, item_cotizacion_id=it2.id); db.add(oiA)
        oiB = OcProveedorItem(oc_proveedor_id=ocpB.id, item_cotizacion_id=it2.id); db.add(oiB); db.flush()
        fA = FacturaProveedor(ocp_id=ocpA.id, invoice_no=f"{MARK}-INV-A"); db.add(fA)
        fB = FacturaProveedor(ocp_id=ocpB.id, invoice_no=f"{MARK}-INV-B"); db.add(fB); db.flush()
        db.add(FacturaProveedorItem(factura_id=fA.id, ocp_item_id=oiA.id, qty_facturada=1, unit_price_usd=100))
        db.add(FacturaProveedorItem(factura_id=fB.id, ocp_item_id=oiB.id, qty_facturada=1, unit_price_usd=999))
        emb2 = Embarque(numero=f"{MARK}-EMB2", estado="en_bodega", forwarder="LATAM Cargo"); db.add(emb2); db.flush()
        # El ítem del embarque apunta a la OC B (precio 999)
        db.add(EmbarqueItem(embarque_id=emb2.id, item_cotizacion_id=it2.id, oc_proveedor_id=ocpB.id))
        db.commit()

        d2 = R.detalle_embarque_pricing(emb2.id, db=db, current_user=user)
        fob = d2["items"][0]["fob_unit"]
        assert _aprox(fob, 999), f"esperaba 999 (OC B), obtuvo {fob}"
        print("OK 7) Multi-OC: usa el FOB de la orden del embarque (999), no la otra (100)")

        # 8) Auto-creación (simula el hook de Logística) + defaults de flete por tipo.
        ocpEUR = OcProveedor(numero=f"{MARK}-OCP-EUR", proveedor="BAUKAT GMBH", moneda="EUR"); db.add(ocpEUR); db.flush()
        embBK = Embarque(numero=f"{MARK}-EMB-BK", estado="en_bodega", forwarder="BAUKAT"); db.add(embBK); db.flush()
        itBK = ItemCotizacion(cotizacion_id=cot2.id, numero_parte="BK-1", descripcion="BK", cantidad=1, peso_unit_lbs=1, precio_unit_cotizacion=50); db.add(itBK); db.flush()
        db.add(EmbarqueItem(embarque_id=embBK.id, item_cotizacion_id=itBK.id, oc_proveedor_id=ocpEUR.id)); db.commit()
        prBK = ensure_pricing_for_embarque(db, embBK, commit=True)
        assert prBK.tipo_embarque == "baukat" and bool(prBK.flete_en_me) is True and prBK.moneda == "EUR", \
            (prBK.tipo_embarque, prBK.flete_en_me, prBK.moneda)
        assert ensure_pricing_for_embarque(db, embBK, commit=True).id == prBK.id, "ensure debe ser idempotente"
        print("OK 8) Auto-creación Baukat: flete prepagado por proveedor (ME/EUR), idempotente")

        embFM = Embarque(numero=f"{MARK}-EMB-FM", estado="en_bodega", forwarder="Fast Mark"); db.add(embFM); db.flush()
        itFM = ItemCotizacion(cotizacion_id=cot2.id, numero_parte="FM-1", descripcion="FM", cantidad=1, peso_unit_lbs=1, precio_unit_cotizacion=50); db.add(itFM); db.flush()
        db.add(EmbarqueItem(embarque_id=embFM.id, item_cotizacion_id=itFM.id, oc_proveedor_id=ids.ocp.id)); db.commit()
        prFM = ensure_pricing_for_embarque(db, embFM, commit=True)
        assert prFM.tipo_embarque == "fastmark" and bool(prFM.flete_en_me) is False, (prFM.tipo_embarque, prFM.flete_en_me)
        print("OK 8b) Auto-creación FastMark: flete CLP local (no prepagado)")

        # El detalle expone correlativo y documentos del embarque (trazabilidad).
        dBK = R.detalle_embarque_pricing(embBK.id, db=db, current_user=user)
        assert dBK["pricing"]["correlativo"] == prBK.id
        assert "documentos" in dBK["embarque"] and "packing_list" in dBK["embarque"]["documentos"]
        print("OK 8c) Detalle expone correlativo y documentos del embarque")

        # 9) Validaciones de robustez.
        try:
            R.guardar_embarque_pricing(embBK.id, R.PricingSaveIn(flete_en_me=True, shipping_me=1000, tc_valor=0), db=db, current_user=user)
            raise AssertionError("debía rechazar flete en ME sin TC")
        except Exception as e:
            assert getattr(e, "status_code", None) == 400, e
        print("OK 9) Rechaza flete en ME sin TC (400)")

        embZ = Embarque(numero=f"{MARK}-EMB-Z", estado="en_bodega", forwarder="LATAM"); db.add(embZ); db.flush()
        itZ = ItemCotizacion(cotizacion_id=cot2.id, numero_parte="Z-1", descripcion="Z", cantidad=1, peso_unit_lbs=1, precio_unit_cotizacion=0); db.add(itZ); db.flush()
        db.add(EmbarqueItem(embarque_id=embZ.id, item_cotizacion_id=itZ.id, oc_proveedor_id=ids.ocp.id)); db.commit()
        zero_gastos = [R.GastoIn(tipo=c["tipo"], glosa=c["glosa"], monto_neto=0, iva=0, capitaliza=c["capitaliza"], orden=c["orden"]) for c in GASTOS_CATALOGO]
        R.guardar_embarque_pricing(embZ.id, R.PricingSaveIn(tc_valor=900, flete_en_me=False, shipping_clp=0, gastos=zero_gastos), db=db, current_user=user)
        try:
            R.cerrar_pricing(embZ.id, db=db, current_user=user)
            raise AssertionError("debía rechazar cerrar con costo landed 0")
        except Exception as e:
            assert getattr(e, "status_code", None) == 400, e
        print("OK 9b) Rechaza cerrar con costo landed 0")

        # (El pricing se crea de forma diferida al abrir el embarque en Contabilidad
        #  —ver pasos 1 y 8—, sin tocar el código de Logística: cerrar_pre_embarque
        #  no se modifica.)

        # 10) Gastos predeterminados (6 líneas fijas) con banco y fecha de factura.
        #     El arancel es gasto local SIN IVA. Banco/fecha por línea persisten.
        gastos_pre = []
        for c in GASTOS_CATALOGO:
            monto = 160_000 if c["tipo"] == "agencia" else 0
            iva = 30_400 if c["tipo"] == "agencia" else 0
            banco = "Banco de Chile" if c["tipo"] == "agencia" else None
            fecha = "2026-06-20" if c["tipo"] == "agencia" else None
            gastos_pre.append(R.GastoIn(tipo=c["tipo"], glosa=c["glosa"], monto_neto=monto, iva=iva,
                                        capitaliza=c["capitaliza"], banco=banco, fecha_factura=fecha, orden=c["orden"]))
        dC = R.guardar_embarque_pricing(embFM.id, R.PricingSaveIn(tc_valor=900, flete_en_me=False, shipping_clp=0, gastos=gastos_pre), db=db, current_user=user)
        assert len(dC["gastos"]) == 6, f"esperaba 6 gastos predeterminados, hay {len(dC['gastos'])}"
        ag = next((g for g in dC["gastos"] if g["tipo"] == "agencia"), None)
        assert ag and ag["banco"] == "Banco de Chile" and ag["fecha_factura"] == "2026-06-20", (ag.get("banco"), ag.get("fecha_factura"))
        # El arancel sigue capitalizando pero sin IVA
        ar = next((g for g in dC["gastos"] if g["tipo"] == "arancel"), None)
        assert ar and ar["capitaliza"] is True and ar["iva"] == 0
        assert _aprox(dC["totales_gastos"]["total_capitaliza"], 160_000), dC["totales_gastos"]
        # Persiste tras recargar (banco + fecha incluidos)
        dC2 = R.detalle_embarque_pricing(embFM.id, db=db, current_user=user)
        ag2 = next((g for g in dC2["gastos"] if g["tipo"] == "agencia"), None)
        assert ag2 and ag2["banco"] == "Banco de Chile" and ag2["fecha_factura"] == "2026-06-20"
        print("OK 10) Gastos predeterminados (6) con banco/fecha por línea: persisten; arancel sin IVA")

        # 11) Robustez: el backend fuerza SIEMPRE las 6 líneas + reglas, aunque el
        #     cliente mande menos líneas o valores inválidos (capitaliza/iva).
        gastos_malos = [
            R.GastoIn(tipo="iva_importacion", glosa="x", monto_neto=500_000, iva=95_000, capitaliza=True, orden=99),
            R.GastoIn(tipo="agencia", glosa="x", monto_neto=160_000, iva=30_400, capitaliza=True, orden=1),
        ]
        dR = R.guardar_embarque_pricing(embFM.id, R.PricingSaveIn(tc_valor=900, flete_en_me=False, shipping_clp=0, gastos=gastos_malos), db=db, current_user=user)
        assert len(dR["gastos"]) == 6, f"el backend debe forzar 6 líneas, hay {len(dR['gastos'])}"
        ivimp = next(g for g in dR["gastos"] if g["tipo"] == "iva_importacion")
        assert ivimp["capitaliza"] is False and ivimp["iva"] == 0, ("iva_importacion mal normalizado", ivimp)
        ar3 = next(g for g in dR["gastos"] if g["tipo"] == "arancel")
        assert ar3["iva"] == 0, ("arancel debe ir sin IVA", ar3)
        # iva_importacion (500k) NO debe inflar el total que capitaliza (solo agencia 160k)
        assert _aprox(dR["totales_gastos"]["total_capitaliza"], 160_000), dR["totales_gastos"]
        print("OK 11) Robustez gastos: backend fuerza 6 líneas + reglas (iva_importacion no capitaliza, iva=0)")

        # 12) Peso editable por ítem (override de peso re-prorratea el flete).
        cotP = Cotizacion(numero=f"{MARK}-COTP", cliente="Cliente Peso"); db.add(cotP); db.flush()
        ocpP = OcProveedor(numero=f"{MARK}-OCP-P", proveedor="PROV P", moneda="USD"); db.add(ocpP); db.flush()
        embP = Embarque(numero=f"{MARK}-EMB-P", estado="en_bodega", forwarder="LATAM"); db.add(embP); db.flush()
        # A: peso cotización 1.0 · B: peso cotización 3.0 · mismo FOB (100) → shipping solo por peso
        itA = ItemCotizacion(cotizacion_id=cotP.id, numero_parte="PA-1", descripcion="A", cantidad=1, peso_unit_lbs=1.0, precio_unit_cotizacion=100); db.add(itA)
        itB = ItemCotizacion(cotizacion_id=cotP.id, numero_parte="PB-1", descripcion="B", cantidad=1, peso_unit_lbs=3.0, precio_unit_cotizacion=100); db.add(itB); db.flush()
        for it in (itA, itB):
            oi = OcProveedorItem(oc_proveedor_id=ocpP.id, item_cotizacion_id=it.id); db.add(oi)
            db.add(EmbarqueItem(embarque_id=embP.id, item_cotizacion_id=it.id, oc_proveedor_id=ocpP.id))
        db.commit()

        # Base: TC 1000, shipping 40.000 CLP, sin gastos → shipping 1:3 = 10.000 / 30.000
        base = R.guardar_embarque_pricing(embP.id, R.PricingSaveIn(tc_valor=1000, flete_en_me=False, shipping_clp=40_000), db=db, current_user=user)
        A = next(r for r in base["items"] if r["numero_parte"] == "PA-1")
        B = next(r for r in base["items"] if r["numero_parte"] == "PB-1")
        assert _aprox(A["shipping_clp"], 10_000) and _aprox(B["shipping_clp"], 30_000), (A["shipping_clp"], B["shipping_clp"])
        assert A["peso_origen"] == "auto" and _aprox(A["peso_unit_lbs"], 1.0)
        eiA = A["embarque_item_id"]

        # 12a) Override peso A → 3.0: ahora 3:3 = 20.000 / 20.000; Σ shipping intacta (40.000)
        d = R.guardar_embarque_pricing(embP.id, R.PricingSaveIn(items=[R.ItemOverrideIn(embarque_item_id=eiA, peso_unit_lbs=3.0, peso_manual=True)]), db=db, current_user=user)
        A = next(r for r in d["items"] if r["embarque_item_id"] == eiA)
        B = next(r for r in d["items"] if r["numero_parte"] == "PB-1")
        assert _aprox(A["peso_unit_lbs"], 3.0) and A["peso_origen"] == "manual", A
        assert _aprox(A["shipping_clp"], 20_000) and _aprox(B["shipping_clp"], 20_000), (A["shipping_clp"], B["shipping_clp"])
        assert _aprox(A["shipping_clp"] + B["shipping_clp"], 40_000)
        print("OK 12a) Override de peso re-prorratea el flete; Σ shipping intacta")

        # 12b) Quitar override (peso_manual=False) → vuelve al peso de la cotización (1.0)
        d = R.guardar_embarque_pricing(embP.id, R.PricingSaveIn(items=[R.ItemOverrideIn(embarque_item_id=eiA, peso_manual=False)]), db=db, current_user=user)
        A = next(r for r in d["items"] if r["embarque_item_id"] == eiA)
        assert _aprox(A["peso_unit_lbs"], 1.0) and A["peso_origen"] == "auto", A
        assert _aprox(A["shipping_clp"], 10_000), A["shipping_clp"]
        print("OK 12b) Quitar override vuelve al peso de la cotización")

        # 12c) Manual <= 0 → auto (no pisa la cotización)
        d = R.guardar_embarque_pricing(embP.id, R.PricingSaveIn(items=[R.ItemOverrideIn(embarque_item_id=eiA, peso_unit_lbs=0, peso_manual=True)]), db=db, current_user=user)
        A = next(r for r in d["items"] if r["embarque_item_id"] == eiA)
        assert _aprox(A["peso_unit_lbs"], 1.0) and A["peso_origen"] == "auto", A
        print("OK 12c) Peso manual <= 0 se ignora (cae al peso de la cotización)")

        # 12d) FOB manual + editar SOLO peso NO revierte el FOB (contrato tri-estado)
        d = R.guardar_embarque_pricing(embP.id, R.PricingSaveIn(items=[R.ItemOverrideIn(embarque_item_id=eiA, fob_unit=555, fob_manual=True)]), db=db, current_user=user)
        d = R.guardar_embarque_pricing(embP.id, R.PricingSaveIn(items=[R.ItemOverrideIn(embarque_item_id=eiA, peso_unit_lbs=2.0, peso_manual=True)]), db=db, current_user=user)
        A = next(r for r in d["items"] if r["embarque_item_id"] == eiA)
        assert _aprox(A["fob_unit"], 555) and A["fob_origen"] == "manual", ("FOB no debe revertirse al editar peso", A)
        assert _aprox(A["peso_unit_lbs"], 2.0) and A["peso_origen"] == "manual", A
        print("OK 12d) Editar solo el peso no revierte el FOB manual (overrides independientes)")

        # 12e) Cerrado congela el peso manual; reabrir lo mantiene
        R.cerrar_pricing(embP.id, db=db, current_user=user)
        dc = R.detalle_embarque_pricing(embP.id, db=db, current_user=user)
        A = next(r for r in dc["items"] if r["embarque_item_id"] == eiA)
        assert A["peso_origen"] == "manual" and _aprox(A["peso_unit_lbs"], 2.0), ("snapshot debe congelar el peso", A)
        assert A["peso_default"] == A["peso_unit_lbs"], "cerrado: default == valor congelado"
        R.reabrir_pricing(embP.id, db=db, current_user=user)
        print("OK 12e) Cerrado congela el peso manual (snapshot); reabrir lo mantiene")

        # 12f) Todos los pesos de cotización en 0 → fallback por FOB sigue; override>0 domina
        cotZ2 = Cotizacion(numero=f"{MARK}-COTZ2", cliente="Cli Z2"); db.add(cotZ2); db.flush()
        ocpZ2 = OcProveedor(numero=f"{MARK}-OCP-Z2", proveedor="PROV Z2", moneda="USD"); db.add(ocpZ2); db.flush()
        embZ2 = Embarque(numero=f"{MARK}-EMB-Z2", estado="en_bodega", forwarder="LATAM"); db.add(embZ2); db.flush()
        izA = ItemCotizacion(cotizacion_id=cotZ2.id, numero_parte="ZA", descripcion="ZA", cantidad=1, peso_unit_lbs=0, precio_unit_cotizacion=100); db.add(izA)
        izB = ItemCotizacion(cotizacion_id=cotZ2.id, numero_parte="ZB", descripcion="ZB", cantidad=1, peso_unit_lbs=0, precio_unit_cotizacion=100); db.add(izB); db.flush()
        for it in (izA, izB):
            db.add(OcProveedorItem(oc_proveedor_id=ocpZ2.id, item_cotizacion_id=it.id))
            db.add(EmbarqueItem(embarque_id=embZ2.id, item_cotizacion_id=it.id, oc_proveedor_id=ocpZ2.id))
        db.commit()
        dz = R.guardar_embarque_pricing(embZ2.id, R.PricingSaveIn(tc_valor=1000, flete_en_me=False, shipping_clp=40_000), db=db, current_user=user)
        # Sin peso en ninguno → shipping por FOB (iguales) → 20.000 / 20.000
        assert _aprox(sum(r["shipping_clp"] for r in dz["items"]), 40_000)
        for r in dz["items"]:
            assert _aprox(r["shipping_clp"], 20_000), r["shipping_clp"]
        eiZA = next(r for r in dz["items"] if r["numero_parte"] == "ZA")["embarque_item_id"]
        # Override peso ZA=5 (único con peso) → se lleva TODO el shipping
        dz = R.guardar_embarque_pricing(embZ2.id, R.PricingSaveIn(items=[R.ItemOverrideIn(embarque_item_id=eiZA, peso_unit_lbs=5, peso_manual=True)]), db=db, current_user=user)
        za = next(r for r in dz["items"] if r["embarque_item_id"] == eiZA)
        assert _aprox(za["shipping_clp"], 40_000), za["shipping_clp"]
        print("OK 12f) Cotización con pesos 0 → fallback por FOB; un override de peso>0 domina el prorrateo")

        print("\n✅ Integración OK")
    finally:
        try:
            db.rollback()
            _cleanup(db)
            print("🧹 Datos de prueba eliminados (DB intacta)")
        except Exception as e:  # pragma: no cover
            db.rollback()
            print(f"⚠️  cleanup falló: {e}")
        db.close()


def test_integration_flujo_completo():
    run()


if __name__ == "__main__":
    run()
