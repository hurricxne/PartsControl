"""tipo_origen en MonzaOcProveedor + guard anti-embarque (FASE 8 Monza, P1/P3).

Espejo del blindaje GA (routers/compras.py _rechazar_items_nacionales), adaptado
al modelo Monza (SIN tabla OcProveedorItem: el vínculo ítem↔OC es directo vía
MonzaCotizacionItem.oc_proveedor_id). Cubre:

  1. Saneo en /comprar: tipo_origen fuera de {'nacional','internacional'} → 400
     (no se corrige en silencio) y NO crea la OC ni mueve los ítems.
  2. tipo_origen None/ausente cae al default 'internacional' (clientes antiguos
     del API no se rompen) y 'nacional' explícito persiste.
  3. Serializadores exponen tipo_origen coalescido en /comprados (decide el CTA
     del front) y en /ocs (badge + dropdown del modal).
  4. Guard /preparar: una selección MIXTA con un ítem nacional → 400 y NADIE se
     prepara (el guard corre ANTES del UPDATE); regresión: el internacional solo
     se prepara 200.
  5. Guard crear_embarque (segunda entrada del pipeline): un ítem nacional que se
     coló a 'preparado' → 400 SIN crear embarque ni tocar al internacional.
  6. Histórico: una OC insertada sin tipo_origen (filas pre-migración) queda
     'internacional' por el DEFAULT de la columna y así la reporta el API.

Datos MARCADOS + limpieza total en try/finally; verificación con SESIÓN NUEVA;
jamás toca datos reales. Requiere la columna tipo_origen en BD (la crea
monza_recepcion_nacional/init_db — create_all no altera tablas existentes).

Corre con:  ./venv/bin/python -m pytest monza_tests/test_oc_tipo_origen.py -q
(también:   ./venv/bin/python monza_tests/test_oc_tipo_origen.py)
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
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem,
    MonzaEmbarque, MonzaEmbarqueItem, MonzaLog, MonzaOcProveedor,
)
from monza_router_abastecimiento import router as abast_router  # noqa: E402
from monza_router_logistica import router as logistica_router  # noqa: E402

MARK = "test-mocp-tipori"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(abast_router)
app.include_router(logistica_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)


def _setup(db):
    """Venta 'vendida' con 2 ítems 'por_comprar': A irá a una OC NACIONAL y
    B a una internacional (la selección mixta es el caso que el guard blinda)."""
    cli = MonzaCliente(nombre=MARK)
    db.add(cli); db.flush()
    suf = uuid.uuid4().hex[:6].upper()
    cot = MonzaCotizacion(numero=f"CT-{MARK[-4:]}-{suf}", cliente_id=cli.id,
                          estado="vendida")
    db.add(cot); db.flush()
    it_nac = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} NAC",
                                 numero_parte=f"{MARK}-PN-NAC", cantidad=10,
                                 estado_linea="por_comprar")
    it_int = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} INT",
                                 numero_parte=f"{MARK}-PN-INT", cantidad=5,
                                 estado_linea="por_comprar")
    db.add_all([it_nac, it_int]); db.commit()
    db.refresh(it_nac); db.refresh(it_int)
    return it_nac.id, it_int.id


def _cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre == MARK).all()]
        emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
                   .filter(MonzaEmbarque.asesor_email == EMAIL).all()]
        db.query(MonzaEmbarqueItem).filter(
            MonzaEmbarqueItem.embarque_id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaEmbarque).filter(
            MonzaEmbarque.id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
        # OCs del test: las creadas por API (proveedor MARK) y la histórica simulada
        db.query(MonzaOcProveedor).filter(
            (MonzaOcProveedor.proveedor_nombre == MARK)
            | (MonzaOcProveedor.numero.like(f"%{MARK}%"))).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _estado_linea(item_id):
    """Lee estado_linea y oc_proveedor_id con SESIÓN NUEVA (nada cacheado)."""
    db2 = SessionLocal()
    try:
        it = db2.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.id == item_id).first()
        return (it.estado_linea, it.oc_proveedor_id)
    finally:
        db2.close()


def test_oc_tipo_origen():
    db = SessionLocal()
    try:
        _cleanup()
        it_nac_id, it_int_id = _setup(db)

        # ── 1) SANEO: tipo_origen basura → 400, sin efectos ──
        r = client.post("/api/monza/abastecimiento/comprar", json={
            "item_ids": [it_nac_id], "proveedor_nombre": MARK,
            "tipo_origen": "colombiana",
        })
        assert r.status_code == 400, f"saneo debía dar 400: {r.status_code} {r.text}"
        assert "tipo_origen" in r.json()["detail"], r.json()
        estado, ocp_id = _estado_linea(it_nac_id)
        assert estado == "por_comprar" and ocp_id is None, \
            f"el 400 de saneo no debe mover el ítem: {estado}/{ocp_id}"
        db.rollback()
        n_ocs = db.query(MonzaOcProveedor).filter(
            MonzaOcProveedor.proveedor_nombre == MARK).count()
        assert n_ocs == 0, f"el 400 de saneo no debe crear OC: {n_ocs}"

        # ── 2) 'nacional' explícito persiste; None cae al default ──
        r = client.post("/api/monza/abastecimiento/comprar", json={
            "item_ids": [it_nac_id], "proveedor_nombre": MARK,
            "tipo_origen": "nacional",
        })
        assert r.status_code == 200, r.text
        ocp_nac_id = r.json()["ocp_id"]
        r = client.post("/api/monza/abastecimiento/comprar", json={
            "item_ids": [it_int_id], "proveedor_nombre": MARK,
            "tipo_origen": None,
        })
        assert r.status_code == 200, r.text
        ocp_int_id = r.json()["ocp_id"]
        db2 = SessionLocal()
        try:
            ocp_nac = db2.get(MonzaOcProveedor, ocp_nac_id)
            ocp_int = db2.get(MonzaOcProveedor, ocp_int_id)
            assert ocp_nac.tipo_origen == "nacional", ocp_nac.tipo_origen
            assert ocp_int.tipo_origen == "internacional", \
                f"None debía caer al default: {ocp_int.tipo_origen}"
        finally:
            db2.close()

        # ── 3) serializadores exponen tipo_origen coalescido ──
        r = client.get("/api/monza/abastecimiento/comprados", params={"q": MARK})
        assert r.status_code == 200, r.text
        por_id = {d["id"]: d for d in r.json()}
        assert por_id[it_nac_id]["tipo_origen"] == "nacional", por_id[it_nac_id]
        assert por_id[it_int_id]["tipo_origen"] == "internacional", por_id[it_int_id]
        r = client.get("/api/monza/abastecimiento/ocs")
        assert r.status_code == 200, r.text
        ocs = {o["id"]: o for o in r.json()}
        assert ocs[ocp_nac_id]["tipo_origen"] == "nacional", ocs[ocp_nac_id]
        assert ocs[ocp_int_id]["tipo_origen"] == "internacional", ocs[ocp_int_id]

        # ── 4) GUARD /preparar: selección mixta → 400 y NADIE se prepara ──
        r = client.post("/api/monza/abastecimiento/preparar", json={
            "item_ids": [it_nac_id, it_int_id],
        })
        assert r.status_code == 400, f"preparar con nacional debía dar 400: {r.status_code} {r.text}"
        detail = r.json()["detail"]
        assert "NACIONAL" in detail and "entrega nacional" in detail, detail
        assert f"{MARK}-PN-NAC" in detail, f"debe listar el numero_parte: {detail}"
        assert _estado_linea(it_nac_id)[0] == "comprado", "nacional no debe moverse"
        assert _estado_linea(it_int_id)[0] == "comprado", \
            "el guard corre ANTES del UPDATE: el internacional tampoco debe moverse"

        # Regresión: el internacional solo se prepara normal
        r = client.post("/api/monza/abastecimiento/preparar", json={
            "item_ids": [it_int_id],
        })
        assert r.status_code == 200 and r.json()["preparados"] == 1, r.text
        assert _estado_linea(it_int_id)[0] == "preparado"

        # ── 5) GUARD crear_embarque: nacional colado a 'preparado' → 400 sin crear nada ──
        # Simula un estado colado (llamada directa al API vieja / dato legado):
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.id == it_nac_id).update({"estado_linea": "preparado"})
        db.commit()
        r = client.post("/api/monza/logistica/embarques", json={
            "item_ids": [it_nac_id, it_int_id],
        })
        assert r.status_code == 400, f"embarque con nacional debía dar 400: {r.status_code} {r.text}"
        assert "NACIONAL" in r.json()["detail"], r.json()
        db2 = SessionLocal()
        try:
            n_emb = db2.query(MonzaEmbarque).filter(
                MonzaEmbarque.asesor_email == EMAIL).count()
            assert n_emb == 0, f"el 400 no debe crear embarque: {n_emb}"
        finally:
            db2.close()
        assert _estado_linea(it_int_id)[0] == "preparado", \
            "el internacional no debe cambiar por el 400 del embarque"
        assert _estado_linea(it_nac_id)[0] == "preparado", \
            "sin efectos colaterales sobre el nacional colado"

        # ── 6) HISTÓRICO: OC insertada sin tipo_origen queda 'internacional' ──
        # (fila pre-migración: el DEFAULT de la columna hace el trabajo)
        hist = MonzaOcProveedor(numero=f"OCP-HIST-{MARK}", proveedor_nombre=MARK)
        db.add(hist); db.commit()
        hist_id = hist.id
        db2 = SessionLocal()
        try:
            h = db2.get(MonzaOcProveedor, hist_id)
            assert h.tipo_origen == "internacional", \
                f"histórico debía quedar 'internacional': {h.tipo_origen!r}"
        finally:
            db2.close()
        r = client.get("/api/monza/abastecimiento/ocs")
        ocs = {o["id"]: o for o in r.json()}
        assert ocs[hist_id]["tipo_origen"] == "internacional", ocs[hist_id]

        print("\n=== TODO OK ===")
    finally:
        db.close()
        _cleanup()
        print("Cleanup OK")


if __name__ == "__main__":
    test_oc_tipo_origen()
