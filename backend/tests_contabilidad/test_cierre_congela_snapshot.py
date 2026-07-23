"""El Cierre de Venta (POST /api/compras/oc-cliente) congela la foto de precios.

Prueba de integración contra la BD real, con dato MARCADO y limpieza total (no toca
datos reales del dueño): crea una cotización de prueba, la cierra vía el endpoint real y
verifica que quedó con `pricing_snapshot` = los parámetros de precio del config global de
ese momento. También cubre idempotencia (el 2º intento no re-pisa la foto).

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_cierre_congela_snapshot.py -q
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from database import get_db  # noqa: E402
from models.models import Cotizacion, ItemCotizacion, OcCliente, ConfiguracionCotizador, Notificacion  # noqa: E402
from routers.compras import router as compras_router  # noqa: E402
from services.pricing_service import snapshot_desde_config, CLAVES_PRICING  # noqa: E402

MARK = "test-tc-congelado"

app = FastAPI()
app.include_router(compras_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, empresa="mineria", email="admin@test.invalid")
client = TestClient(app)


def _config_global_pricing() -> dict:
    db = SessionLocal()
    try:
        cfg = db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
        assert cfg is not None, "no hay config global (id=1) en la BD"
        return snapshot_desde_config({k: getattr(cfg, k, None) for k in CLAVES_PRICING})
    finally:
        db.close()


def _crear_cotizacion_marcada(pricing_snapshot: str | None = None) -> int:
    db = SessionLocal()
    try:
        cot = Cotizacion(numero=f"{MARK}-{uuid.uuid4().hex[:8]}", cliente=MARK,
                         origen="costo", fase_comercial="validada",
                         pricing_snapshot=pricing_snapshot)
        db.add(cot)
        db.flush()
        db.add(ItemCotizacion(
            cotizacion_id=cot.id, item_num=1, descripcion=MARK, numero_parte="X-1",
            cantidad=10, precio_unit_cotizacion=100.0, peso_unit_lbs=5.0, margen_pct=0.19,
            estado_item="ingresado"))
        db.commit()
        return cot.id
    finally:
        db.close()


def _limpiar(cot_id: int):
    # Bulk deletes en ORDEN FK-seguro (hijos antes que padres): oc_cliente y items
    # referencian a la cotización; las notificaciones referencian a la OC por entidad_id.
    db = SessionLocal()
    try:
        oc_ids = [oc.id for oc in db.query(OcCliente).filter(
            OcCliente.cotizacion_id == cot_id).all()]
        if oc_ids:
            db.query(Notificacion).filter(
                Notificacion.entidad_tipo == "oc_cliente",
                Notificacion.entidad_id.in_(oc_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(
            OcCliente.cotizacion_id == cot_id).delete(synchronize_session=False)
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == cot_id).delete(synchronize_session=False)
        db.query(Cotizacion).filter(
            Cotizacion.id == cot_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _snapshot_de(cot_id: int):
    db = SessionLocal()
    try:
        cot = db.query(Cotizacion).filter(Cotizacion.id == cot_id).first()
        return (cot.pricing_snapshot, cot.pricing_snapshot_at) if cot else (None, None)
    finally:
        db.close()


def test_cierre_de_venta_congela_la_foto_de_precios():
    cot_id = _crear_cotizacion_marcada()
    try:
        # antes de cerrar: sin foto
        assert _snapshot_de(cot_id)[0] is None

        esperado = _config_global_pricing()
        r = client.post("/api/compras/oc-cliente",
                        json={"cotizacion_id": cot_id, "numero_oc": f"OC-{MARK}"})
        assert r.status_code == 201, f"cierre de venta falló: {r.status_code} {r.text[:200]}"

        snap_json, snap_at = _snapshot_de(cot_id)
        assert snap_json is not None, "el Cierre de Venta NO congeló la foto"
        assert snap_at is not None, "falta la marca de tiempo de la foto"
        assert json.loads(snap_json) == esperado, (
            f"la foto no calza con el config global: {json.loads(snap_json)} vs {esperado}")
        # la foto SIEMPRE trae el dólar (parámetro central)
        assert "tipo_cambio_usd" in json.loads(snap_json)
    finally:
        _limpiar(cot_id)


def test_cierre_es_idempotente_no_repisa_la_foto():
    cot_id = _crear_cotizacion_marcada()
    try:
        r1 = client.post("/api/compras/oc-cliente",
                         json={"cotizacion_id": cot_id, "numero_oc": f"OC-{MARK}-1"})
        assert r1.status_code == 201
        snap1, at1 = _snapshot_de(cot_id)

        # 2º intento (retry del cierre): devuelve la OC existente y NO cambia la foto
        r2 = client.post("/api/compras/oc-cliente",
                         json={"cotizacion_id": cot_id, "numero_oc": f"OC-{MARK}-2"})
        assert r2.status_code in (200, 201)
        snap2, at2 = _snapshot_de(cot_id)
        assert snap2 == snap1, "la foto se re-pisó en un reintento del cierre"
    finally:
        _limpiar(cot_id)


def test_cierre_no_repisa_una_foto_ya_existente():
    """Ejercita DIRECTAMENTE el guard `if not cot.pricing_snapshot` de crear_oc_cliente:
    una cotización que YA trae foto (distinta del global de hoy) se cierra por primera vez
    y su foto NO se sobrescribe con el config global — el freeze-forward respeta la foto
    que ya existía."""
    foto_previa = json.dumps({k: (777.0 if k == "tipo_cambio_usd" else 1.0)
                              for k in CLAVES_PRICING})
    cot_id = _crear_cotizacion_marcada(pricing_snapshot=foto_previa)
    try:
        # la foto previa es distinta del config global real (evita falso positivo)
        assert _config_global_pricing() != json.loads(foto_previa)

        r = client.post("/api/compras/oc-cliente",
                        json={"cotizacion_id": cot_id, "numero_oc": f"OC-{MARK}-guard"})
        assert r.status_code == 201, f"{r.status_code} {r.text[:200]}"

        snap_json, _ = _snapshot_de(cot_id)
        assert json.loads(snap_json) == json.loads(foto_previa), (
            "el cierre RE-PISÓ una foto que ya existía (guard roto)")
        assert json.loads(snap_json)["tipo_cambio_usd"] == 777.0
    finally:
        _limpiar(cot_id)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
