"""La estampa de FLETE por ítem del lead (arreglo 4 del equipo, 2026-08-21).

EL PROBLEMA QUE CIERRA
    La calculadora ahora acepta subconjuntos ("los alemanes en EUR, los americanos en
    USD"), pero el flete vivía SOLO a nivel lead y cada corrida PISABA la anterior: al
    reabrir, la siembra usaba el flete equivocado para las filas de la otra corrida, y la
    cotización emitida congelaba en TODOS los ítems una tarifa que no produjo sus precios.
    Ahora cada ítem guarda el flete de SU corrida (monza_lead_items.moneda_tarifa/
    tarifa_aerea, contrato en MonzaLeadItem) y la emisión lo lee por lead_item_id.

SONDAS DE PODER DISCRIMINANTE
    · §1 usa DOS corridas con moneda y tarifa distintas: si la estampa se resolviera a
      nivel lead, el ítem de la primera corrida quedaría con el flete de la segunda y
      el check cae.
    · §3 re-aplica un subconjunto que INCLUYE una fila preexistente SIN cambios (lo que
      handleAplicar manda siempre): con estampado incondicional, esa fila ganaría el
      flete de la corrida nueva — foto falsa — y el check cae.
    · §5 crea la cotización tras corridas mixtas: cada MonzaCotizacionItem debe congelar
      el flete de SU corrida (no el de cabecera), y la cabecera pinea la regla
      "subconjunto uniforme → su par; mixto → el del lead".
    · §6 recorre el camino nuevo de las calidades extra: ítem pelado + /aplicar en la
      misma corrida → nace CON precio y CON estampa (antes ItemIn botaba el precio).

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_lead_item_flete_estampa.py -q
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
    MonzaCliente, MonzaConfig, MonzaCotizacion, MonzaCotizacionItem,
    MonzaLead, MonzaLeadActividad, MonzaLeadItem, MonzaLog,
)
from monza_router_cotizador import router as cotizador_router  # noqa: E402
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402
from monza_router_leads import router as leads_router  # noqa: E402

MARK = "test-mzflete"
LEAD_MARK = "L-MZFL"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(cotizador_router)
app.include_router(leads_router)
app.include_router(cotizaciones_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _config():
    """TC y tarifas CONOCIDOS (mismos números que test_cotizador_parametros)."""
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
        cfg.tc_eur_clp = 1000.0
        cfg.tarifa_aerea_por_kg = 5.0
        cfg.moneda_tarifa = "EUR"
        cfg.tarifa_aerea_eur_por_kg = 4.5
        cfg.tarifa_aerea_usd_por_kg = 7.0
        cfg.iva_pct = 19.0
        db.commit()
        return previo
    finally:
        db.close()


def _restaurar_config(previo):
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
                f"monza_config quedó con los valores del test (esperado {tuple(previo)}, "
                f"quedó {actual}): restáurala antes de cotizar")
    finally:
        db.close()


def _seed():
    """Lead con 2 ítems sin precio (el alemán y el americano)."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA")
        db.add(cli)
        db.flush()
        lead = MonzaLead(numero=f"{LEAD_MARK}-{uuid.uuid4().hex[:6].upper()}",
                         cliente_id=cli.id, estado="pendiente")
        db.add(lead)
        db.flush()
        it_de = MonzaLeadItem(lead_id=lead.id, descripcion="Bomba de agua (Alemania)", cantidad=1)
        it_us = MonzaLeadItem(lead_id=lead.id, descripcion="Alternador (USA)", cantidad=1)
        db.add_all([it_de, it_us])
        db.commit()
        return lead.id, it_de.id, it_us.id
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


def _aplicar(lead_id, items, moneda=None, tarifa=None):
    body = {"lead_id": lead_id, "items": items}
    if moneda is not None:
        body["moneda_tarifa"] = moneda
    if tarifa is not None:
        body["tarifa_aerea"] = tarifa
    return client.post("/api/monza/cotizador/aplicar", json=body)


def _fila(item_id, precio, costo, moneda, peso, markup):
    """Una fila del payload de /aplicar, como la arma handleAplicar."""
    return {"item_id": item_id, "calidad": "genuine", "precio_clp": precio,
            "costo": costo, "moneda": moneda, "peso_kg": peso, "markup_pct": markup}


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        lead_ids = [r[0] for r in db.query(MonzaLead.id)
                    .filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).all()]
        cli_ids = [r[0] for r in db.query(MonzaCliente.id)
                   .filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .filter(MonzaCotizacion.cliente_id.in_(cli_ids or [0])).all()]
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLeadActividad).filter(
            MonzaLeadActividad.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLeadItem).filter(
            MonzaLeadItem.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLead).filter(MonzaLead.id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.id.in_(cli_ids or [0])).delete(
            synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
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
        lead_id, it_de, it_us = _seed()

        # ── 1) DOS corridas con flete distinto: cada ítem conserva SU estampa ─────────
        # Corrida A (alemán): EUR 4.5 · costo 100 EUR × 1000 + 2 kg × 4.5 × 1000 = 109.000
        r = _aplicar(lead_id, [_fila(it_de, 109000, 100.0, "EUR", 2.0, 0.0)],
                     moneda="EUR", tarifa=4.5)
        check("1a corrida EUR responde 200", r.status_code == 200, r.text[:200])
        # Corrida B (americano): USD 7.0 · costo 100 USD × 900 + 2 kg × 7 × 900 = 102.600
        r = _aplicar(lead_id, [_fila(it_us, 102600, 100.0, "USD", 2.0, 0.0)],
                     moneda="USD", tarifa=7.0)
        check("1b corrida USD responde 200", r.status_code == 200, r.text[:200])
        a, b = _item(it_de), _item(it_us)
        check("1c SONDA: el ítem alemán conserva la estampa EUR de SU corrida",
              (a.moneda_tarifa, a.tarifa_aerea) == ("EUR", 4.5),
              (a.moneda_tarifa, a.tarifa_aerea, "a nivel lead quedaría USD/7.0"))
        check("1d el ítem americano estampa USD de la suya",
              (b.moneda_tarifa, b.tarifa_aerea) == ("USD", 7.0),
              (b.moneda_tarifa, b.tarifa_aerea))
        ld = _lead(lead_id)
        check("1e el LEAD retrata la ÚLTIMA corrida (comportamiento de siempre)",
              (ld.moneda_tarifa, ld.tarifa_aerea) == ("USD", 7.0),
              (ld.moneda_tarifa, ld.tarifa_aerea))

        # ── 2) _item_dict expone las estampas (la siembra del modal vive de esto) ─────
        r = client.get(f"/api/monza/leads/{lead_id}")
        check("2a detalle responde 200", r.status_code == 200, r.text[:200])
        api = {x["id"]: x for x in r.json().get("items", [])}
        check("2b el alemán viaja con moneda_tarifa/tarifa_aerea EUR/4.5",
              (api.get(it_de, {}).get("moneda_tarifa"),
               api.get(it_de, {}).get("tarifa_aerea")) == ("EUR", 4.5), api.get(it_de))
        check("2c el americano con USD/7.0",
              (api.get(it_us, {}).get("moneda_tarifa"),
               api.get(it_us, {}).get("tarifa_aerea")) == ("USD", 7.0), api.get(it_us))

        # ── 3) Estampa CONDICIONAL: la fila preexistente sin cambios NO se re-estampa ──
        # handleAplicar manda TODAS las filas seleccionadas. Se re-aplica la corrida USD
        # incluyendo al alemán TAL CUAL está guardado (sin recalcular): su estampa EUR
        # tiene que sobrevivir. Con estampado incondicional quedaría USD — foto falsa.
        r = _aplicar(lead_id, [
            _fila(it_de, 109000, 100.0, "EUR", 2.0, 0.0),   # sin cambios → NO se estampa
            _fila(it_us, 105000, 100.0, "USD", 2.0, 1.0),   # markup cambió → SÍ
        ], moneda="USD", tarifa=7.0)
        check("3a re-aplicar responde 200", r.status_code == 200, r.text[:200])
        a, b = _item(it_de), _item(it_us)
        check("3b SONDA: la fila preexistente sin cambios CONSERVA su estampa EUR/4.5",
              (a.moneda_tarifa, a.tarifa_aerea) == ("EUR", 4.5),
              (a.moneda_tarifa, a.tarifa_aerea, "estampado incondicional la pisaría"))
        check("3c la fila recalculada sí gana la estampa de la corrida",
              (b.moneda_tarifa, b.tarifa_aerea) == ("USD", 7.0),
              (b.moneda_tarifa, b.tarifa_aerea))
        check("3d y su precio nuevo quedó guardado", b.precio_clp == 105000, b.precio_clp)

        # ── 3-bis) SONDA de la TOLERANCIA FLOAT (calibración de _fila_recalculada) ────
        # 123.45 NO es representable exacto en el FLOAT de MySQL: se guarda como
        # ~123.44999694824. Si la tolerancia relativa se reemplazara por igualdad
        # estricta, re-enviar la fila TAL CUAL se marcaría «recalculada» por el
        # round-trip y la estampa se pisaría — la foto falsa que A1 cierra. Con la
        # tolerancia, el re-envío idéntico NO re-estampa.
        r = client.post(f"/api/monza/leads/{lead_id}/items", json={
            "descripcion": "Sonda tolerancia FLOAT", "calidad": "genuine", "cantidad": 1})
        check("3e el ítem de la sonda se crea", r.status_code == 201, r.text[:200])
        tol_id = r.json()["id"]
        fila_tol = _fila(tol_id, 130000, 123.45, "EUR", 2.0, 3.5)
        r = _aplicar(lead_id, [fila_tol], moneda="EUR", tarifa=4.5)
        check("3f la corrida EUR lo estampa", r.status_code == 200
              and (_item(tol_id).moneda_tarifa, _item(tol_id).tarifa_aerea) == ("EUR", 4.5),
              (_item(tol_id).moneda_tarifa, _item(tol_id).tarifa_aerea))
        # Re-envío BYTE A BYTE de la misma fila dentro de una corrida USD: sin cambios
        # reales, la estampa EUR debe sobrevivir aunque el costo guardado difiera del
        # enviado en ~2.5e-8 relativo por el round-trip.
        r = _aplicar(lead_id, [fila_tol], moneda="USD", tarifa=7.0)
        check("3g SONDA: el round-trip FLOAT no cuenta como recálculo (estampa intacta)",
              r.status_code == 200
              and (_item(tol_id).moneda_tarifa, _item(tol_id).tarifa_aerea) == ("EUR", 4.5),
              (_item(tol_id).moneda_tarifa, _item(tol_id).tarifa_aerea,
               "con igualdad estricta quedaría USD/7.0"))

        # ── 4) /aplicar SIN flete en el body estampa el flete EFECTIVO del lead ────────
        r = _aplicar(lead_id, [_fila(it_us, 106000, 100.0, "USD", 2.0, 2.0)])
        check("4a aplicar sin flete responde 200", r.status_code == 200, r.text[:200])
        b = _item(it_us)
        check("4b la estampa sale del flete efectivo (el del lead: USD/7.0)",
              (b.moneda_tarifa, b.tarifa_aerea) == ("USD", 7.0),
              (b.moneda_tarifa, b.tarifa_aerea))

        # ── 5) La cotización EMITIDA congela el flete POR ÍTEM (vía lead_item_id) ─────
        def _cot_item(desc, item_id, precio):
            return {"descripcion": desc, "calidad": "genuine", "cantidad": 1,
                    "precio_unitario_clp": precio, "subtotal_clp": precio,
                    "lead_item_id": item_id}

        cli_id = _lead(lead_id).cliente_id
        r = client.post("/api/monza/cotizaciones", json={
            "lead_id": lead_id, "cliente_id": cli_id,
            "items": [_cot_item("Bomba de agua (Alemania)", it_de, 109000),
                      _cot_item("Alternador (USA)", it_us, 106000)],
        })
        check("5a emitir con corridas MIXTAS responde 201", r.status_code == 201, r.text[:300])
        cot_id = r.json()["id"]
        db = SessionLocal()
        try:
            cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
            its = {i.descripcion: i for i in cot.items}
            check("5b SONDA: la línea alemana congela la tarifa de SU corrida (4.5)",
                  its["Bomba de agua (Alemania)"].tarifa_aerea == 4.5,
                  (its["Bomba de agua (Alemania)"].tarifa_aerea, "con cabecera sería 7.0"))
            check("5c la línea americana congela 7.0",
                  its["Alternador (USA)"].tarifa_aerea == 7.0,
                  its["Alternador (USA)"].tarifa_aerea)
            # La tarifa viaja CON su moneda (hallazgo MED1 de la ronda escéptica):
            # sin la columna, "4.5" en una cotización cuya cabecera dice USD era ambiguo.
            check("5c-bis cada línea guarda TAMBIÉN la moneda de su tarifa (EUR / USD)",
                  (its["Bomba de agua (Alemania)"].moneda_tarifa,
                   its["Alternador (USA)"].moneda_tarifa) == ("EUR", "USD"),
                  (its["Bomba de agua (Alemania)"].moneda_tarifa,
                   its["Alternador (USA)"].moneda_tarifa))
            check("5d PIN: con mezcla, la CABECERA retrata el flete del LEAD (última corrida)",
                  (cot.moneda_tarifa, cot.tarifa_aerea) == ("USD", 7.0),
                  (cot.moneda_tarifa, cot.tarifa_aerea))
        finally:
            db.close()

        # Subconjunto UNIFORME (solo el alemán): la cabecera retrata SU par, no el del lead.
        r = client.post("/api/monza/cotizaciones", json={
            "lead_id": lead_id, "cliente_id": cli_id,
            "items": [_cot_item("Bomba de agua (Alemania)", it_de, 109000)],
        })
        check("5e emitir subconjunto uniforme responde 201", r.status_code == 201, r.text[:300])
        db = SessionLocal()
        try:
            cot2 = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == r.json()["id"]).first()
            check("5f SONDA: cabecera del subconjunto uniforme = EUR/4.5 (no la del lead USD/7.0)",
                  (cot2.moneda_tarifa, cot2.tarifa_aerea) == ("EUR", 4.5),
                  (cot2.moneda_tarifa, cot2.tarifa_aerea))
        finally:
            db.close()

        # Fail-closed: un lead_item_id AJENO se ignora y la línea usa la cabecera.
        lead2_id, it2_de, _ = _seed()
        r = client.post("/api/monza/cotizaciones", json={
            "lead_id": lead2_id, "cliente_id": _lead(lead2_id).cliente_id,
            "items": [{"descripcion": "Intruso", "calidad": "genuine", "cantidad": 1,
                       "precio_unitario_clp": 1000, "subtotal_clp": 1000,
                       "lead_item_id": it_de}],   # ítem de OTRO lead
        })
        check("5g emitir con lead_item_id ajeno responde 201 (se ignora, no 500)",
              r.status_code == 201, r.text[:300])
        db = SessionLocal()
        try:
            cot3 = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == r.json()["id"]).first()
            check("5h la línea intrusa NO hereda la estampa ajena: usa la cabecera",
                  cot3.items[0].tarifa_aerea == cot3.tarifa_aerea,
                  (cot3.items[0].tarifa_aerea, cot3.tarifa_aerea))
            check("5i y su moneda también es la de cabecera",
                  cot3.items[0].moneda_tarifa == cot3.moneda_tarifa,
                  (cot3.items[0].moneda_tarifa, cot3.moneda_tarifa))
        finally:
            db.close()

        # ── 6) Camino nuevo de las calidades EXTRA: ítem pelado + /aplicar juntos ──────
        # (Antes handleAplicar mandaba precio_clp por addItem y Pydantic lo botaba: el
        # ítem nacía Sin precio. Ahora nace pelado y entra al MISMO /aplicar.)
        r = client.post(f"/api/monza/leads/{lead_id}/items", json={
            "descripcion": "Bomba de agua (calidad OEM)", "calidad": "oem", "cantidad": 1})
        check("6a el ítem extra pelado se crea", r.status_code == 201, r.text[:200])
        extra_id = r.json()["id"]
        r = _aplicar(lead_id, [_fila(extra_id, 80000, 72.0, "EUR", 2.0, 0.0)],
                     moneda="EUR", tarifa=4.5)
        check("6b aplicar sobre el extra responde 200", r.status_code == 200, r.text[:200])
        ex = _item(extra_id)
        check("6c SONDA: el extra nace CON precio (antes quedaba Sin precio)",
              ex.precio_clp == 80000, ex.precio_clp)
        check("6d con sus parámetros (costo 72 EUR)", (ex.costo, ex.moneda) == (72.0, "EUR"),
              (ex.costo, ex.moneda))
        check("6e y CON estampa de su corrida",
              (ex.moneda_tarifa, ex.tarifa_aerea) == ("EUR", 4.5),
              (ex.moneda_tarifa, ex.tarifa_aerea))

    finally:
        _limpiar()
        _restaurar_config(previo)
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_lead_item_flete_estampa():
    run()


if __name__ == "__main__":
    run()
