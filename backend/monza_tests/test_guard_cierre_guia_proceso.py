"""Fase 6 · decisión del cierre de despacho frente a una guía 52 EN PROCESO.

LA TRAMPA (informe del experto 2). Cerrar un despacho lo deja 'despachado', el
ÚNICO estado facturable de Monza. Si en ese momento la guía electrónica 52 sigue
sin folio, `numero_guia` conserva el N° que el operador tecleó a mano (el módulo
Wasabil lo pisa con el folio real recién al confirmarse la emisión) — y una
factura 33 emitida ahí saldría al SII referenciando un folio que el SII NO
reconoce. Es irreversible.

LA DECISIÓN (menor riesgo operativo, documentada en monza_router_despachos.py:
_cerrar_despacho_tx): el cierre NO se bloquea. Registra un hecho FÍSICO (la
mercadería salió) y bloquearlo dejaría la bodega rehén de un sistema externo —
un DTE atascado en pendiente/ambiguo haría el despacho imposible de cerrar, la
línea sin voltear y la venta trabada, sin escape. El candado va donde ocurre la
acción irreversible: la EMISIÓN de la factura 33
(_guia_electronica_en_proceso, monza_wasabil_dte/router.py). Espejo del espíritu
de Grupo AM (commit a0d4671). Desde despachos se aporta la SEÑAL:
`guia_sii_en_proceso` + `aviso` en la respuesta, en el log y en la notificación.

ESTA SUITE BLINDA esa decisión (que nadie la invierta sin darse cuenta):

  1. Sin guía electrónica          → cierra · flag False (caso normal).
  2. Claim de emisión EN VUELO     → CIERRA IGUAL (no se bloquea la bodega)
                                     · flag True · aviso · log y notificación.
  3. Procesando (uuid, sin folio)  → cierra · flag True.
  4. Pendiente (borrador Wasabil)  → cierra · flag True.
  5. EMITIDA con folio             → cierra · flag False (ahí `numero_guia` ES el
                                     folio real: la referencia 52 es legítima).
  6. FALLIDA sin uuid ni claim     → cierra · flag False (direccionalidad: sin
                                     guía electrónica el N° manual es válido).
  7. Claim EXPIRADO sin uuid       → cierra · flag False (el claim vencido no es
                                     una emisión viva).
  8. ASIMETRÍA deliberada con la guía en proceso: cerrar SÍ (200), anular NO
     (409) y pisar el N° de guía a mano NO (409) — el folio queda protegido y el
     N° viejo no se puede "arreglar" a mano dentro de la ventana.

Datos MARCADOS + limpieza total en try/finally; nunca toca datos reales.
CERO llamadas a Wasabil: las filas de DTE se escriben directo en la BD.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_guard_cierre_guia_proceso.py -q
(también:   ./venv/bin/python monza_tests/test_guard_cierre_guia_proceso.py)
"""
import os
import sys
import uuid as uuid_mod
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem,
    MonzaDespacho, MonzaDespachoItem, MonzaLog, MonzaNotificacion,
)
import monza_router_despachos as despachos_mod  # noqa: E402
from monza_router_despachos import router as despachos_router  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    MonzaWasabilDte, CLAIM_TTL_SEGUNDOS,
    STATUS_PENDIENTE, STATUS_PROCESANDO, STATUS_EMITIDO, STATUS_FALLIDO,
)
from monza_wasabil_dte.service import TIPO_DOC_GUIA  # noqa: E402

MARK = "test-guard-cierre-52"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(despachos_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)


def _setup(db):
    """Venta 'vendida' con 1 ítem en bodega (40 uds) — sin recepción registrada, o
    sea tope físico = cantidad vendida. Alcanza para los 8 despachos de la suite."""
    cli = MonzaCliente(nombre=MARK)
    db.add(cli); db.flush()
    suf = uuid_mod.uuid4().hex[:6].upper()
    cot = MonzaCotizacion(numero=f"CT-GC52-{suf}", cliente_id=cli.id, estado="vendida")
    db.add(cot); db.flush()
    item = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} A",
                               cantidad=40, estado_linea="en_bodega")
    db.add(item); db.commit()
    return cot.id, item.id


def _nuevo_despacho(cot_id: int, item_id: int, numero_guia: str = "") -> int:
    """Despacho en preparación por 1 unidad, vía API (camino real)."""
    r = client.post("/api/monza/despachos/crear", json={
        "cotizacion_id": cot_id,
        "items": [{"item_id": item_id, "qty": 1}],
        "numero_guia": numero_guia,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _dte(db, despacho_id: int, *, status_id=None, uuid_w=None, folio=None,
         en_vuelo_hace_seg=None) -> MonzaWasabilDte:
    """Fila de DTE 52 escrita DIRECTO en la BD (jamás se llama a Wasabil)."""
    en_vuelo = None
    if en_vuelo_hace_seg is not None:
        en_vuelo = datetime.utcnow() - timedelta(seconds=en_vuelo_hace_seg)
    fila = MonzaWasabilDte(
        empresa="automotriz", tipo_dte=TIPO_DOC_GUIA, despacho_id=despacho_id,
        uuid=uuid_w, status_id=status_id, folio=folio, en_vuelo_desde=en_vuelo,
    )
    db.add(fila); db.commit()
    return fila


def _cerrar(despacho_id: int):
    return client.post(f"/api/monza/despachos/entidades/{despacho_id}/cerrar")


def _cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre == MARK).all()]
        dsp_ids = [r[0] for r in db.query(MonzaDespacho.id).filter(
            MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        # Los DTE PRIMERO: la FK monza_wasabil_dte.despacho_id no tiene ON DELETE.
        db.query(MonzaWasabilDte).filter(
            MonzaWasabilDte.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "despacho",
            MonzaNotificacion.entidad_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
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


def _estado(db, despacho_id: int) -> str:
    """Estado leído con la sesión refrescada (no desde el identity map viejo)."""
    d = db.query(MonzaDespacho).populate_existing().filter(
        MonzaDespacho.id == despacho_id).first()
    return d.estado


def test_guard_cierre_guia_proceso():
    db = SessionLocal()
    try:
        _cleanup()
        cot_id, item_id = _setup(db)

        # ── 1) Sin guía electrónica → cierra, sin señal ────────────────────────
        d1 = _nuevo_despacho(cot_id, item_id, numero_guia="G-MANUAL-1")
        r = _cerrar(d1)
        assert r.status_code == 200, r.text
        assert r.json()["guia_sii_en_proceso"] is False, r.json()
        assert r.json()["aviso"] is None, r.json()
        assert _estado(db, d1) == "despachado"

        # ── 2) Claim EN VUELO (sin uuid, sin folio) → CIERRA IGUAL + señal ─────
        # El corazón de la decisión: la bodega NUNCA queda trabada por el SII.
        d2 = _nuevo_despacho(cot_id, item_id, numero_guia="G-MANUAL-VIEJO")
        _dte(db, d2, status_id=STATUS_PENDIENTE, en_vuelo_hace_seg=1)
        r = _cerrar(d2)
        assert r.status_code == 200, f"el cierre NO debe bloquearse: {r.text}"
        assert r.json()["guia_sii_en_proceso"] is True, r.json()
        assert "folio" in (r.json()["aviso"] or ""), r.json()
        assert _estado(db, d2) == "despachado"
        # El N° manual viejo sigue ahí: por eso la factura 33 no puede referenciarlo.
        dsp2 = db.query(MonzaDespacho).populate_existing().filter(
            MonzaDespacho.id == d2).first()
        assert dsp2.numero_guia == "G-MANUAL-VIEJO", dsp2.numero_guia
        # La señal queda registrada para el operador (log + notificación).
        log = (db.query(MonzaLog).filter(MonzaLog.entidad == "despacho",
                                         MonzaLog.entidad_id == d2)
               .order_by(MonzaLog.id.desc()).first())
        assert log and "guía SII en proceso" in (log.detalle or ""), log and log.detalle
        notif = (db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "despacho",
            MonzaNotificacion.entidad_id == d2).order_by(MonzaNotificacion.id.desc()).first())
        assert notif and "folio" in (notif.mensaje or ""), notif and notif.mensaje

        # ── 3) Procesando en el SII (uuid, sin folio) → cierra + señal ─────────
        d3 = _nuevo_despacho(cot_id, item_id)
        _dte(db, d3, status_id=STATUS_PROCESANDO, uuid_w=f"uuid-{MARK}-3")
        r = _cerrar(d3)
        assert r.status_code == 200, r.text
        assert r.json()["guia_sii_en_proceso"] is True, r.json()

        # ── 4) Pendiente (borrador en Wasabil, uuid, sin folio) → cierra + señal
        d4 = _nuevo_despacho(cot_id, item_id)
        _dte(db, d4, status_id=STATUS_PENDIENTE, uuid_w=f"uuid-{MARK}-4")
        r = _cerrar(d4)
        assert r.status_code == 200, r.text
        assert r.json()["guia_sii_en_proceso"] is True, r.json()

        # ── 5) EMITIDA con folio → cierra SIN señal ────────────────────────────
        # Con folio, `numero_guia` ya es el folio real: la referencia 52 de la
        # factura es legítima y no hay nada que advertir.
        d5 = _nuevo_despacho(cot_id, item_id)
        _dte(db, d5, status_id=STATUS_EMITIDO, uuid_w=f"uuid-{MARK}-5", folio="9001")
        r = _cerrar(d5)
        assert r.status_code == 200, r.text
        assert r.json()["guia_sii_en_proceso"] is False, r.json()

        # ── 6) FALLIDA sin uuid ni claim → cierra SIN señal ────────────────────
        # Direccionalidad (la mitad que NO debe bloquear): no hay guía electrónica,
        # el N° manual vuelve a ser la referencia válida.
        d6 = _nuevo_despacho(cot_id, item_id, numero_guia="G-MANUAL-6")
        _dte(db, d6, status_id=STATUS_FALLIDO)
        r = _cerrar(d6)
        assert r.status_code == 200, r.text
        assert r.json()["guia_sii_en_proceso"] is False, r.json()

        # ── 7) Claim EXPIRADO sin uuid → cierra SIN señal ──────────────────────
        d7 = _nuevo_despacho(cot_id, item_id)
        _dte(db, d7, status_id=STATUS_PENDIENTE,
             en_vuelo_hace_seg=CLAIM_TTL_SEGUNDOS + 60)
        r = _cerrar(d7)
        assert r.status_code == 200, r.text
        assert r.json()["guia_sii_en_proceso"] is False, r.json()

        # ── 8) ASIMETRÍA con la guía en proceso: cerrar sí, anular/pisar no ────
        d8 = _nuevo_despacho(cot_id, item_id, numero_guia="G-MANUAL-8")
        _dte(db, d8, status_id=STATUS_PROCESANDO, uuid_w=f"uuid-{MARK}-8")
        # Anular destruiría un documento tributario vivo → 409 (sigue bloqueado).
        r_anu = client.delete(f"/api/monza/despachos/entidades/{d8}")
        assert r_anu.status_code == 409, r_anu.text
        # "Arreglar" el N° a mano dentro de la ventana tampoco: lo fija el SII.
        r_put = client.put(f"/api/monza/despachos/entidades/{d8}",
                           json={"numero_guia": "G-INVENTADO"})
        assert r_put.status_code == 409, r_put.text
        # Y el cierre, que es un hecho físico, SÍ pasa.
        r = _cerrar(d8)
        assert r.status_code == 200, r.text
        assert r.json()["guia_sii_en_proceso"] is True, r.json()
        assert _estado(db, d8) == "despachado"

        # ── Verificación del helper con SESIÓN NUEVA (sin identity map sucio) ──
        db2 = SessionLocal()
        try:
            esperado = {d1: False, d2: True, d3: True, d4: True,
                        d5: False, d6: False, d7: False, d8: True}
            for dsp_id, esp in esperado.items():
                got = despachos_mod._guia_sii_en_proceso(db2, dsp_id)
                assert got is esp, f"despacho {dsp_id}: esperado {esp}, obtuve {got}"
        finally:
            db2.close()
        print("\n=== TODO OK (8 escenarios) ===")
    finally:
        db.close()
        _cleanup()
        print("Cleanup OK")


if __name__ == "__main__":
    test_guard_cierre_guia_proceso()
