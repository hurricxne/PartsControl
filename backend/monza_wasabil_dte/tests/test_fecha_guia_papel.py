"""La factura cita la guía EN PAPEL con la fecha en que esa guía se EMITIÓ (MonzaParts).

Espejo de wasabil_dte/tests/test_fecha_guia_papel.py (MachParts). Suites SEPARADAS a
propósito: cada marca tiene su propio módulo SII sobre sus propias tablas, y esta suite
además VERIFICA que no se crucen (sección 7).

EL BUG QUE CIERRA
    Con la guía emitida fuera del sistema (portal del SII / papel) y sólo su número
    registrado, la referencia 52 del DTE 33 sacaba la fecha de `fecha_despacho` — el
    instante en que se cerró el despacho, no la fecha de la guía. Ahora sale de
    `monza_despachos.fecha_guia`, y si falta se BLOQUEA en vez de sustituirla.

SONDAS DE PODER DISCRIMINANTE
    Las fechas del escenario son DISTINTAS a propósito (guía emitida el 2026-07-15, cierre
    del despacho el 2026-06-20 que pone el harness): con el código viejo la sección 1
    falla. La sección 2 exige el bloqueo, que antes no existía.

Sin red y sin emitir. Datos con MARK propio y limpieza al final.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_fecha_guia_papel.py -q
"""
import inspect
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from auth import get_current_user  # noqa: E402
from database import SessionLocal  # noqa: E402
import monza_router_despachos as monza_despachos_router  # noqa: E402
from monza_models import MonzaDespacho  # noqa: E402
from monza_wasabil_dte.models import MonzaWasabilDte, STATUS_EMITIDO  # noqa: E402
from monza_wasabil_dte import router as mz_router  # noqa: E402
from monza_wasabil_dte.router import (  # noqa: E402
    _fecha_guia_papel, _referencia_guia_de_despacho,
)
from monza_wasabil_dte.service import armar_referencias_factura, hoy_chile  # noqa: E402
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    Checker, FakeWasabil, crear_venta, limpiar,
)
from monza_router_despachos import _parse_fecha_guia  # noqa: E402

MARK = "__MZFG__"
CURRENT = {"empresa": "automotriz", "id": None}

fake = FakeWasabil(MARK)
check = Checker()

# App propia: `montar_app` del harness monta Contabilidad + Wasabil de Monza, no el router
# de Despachos, que es donde vive el PUT que carga la fecha (sección 6). El usuario se
# fuerza a 'automotriz': el router entero está candado con require_empresa("automotriz").
_app = FastAPI()
_app.include_router(monza_despachos_router.router)
_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"], email="test-mzfg@monza.cl")
client = TestClient(_app)

# La guía se emitió el 15; el harness cierra el despacho el 2026-06-20. La diferencia ES
# la prueba: con el código viejo la referencia salía con la fecha del cierre.
FECHA_GUIA_REAL = date(2026, 7, 15)
FECHA_CIERRE_HARNESS = date(2026, 6, 20)


def run():
    db = SessionLocal()
    fake.install()   # re-instalar el fake propio (la última instalación gana: anti-flaky)
    limpiar(db, MARK)
    try:
        CURRENT["empresa"] = "automotriz"
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="900123")
        desp.fecha_guia = FECHA_GUIA_REAL
        db.commit()

        # ── 1) La referencia lleva la fecha de EMISIÓN, no la del cierre ─────────────
        folio, fecha, problema = _referencia_guia_de_despacho(db, desp.id)
        check("1a guía en papel: la referencia 52 usa la fecha de EMISIÓN de la guía",
              fecha == FECHA_GUIA_REAL, (fecha, "esperado", FECHA_GUIA_REAL))
        check("1b sonda: NO usa la fecha de cierre del despacho (el bug viejo)",
              fecha != FECHA_CIERRE_HARNESS, fecha)
        check("1c el folio del papel se sigue citando y no hay problema",
              folio == "900123" and problema is None, (folio, problema))

        refs, problemas_ref = armar_referencias_factura(
            numero_oc="OC-4501", fecha_oc=date(2026, 6, 10),
            guia_folio=folio, guia_fecha=fecha)
        ref52 = next((r for r in refs if str(r.get("documentType")) == "52"), None)
        check("1d el DTE 33 sale con date = fecha de emisión de la guía",
              ref52 is not None and ref52.get("date") == FECHA_GUIA_REAL.isoformat(),
              (ref52, problemas_ref))

        # ── 2) Sin fecha de emisión: se BLOQUEA ──────────────────────────────────────
        desp.fecha_guia = None
        db.commit()
        folio2, fecha2, problema2 = _referencia_guia_de_despacho(db, desp.id)
        check("2a guía en papel SIN fecha: reporta problema bloqueante",
              bool(problema2), problema2)
        check("2b no devuelve folio ni fecha (nada que emitir a medias)",
              folio2 is None and fecha2 is None, (folio2, fecha2))
        check("2c el mensaje dice QUÉ falta y DÓNDE cargarlo",
              problema2 and "FECHA DE EMISIÓN" in problema2 and "Despachos" in problema2,
              problema2)
        check("2d el mensaje nombra la guía concreta",
              problema2 and "900123" in problema2, problema2)

        # ── 3) Guía ELECTRÓNICA: manda el DTE, no esta columna ───────────────────────
        # fecha_guia queda con un valor RUIDOSO: si el camino electrónico la leyera por
        # error, la fecha saldría 2020-01-01 y el test cae.
        desp.fecha_guia = date(2020, 1, 1)
        db.commit()
        dte = MonzaWasabilDte(tipo_dte=52, despacho_id=desp.id, status_id=STATUS_EMITIDO,
                              uuid="uuid-emitida-mz", folio="551",
                              payload_json='{"documentDate": "2026-07-18"}')
        db.add(dte); db.commit()
        folio3, fecha3, problema3 = _referencia_guia_de_despacho(db, desp.id)
        check("3a guía electrónica: la fecha sale del documentDate del DTE 52",
              fecha3 == date(2026, 7, 18), fecha3)
        check("3b guía electrónica: ignora despacho.fecha_guia",
              fecha3 != date(2020, 1, 1), fecha3)
        check("3c guía electrónica: cita el folio del SII y no reporta problema",
              folio3 == "551" and problema3 is None, (folio3, problema3))
        db.delete(dte); db.commit()

        # ── 4) Sonda ANTI-DERIVA del resolver ────────────────────────────────────────
        src = inspect.getsource(_referencia_guia_de_despacho)
        check("4a el resolver usa el helper único", "_fecha_guia_papel(" in src)
        codigo = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        check("4b ya no lee fecha_despacho para la referencia 52",
              "fecha_despacho" not in codigo,
              [l for l in codigo.splitlines() if "fecha_despacho" in l])

        # ── 5) Validación de lo que teclea el operador (PUT de cabecera) ─────────────
        check("5a acepta AAAA-MM-DD", _parse_fecha_guia("2026-07-15") == date(2026, 7, 15))
        check("5b vacío = borrar la fecha (estado legítimo)",
              _parse_fecha_guia("") is None and _parse_fecha_guia(None) is None)
        for malo in ("15-07-2026", "hoy", "2026-13-40", "2026-07-155", "2026-02-31"):
            try:
                _parse_fecha_guia(malo)
                check(f"5c rechaza formato inválido '{malo}'", False, "no lanzó")
            except HTTPException as e:
                check(f"5c rechaza formato inválido '{malo}'", e.status_code == 400, e.detail)
        try:
            _parse_fecha_guia((hoy_chile() + timedelta(days=1)).isoformat())
            check("5d rechaza fecha futura", False, "no lanzó")
        except HTTPException as e:
            check("5d rechaza fecha futura", e.status_code == 400, e.detail)
        try:
            _parse_fecha_guia((hoy_chile() - timedelta(days=3000)).isoformat())
            check("5e rechaza un año absurdo (tipeo)", False, "no lanzó")
        except HTTPException as e:
            check("5e rechaza un año absurdo (tipeo)", e.status_code == 400, e.detail)

        # ── 6) El PUT de cabecera guarda de verdad (texto → columna DATE) ────────────
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"fecha_guia": "2026-07-15"})
        check("6a PUT acepta la fecha", r.status_code == 200, (r.status_code, r.text[:200]))
        db.rollback()
        fresco = db.query(MonzaDespacho).filter(MonzaDespacho.id == desp.id).first()
        check("6b la fecha queda persistida como DATE, no como texto",
              fresco.fecha_guia == date(2026, 7, 15), fresco.fecha_guia)
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"fecha_guia": "15-07-2026"})
        check("6c PUT rechaza formato inválido con 400", r.status_code == 400, r.status_code)
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}", json={"fecha_guia": None})
        db.rollback()
        fresco = db.query(MonzaDespacho).filter(MonzaDespacho.id == desp.id).first()
        check("6d PUT con null borra la fecha",
              r.status_code == 200 and fresco.fecha_guia is None,
              (r.status_code, fresco.fecha_guia))
        # Tri-estado: un PUT que NO menciona fecha_guia no puede borrarla.
        fresco.fecha_guia = FECHA_GUIA_REAL
        db.commit()
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"transportista": "Starken"})
        db.rollback()
        fresco = db.query(MonzaDespacho).filter(MonzaDespacho.id == desp.id).first()
        check("6e un PUT que no menciona la fecha NO la pisa",
              fresco.fecha_guia == FECHA_GUIA_REAL, fresco.fecha_guia)

        # ── 7) Sin cruce entre marcas ────────────────────────────────────────────────
        from wasabil_dte import router as ga_router
        check("7a cada marca tiene su propio helper (módulos distintos)",
              mz_router._fecha_guia_papel.__module__ != ga_router._fecha_guia_papel.__module__,
              (mz_router._fecha_guia_papel.__module__, ga_router._fecha_guia_papel.__module__))
        check("7b el resolver de Monza consulta SOLO monza_despachos",
              "MonzaDespacho" in src and "models.models" not in src)
        f_ok, p_ok = _fecha_guia_papel(SimpleNamespace(fecha_guia=FECHA_GUIA_REAL,
                                                       numero_guia="900123"))
        check("7c helper Monza: con fecha devuelve la fecha y sin problema",
              f_ok == FECHA_GUIA_REAL and p_ok is None, (f_ok, p_ok))

        # ── 8) La referencia 52 no puede fecharse DESPUÉS de la factura ──────────────
        # Lo encontró el enjambre adversarial: `fecha_emision` de la factura es un campo
        # LIBRE sin tope, así que backdatando la factura se llegaba a un DTE 33 REAL que
        # declara haberse emitido ANTES que la guía que dice amparar. Ninguna de las dos
        # fechas es inválida por sí sola; lo inválido es el orden, y nadie las cruzaba.
        refs_ok, prob_ok = armar_referencias_factura(
            numero_oc="OC-4501", fecha_oc=date(2026, 7, 1), guia_folio="900123",
            guia_fecha=date(2026, 7, 15), fecha_documento=date(2026, 7, 20))
        check("8a guía ANTERIOR a la factura: se emite normal",
              not prob_ok and any(str(r.get("documentType")) == "52" for r in refs_ok),
              (refs_ok, prob_ok))
        refs_mismo, prob_mismo = armar_referencias_factura(
            numero_oc="OC-4501", fecha_oc=date(2026, 7, 1), guia_folio="900123",
            guia_fecha=date(2026, 7, 20), fecha_documento=date(2026, 7, 20))
        check("8b guía del MISMO día que la factura: válido (el borde no bloquea)",
              not prob_mismo, prob_mismo)
        refs_mal, prob_mal = armar_referencias_factura(
            numero_oc="OC-4501", fecha_oc=date(2026, 7, 1), guia_folio="900123",
            guia_fecha=date(2026, 7, 25), fecha_documento=date(2026, 7, 20))
        check("8c guía POSTERIOR a la factura: bloquea", bool(prob_mal), prob_mal)
        check("8d y la referencia 52 NO se arma (no sale a medias)",
              not any(str(r.get("documentType")) == "52" for r in refs_mal), refs_mal)
        check("8e el mensaje nombra las DOS fechas, para saber cuál corregir",
              prob_mal and "2026-07-25" in prob_mal[0] and "2026-07-20" in prob_mal[0],
              prob_mal)
        # Control: sin fecha_documento el cruce NO corre (llamadores viejos intactos).
        _r, prob_sin = armar_referencias_factura(
            numero_oc="OC-4501", fecha_oc=date(2026, 7, 1), guia_folio="900123",
            guia_fecha=date(2026, 7, 25))
        check("8f sin fecha_documento el control no corre (compatibilidad)",
              not prob_sin, prob_sin)
    finally:
        limpiar(db, MARK)
        db.close()

    check.finish()


def test_factura_monza_cita_la_guia_de_papel_con_su_fecha_de_emision(): run()


if __name__ == "__main__":
    run()
    print("TODO OK")
