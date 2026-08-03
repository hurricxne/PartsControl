"""Guard: anular un despacho con la emisión de su guía 52 SIN RESOLVER.

Port a Grupo AM del endurecimiento que MonzaParts ya tiene
(monza_router_despachos.py:_guia_electronica_activa · monza_tests/test_aud_despachos.py).
GA es la marca que YA EMITE DE VERDAD, así que este es el hueco más caro:

  Si Wasabil da timeout/5xx, `_emitir_en_wasabil` conserva `en_vuelo_desde` a propósito
  (fallo AMBIGUO: el documento PUDO nacer con folio real en el SII). Cuando el claim
  expiraba por TTL, `_guia_electronica_activa` devolvía None → se podía ANULAR el
  despacho, la mercadería quedaba libre y salía una SEGUNDA guía 52 LEGAL por lo mismo.
  Verificado antes del fix: HTTP 200 y el despacho en estado 'anulado'.

Se cubren las tres direcciones, porque el fix es ASIMÉTRICO a propósito:
  1. ambiguo + claim vencido → anular BLOQUEA (409 que manda a «Reintentar»)
  2. fallo CONFIRMADO (sin en_vuelo_desde) → anular SIGUE PERMITIDO (si no, el
     despacho quedaría IMBORRABLE)
  3. PUT del N° de guía con ambiguo vencido → SIGUE PERMITIDO (no es irreversible:
     `incluir_ambiguo` NO se pasa en _rechazar_si_pisa_folio)

JAMÁS se llama al API de Wasabil (ni con issue=False): las filas DTE se fabrican
directo en la BD. Datos con prefijo MARK propio y limpieza verificada con sesión NUEVA.

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_anular_guia_ambigua.py -q
(también:   ./venv/bin/python wasabil_dte/tests/test_anular_guia_ambigua.py)
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
)
from wasabil_dte.models import (  # noqa: E402
    WasabilDte, STATUS_PENDIENTE, STATUS_FALLIDO, CLAIM_TTL_SEGUNDOS,
)
from routers.despachos import router as despachos_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura wasabil_dte

MARK = "__TEST_ANUL52__"

app = FastAPI()
app.include_router(despachos_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=None, email=f"{MARK}@test.invalid", empresa="mineria")   # candado 'mineria' del router
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ─── Datos MARCADOS ────────────────────────────────────────────────────────────
def _venta(db, estado_despacho="en_preparacion"):
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} HEPI", rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro", cantidad=10, estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    d = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                 estado=estado_despacho)
    db.add(d); db.flush()
    db.add(DespachoItem(despacho_id=d.id, item_cotizacion_id=it.id, qty_despachada=10))
    db.commit()
    return cot, oc, d, it


def _dte_guia(db, despacho, **kw):
    """Fila DTE 52 fabricada a mano (los estados que el guard debe distinguir)."""
    base = dict(empresa="mineria", tipo_dte=52, despacho_id=despacho.id,
                uuid=None, status_id=None, en_vuelo_desde=None, folio=None, error=None)
    base.update(kw)
    fila = WasabilDte(**base)
    db.add(fila)
    db.commit()
    return fila


def _limpiar(db):
    db.rollback()
    S = False
    cot_ids = [c.id for c in db.query(Cotizacion)
               .filter(Cotizacion.numero.like(f"{MARK}%")).all()]
    oc_ids = [o.id for o in db.query(OcCliente)
              .filter(OcCliente.cotizacion_id.in_(cot_ids or [0])).all()]
    dsp_ids = [d.id for d in db.query(Despacho)
               .filter(Despacho.oc_cliente_id.in_(oc_ids or [0])).all()]
    db.query(WasabilDte).filter(
        WasabilDte.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(DespachoItem).filter(
        DespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(Despacho).filter(Despacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(OcCliente).filter(OcCliente.id.in_(oc_ids or [0])).delete(synchronize_session=S)
    db.query(ItemCotizacion).filter(
        ItemCotizacion.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.commit()


def _verificar_limpieza():
    db2 = SessionLocal()   # sesión NUEVA (regla de la casa: no el caché del ORM)
    try:
        restos = (
            db2.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
            + db2.query(OcCliente).filter(OcCliente.numero_oc.like(f"{MARK}%")).count()
            + db2.query(Despacho).filter(Despacho.numero_despacho.like(f"{MARK}%")).count()
        )
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db2.close()


# El claim se envejece más allá del TTL: es lo que hace el reloj en producción.
_VENCIDO = timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)


def run():
    db = SessionLocal()
    _limpiar(db)
    try:
        # ═══ 1 · emisión AMBIGUA (uuid NULL + en_vuelo_desde) ═══════════════════
        _cot, _oc, d, _it = _venta(db)
        dte = _dte_guia(db, d, status_id=STATUS_PENDIENTE,
                        error="timeout hablando con Wasabil (fallo AMBIGUO)",
                        en_vuelo_desde=datetime.utcnow())
        r = client.delete(f"/api/despachos/{d.id}")
        check("1 claim FRESCO → anular 409 (esperar el resultado)",
              r.status_code == 409 and "en curso" in r.json()["detail"], r.text)

        dte.en_vuelo_desde = datetime.utcnow() - _VENCIDO
        db.commit()
        db.rollback()   # cierra el snapshot/locks de ESTA sesión antes del request
        r = client.delete(f"/api/despachos/{d.id}")
        check("1 claim VENCIDO pero AMBIGUO → anular sigue 409 (antes dejaba anular)",
              r.status_code == 409, r.text)
        check("1 el 409 manda a «Reintentar» (esperar ya no resuelve nada)",
              r.status_code == 409 and "Reintentar" in r.json()["detail"], r.text)
        db.rollback()
        check("1 el despacho quedó en preparación (mercadería NO liberada)",
              db.get(Despacho, d.id).estado == "en_preparacion",
              db.get(Despacho, d.id).estado)

        _limpiar(db)

        # ═══ 1.b · asimetría DELIBERADA del fix ═════════════════════════════════
        # Editar el N° de guía a mano NO es irreversible, así que `incluir_ambiguo` NO se
        # pasa en _rechazar_si_pisa_folio. Si se endureciera también ahí, un DTE ambiguo
        # dejaría la cabecera del despacho trabada para siempre. Despacho PROPIO para que
        # este check no dependa del resultado del anterior.
        _cot, _oc, d_put, _it = _venta(db)
        _dte_guia(db, d_put, status_id=STATUS_PENDIENTE, error="timeout",
                  en_vuelo_desde=datetime.utcnow() - _VENCIDO)
        r = client.put(f"/api/despachos/{d_put.id}", json={"numero_guia": "G-MANUAL-1"})
        check("1.b PUT numero_guia con ambiguo vencido SIGUE permitido (es reversible)",
              r.status_code == 200, r.text)
        _limpiar(db)

        # ═══ 2 · guía EMITIDA con folio → 409 que manda a Wasabil ═══════════════
        _cot, _oc, d, _it = _venta(db)
        _dte_guia(db, d, status_id=3, uuid="uuid-ok", folio="136")
        r = client.delete(f"/api/despachos/{d.id}")
        check("2 guía EMITIDA (folio) → 409 mencionando el folio y Wasabil",
              r.status_code == 409 and "136" in r.json()["detail"]
              and "Wasabil" in r.json()["detail"], r.text)
        _limpiar(db)

        # ═══ 3 · fallo CONFIRMADO → anular DEBE seguir permitido ════════════════
        # _emitir_en_wasabil limpia `en_vuelo_desde` cuando el error NO es ambiguo
        # (4xx, token malo, conexión rechazada): ahí consta que NO se creó nada.
        _cot, _oc, d, _it = _venta(db)
        _dte_guia(db, d, status_id=STATUS_FALLIDO, error="conexión rechazada",
                  en_vuelo_desde=None)
        r = client.delete(f"/api/despachos/{d.id}")
        check("3 fallo CONFIRMADO → anular 200 (el despacho no queda imborrable)",
              r.status_code == 200, r.text)
        db.rollback()
        check("3 el despacho quedó anulado", db.get(Despacho, d.id).estado == "anulado")
        _limpiar(db)

        # ═══ 4 · fallido CON uuid y claim vencido → tampoco queda atrapado ══════
        # (el documento existe y el SII lo rechazó; la rama ambigua exige uuid NULL)
        _cot, _oc, d, _it = _venta(db)
        _dte_guia(db, d, uuid="uuid-fail", status_id=STATUS_FALLIDO, folio=None,
                  error="RUT receptor rechazado por el SII",
                  en_vuelo_desde=datetime.utcnow() - _VENCIDO)
        r = client.delete(f"/api/despachos/{d.id}")
        check("4 fallido CON uuid y claim vencido → anular 200 (no lo atrapa el ambiguo)",
              r.status_code == 200, r.text)
        _limpiar(db)

        # ═══ 5 · sin fila DTE → el flujo normal no se toca ══════════════════════
        _cot, _oc, d, _it = _venta(db)
        r = client.delete(f"/api/despachos/{d.id}")
        check("5 despacho SIN guía electrónica → anular 200 (sin regresión)",
              r.status_code == 200, r.text)
    finally:
        _limpiar(db)
        db.close()
        _verificar_limpieza()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_anular_despacho_con_guia_52_ambigua():
    run()


if __name__ == "__main__":
    run()
