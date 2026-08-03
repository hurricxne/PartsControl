"""Auditoría integral Fases 1-6 (MonzaParts) — reparaciones de Cotizaciones + Config.

Cubre, contra la BD real (datos MARCADOS + limpieza total; lecturas con sesión nueva):

  #8  El PATCH con estado='despachado' ya NO marca todas las líneas a ciegas: exige
      COBERTURA REAL de despachos CERRADOS y que la venta venga de 'vendida'.
      (Antes: 5 unidades sin un solo despacho quedaban 'despachado' y la mercadería
       se trababa — POST /despachos/crear respondía 400 "Item no está en bodega".)
  #14 pct_adelanto deja de ser write-only (se serializa) y no BAJA si hay plata de
      adelanto registrada (antes el re-cierre lo dejaba en 0 en silencio y el
      cortafuego de adelanto de Abastecimiento dejaba de frenar la OC de proveedor).
  #20 oc_fecha se serializa, para que el modal no la reinicialice con HOY y pise la
      fecha real que viaja como referencia 801 al SII.
  #5  (punto 1) PUT /api/monza/config rechaza iva_pct = 0 (y > 100): con tasa 0 la
      venta se cerraba sin IVA pero la facturación usaba el 19 % por defecto,
      inventando impuesto ante el SII y dejando mercadería imposible de facturar.

NOTA: los casos de config son SOLO de rechazo (422), a propósito: así la suite nunca
escribe en la fila real de MonzaConfig (id=1). El camino feliz se valida a nivel de
schema, sin tocar la BD.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_aud_cotizaciones.py -q
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaConfig, MonzaCotizacion, MonzaCotizacionItem,
    MonzaDespacho, MonzaDespachoItem, MonzaLog, MonzaNotificacion,
)
from monza_contabilidad.models import MonzaContAdelanto  # noqa: E402
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402
from monza_router_config import router as config_router, ConfigIn  # noqa: E402

MARK = "test-aud-cot"
EMAIL = f"{MARK}@test.invalid"

CURRENT = {"id": 1, "empresa": "automotriz"}
app = FastAPI()
app.include_router(cotizaciones_router)
app.include_router(config_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], email=EMAIL, empresa=CURRENT["empresa"])
client = TestClient(app)


# ── Seed / limpieza ──────────────────────────────────────────────────────────

def _crear_venta(estado="vendida", cantidad=5, estado_linea="en_bodega",
                 pct_adelanto=0, adelanto_verificado=0, oc_fecha=None, con_item=True):
    """Venta MARCADA con (opcionalmente) 1 ítem. Devuelve (cot_id, item_id)."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA")
        db.add(cli); db.flush()
        cot = MonzaCotizacion(
            numero=f"CAUD-{uuid.uuid4().hex[:8].upper()}",
            cliente_id=cli.id, estado=estado, oc_cliente=f"OC-{MARK}",
            oc_fecha=oc_fecha, pct_adelanto=pct_adelanto,
            adelanto_verificado=adelanto_verificado, total_bruto=119000)
        db.add(cot); db.flush()
        item_id = None
        if con_item:
            it = MonzaCotizacionItem(
                cotizacion_id=cot.id, descripcion=f"{MARK} pieza",
                cantidad=cantidad, estado_linea=estado_linea,
                precio_unitario_clp=10000, subtotal_clp=10000 * cantidad)
            db.add(it); db.flush()
            item_id = it.id
        db.commit()
        return cot.id, item_id
    finally:
        db.close()


def _despacho_cerrado(cot_id, item_id, qty):
    """Despacho CERRADO ('despachado') que cubre qty unidades del ítem."""
    db = SessionLocal()
    try:
        d = MonzaDespacho(numero=f"DSP-{MARK}-{uuid.uuid4().hex[:6]}",
                          cotizacion_id=cot_id, estado="despachado")
        db.add(d); db.flush()
        db.add(MonzaDespachoItem(despacho_id=d.id, item_id=item_id, qty_despachada=qty))
        db.commit()
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
        desp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                    .filter(MonzaDespacho.numero.like(f"DSP-{MARK}%")).all()]
        db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id.in_(desp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.id.in_(desp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(
            MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _cot(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    finally:
        db.close()


def _linea(item_id):
    db = SessionLocal()
    try:
        it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == item_id).first()
        return it.estado_linea if it else None
    finally:
        db.close()


# ── #8) 'despachado' exige cobertura real de despachos CERRADOS ──────────────

def test_despachado_sin_despachos_da_409_y_no_traba_la_mercaderia():
    _limpiar()
    cot_id, item_id = _crear_venta(cantidad=5, estado_linea="en_bodega")
    try:
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        assert r.status_code == 409, f"sin un solo despacho CERRADO debía dar 409: {r.text}"
        assert "despachos CERRADOS" in r.json()["detail"]
        # La mercadería NO queda trabada: la línea sigue despachable desde Despachos
        assert _linea(item_id) == "en_bodega", "el flip ciego trababa la mercadería"
        assert _cot(cot_id).estado == "vendida"
    finally:
        _limpiar()


def test_despachado_con_cobertura_parcial_da_409():
    _limpiar()
    cot_id, item_id = _crear_venta(cantidad=5, estado_linea="en_bodega")
    try:
        _despacho_cerrado(cot_id, item_id, 3)  # llegaron 3 de 5
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        assert r.status_code == 409, r.text
        assert "3 de 5" in r.json()["detail"], r.text
        assert _linea(item_id) == "en_bodega"
    finally:
        _limpiar()


def test_despachado_con_cobertura_total_pasa():
    _limpiar()
    cot_id, item_id = _crear_venta(cantidad=5, estado_linea="en_bodega")
    try:
        _despacho_cerrado(cot_id, item_id, 5)
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        assert r.status_code == 200, r.text
        assert _cot(cot_id).estado == "despachado"
        assert _linea(item_id) == "despachado"
        # Idempotente: el re-PATCH sigue pasando (las líneas ya despachadas no se re-validan)
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        assert r.status_code == 200, r.text
    finally:
        _limpiar()


def test_despachado_sin_items_sigue_pasando():
    """Venta administrativa sin líneas: el guard se satisface de forma vacía.

    Es el caso que ejercitan monza_tests/test_cierre_estados_foto.py:191,202."""
    _limpiar()
    cot_id, _ = _crear_venta(con_item=False)
    try:
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        assert r.status_code == 200, r.text
        assert _cot(cot_id).estado == "despachado"
    finally:
        _limpiar()


def test_salto_propuesta_a_despachado_da_409():
    """Sin pasar por 'vendida' se salta la OC obligatoria, fecha_venta y el LTV."""
    _limpiar()
    cot_id, _ = _crear_venta(estado="propuesta", con_item=False)
    try:
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        assert r.status_code == 409, r.text
        assert "cerrada" in r.json()["detail"]
        assert _cot(cot_id).estado == "propuesta"
    finally:
        _limpiar()


# ── #14) pct_adelanto: visible + no baja con plata registrada ────────────────

def test_pct_adelanto_y_oc_fecha_se_serializan():
    """#14 + #20: el modal necesita LEER la condición vigente y la fecha real de OC."""
    _limpiar()
    from datetime import date
    cot_id, _ = _crear_venta(pct_adelanto=50, oc_fecha=date(2026, 6, 2), con_item=False)
    try:
        r = client.get(f"/api/monza/cotizaciones/{cot_id}")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["pct_adelanto"] == 50, "pct_adelanto era write-only: el modal re-elegía a ciegas"
        assert d["adelanto_verificado"] is False
        assert d["oc_fecha"] == "2026-06-02", \
            "oc_fecha era write-only: el modal la reinicializaba con HOY y pisaba la ref 801"
    finally:
        _limpiar()


def test_bajar_pct_adelanto_con_adelanto_registrado_da_409():
    _limpiar()
    cot_id, _ = _crear_venta(pct_adelanto=50, con_item=False)
    try:
        db = SessionLocal()
        try:
            db.add(MonzaContAdelanto(cotizacion_id=cot_id, monto=59500))
            db.commit()
        finally:
            db.close()
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"pct_adelanto": 0})
        assert r.status_code == 409, r.text
        assert "adelanto" in r.json()["detail"].lower()
        assert int(_cot(cot_id).pct_adelanto or 0) == 50, "el 50% no debe perderse en silencio"
    finally:
        _limpiar()


def test_bajar_pct_adelanto_con_bandera_verificada_da_409():
    _limpiar()
    cot_id, _ = _crear_venta(pct_adelanto=50, adelanto_verificado=1, con_item=False)
    try:
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"pct_adelanto": 0})
        assert r.status_code == 409, r.text
        assert int(_cot(cot_id).pct_adelanto or 0) == 50
    finally:
        _limpiar()


def test_bajar_pct_adelanto_sin_plata_se_permite():
    """Corregir la condición de pago antes de que entre plata sigue siendo legítimo."""
    _limpiar()
    cot_id, _ = _crear_venta(pct_adelanto=50, con_item=False)
    try:
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"pct_adelanto": 0})
        assert r.status_code == 200, r.text
        assert int(_cot(cot_id).pct_adelanto or 0) == 0
    finally:
        _limpiar()


def test_subir_pct_adelanto_con_plata_se_permite():
    """El guard es de BAJADA: subir el % no pone en riesgo la plata ya verificada."""
    _limpiar()
    cot_id, _ = _crear_venta(pct_adelanto=50, adelanto_verificado=1, con_item=False)
    try:
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"pct_adelanto": 70})
        assert r.status_code == 200, r.text
        assert int(_cot(cot_id).pct_adelanto or 0) == 70
    finally:
        _limpiar()


# ── #5 punto 1) La config no acepta iva_pct = 0 (ni > 100) ───────────────────

def _iva_actual():
    db = SessionLocal()
    try:
        cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
        return cfg.iva_pct if cfg else None
    finally:
        db.close()


@pytest.mark.parametrize("valor", [0, -5, 150])
def test_config_rechaza_iva_invalido(valor):
    """No escribe nada en la BD: el rechazo ocurre en la validación del schema."""
    antes = _iva_actual()
    r = client.put("/api/monza/config", json={"iva_pct": valor})
    assert r.status_code == 422, f"iva_pct={valor} debía rechazarse: {r.text}"
    assert "IVA" in r.text or "iva" in r.text
    assert _iva_actual() == antes, "la config no debe mutar en un rechazo"


def test_config_acepta_iva_valido_a_nivel_schema():
    """Camino feliz sin tocar la fila real de MonzaConfig (id=1)."""
    assert ConfigIn(iva_pct=19).iva_pct == 19
    assert ConfigIn(iva_pct=100).iva_pct == 100
    # Omitir el campo sigue siendo válido (el PUT es parcial: exclude_none)
    assert ConfigIn(tc_usd_clp=950).iva_pct is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
