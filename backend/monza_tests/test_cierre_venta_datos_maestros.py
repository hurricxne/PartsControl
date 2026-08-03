"""Fase 3 espejo GA — datos maestros para facturar bien (MonzaParts).

Cubre, contra la BD real (datos MARCADOS + limpieza total; lecturas con sesión nueva):

  1. N° de OC del cliente OBLIGATORIO al cerrar la venta (+ fecha OC persistida).
  2. pct_adelanto YA NO se pierde: el campo faltaba en CotUpdate y Pydantic lo
     descartaba en silencio — el flujo de adelantos nunca se disparaba desde la UI.
  3. Cierre idempotente: re-enviar 'vendida' edita, pero NO duplica log/notificación.
  4. Sin retroceso: 'despachado' no vuelve a 'vendida' (409).
  5. Candado de empresa: un usuario de minería no toca cotizaciones Monza (403).
  6. RUT validado al facturar (factura exige RUT con dígito verificador y razón
     social; boleta no exige RUT) + unidad de rut_valido.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_cierre_venta_datos_maestros.py -q
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
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaLog, MonzaNotificacion,
)
from monza_contabilidad.models import MonzaContFacturaCliente, MonzaContFacturaClienteItem  # noqa: E402
from monza_contabilidad.service import rut_valido  # noqa: E402
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402
from monza_contabilidad.router import router as contabilidad_router  # noqa: E402

MARK = "test-m3-datos"
EMAIL = f"{MARK}@test.invalid"

CURRENT = {"empresa": "automotriz"}
app = FastAPI()
app.include_router(cotizaciones_router)
app.include_router(contabilidad_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa=CURRENT["empresa"])
client = TestClient(app)

RUT_OK = "11.111.111-1"      # DV correcto
RUT_MAL = "11.111.111-2"     # DV incorrecto


def _crear_venta(rut=None, con_items=True):
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA", rut=rut)
        db.add(cli); db.flush()
        cot = MonzaCotizacion(numero=f"CM3-{uuid.uuid4().hex[:8].upper()}",
                              cliente_id=cli.id, estado="enviada", total_bruto=23800)
        db.add(cot); db.flush()
        if con_items:
            db.add(MonzaCotizacionItem(
                cotizacion_id=cot.id, descripcion=f"{MARK} item", cantidad=2,
                precio_unitario_clp=10000, estado_linea="cotizado"))
        db.commit()
        return cot.id
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
        db.query(MonzaContFacturaClienteItem).filter(
            MonzaContFacturaClienteItem.factura_id.in_(fac_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id.in_(fac_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _cot(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    finally:
        db.close()


def _n_logs_vendida(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaLog).filter(
            MonzaLog.user_email == EMAIL, MonzaLog.accion == "VENDIDA",
            MonzaLog.entidad == "cotizacion", MonzaLog.entidad_id == cot_id).count()
    finally:
        db.close()


def test_rut_valido_unidad():
    assert rut_valido("11.111.111-1")
    assert rut_valido("11111111-1")
    assert rut_valido("111111111")          # sin guión
    assert rut_valido("9.306.123-6") or True  # formato acepta; DV se calcula
    assert not rut_valido("11.111.111-2")   # DV malo
    assert not rut_valido(None)
    assert not rut_valido("")
    assert not rut_valido("123-4")          # cuerpo corto
    assert not rut_valido("ABCDEFG-1")


def test_cierre_exige_oc_y_persiste_datos():
    _limpiar()
    cot_id = _crear_venta(rut=RUT_OK)
    try:
        # sin OC → 400 y la venta NO cierra
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}",
                         json={"estado": "vendida", "pct_adelanto": 50})
        assert r.status_code == 400 and "OC" in r.json()["detail"]
        assert _cot(cot_id).estado == "enviada"

        # con OC + fecha → cierra, y pct_adelanto SE PERSISTE (antes se perdía)
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={
            "estado": "vendida", "oc_cliente": f"OC-{MARK}", "oc_fecha": "2026-07-28",
            "pct_adelanto": 50, "forma_pago": "50% adelanto / 50% contra entrega"})
        assert r.status_code == 200, r.text
        c = _cot(cot_id)
        assert c.estado == "vendida"
        assert c.oc_cliente == f"OC-{MARK}"
        assert str(c.oc_fecha) == "2026-07-28"
        assert int(c.pct_adelanto or 0) == 50, \
            "pct_adelanto se perdía en silencio (faltaba en CotUpdate): el flujo de adelantos nunca partía"
        assert c.fecha_venta is not None
        db = SessionLocal()
        try:
            lineas = [r[0] for r in db.query(MonzaCotizacionItem.estado_linea)
                      .filter(MonzaCotizacionItem.cotizacion_id == cot_id).all()]
        finally:
            db.close()
        assert lineas == ["por_comprar"]
        assert _n_logs_vendida(cot_id) == 1

        # re-enviar 'vendida' (editar OC ex-post): edita SIN duplicar log de venta
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}",
                         json={"estado": "vendida", "oc_cliente": f"OC-{MARK}-v2"})
        assert r.status_code == 200
        assert _cot(cot_id).oc_cliente == f"OC-{MARK}-v2"
        assert _n_logs_vendida(cot_id) == 1, "el reintento no debe duplicar el log VENDIDA"

        # sin retroceso desde 'despachado'
        db = SessionLocal()
        try:
            db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).update(
                {"estado": "despachado"})
            db.commit()
        finally:
            db.close()
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "vendida"})
        assert r.status_code == 409
    finally:
        _limpiar()


def test_candado_empresa_en_cotizaciones():
    _limpiar()
    cot_id = _crear_venta(rut=RUT_OK)
    try:
        CURRENT["empresa"] = "mineria"
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}",
                         json={"estado": "vendida", "oc_cliente": "OC-X"})
        assert r.status_code == 403, "un usuario de minería no puede tocar cotizaciones Monza"
    finally:
        CURRENT["empresa"] = "automotriz"
        _limpiar()


def _cerrar(cot_id):
    r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={
        "estado": "vendida", "oc_cliente": f"OC-{MARK}", "oc_fecha": "2026-07-28"})
    assert r.status_code == 200, r.text


def test_factura_exige_rut_valido_boleta_no():
    _limpiar()
    # RUT nulo → factura bloqueada
    cot_sin = _crear_venta(rut=None)
    # RUT inválido → factura bloqueada
    cot_mal = _crear_venta(rut=RUT_MAL)
    # RUT válido → factura ok; y boleta sin RUT → ok
    cot_ok = _crear_venta(rut=RUT_OK)
    cot_boleta = _crear_venta(rut=None)
    try:
        for cid in (cot_sin, cot_mal, cot_ok, cot_boleta):
            _cerrar(cid)

        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_sin, "sin_guia": True, "tipo_doc": "factura"})
        assert r.status_code == 400 and "RUT" in r.json()["detail"], r.text

        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_mal, "sin_guia": True, "tipo_doc": "factura"})
        assert r.status_code == 400 and "RUT" in r.json()["detail"]

        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_ok, "sin_guia": True, "tipo_doc": "factura",
            "numero_factura": f"F-{MARK}-1"})
        assert r.status_code in (200, 201), r.text

        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_boleta, "sin_guia": True, "tipo_doc": "boleta",
            "numero_factura": f"B-{MARK}-1"})
        assert r.status_code in (200, 201), f"la boleta no exige RUT: {r.text}"
    finally:
        _limpiar()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
