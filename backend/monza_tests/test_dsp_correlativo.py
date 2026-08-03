"""Correlativo DSP de despachos Monza — carrera simulada + numero legado.

Espejo de routers/tests/test_despachos_guards.py:146-171 (Grupo AM), adaptado al
modelo Monza (la cotización ES la venta, sin OcCliente). Cubre:

  1. Un numero legado NO numérico (p.ej. 'DSP-2026-LEGADO') no revienta
     _gen_num_desp con 500: cae al fallback n=0 y el UNIQUE + retry convierten
     una eventual colisión en 409 controlado (comentario en el propio helper).
  2. Choque de correlativo (carrera simulada con monkeypatch del generador):
     crear reintenta, recalcula y termina creando con un N° ÚNICO distinto.
  3. Dos creaciones seguidas generan correlativos DISTINTOS.

Datos MARCADOS + limpieza total en try/finally; nunca toca datos reales.
Requiere el UNIQUE de monza_despachos.numero (unique=True del modelo en
create_all; en BD existente lo crea migrations/monza_despachos_ciclo_vida.py).

Corre con:  ./venv/bin/python -m pytest monza_tests/test_dsp_correlativo.py -q
(también:   ./venv/bin/python monza_tests/test_dsp_correlativo.py)
"""
import os
import sys
import uuid
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem,
    MonzaDespacho, MonzaDespachoItem, MonzaLog,
)
import monza_router_despachos as despachos_mod  # noqa: E402
from monza_router_despachos import router as despachos_router  # noqa: E402

MARK = "test-dsp-corr"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(despachos_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)


def _setup(db):
    """Venta 'vendida' con 1 ítem en bodega (10 uds), sin recepción registrada
    (→ el tope físico es la cantidad vendida: comportamiento histórico)."""
    cli = MonzaCliente(nombre=MARK)
    db.add(cli); db.flush()
    suf = uuid.uuid4().hex[:6].upper()
    cot = MonzaCotizacion(numero=f"CT-{MARK[-4:]}-{suf}", cliente_id=cli.id, estado="vendida")
    db.add(cot); db.flush()
    item = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} A",
                               cantidad=10, estado_linea="en_bodega")
    db.add(item); db.commit()
    db.refresh(cot); db.refresh(item)
    return cot.id, item.id


def _cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre == MARK).all()]
        # Despachos de nuestras cotizaciones + el legado marcado (numero con MARK)
        dsp_ids = [r[0] for r in db.query(MonzaDespacho.id).filter(
            (MonzaDespacho.cotizacion_id.in_(cot_ids or [0]))
            | (MonzaDespacho.numero.like(f"%{MARK}%"))).all()]
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def test_dsp_correlativo():
    db = SessionLocal()
    orig_gen = despachos_mod._gen_num_desp
    try:
        _cleanup()
        cot_id, item_id = _setup(db)
        anio = datetime.utcnow().year

        # ── 1) numero legado NO numérico no revienta _gen_num_desp ──
        # El legado queda como la ÚLTIMA fila DSP-{anio}-% (id más alto): el helper
        # debe caer al fallback n=0 sin ValueError (antes un sufijo así daba 500).
        legado = MonzaDespacho(numero=f"DSP-{anio}-{MARK}", cotizacion_id=cot_id,
                               estado="anulado")
        db.add(legado); db.commit()
        num = orig_gen(db)
        assert isinstance(num, str) and num.startswith(f"DSP-{anio}-"), num
        assert num.endswith("-0001"), f"fallback n=0 esperado: {num}"
        # Se retira el legado ANTES de crear por API: con él presente, el fallback
        # 0001 podría chocar con un despacho real y ensuciar los checks siguientes.
        db.delete(legado); db.commit()

        # ── 2) choque de correlativo (carrera simulada) → reintenta ──
        numero_ocupado = orig_gen(db)
        db.add(MonzaDespacho(numero=numero_ocupado, cotizacion_id=cot_id,
                             estado="en_preparacion"))
        db.commit()
        llamadas = {"n": 0}

        def _gen_con_choque(session):
            # 1ª llamada: devuelve el N° ya ocupado (simula al otro request que
            # ganó la carrera); siguientes: el cálculo real — espejo GA.
            llamadas["n"] += 1
            return numero_ocupado if llamadas["n"] == 1 else orig_gen(session)

        despachos_mod._gen_num_desp = _gen_con_choque
        try:
            r = client.post("/api/monza/despachos/crear", json={
                "cotizacion_id": cot_id,
                "items": [{"item_id": item_id, "qty": 2}],
            })
        finally:
            despachos_mod._gen_num_desp = orig_gen
        assert r.status_code == 200, r.text
        assert r.json()["numero"] != numero_ocupado, r.json()
        assert llamadas["n"] >= 2, f"no reintentó: {llamadas['n']} llamada(s)"
        numero_1 = r.json()["numero"]

        # ── 3) dos creaciones seguidas → correlativos distintos ──
        r2 = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": cot_id,
            "items": [{"item_id": item_id, "qty": 2}],
        })
        assert r2.status_code == 200, r2.text
        assert r2.json()["numero"] not in (numero_1, numero_ocupado), r2.json()

        # Verificación con SESIÓN NUEVA: los 3 números persistidos son únicos.
        db2 = SessionLocal()
        try:
            nums = [n for (n,) in db2.query(MonzaDespacho.numero)
                    .filter(MonzaDespacho.cotizacion_id == cot_id).all()]
            assert len(nums) == len(set(nums)) == 3, nums
        finally:
            db2.close()
        print("\n=== TODO OK ===")
    finally:
        despachos_mod._gen_num_desp = orig_gen
        db.close()
        _cleanup()
        print("Cleanup OK")


if __name__ == "__main__":
    test_dsp_correlativo()
