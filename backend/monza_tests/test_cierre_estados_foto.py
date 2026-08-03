"""Paridad Monza↔GA — estados del cierre + foto de precios (MonzaParts).

Cubre, contra la BD real (datos MARCADOS + limpieza total; lecturas con sesión nueva):

  1. Sin DES-CIERRE con plata: una venta 'vendida' con factura, adelanto o despacho
     no-anulado NO retrocede a propuesta/enviada/rechazada (409).
  2. Des-cierre SIN plata permitido: corrige un cierre por error recién hecho.
  3. Transición 'despachado' idempotente: el re-PATCH no vuelve a sumar el LTV del
     cliente (bug de plata), ni duplica la notificación ni el log DESPACHADO.
  4. Foto de precios completa al crear: moneda_tarifa congelada en el header y
     tc_aplicado/tarifa_aerea por ítem según su moneda (EUR→tc_eur, USD→tc_usd,
     otro→1.0), resueltos SERVER-side; config_snapshot expone moneda_tarifa.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_cierre_estados_foto.py -q
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
    MonzaDespacho, MonzaLead, MonzaLog, MonzaNotificacion,
)
from monza_contabilidad.models import MonzaContAdelanto, MonzaContFacturaCliente  # noqa: E402
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402

MARK = "test-m3-foto"
LEAD_MARK = "L-M3F"  # MonzaLead.numero es String(20): prefijo corto propio del test
EMAIL = f"{MARK}@test.invalid"

CURRENT = {"id": 1, "empresa": "automotriz"}
app = FastAPI()
app.include_router(cotizaciones_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], email=EMAIL, empresa=CURRENT["empresa"])
client = TestClient(app)


# ── Seed / limpieza ──────────────────────────────────────────────────────────

def _crear_venta(estado="vendida", total_bruto=119000, lead_id=None, cliente_id=None):
    db = SessionLocal()
    try:
        if cliente_id is None:
            cli = MonzaCliente(nombre=f"{MARK} SpA")
            db.add(cli); db.flush()
            cliente_id = cli.id
        cot = MonzaCotizacion(
            numero=f"CMF-{uuid.uuid4().hex[:8].upper()}",
            cliente_id=cliente_id, lead_id=lead_id,
            estado=estado, oc_cliente=f"OC-{MARK}", total_bruto=total_bruto)
        db.add(cot); db.commit()
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
        db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.numero.like(f"DSP-{MARK}%")).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLead).filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).delete(synchronize_session=S)
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


# ── 1) Des-cierre con plata → 409 ────────────────────────────────────────────

def test_des_cierre_con_plata_da_409():
    _limpiar()
    cot_fact = _crear_venta()
    cot_adel = _crear_venta()
    cot_desp = _crear_venta()
    try:
        db = SessionLocal()
        try:
            db.add(MonzaContFacturaCliente(
                cotizacion_id=cot_fact, numero_cotizacion=f"CMF-{MARK}",
                cliente_nombre=f"{MARK} SpA", monto_bruto=119000))
            db.add(MonzaContAdelanto(cotizacion_id=cot_adel, monto=59500))
            db.add(MonzaDespacho(
                numero=f"DSP-{MARK}-{uuid.uuid4().hex[:6]}", cotizacion_id=cot_desp,
                estado="despachado"))
            # Un despacho ANULADO no cuenta como logística colgando
            db.add(MonzaDespacho(
                numero=f"DSP-{MARK}-{uuid.uuid4().hex[:6]}", cotizacion_id=cot_fact,
                estado="anulado"))
            db.commit()
        finally:
            db.close()

        for cot_id, que in ((cot_fact, "factura"), (cot_adel, "adelanto"), (cot_desp, "despacho")):
            r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "enviada"})
            assert r.status_code == 409, f"des-cierre con {que} debía dar 409: {r.text}"
            assert "cerrada" in r.json()["detail"]
            assert _cot(cot_id).estado == "vendida", f"el estado no debe retroceder ({que})"
    finally:
        _limpiar()


# ── 2) Des-cierre sin plata → permitido ──────────────────────────────────────

def test_des_cierre_sin_plata_se_permite():
    _limpiar()
    cot_id = _crear_venta()
    try:
        # Sin facturas/adelanto/despachos: corregir un cierre por error recién hecho
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "enviada"})
        assert r.status_code == 200, r.text
        assert _cot(cot_id).estado == "enviada"
    finally:
        _limpiar()


# ── 3) Re-PATCH 'despachado' idempotente (LTV / notif / log) ─────────────────

def _n_notifs_despacho(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id == cot_id,
            MonzaNotificacion.titulo.like("Despacho realizado%")).count()
    finally:
        db.close()


def _n_logs_despachado(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaLog).filter(
            MonzaLog.user_email == EMAIL, MonzaLog.accion == "DESPACHADO",
            MonzaLog.entidad == "cotizacion", MonzaLog.entidad_id == cot_id).count()
    finally:
        db.close()


def test_re_patch_despachado_no_duplica_ltv_ni_notifs():
    _limpiar()
    TOTAL = 119000
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA", ltv=0)
        db.add(cli); db.flush()
        lead = MonzaLead(numero=f"{LEAD_MARK}-{uuid.uuid4().hex[:8].upper()}",
                         cliente_id=cli.id, estado="en_proceso")
        db.add(lead); db.flush()
        cli_id, lead_id = cli.id, lead.id
        db.commit()
    finally:
        db.close()
    cot_id = _crear_venta(total_bruto=TOTAL, lead_id=lead_id, cliente_id=cli_id)
    try:
        # Primer PATCH: despacho NUEVO → suma LTV, 1 notif, 1 log DESPACHADO
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            assert int(db.query(MonzaCliente).get(cli_id).ltv or 0) == TOTAL
        finally:
            db.close()
        assert _n_notifs_despacho(cot_id) == 1
        assert _n_logs_despachado(cot_id) == 1

        # Re-PATCH: NO vuelve a sumar el LTV ni repite notif/log (antes: bug de plata)
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            assert int(db.query(MonzaCliente).get(cli_id).ltv or 0) == TOTAL, \
                "el re-PATCH 'despachado' duplicaba el LTV del cliente"
        finally:
            db.close()
        assert _n_notifs_despacho(cot_id) == 1, "la notificación de despacho no debe repetirse"
        assert _n_logs_despachado(cot_id) == 1, "el log DESPACHADO no debe repetirse"
    finally:
        _limpiar()


# ── 4) Foto de precios completa al crear ─────────────────────────────────────

def _primer_user_id():
    from models.models import User
    db = SessionLocal()
    try:
        u = db.query(User).order_by(User.id).first()
        return u.id if u else None
    finally:
        db.close()


def _cfg_vals():
    db = SessionLocal()
    try:
        cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
        if not cfg:
            return None
        return {"tc_usd": cfg.tc_usd_clp, "tc_eur": cfg.tc_eur_clp,
                "tarifa": cfg.tarifa_aerea_por_kg, "moneda_tarifa": cfg.moneda_tarifa}
    finally:
        db.close()


def test_foto_completa_al_crear():
    _limpiar()
    user_id = _primer_user_id()
    if user_id is None:
        pytest.skip("BD sin usuarios: create_cotizacion exige asesor_id válido (FK users)")
    cfg = _cfg_vals()
    if cfg is None:
        pytest.skip("BD sin MonzaConfig id=1")
    CURRENT["id"] = user_id
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA")
        db.add(cli); db.commit()
        cli_id = cli.id
    finally:
        db.close()
    try:
        r = client.post("/api/monza/cotizaciones", json={
            "cliente_id": cli_id,
            "items": [
                {"descripcion": f"{MARK} eur", "moneda": "EUR", "cantidad": 1,
                 "precio_unitario_clp": 10000},
                {"descripcion": f"{MARK} usd", "moneda": "USD", "cantidad": 1,
                 "precio_unitario_clp": 20000},
                {"descripcion": f"{MARK} clp", "moneda": "CLP", "cantidad": 1,
                 "precio_unitario_clp": 30000},
            ]})
        assert r.status_code == 201, r.text
        cot_id = r.json()["id"]

        # Verificación con sesión NUEVA: la foto quedó persistida y completa
        db = SessionLocal()
        try:
            c = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
            assert c.moneda_tarifa == cfg["moneda_tarifa"], \
                "moneda_tarifa debe congelarse desde la config al crear (freeze-forward)"
            assert c.tc_usd_clp == cfg["tc_usd"] and c.tc_eur_clp == cfg["tc_eur"]
            items = {it.descripcion: it for it in db.query(MonzaCotizacionItem)
                     .filter(MonzaCotizacionItem.cotizacion_id == cot_id).all()}
        finally:
            db.close()
        assert items[f"{MARK} eur"].tc_aplicado == cfg["tc_eur"], "ítem EUR congela tc_eur_clp"
        assert items[f"{MARK} usd"].tc_aplicado == cfg["tc_usd"], "ítem USD congela tc_usd_clp"
        assert items[f"{MARK} clp"].tc_aplicado == 1.0, "moneda sin TC congela 1.0"
        for it in items.values():
            assert it.tarifa_aerea == cfg["tarifa"], "cada ítem congela la tarifa aérea vigente"

        # El detalle expone la foto (config_snapshot.moneda_tarifa + campos por ítem)
        r = client.get(f"/api/monza/cotizaciones/{cot_id}")
        assert r.status_code == 200
        det = r.json()
        assert det["config_snapshot"]["moneda_tarifa"] == cfg["moneda_tarifa"]
        assert all("tc_aplicado" in it and "tarifa_aerea" in it for it in det["items"])
    finally:
        CURRENT["id"] = 1
        _limpiar()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
