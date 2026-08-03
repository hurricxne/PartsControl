"""Regresión del TOPE FÍSICO tras el UNION nacional en monza_router_despachos.py.

Este es el ÚNICO toque a código compartido de Compras Nacionales Monza (Fase 8):
el UNION aditivo en `monza_router_despachos.py::_qty_recibida_utilizable`. La suite
confirma las dos reglas de oro del diseño (espejo de
routers/tests/test_recepcion_nacional_tope.py de Grupo AM):
  (a) el ítem INTERNACIONAL queda EXACTAMENTE igual (con y sin recepción de embarque);
  (b) el ítem NACIONAL solo es despachable por lo RECIBIDO (nunca por lo vendido), y
      un nacional sin recepción cerrada NO es despachable.

Extra Monza (escenario 11, sin equivalente GA): monza_router_bodega usa el MISMO
helper para los faltantes acumulados al cerrar una recepción de EMBARQUE — con
ítems nacionales coexistiendo en la misma venta, el reclamo del ítem internacional
NO debe contar unidades nacionales (disjunción de item_ids preservada).

Inserta las recepciones directamente en la BD (embarque y nacional) para ejercer el
dict combinado de `_qty_recibida_utilizable`, luego valida el tope contra el endpoint
real de detalle de avance y creación de despachos. LIMPIA todo al final.

Corre con:  ./venv/bin/python monza_tests/test_recepcion_nacional_tope.py
       o:   ./venv/bin/python -m pytest monza_tests/test_recepcion_nacional_tope.py -q
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaDespacho, MonzaDespachoItem,
    MonzaEmbarque, MonzaEmbarqueItem, MonzaRecepcion, MonzaRecepcionItem,
    MonzaReclamo, MonzaLog, MonzaNotificacion,
)
from monza_recepcion_nacional.models import (  # noqa: E402
    MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
)
from monza_router_despachos import router as despachos_router, _qty_recibida_utilizable  # noqa: E402
from monza_router_bodega import router as bodega_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

# monza_cotizaciones.numero es String(20): el MARK va en el CLIENTE (ancla de
# limpieza, patrón test_bodega_despachos_flujo) y el número usa prefijo corto.
MARK = "__TEST_MRN_TOPE__"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(despachos_router)
app.include_router(bodega_router)
# Lambda seca: esta suite NO prueba carreras (los locks se certifican en la suite
# de integración del paquete con auth realista + SELECT 1, lección G13).
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _escenario(db, cantidades=(10,), estado_linea="en_bodega"):
    """Venta marcada con N ítems. estado_linea='en_bodega' simula la línea ya
    liberada para despacho; 'comprado' ejerce el gate de estado (disponible 0)."""
    cli = MonzaCliente(nombre=MARK)
    db.add(cli); db.flush()
    suf = uuid.uuid4().hex[:6].upper()
    cot = MonzaCotizacion(numero=f"CT-MRN-{suf}", cliente_id=cli.id, estado="vendida")
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion=f"{MARK} Parte {n}",
            numero_parte=f"P-MRN-{n}", cantidad=cant, estado_linea=estado_linea,
        )
        db.add(it); items.append(it)
    db.commit()
    for obj in [cot] + items:
        db.refresh(obj)
    return cot, items


def _recepcion_embarque(db, item, estado_recep="completo", qty=8):
    """Embarque + recepción de EMBARQUE cerrada (fuente 1) insertados directo."""
    suf = uuid.uuid4().hex[:6].upper()
    emb = MonzaEmbarque(numero=f"{MARK}-E-{suf}", estado="en_bodega")
    db.add(emb); db.flush()
    db.add(MonzaEmbarqueItem(embarque_id=emb.id, item_id=item.id)); db.flush()
    rec = MonzaRecepcion(embarque_id=emb.id, estado="cerrada")
    db.add(rec); db.flush()
    db.add(MonzaRecepcionItem(recepcion_id=rec.id, item_id=item.id,
                              estado_recepcion=estado_recep, qty_recibida=qty))
    db.commit()
    return emb


def _recepcion_nacional(db, item, estado_recep="completo", qty=10, estado="cerrada"):
    """Recepción NACIONAL (fuente 2) insertada directo, cerrada por defecto."""
    rec = MonzaRecepcionNacional(numero_guia_proveedor=f"{MARK}-G", estado=estado)
    db.add(rec); db.flush()
    db.add(MonzaRecepcionNacionalItem(recepcion_id=rec.id, item_cotizacion_id=item.id,
                                      numero_parte=item.numero_parte, qty_recibida=qty,
                                      estado_recepcion=estado_recep))
    db.commit()
    return rec


def _detalle_item(cot_id, item_id):
    r = client.get(f"/api/monza/despachos/avance/{cot_id}")
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["items"] if x["id"] == item_id)


def _despachar(cot_id, item_id, qty):
    return client.post("/api/monza/despachos/crear", json={
        "cotizacion_id": cot_id,
        "items": [{"item_id": item_id, "qty": qty}],
    })


def _recibidos(item_ids):
    """Dict del helper real con SESIÓN NUEVA (la del cliente puede mentir)."""
    db = SessionLocal()
    try:
        return _qty_recibida_utilizable(db, item_ids)
    finally:
        db.close()


def _limpiar(db):
    db.rollback()
    S = "fetch"
    cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
               .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
               .filter(MonzaCliente.nombre == MARK).all()]
    item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
    dsp_ids = [r[0] for r in db.query(MonzaDespacho.id)
               .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
    emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
               .filter(MonzaEmbarque.numero.like(f"{MARK}%")).all()]
    rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
               .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
    # Recepciones nacionales del MARK: por ítem Y por guía (rezagos sin ítem)
    rn_ids = [r[0] for r in db.query(MonzaRecepcionNacionalItem.recepcion_id)
              .filter(MonzaRecepcionNacionalItem.item_cotizacion_id.in_(item_ids or [0])).all()]
    rn_ids += [r[0] for r in db.query(MonzaRecepcionNacional.id)
               .filter(MonzaRecepcionNacional.numero_guia_proveedor.like(f"{MARK}%")).all()]
    rn_ids = list(set(rn_ids))
    if rn_ids:
        db.query(MonzaRecepcionNacionalItem).filter(
            MonzaRecepcionNacionalItem.recepcion_id.in_(rn_ids)).delete(synchronize_session=S)
        db.query(MonzaRecepcionNacional).filter(
            MonzaRecepcionNacional.id.in_(rn_ids)).delete(synchronize_session=S)
    # Notifs con scope por entidad (no producto cruzado con ids ajenos)
    db.query(MonzaNotificacion).filter(
        MonzaNotificacion.entidad == "cotizacion",
        MonzaNotificacion.entidad_id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaNotificacion).filter(
        MonzaNotificacion.entidad == "recepcion",
        MonzaNotificacion.entidad_id.in_(rec_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaNotificacion).filter(
        MonzaNotificacion.entidad == "despacho",
        MonzaNotificacion.entidad_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
    db.query(MonzaReclamo).filter(
        MonzaReclamo.item_id.in_(item_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaDespachoItem).filter(
        MonzaDespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaDespacho).filter(
        MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaRecepcionItem).filter(
        MonzaRecepcionItem.recepcion_id.in_(rec_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaRecepcion).filter(
        MonzaRecepcion.id.in_(rec_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaEmbarqueItem).filter(
        MonzaEmbarqueItem.embarque_id.in_(emb_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaEmbarque).filter(
        MonzaEmbarque.id.in_(emb_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacionItem).filter(
        MonzaCotizacionItem.id.in_(item_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacion).filter(
        MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
    db.commit()


def run():
    db = SessionLocal()
    try:
        _limpiar(db)

        # ═══ 1. INTERNACIONAL con recepción de embarque parcial (8/10) → intacto ═══
        cot, (it,) = _escenario(db, (10,))
        _recepcion_embarque(db, it, "completo", 8)
        d = _detalle_item(cot.id, it.id)
        check("internacional con recepción 8/10: disponible 8 (UNION no altera)",
              d["qty_disponible"] == 8, d)
        check("internacional: despachar 9 → 400", _despachar(cot.id, it.id, 9).status_code == 400)
        check("internacional: despachar 8 → 200", _despachar(cot.id, it.id, 8).status_code == 200)
        _limpiar(db)

        # ═══ 2. INTERNACIONAL sin recepción (histórico) → tope = vendido ═══
        cot, (it,) = _escenario(db, (10,))
        d = _detalle_item(cot.id, it.id)
        check("internacional sin recepción: disponible = 10 (histórico intacto)",
              d["qty_disponible"] == 10, d)
        check("internacional sin recepción: despachar 10 → 200",
              _despachar(cot.id, it.id, 10).status_code == 200)
        _limpiar(db)

        # ═══ 3. NACIONAL 'comprado' sin recepción → indespachable (gate de estado) ═══
        cot, (it,) = _escenario(db, (10,), estado_linea="comprado")
        d = _detalle_item(cot.id, it.id)
        check("nacional comprado sin recepción: disponible 0", d["qty_disponible"] == 0, d)
        check("nacional comprado sin recepción: despachar → 400",
              _despachar(cot.id, it.id, 5).status_code == 400)
        _limpiar(db)

        # ═══ 4. NACIONAL recepción cerrada completa (10/10) → despacha 100% ═══
        cot, (it,) = _escenario(db, (10,))
        _recepcion_nacional(db, it, "completo", 10)
        d = _detalle_item(cot.id, it.id)
        check("nacional completo 10: disponible 10", d["qty_disponible"] == 10, d)
        check("nacional completo 10: despachar 10 → 200",
              _despachar(cot.id, it.id, 10).status_code == 200)
        _limpiar(db)

        # ═══ 5. NACIONAL recepción parcial (6/10) → tope por lo recibido ═══
        cot, (it,) = _escenario(db, (10,))
        _recepcion_nacional(db, it, "completo", 6)
        d = _detalle_item(cot.id, it.id)
        check("nacional parcial 6: disponible 6", d["qty_disponible"] == 6, d)
        check("nacional parcial 6: despachar 7 → 400", _despachar(cot.id, it.id, 7).status_code == 400)
        check("nacional parcial 6: despachar 6 → 200", _despachar(cot.id, it.id, 6).status_code == 200)
        _limpiar(db)

        # ═══ 6. NACIONAL recepción ABIERTA (no cerrada) → no suma → indespachable ═══
        cot, (it,) = _escenario(db, (10,), estado_linea="comprado")
        _recepcion_nacional(db, it, "completo", 10, estado="abierta")
        d = _detalle_item(cot.id, it.id)
        check("nacional recepción ABIERTA: no suma (disponible 0)", d["qty_disponible"] == 0, d)
        check("nacional recepción ABIERTA: helper sin entrada para el ítem",
              it.id not in _recibidos([it.id]), _recibidos([it.id]))
        _limpiar(db)

        # ═══ 7. NACIONAL 'no_llego' (qty 0) → no suma; indespachable ═══
        cot, (it,) = _escenario(db, (10,), estado_linea="comprado")
        _recepcion_nacional(db, it, "no_llego", 0)
        d = _detalle_item(cot.id, it.id)
        check("nacional no_llego: no suma (disponible 0)", d["qty_disponible"] == 0, d)
        check("nacional no_llego: helper sin entrada para el ítem",
              it.id not in _recibidos([it.id]), _recibidos([it.id]))
        _limpiar(db)

        # ═══ 8. NACIONAL 'sobrante' (12/10) → tope a lo vendido (10) ═══
        cot, (it,) = _escenario(db, (10,))
        _recepcion_nacional(db, it, "sobrante", 12)
        d = _detalle_item(cot.id, it.id)
        check("nacional sobrante 12: disponible topeado a 10 (lo vendido)",
              d["qty_disponible"] == 10, d)
        _limpiar(db)

        # ═══ 9. NACIONAL entregas sucesivas (3 + 4 cerradas) → tope acumula = 7 ═══
        cot, (it,) = _escenario(db, (10,))
        _recepcion_nacional(db, it, "faltante", 3)   # 'faltante' SUMA lo que SÍ llegó
        _recepcion_nacional(db, it, "completo", 4)
        d = _detalle_item(cot.id, it.id)
        check("nacional 3+4: tope acumula = 7 (min(10,7))", d["qty_disponible"] == 7, d)
        check("nacional 3+4: despachar 8 → 400", _despachar(cot.id, it.id, 8).status_code == 400)
        check("nacional 3+4: despachar 7 → 200", _despachar(cot.id, it.id, 7).status_code == 200)
        _limpiar(db)

        # ═══ 10. VENTA MIXTA (nacional + internacional) → cada ítem por su fuente ═══
        cot, (nac, intl) = _escenario(db, (10, 10))
        _recepcion_nacional(db, nac, "completo", 6)      # nacional: tope 6
        _recepcion_embarque(db, intl, "completo", 9)     # internacional: tope 9
        dn = _detalle_item(cot.id, nac.id)
        di = _detalle_item(cot.id, intl.id)
        check("mixta: ítem nacional topeado a 6", dn["qty_disponible"] == 6, dn)
        check("mixta: ítem internacional topeado a 9", di["qty_disponible"] == 9, di)
        rec_dict = _recibidos([nac.id, intl.id])
        check("mixta: helper devuelve cada ítem por SU fuente (6.0 / 9), sin cruzar",
              rec_dict.get(nac.id) == 6 and rec_dict.get(intl.id) == 9, rec_dict)
        _limpiar(db)

        # ═══ 11. EXTRA MONZA: cierre de recepción de EMBARQUE con ítem nacional
        #        coexistiendo → los faltantes acumulados (monza_router_bodega usa el
        #        MISMO helper) NO cuentan unidades nacionales del OTRO ítem ═══
        cot, (intl, nac) = _escenario(db, (10, 10))
        # El ítem internacional viene llegando por embarque (flujo real: 'embarcado')
        intl.estado_linea = "embarcado"; db.commit()
        # El ítem nacional YA está en bodega con su recepción nacional cerrada 10/10
        _recepcion_nacional(db, nac, "completo", 10)
        # Embarque SOLO con el ítem internacional (disjunción por construcción)
        suf = uuid.uuid4().hex[:6].upper()
        emb = MonzaEmbarque(numero=f"{MARK}-E11-{suf}", estado="en_bodega")
        db.add(emb); db.flush()
        db.add(MonzaEmbarqueItem(embarque_id=emb.id, item_id=intl.id)); db.commit()
        r = client.post(f"/api/monza/bodega/embarques/{emb.id}/recibir")
        check("11: recepción de embarque abierta", r.status_code == 200, r.text)
        rid = r.json()["recepcion_id"]
        r = client.patch(f"/api/monza/bodega/recepciones/{rid}/items/{intl.id}",
                         json={"estado_recepcion": "faltante", "qty_recibida": 6})
        check("11: marcar faltante 6/10", r.status_code == 200, r.text)
        r = client.post(f"/api/monza/bodega/recepciones/{rid}/cerrar", json={})
        check("11: cierre de recepción de embarque OK", r.status_code == 200, r.text)
        db2 = SessionLocal()
        try:
            recls = db2.query(MonzaReclamo).filter(MonzaReclamo.item_id == intl.id).all()
            # Reclamo = 10 − (recibido_previo 0 + 6) = 4. Si el UNION contara las 10
            # unidades NACIONALES del otro ítem, el faltante saldría distinto/ausente.
            check("11: reclamo faltante del intl = 4 (sin contaminación nacional)",
                  len(recls) == 1 and recls[0].motivo == "faltante"
                  and (recls[0].qty_afectada or 0) == 4,
                  [(x.motivo, x.qty_afectada) for x in recls])
            check("11: ítem nacional intacto (en_bodega, sin reclamos)",
                  db2.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == nac.id)
                  .first().estado_linea == "en_bodega"
                  and db2.query(MonzaReclamo).filter(MonzaReclamo.item_id == nac.id).count() == 0)
        finally:
            db2.close()
        # Reposición: SEGUNDO embarque con el mismo ítem intl → el faltante pendiente
        # se calcula contra lo acumulado (6 del primer cierre), no contra la venta
        # completa — y las 10 nacionales del otro ítem siguen sin contaminar.
        suf = uuid.uuid4().hex[:6].upper()
        emb2 = MonzaEmbarque(numero=f"{MARK}-E12-{suf}", estado="en_bodega")
        db.add(emb2); db.flush()
        db.add(MonzaEmbarqueItem(embarque_id=emb2.id, item_id=intl.id)); db.commit()
        rid2 = client.post(f"/api/monza/bodega/embarques/{emb2.id}/recibir").json()["recepcion_id"]
        client.patch(f"/api/monza/bodega/recepciones/{rid2}/items/{intl.id}",
                     json={"estado_recepcion": "faltante", "qty_recibida": 3})
        r = client.post(f"/api/monza/bodega/recepciones/{rid2}/cerrar", json={})
        check("11: segundo cierre OK", r.status_code == 200, r.text)
        db2 = SessionLocal()
        try:
            qtys = sorted((x.qty_afectada or 0) for x in db2.query(MonzaReclamo)
                          .filter(MonzaReclamo.item_id == intl.id).all())
            # 10 − (6 acumulado + 3) = 1. Con contaminación nacional daría 0 (sin reclamo).
            check("11: faltante acumulado del intl = [1, 4] (recibido_previo solo embarque)",
                  qtys == [1, 4], qtys)
        finally:
            db2.close()
        di = _detalle_item(cot.id, intl.id)
        dn = _detalle_item(cot.id, nac.id)
        check("11: tope intl tras 6+3 = 9", di["qty_disponible"] == 9, di)
        check("11: tope nacional sigue 10", dn["qty_disponible"] == 10, dn)
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_recepcion_nacional_tope():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
