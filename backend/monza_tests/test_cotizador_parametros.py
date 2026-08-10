"""La calculadora de precios GUARDA sus parámetros (MonzaParts).

EL BUG QUE CIERRA
    El dueño reportó: «en la calculadora de precios, una vez guardo aplicar precios en USD o
    moneda extranjera, cuando vuelvo a abrir me muestra CLP, y en el costo muestra lo mismo que
    en el precio CLP».

    Causa raíz: `aplicar_precios` guardaba SOLO `precio_clp`. El schema `AplicarPrecioItem` ni
    siquiera tenía costo, moneda, peso ni markup, y `monza_lead_items` no tenía columnas para
    ellos. Los parámetros NUNCA llegaban a la base, así que al reabrir no había nada que
    restaurar y la pantalla caía en un camino de emergencia que mostraba el precio final como si
    fuera el costo, en CLP con markup 0.

    Ojo con la asimetría que lo hacía difícil de ver: la COTIZACIÓN (monza_cotizacion_items) sí
    congelaba estos campos. El agujero estaba una etapa antes, en el LEAD.

LO SEGUNDO QUE CIERRA
    La moneda del flete aéreo salía SIEMPRE de la configuración global (`monza_config`, EUR por
    defecto) y no había forma de elegir dólares al cotizar. Ahora se elige por COTIZACIÓN —una
    sola para todos los ítems— y se guarda en el lead.

SONDAS DE PODER DISCRIMINANTE
    · §2 usa USD con un TC distinto del de EUR a propósito: si el guardado se pierde, el costo
      vuelve como el precio en CLP y el check se cae.
    · §3 pide el flete en USD teniendo la configuración global en EUR: con el código viejo el
      flete se calculaba con el TC del euro y el precio no coincide.
    · §5 manda costo 0 y markup 0, que son valores LEGÍTIMOS: con un `if ap.costo:` en vez de
      `is not None` se descartarían en silencio.

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_cotizador_parametros.py -q
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
    MonzaCliente, MonzaConfig, MonzaLead, MonzaLeadItem, MonzaLeadActividad, MonzaLog,
)
from monza_router_cotizador import router as cotizador_router  # noqa: E402
from monza_router_leads import router as leads_router  # noqa: E402

MARK = "test-mzcotpar"
LEAD_MARK = "L-MZCP"          # MonzaLead.numero es String(20): prefijo corto propio
EMAIL = f"{MARK}@test.invalid"

CURRENT = {"id": 1, "empresa": "automotriz"}
app = FastAPI()
app.include_router(cotizador_router)
app.include_router(leads_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], email=EMAIL, empresa=CURRENT["empresa"])
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _config():
    """TC y tarifa CONOCIDOS: los checks comparan contra números escritos a mano, así que la
    config no puede quedar a lo que haya en la base de desarrollo. Se restaura al final."""
    db = SessionLocal()
    try:
        cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
        if not cfg:
            cfg = MonzaConfig(id=1)
            db.add(cfg)
        previo = (cfg.tc_usd_clp, cfg.tc_eur_clp, cfg.tarifa_aerea_por_kg,
                  cfg.moneda_tarifa, cfg.iva_pct,
                  cfg.tarifa_aerea_eur_por_kg, cfg.tarifa_aerea_usd_por_kg)
        cfg.tc_usd_clp = 900.0
        cfg.tc_eur_clp = 1000.0      # distinto del USD a propósito: delata el TC equivocado
        cfg.tarifa_aerea_por_kg = 5.0
        cfg.moneda_tarifa = "EUR"    # global en EUR: la cotización va a pedir USD
        # Tarifa POR MONEDA (2026-08-08): desde que existen estas columnas son la fuente
        # de verdad y le GANAN al legado `tarifa_aerea_por_kg`. Sin sembrarlas, los checks
        # de este archivo calculaban contra el 5.0 legado mientras el motor usaba la
        # tarifa real de la base de desarrollo — el mismo número tiene que estar en las
        # dos para que los totales escritos a mano sigan siendo verificables.
        cfg.tarifa_aerea_eur_por_kg = 5.0
        cfg.tarifa_aerea_usd_por_kg = 5.0
        cfg.iva_pct = 19.0
        db.commit()
        return previo
    finally:
        db.close()


def _restaurar_config(previo):
    """Devuelve monza_config a como estaba — con REINTENTO y verificación.

    Esta fila le pone TC, tarifa e IVA a toda cotización de MonzaParts. Una restauración
    que falle en silencio deja al dueño cotizando con los números del test: ya pasó una
    vez (los TC quedaron en 1000/900 y la tarifa en 5,0 tras una corrida interrumpida),
    y por eso ahora reintenta y COMPRUEBA en una lectura nueva, fallando ruidosamente si
    no quedó igual."""
    if not previo:
        return
    for _ in range(3):
        db = SessionLocal()
        try:
            cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
            if cfg:
                (cfg.tc_usd_clp, cfg.tc_eur_clp, cfg.tarifa_aerea_por_kg,
                 cfg.moneda_tarifa, cfg.iva_pct,
                 cfg.tarifa_aerea_eur_por_kg, cfg.tarifa_aerea_usd_por_kg) = previo
                db.commit()
            break
        except Exception:
            db.rollback()
        finally:
            db.close()
    db = SessionLocal()
    try:
        cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
        if cfg:
            actual = (cfg.tc_usd_clp, cfg.tc_eur_clp, cfg.tarifa_aerea_por_kg,
                      cfg.moneda_tarifa, cfg.iva_pct,
                      cfg.tarifa_aerea_eur_por_kg, cfg.tarifa_aerea_usd_por_kg)
            assert actual == tuple(previo), (
                "la configuración REAL de MonzaParts quedó con los valores del test "
                f"(esperado {tuple(previo)}, quedó {actual}): restáurala antes de cotizar")
    finally:
        db.close()


def _seed():
    """Lead con 1 ítem, sin precio ni parámetros (el estado de partida real)."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA")
        db.add(cli)
        db.flush()
        lead = MonzaLead(numero=f"{LEAD_MARK}-{uuid.uuid4().hex[:6].upper()}",
                         cliente_id=cli.id, estado="pendiente")
        db.add(lead)
        db.flush()
        it = MonzaLeadItem(lead_id=lead.id, descripcion="Filtro de aceite motor",
                           numero_parte="A2761800009", cantidad=2)
        db.add(it)
        db.commit()
        return lead.id, it.id
    finally:
        db.close()


def _item(item_id):
    db = SessionLocal()
    try:
        return db.query(MonzaLeadItem).filter(MonzaLeadItem.id == item_id).first()
    finally:
        db.close()


def _lead(lead_id):
    db = SessionLocal()
    try:
        return db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        lead_ids = [r[0] for r in db.query(MonzaLead.id)
                    .filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).all()]
        db.query(MonzaLeadActividad).filter(
            MonzaLeadActividad.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLeadItem).filter(
            MonzaLeadItem.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLead).filter(MonzaLead.id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(
            synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    """Sesión NUEVA (regla de la casa): una reutilizada podría servir su propio snapshot."""
    db = SessionLocal()
    try:
        restos = (db.query(MonzaLead).filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).count()
                  + db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    previo = _config()
    _limpiar()
    try:
        lead_id, item_id = _seed()

        # ── 1) Calcular con el flete en USD teniendo la config global en EUR ─────────
        r = client.post("/api/monza/cotizador/calcular", json={
            "lead_id": lead_id,
            "moneda_tarifa": "USD", "tarifa_aerea": 5.0,
            "items": [{"item_id": item_id, "calidades": [{
                "calidad": "Genuine", "costo": 100.0, "moneda": "USD",
                "peso_kg": 2.0, "markup_pct": 28.0}]}],
        })
        check("1a calcular responde 200", r.status_code == 200, r.text[:200])
        cal = r.json()["items"][0]["calidades"][0]
        # costo 100 USD × 900 = 90.000 · flete 2 kg × 5 USD × 900 = 9.000 · +28% = 126.720
        check("1b el costo se convierte con el TC del DÓLAR (no el del euro)",
              cal["costo_clp"] == 90000, cal["costo_clp"])
        check("1c el flete usa el TC del DÓLAR porque la cotización lo pidió en USD",
              cal["flete_clp"] == 9000, (cal["flete_clp"], "esperado 9000; con EUR daría 10000"))
        check("1d precio neto = (costo + flete) × 1,28",
              cal["precio_neto"] == 126720, cal["precio_neto"])
        check("1e la respuesta declara el flete EFECTIVO, no el global",
              r.json()["config"]["moneda_tarifa"] == "USD", r.json()["config"])

        # ── 2) Aplicar: los parámetros TIENEN que quedar guardados ──────────────────
        r = client.post("/api/monza/cotizador/aplicar", json={
            "lead_id": lead_id,
            "moneda_tarifa": "USD", "tarifa_aerea": 5.0,
            "items": [{"item_id": item_id, "calidad": "Genuine", "precio_clp": 126720,
                       "costo": 100.0, "moneda": "USD", "peso_kg": 2.0, "markup_pct": 28.0}],
        })
        check("2a aplicar responde 200", r.status_code == 200, r.text[:200])
        it = _item(item_id)
        check("2b el COSTO quedó guardado en su moneda (100, no el precio 126.720)",
              it.costo == 100.0, it.costo)
        check("2c la MONEDA quedó en USD (no CLP)", (it.moneda or "") == "USD", it.moneda)
        check("2d el PESO quedó guardado", it.peso_kg == 2.0, it.peso_kg)
        check("2e el MARKUP quedó guardado (no 0)", it.markup_pct == 28.0, it.markup_pct)
        check("2f el TC aplicado se resolvió en el SERVIDOR desde la moneda del ítem",
              it.tc_aplicado == 900.0, it.tc_aplicado)
        check("2g y el precio final sigue siendo el mismo", it.precio_clp == 126720, it.precio_clp)

        # ── 3) El flete elegido quedó en el LEAD (uno para todos sus ítems) ─────────
        ld = _lead(lead_id)
        check("3a la moneda del flete quedó en el lead", (ld.moneda_tarifa or "") == "USD",
              ld.moneda_tarifa)
        check("3b y la tarifa también", ld.tarifa_aerea == 5.0, ld.tarifa_aerea)

        # ── 4) Al REABRIR, la pantalla recibe los parámetros (no el precio como costo) ──
        r = client.get(f"/api/monza/leads/{lead_id}")
        check("4a el detalle del lead responde 200", r.status_code == 200, r.text[:200])
        data = r.json()
        item_api = next((x for x in data.get("items", []) if x["id"] == item_id), None)
        check("4b el ítem viaja con su costo original", item_api and item_api.get("costo") == 100.0,
              item_api)
        check("4c y con su moneda USD", item_api and item_api.get("moneda") == "USD", item_api)
        check("4d SONDA: el costo NO es el precio final (que es el bug reportado)",
              item_api and item_api.get("costo") != item_api.get("precio_clp"),
              (item_api or {}).get("costo"))
        check("4e el markup viaja para reconstruir la pantalla",
              item_api and item_api.get("markup_pct") == 28.0, item_api)
        check("4f el lead declara el flete elegido", data.get("moneda_tarifa") == "USD", data)

        # ── 5) Cero es un valor LEGÍTIMO, no "sin dato" ─────────────────────────────
        r = client.post("/api/monza/cotizador/aplicar", json={
            "lead_id": lead_id,
            "items": [{"item_id": item_id, "calidad": "Genuine", "precio_clp": 0,
                       "costo": 0, "moneda": "CLP", "peso_kg": 0, "markup_pct": 0}],
        })
        check("5a aplicar con ceros responde 200", r.status_code == 200, r.text[:200])
        it = _item(item_id)
        check("5b costo 0 se GUARDA (con un truthy check se perdería)", it.costo == 0.0, it.costo)
        check("5c markup 0 se GUARDA", it.markup_pct == 0.0, it.markup_pct)
        check("5d la moneda cambió a CLP", (it.moneda or "") == "CLP", it.moneda)

        # ── 6) Sin flete en el body, NO se pisa lo que el operador ya eligió ────────
        ld = _lead(lead_id)
        check("6 el flete del lead sobrevive a un aplicar que no lo menciona",
              (ld.moneda_tarifa or "") == "USD" and ld.tarifa_aerea == 5.0,
              (ld.moneda_tarifa, ld.tarifa_aerea))

        # ── 7) Sin elección, manda la configuración global (comportamiento anterior) ──
        lead2_id, item2_id = _seed()
        r = client.post("/api/monza/cotizador/calcular", json={
            "lead_id": lead2_id,
            "items": [{"item_id": item2_id, "calidades": [{
                "calidad": "Genuine", "costo": 100.0, "moneda": "USD",
                "peso_kg": 2.0, "markup_pct": 0.0}]}],
        })
        cal2 = r.json()["items"][0]["calidades"][0]
        check("7a sin elegir flete se usa el global (EUR): 2 × 5 × 1000 = 10.000",
              cal2["flete_clp"] == 10000, cal2["flete_clp"])
        check("7b y la respuesta lo declara EUR",
              r.json()["config"]["moneda_tarifa"] == "EUR", r.json()["config"])

    finally:
        _limpiar()
        _restaurar_config(previo)
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_cotizador_parametros_monza():
    run()


if __name__ == "__main__":
    run()
