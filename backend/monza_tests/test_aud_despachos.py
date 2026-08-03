"""Regresiones de la AUDITORÍA INTEGRAL Fases 1→6 · router de Despachos Monza.

Cubre los hallazgos reparados en backend/monza_router_despachos.py:

  · #2 / #3 — Guía 52 con emisión AMBIGUA (uuid NULL + en_vuelo_desde puesto) cuyo
    claim ya VENCIÓ: anular el despacho debe seguir bloqueado (409 que manda a
    Reintentar). Antes se permitía, la mercadería volvía a bodega y salía una
    SEGUNDA guía 52 real al SII. El fallo CONFIRMADO (sin en_vuelo_desde) tiene que
    seguir permitiendo anular: si no, el despacho quedaría imborrable.
  · #13 — El router no tenía require_empresa("automotriz"): un usuario de minería
    leía y mutaba despachos de MonzaParts.
  · #17 — El tablero de avance etiquetaba 'listo' (y /counts contaba) una venta con
    línea en bodega SIN cupo despachable (llegó parcial y esas unidades ya salieron).

JAMÁS se llama al API de Wasabil: las filas DTE se fabrican directo en la BD.
Datos con prefijo MARK propio y limpieza total verificada con sesión NUEVA.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_aud_despachos.py -q
"""
import os
import sys
import uuid as _uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
import models.models  # noqa: E402,F401  (FK users.id resolubles en create_all)
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaDespacho, MonzaDespachoItem,
    MonzaEmbarque, MonzaRecepcion, MonzaRecepcionItem, MonzaLog, MonzaNotificacion,
)
from monza_router_despachos import router as despachos_router  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    MonzaWasabilDte, STATUS_FALLIDO, CLAIM_TTL_SEGUNDOS,
)

Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura monza_wasabil_dte

# Prefijo corto a propósito: monza_cotizaciones.numero es String(20).
MARK = "AUDDSP-T"
EMAIL = f"{MARK}@test.invalid"
CURRENT = {"empresa": "automotriz"}

app = FastAPI()
app.include_router(despachos_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa=CURRENT["empresa"])
client = TestClient(app)


# ─── Datos MARCADOS ────────────────────────────────────────────────────────────
def _venta(db, cantidad=5, estado_linea="en_bodega"):
    suf = _uuid.uuid4().hex[:6].upper()
    cli = MonzaCliente(nombre=f"{MARK} CLIENTE", rut="77.111.222-3")
    db.add(cli); db.flush()
    cot = MonzaCotizacion(numero=f"{MARK}-{suf}", cliente_id=cli.id, estado="vendida",
                          oc_cliente=f"OC-{suf}", total_bruto=100000)
    db.add(cot); db.flush()
    it = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} filtro",
                             cantidad=cantidad, precio_unitario_clp=10000,
                             estado_linea=estado_linea)
    db.add(it); db.flush()
    db.commit()
    return cot, it


def _despacho(db, cot, it, qty, estado="en_preparacion"):
    suf = _uuid.uuid4().hex[:6].upper()
    d = MonzaDespacho(numero=f"{MARK}-DSP-{suf}", cotizacion_id=cot.id,
                      cliente_nombre=f"{MARK} CLIENTE", estado=estado,
                      destinatario="Juan Pérez", direccion_entrega="Av. Siempre Viva 100")
    db.add(d); db.flush()
    db.add(MonzaDespachoItem(despacho_id=d.id, item_id=it.id, qty_despachada=qty))
    db.commit()
    return d


def _recepcion(db, it, qty_recibida, estado_recepcion="faltante"):
    """Recepción de embarque CERRADA: fija el tope físico del ítem."""
    suf = _uuid.uuid4().hex[:6].upper()
    emb = MonzaEmbarque(numero=f"{MARK}-E-{suf}", estado="recibido")
    db.add(emb); db.flush()
    rec = MonzaRecepcion(embarque_id=emb.id, estado="cerrada",
                         fecha_cierre=datetime.utcnow(), usuario_email=EMAIL)
    db.add(rec); db.flush()
    ri = MonzaRecepcionItem(recepcion_id=rec.id, item_id=it.id,
                            estado_recepcion=estado_recepcion, qty_recibida=qty_recibida)
    db.add(ri)
    db.commit()
    return rec, ri


def _dte_guia(db, despacho, **kw):
    """Fila DTE 52 fabricada a mano (estados que el guard debe distinguir)."""
    base = dict(empresa="automotriz", tipo_dte=52, despacho_id=despacho.id,
                uuid=None, status_id=None, en_vuelo_desde=None, folio=None, error=None)
    base.update(kw)
    fila = MonzaWasabilDte(**base)
    db.add(fila)
    db.commit()
    return fila


def _limpiar():
    db = SessionLocal()
    try:
        db.rollback()
        S = False
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .filter(MonzaCotizacion.numero.like(f"{MARK}%")).all()]
        item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                    .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
        dsp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                   .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
                   .filter(MonzaEmbarque.numero.like(f"{MARK}%")).all()]
        rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
                   .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
        db.query(MonzaWasabilDte).filter(
            MonzaWasabilDte.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "despacho",
            MonzaNotificacion.entidad_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcionItem).filter(
            MonzaRecepcionItem.recepcion_id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcion).filter(
            MonzaRecepcion.id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaEmbarque).filter(
            MonzaEmbarque.id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.id.in_(item_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(
            MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db2 = SessionLocal()  # sesión NUEVA (regla de la casa)
    try:
        restos = (
            db2.query(MonzaCotizacion).filter(MonzaCotizacion.numero.like(f"{MARK}%")).count()
            + db2.query(MonzaDespacho).filter(MonzaDespacho.numero.like(f"{MARK}%")).count()
            + db2.query(MonzaEmbarque).filter(MonzaEmbarque.numero.like(f"{MARK}%")).count()
            + db2.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count()
            + db2.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).count()
        )
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db2.close()


# ═══ Hallazgos #2 / #3 · guía 52 AMBIGUA con el claim vencido ═══════════════════
def test_anular_con_guia_ambigua_claim_vencido_bloquea():
    _limpiar()
    db = SessionLocal()
    try:
        cot, it = _venta(db)
        d = _despacho(db, cot, it, qty=2)
        # Emisión ambigua: el POST salió, Wasabil nunca confirmó (sin uuid, sin folio).
        dte = _dte_guia(db, d, en_vuelo_desde=datetime.utcnow())

        r = client.delete(f"/api/monza/despachos/entidades/{d.id}")
        assert r.status_code == 409, r.text
        assert "en curso" in r.json()["detail"], r.text

        # Se envejece el claim más allá del TTL: ANTES esto dejaba anular (hallazgo).
        dte.en_vuelo_desde = datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)
        db.commit()
        r = client.delete(f"/api/monza/despachos/entidades/{d.id}")
        assert r.status_code == 409, f"claim vencido pero AMBIGUO debe seguir bloqueando: {r.text}"
        assert "Reintentar" in r.json()["detail"], r.text
        db.rollback()
        assert db.get(MonzaDespacho, d.id).estado == "en_preparacion"

        # Asimetría DELIBERADA (fix del hallazgo #2): el endurecimiento es SOLO para
        # anular, que es irreversible. Editar el N° de guía a mano con el claim ya
        # vencido sigue permitido — si no, un DTE ambiguo dejaría la cabecera trabada.
        r = client.put(f"/api/monza/despachos/entidades/{d.id}",
                       json={"numero_guia": "GD-MANUAL-1"})
        assert r.status_code == 200, r.text
    finally:
        db.close()
        _limpiar()
        _verificar_limpieza()


def test_anular_con_fallo_confirmado_sigue_permitido():
    """El fix NO debe dejar despachos imborrables: sin `en_vuelo_desde` el fallo está
    CONFIRMADO (lo limpia _emitir_en_wasabil cuando el error no es ambiguo)."""
    _limpiar()
    db = SessionLocal()
    try:
        cot, it = _venta(db)
        d = _despacho(db, cot, it, qty=2)
        _dte_guia(db, d, status_id=STATUS_FALLIDO, error="RUT receptor rechazado",
                  en_vuelo_desde=None)
        r = client.delete(f"/api/monza/despachos/entidades/{d.id}")
        assert r.status_code == 200, r.text
        db.rollback()
        assert db.get(MonzaDespacho, d.id).estado == "anulado"

        # Fallido CON uuid y claim vencido (el documento existe y el SII lo rechazó):
        # tampoco queda atrapado por la rama ambigua (esa exige uuid NULL).
        cot2, it2 = _venta(db)
        d2 = _despacho(db, cot2, it2, qty=2)
        _dte_guia(db, d2, uuid="uuid-fail", status_id=STATUS_FALLIDO, error="rechazada",
                  en_vuelo_desde=datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60))
        r = client.delete(f"/api/monza/despachos/entidades/{d2.id}")
        assert r.status_code == 200, r.text
    finally:
        db.close()
        _limpiar()
        _verificar_limpieza()


# ═══ Hallazgo #13 · candado de empresa ═════════════════════════════════════════
def test_router_despachos_candado_automotriz():
    _limpiar()
    db = SessionLocal()
    try:
        cot, it = _venta(db)
        d = _despacho(db, cot, it, qty=2)
        CURRENT["empresa"] = "mineria"
        for r in (client.get("/api/monza/despachos/counts"),
                  client.get("/api/monza/despachos/listos"),
                  client.get("/api/monza/despachos/avance", params={"tab": "listas"}),
                  client.get(f"/api/monza/despachos/entidades/{d.id}"),
                  client.delete(f"/api/monza/despachos/entidades/{d.id}")):
            assert r.status_code == 403, f"minería no debe entrar a Monza: {r.request.url} {r.text}"
        db.rollback()
        assert db.get(MonzaDespacho, d.id).estado == "en_preparacion", \
            "el DELETE de minería no debe haber tocado nada"
        CURRENT["empresa"] = "automotriz"
        assert client.get("/api/monza/despachos/counts").status_code == 200
    finally:
        CURRENT["empresa"] = "automotriz"
        db.close()
        _limpiar()
        _verificar_limpieza()


# ═══ Hallazgo #17 · 'listo' sin cupo despachable ═══════════════════════════════
def test_avance_y_counts_no_marcan_listo_sin_cupo():
    _limpiar()
    db = SessionLocal()
    try:
        base = client.get("/api/monza/despachos/counts").json()

        # 5 vendidas, llegaron 2 (el resto viene en camino) y esas 2 YA se despacharon
        # con el despacho CERRADO: la línea sigue en_bodega, pero no queda nada que sacar.
        cot, it = _venta(db, cantidad=5)
        rec, ri = _recepcion(db, it, qty_recibida=2)
        _despacho(db, cot, it, qty=2, estado="despachado")

        r = client.get("/api/monza/despachos/avance", params={"tab": "listas"})
        assert r.status_code == 200, r.text
        card = next((c for c in r.json() if c["id"] == cot.id), None)
        assert card is not None, "la venta NO se oculta: sigue esperando stock"
        assert card["items_en_bodega"] == 1
        assert card["items_con_cupo"] == 0
        assert card["estado"] == "en_espera_stock", card

        assert not any(v["id"] == cot.id for v in client.get("/api/monza/despachos/listos").json())

        ahora = client.get("/api/monza/despachos/counts").json()
        assert ahora["items_listos"] == base["items_listos"], \
            "un ítem sin cupo no debe sumar a items_listos"
        assert ahora["ventas_listas"] == base["ventas_listas"]

        # Llegan las 3 que faltaban → vuelve a haber cupo: 'listo' y counts +1.
        ri.qty_recibida = 5
        ri.estado_recepcion = "completo"
        db.commit()
        card = next(c for c in client.get(
            "/api/monza/despachos/avance", params={"tab": "listas"}).json()
            if c["id"] == cot.id)
        assert card["estado"] == "listo" and card["items_con_cupo"] == 1, card
        ahora = client.get("/api/monza/despachos/counts").json()
        assert ahora["items_listos"] == base["items_listos"] + 1
        assert ahora["ventas_listas"] == base["ventas_listas"] + 1
    finally:
        db.close()
        _limpiar()
        _verificar_limpieza()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
