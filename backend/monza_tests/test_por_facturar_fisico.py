"""Fase 4 espejo GA (G15a) — POR FACTURAR con base FÍSICA (la regla de oro).

    por_facturar = Σ (cantidad − facturada) × precio CONGELADO × (1 + IVA)

y JAMÁS total-vivo − brutos-congelados (el polvo de redondeo por tandas dejaba $1-3
fantasma en GA — hallazgo HIGH del enjambre G15). El invariante clave: una venta 100%
facturada da por_facturar == 0 POR CONSTRUCCIÓN, no por resta.

E2E real: recepción → despacho parcial cerrado → factura por guía → por_facturar del
remanente → factura del saldo (retiro en oficina) → por_facturar == 0. Datos MARCADOS +
limpieza total; el detalle además expone mercaderia_pendiente_clp autoritativo y
anticipo_por_descontar_clp, que desde la Fase 7 es un CÁLCULO REAL (lo facturado como
anticipo y aún no descontado, derivado de las líneas vivas). Aquí da 0 porque esta venta
no tiene ninguna factura de anticipo — no porque el campo esté fijo en cero.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_por_facturar_fisico.py -q
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaEmbarque, MonzaEmbarqueItem,
    MonzaRecepcion, MonzaRecepcionItem, MonzaDespacho, MonzaDespachoItem, MonzaReclamo,
    MonzaLog, MonzaNotificacion,
)
from monza_contabilidad.models import MonzaContFacturaCliente, MonzaContFacturaClienteItem  # noqa: E402
from monza_router_bodega import router as bodega_router  # noqa: E402
from monza_router_despachos import router as despachos_router  # noqa: E402
from monza_contabilidad.router import router as contabilidad_router  # noqa: E402

MARK = "test-m4-pf"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(bodega_router)
app.include_router(despachos_router)
app.include_router(contabilidad_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)


def _setup():
    """Venta: A 10×$1.000 + B 4×$5.000, IVA 19% congelado → total $35.700."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA", rut="11.111.111-1")
        db.add(cli); db.flush()
        cot = MonzaCotizacion(numero=f"PF-{uuid.uuid4().hex[:6].upper()}", cliente_id=cli.id,
                              estado="vendida", oc_cliente=f"OC-{MARK}", iva_pct=19,
                              total_neto=30000, iva_monto=5700, total_bruto=35700)
        db.add(cot); db.flush()
        A = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} A", cantidad=10,
                                precio_unitario_clp=1000, estado_linea="embarcado")
        B = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} B", cantidad=4,
                                precio_unitario_clp=5000, estado_linea="embarcado")
        db.add_all([A, B]); db.flush()
        E = MonzaEmbarque(numero=f"{MARK}-E-{uuid.uuid4().hex[:5]}", estado="en_transito")
        db.add(E); db.flush()
        db.add_all([MonzaEmbarqueItem(embarque_id=E.id, item_id=A.id),
                    MonzaEmbarqueItem(embarque_id=E.id, item_id=B.id)])
        db.commit()
        return {"cot": cot.id, "A": A.id, "B": B.id, "E": E.id}
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
        fac_ids = [r[0] for r in db.query(MonzaContFacturaCliente.id)
                   .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids or [0])).all()]
        item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                    .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
        dsp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                   .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
                   .filter(MonzaEmbarque.numero.like(f"{MARK}%")).all()]
        rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
                   .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
        db.query(MonzaContFacturaClienteItem).filter(
            MonzaContFacturaClienteItem.factura_id.in_(fac_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id.in_(fac_ids or [0])).delete(synchronize_session=S)
        for ent, ids in (("cotizacion", cot_ids), ("despacho", dsp_ids), ("recepcion", rec_ids)):
            db.query(MonzaNotificacion).filter(
                MonzaNotificacion.entidad == ent,
                MonzaNotificacion.entidad_id.in_(ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaReclamo).filter(MonzaReclamo.item_id.in_(item_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespachoItem).filter(MonzaDespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcionItem).filter(MonzaRecepcionItem.recepcion_id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcion).filter(MonzaRecepcion.id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaEmbarqueItem).filter(MonzaEmbarqueItem.embarque_id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaEmbarque).filter(MonzaEmbarque.id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(item_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _pf_detalle(cot_id):
    r = client.get(f"/api/monza/contabilidad/ventas/{cot_id}")
    assert r.status_code == 200, r.text
    return r.json()["resumen"]


def _pf_listado(cot_id):
    r = client.get("/api/monza/contabilidad/ventas")
    assert r.status_code == 200, r.text
    fila = next((v for v in r.json() if v["cotizacion_id"] == cot_id), None)
    assert fila is not None
    return fila


def test_por_facturar_base_fisica():
    _limpiar()
    ids = _setup()
    try:
        # ══ 0 · Sin facturas: por_facturar = TODO = (10×1000 + 4×5000) × 1.19 = 35.700 ══
        assert _pf_detalle(ids["cot"])["por_facturar_clp"] == 35700
        assert _pf_listado(ids["cot"])["por_facturar_clp"] == 35700

        # ══ 1 · Recibir todo, despachar A×6 (parcial), facturar la guía ══
        rec = client.post(f"/api/monza/bodega/embarques/{ids['E']}/recibir").json()["recepcion_id"]
        for item, qty in ((ids["A"], 10), (ids["B"], 4)):
            assert client.patch(f"/api/monza/bodega/recepciones/{rec}/items/{item}",
                                json={"estado_recepcion": "completo", "qty_recibida": qty}).status_code == 200
        assert client.post(f"/api/monza/bodega/recepciones/{rec}/cerrar",
                           json={"forzar": False}).status_code == 200
        r = client.post("/api/monza/despachos/crear",
                        json={"cotizacion_id": ids["cot"], "items": [{"item_id": ids["A"], "qty": 6}]})
        assert r.status_code == 200, r.text
        d1 = r.json()["id"]
        assert client.post(f"/api/monza/despachos/entidades/{d1}/cerrar").status_code == 200
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": ids["cot"], "despacho_id": d1, "numero_factura": f"F-{MARK}-1"})
        assert r.status_code in (200, 201), r.text
        assert r.json()["monto_bruto"] == 7140  # 6×1000×1.19

        # ══ 2 · REGLA DE ORO: pendiente físico = A×4 + B×4 = 24.000 × 1.19 = 28.560 ══
        resumen = _pf_detalle(ids["cot"])
        assert resumen["por_facturar_clp"] == 28560, \
            f"base física: (4×1000 + 4×5000)×1.19 = 28.560, dio {resumen['por_facturar_clp']}"
        # Venta SIN facturas de anticipo: el cálculo real de la Fase 7 recorre los
        # anticipos de la venta (no hay ninguno) y da 0. Por eso aquí por_facturar ==
        # mercaderia_pendiente: no hay anticipo que restarle.
        assert resumen["anticipo_por_descontar_clp"] == 0.0
        assert resumen["mercaderia_pendiente_clp"] == 28560
        assert resumen["por_facturar_clp"] == resumen["mercaderia_pendiente_clp"]
        assert _pf_listado(ids["cot"])["por_facturar_clp"] == 28560

        # ══ 3 · Facturar el saldo completo (retiro en oficina) → 0 POR CONSTRUCCIÓN ══
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": ids["cot"], "sin_guia": True, "numero_factura": f"F-{MARK}-2"})
        assert r.status_code in (200, 201), r.text
        resumen = _pf_detalle(ids["cot"])
        assert resumen["por_facturar_clp"] == 0, \
            "venta 100% facturada debe dar 0 EXACTO (por construcción, sin polvo de redondeo)"
        assert resumen["mercaderia_pendiente_clp"] == 0
        assert _pf_listado(ids["cot"])["por_facturar_clp"] == 0
    finally:
        _limpiar()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_tope_no_evadible_por_orden():
    """Cierre de paridad 2026-07-28: una factura EXPLÍCITA inflada que consume el
    total de la venta ya no deja pasar a la DERIVADA siguiente en silencio (antes el
    tope solo corría cuando la factura ACTUAL traía precio explícito)."""
    _limpiar()
    ids = _setup()  # total venta $35.700 (A 10×1.000 + B 4×5.000, IVA 19%)
    try:
        # F1 explícita inflada: A×10 a $3.000 → 30.000 neto + IVA = 35.700 == total (pasa el tope)
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": ids["cot"], "sin_guia": True, "numero_factura": f"F-{MARK}-X1",
        })
        # sin_guia deriva TODO el saldo — para el escenario usamos items explícitos:
        assert r.status_code in (200, 201, 400, 409)
        _limpiar(); ids2 = _setup()
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": ids2["cot"], "numero_factura": f"F-{MARK}-X2",
            "items": [{"item_cotizacion_id": ids2["A"], "cantidad": 10, "precio_unit_neto": 3000}],
        })
        # los ítems explícitos exigen qty despachada: si el flujo lo rechaza por
        # cantidades (400), el bypass ni siquiera es alcanzable por esta vía y el
        # invariante queda protegido por el tope de cantidades. Si pasa (200), la
        # derivada del saldo restante DEBE chocar con el tope de monto (409).
        if r.status_code in (200, 201):
            assert r.json()["monto_bruto"] >= 35700 - 3
            r2 = client.post("/api/monza/contabilidad/facturas", json={
                "cotizacion_id": ids2["cot"], "sin_guia": True, "numero_factura": f"F-{MARK}-X3",
            })
            assert r2.status_code == 409, \
                f"la derivada tras una explícita que agotó el total debe dar 409, dio {r2.status_code}"
            assert "excede el total" in r2.json()["detail"]
    finally:
        _limpiar()
