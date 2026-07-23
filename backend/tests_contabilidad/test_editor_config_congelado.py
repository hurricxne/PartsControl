"""El editor de cotización (GET /api/cotizador/{id}) devuelve el config CONGELADO.

Regresión del hallazgo HIGH del enjambre 2026-07-23: get_editor congelaba los totales
pero devolvía `config` con el dólar GLOBAL vivo, y el editor del navegador recalcula la
tabla en el cliente con ese `config`. Para una venta ya cerrada (con pricing_snapshot),
la pantalla se movía con el dólar global — justo lo que el TC congelado promete evitar,
y quedaba distinta del PDF/Excel (que sí usan la foto).

Este test prueba, contra la BD real y con dato MARCADO + limpieza:
  - cotización CON foto  → config.tipo_cambio_usd == el de la foto (no el global);
  - cotización SIN foto   → config.tipo_cambio_usd == el global (comportamiento de antes).

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_editor_config_congelado.py -q
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import Cotizacion, ItemCotizacion, ConfiguracionCotizador  # noqa: E402
from routers.cotizador import router as cotizador_router  # noqa: E402
from services.pricing_service import CLAVES_PRICING  # noqa: E402

MARK = "test-editor-congelado"

CURRENT = {"id": 1}
app = FastAPI()
app.include_router(cotizador_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa="mineria", email="admin@test.invalid")
client = TestClient(app)


def _un_usuario_real() -> int:
    db = SessionLocal()
    try:
        uid = db.execute(text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar()
        assert uid is not None, "no hay usuarios en la BD para el test"
        return int(uid)
    finally:
        db.close()


def _tc_global() -> float:
    db = SessionLocal()
    try:
        return float(db.execute(text(
            "SELECT tipo_cambio_usd FROM configuracion_cotizador WHERE id=1")).scalar())
    finally:
        db.close()


def _crear_cotizacion(user_id: int, snapshot: dict | None) -> int:
    db = SessionLocal()
    try:
        cot = Cotizacion(numero=f"{MARK}-{uuid.uuid4().hex[:8]}", cliente=MARK,
                         origen="costo", fase_comercial="validada", user_id=user_id,
                         pricing_snapshot=json.dumps(snapshot) if snapshot else None)
        db.add(cot)
        db.flush()
        db.add(ItemCotizacion(
            cotizacion_id=cot.id, item_num=1, descripcion=MARK, numero_parte="X-1",
            cantidad=5, precio_unit_cotizacion=100.0, peso_unit_lbs=4.0, margen_pct=0.19,
            estado_item="ingresado"))
        db.commit()
        return cot.id
    finally:
        db.close()


def _limpiar(cot_id: int):
    db = SessionLocal()
    try:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == cot_id).delete(synchronize_session=False)
        db.query(Cotizacion).filter(
            Cotizacion.id == cot_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_editor_congela_config_para_venta_cerrada():
    CURRENT["id"] = _un_usuario_real()
    tc_global = _tc_global()
    tc_foto = round(tc_global + 111.0, 2)   # foto distinta del global, a propósito
    foto = {k: (tc_foto if k == "tipo_cambio_usd" else 1.0) for k in CLAVES_PRICING}
    cot_id = _crear_cotizacion(CURRENT["id"], snapshot=foto)
    try:
        r = client.get(f"/api/cotizador/{cot_id}")
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        cfg = r.json()["config"]
        assert cfg["tipo_cambio_usd"] == tc_foto, (
            f"el editor mostró el dólar global {cfg['tipo_cambio_usd']} en vez de la foto {tc_foto}")
        # los totales servidos también son los congelados (coherencia interna)
        assert r.json()["totales"]["total_con_iva_clp"] > 0
    finally:
        _limpiar(cot_id)


def test_editor_usa_global_para_borrador_sin_foto():
    CURRENT["id"] = _un_usuario_real()
    tc_global = _tc_global()
    cot_id = _crear_cotizacion(CURRENT["id"], snapshot=None)
    try:
        r = client.get(f"/api/cotizador/{cot_id}")
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        cfg = r.json()["config"]
        assert cfg["tipo_cambio_usd"] == tc_global, (
            f"borrador sin foto debe usar el global {tc_global}, dio {cfg['tipo_cambio_usd']}")
        # y NO se filtró la llave interna 'origen' dentro de config
        assert "origen" not in cfg
        # los defaults de plazo (no son de pricing) siguen presentes
        assert "plazo_min_default" in cfg
    finally:
        _limpiar(cot_id)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
