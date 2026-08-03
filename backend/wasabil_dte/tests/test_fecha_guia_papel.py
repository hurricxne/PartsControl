"""La factura cita la guía EN PAPEL con la fecha en que esa guía se EMITIÓ (MachParts).

EL BUG QUE CIERRA
    Cuando la guía de despacho se emite fuera de PartsControl (portal del SII o talonario)
    y en el sistema sólo se registra su NÚMERO, la referencia tipo 52 del DTE 33 sacaba su
    fecha de `despacho.fecha_despacho` — que NO es la fecha de la guía, sino el instante en
    que se cerró el despacho, puesto por el reloj del servidor. Emitir la guía un día y
    cerrar el despacho otro (lo habitual) mandaba al SII una factura que referencia la guía
    con una fecha que la guía no tiene. Un DTE emitido no se corrige.

    Ahora la fecha sale de `despacho.fecha_guia`, que teclea el operador, y si falta se
    BLOQUEA en vez de sustituirla por una parecida.

SONDAS DE PODER DISCRIMINANTE
    Cada caso está armado para FALLAR si se revierte el arreglo:
      · Sección 1 pone fecha_guia y fecha_despacho DISTINTAS a propósito: con el código
        viejo la referencia sale con la del cierre y el test cae.
      · Sección 2 exige el bloqueo: con el código viejo no había problema que reportar.
      · Sección 4 es la sonda ANTI-DERIVA sobre los dos gemelos del router (ver allí).

SIN RED Y SIN EMITIR: sólo funciones del router contra la BD local, con `buscar_documentos`
y `obtener_documento` SIMULADOS a nivel de corrida. Datos MARCADOS y limpieza al final.

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_fecha_guia_papel.py -q
"""
import inspect
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from auth import get_current_user  # noqa: E402
from database import SessionLocal  # noqa: E402
from models.models import Cotizacion, OcCliente, Despacho  # noqa: E402
import routers.despachos as despachos_router  # noqa: E402
from wasabil_dte.models import WasabilDte, STATUS_EMITIDO  # noqa: E402
from wasabil_dte import client as wasabil_client  # noqa: E402
from wasabil_dte import router as ga_router  # noqa: E402
from wasabil_dte.router import (  # noqa: E402
    _fecha_guia_papel, _guia_referencia_de_factura, _preparar_emision_factura,
)
from wasabil_dte.service import armar_referencias_factura, hoy_chile  # noqa: E402
from routers.despachos import _parse_fecha_guia  # noqa: E402

MARK = "__TEST_FECHAGUIA__"
_fails: list = []

# App mínima con SOLO el router de despachos de MachParts, para probar el PUT de verdad
# (sección 8). El usuario se fuerza a 'mineria': el router entero está candado con
# require_empresa("mineria") y con otra empresa ni siquiera entra al endpoint.
_app = FastAPI()
_app.include_router(despachos_router.router)
_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=None, empresa="mineria")
_client = TestClient(_app)

# Fechas del escenario: la guía se emitió el 15 y el despacho se cerró en el sistema el 20.
# La diferencia ES la prueba — con el código viejo la referencia salía con el 20.
FECHA_GUIA_REAL = date(2026, 7, 15)
FECHA_CIERRE_DESPACHO = datetime(2026, 7, 20, 9, 30)


def _buscar_fake(search):
    """Wasabil no tiene NADA con esa referencia: deja el camino de la guía en papel libre
    (si no, `_problema_52_de_papel` bloquearía por otro motivo y taparía lo que se prueba)."""
    return [], True


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


def _limpiar(db):
    db.rollback()
    db.execute(text("DELETE FROM wasabil_dte WHERE despacho_id IN "
                    "(SELECT id FROM despachos WHERE numero_despacho LIKE :m)"), {"m": f"{MARK}%"})
    db.execute(text("DELETE FROM despachos WHERE numero_despacho LIKE :m"), {"m": f"{MARK}%"})
    db.execute(text("DELETE FROM oc_cliente WHERE cotizacion_id IN "
                    "(SELECT id FROM cotizaciones WHERE numero LIKE :m)"), {"m": f"{MARK}%"})
    db.execute(text("DELETE FROM cotizaciones WHERE numero LIKE :m"), {"m": f"{MARK}%"})
    db.commit()


def run():
    db = SessionLocal()
    _limpiar(db)  # por si una corrida anterior murió a medias
    # El simulacro se instala DENTRO de run() y se restaura al salir: instalarlo al importar
    # se filtraba a las demás suites de la misma corrida de pytest.
    _orig_buscar = wasabil_client.buscar_documentos
    _orig_obtener = wasabil_client.obtener_documento
    wasabil_client.buscar_documentos = _buscar_fake
    wasabil_client.obtener_documento = lambda uuid: {"uuid": uuid}
    try:
        cot = Cotizacion(numero=f"{MARK}-C", cliente=f"{MARK}", rut_cliente="78.279.030-7")
        db.add(cot); db.flush()
        oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK[-6:]}", fecha_oc="2026-07-01")
        db.add(oc); db.flush()
        desp = Despacho(numero_despacho=f"{MARK}-D", oc_cliente_id=oc.id, estado="despachado",
                        guia_firmada=1, numero_guia="900123",
                        fecha_guia=FECHA_GUIA_REAL,
                        fecha_despacho=FECHA_CIERRE_DESPACHO)
        db.add(desp); db.flush(); db.commit()
        fac = SimpleNamespace(despacho_id=desp.id, despacho=desp, es_anticipo=0)

        # ── 1) La referencia lleva la fecha de EMISIÓN, no la del cierre ──────────────
        folio, fecha, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("1a guía en papel: la referencia 52 usa la fecha de EMISIÓN de la guía",
              fecha == FECHA_GUIA_REAL, (fecha, "esperado", FECHA_GUIA_REAL))
        check("1b sonda: NO usa la fecha de cierre del despacho (el bug viejo)",
              fecha != FECHA_CIERRE_DESPACHO.date(), fecha)
        check("1c el folio del papel se sigue citando y no hay problema",
              folio == "900123" and problema is None, (folio, problema))

        # La fecha tiene que llegar VIVA al documento, no quedarse en el resolver.
        refs, problemas_ref = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 7, 1), guia_folio=folio, guia_fecha=fecha)
        ref52 = next((r for r in refs if str(r.get("documentType")) == "52"), None)
        check("1d el DTE 33 sale con date = fecha de emisión de la guía",
              ref52 is not None and ref52.get("date") == FECHA_GUIA_REAL.isoformat(),
              (ref52, problemas_ref))

        # ── 2) Sin fecha de emisión: se BLOQUEA (no se inventa una parecida) ──────────
        desp.fecha_guia = None
        db.commit()
        folio2, fecha2, problema2, _aud2 = _guia_referencia_de_factura(db, fac)
        check("2a guía en papel SIN fecha: reporta problema bloqueante",
              bool(problema2), problema2)
        check("2b no devuelve folio ni fecha (nada que emitir a medias)",
              folio2 is None and fecha2 is None, (folio2, fecha2))
        check("2c el mensaje dice QUÉ falta y DÓNDE cargarlo",
              problema2 and "FECHA DE EMISIÓN" in problema2 and "Despachos" in problema2,
              problema2)
        check("2d el mensaje nombra la guía concreta",
              problema2 and "900123" in problema2, problema2)
        # Control del riesgo real: sin bloqueo, la 52 salía SIN fecha y el documento se
        # emitía igual. Esto documenta por qué el guard tiene que estar ANTES.
        refs_sin, _p = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 7, 1), guia_folio="900123", guia_fecha=None)
        ref52_sin = next((r for r in refs_sin if str(r.get("documentType")) == "52"), None)
        check("2e control: sin el guard, la referencia 52 viajaría SIN fecha",
              ref52_sin is not None and "date" not in ref52_sin, ref52_sin)

        # ── 3) Guía ELECTRÓNICA: manda el DTE, no esta columna ───────────────────────
        # fecha_guia se deja con un valor RUIDOSO a propósito: si el camino electrónico la
        # leyera por error, la fecha saldría 2020-01-01 y el test cae.
        desp.fecha_guia = date(2020, 1, 1)
        db.commit()
        dte = WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp.id,
                         status_id=STATUS_EMITIDO, uuid="uuid-emitida", folio="551",
                         payload_json='{"documentDate": "2026-07-18"}')
        db.add(dte); db.commit()
        folio3, fecha3, problema3, _aud3 = _guia_referencia_de_factura(db, fac)
        check("3a guía electrónica: la fecha sale del documentDate del DTE 52",
              fecha3 == date(2026, 7, 18), fecha3)
        check("3b guía electrónica: ignora despacho.fecha_guia",
              fecha3 != date(2020, 1, 1), fecha3)
        check("3c guía electrónica: cita el folio del SII y no reporta problema",
              folio3 == "551" and problema3 is None, (folio3, problema3))
        db.delete(dte); db.commit()
        desp.fecha_guia = FECHA_GUIA_REAL
        db.commit()

        # ── 4) Sonda ANTI-DERIVA de los dos gemelos ──────────────────────────────────
        # El router tiene DOS lugares que resuelven la referencia 52 y los dos están en el
        # camino de la emisión: `_preparar_emision_factura` es el GUARD (lo corren
        # /facturas/preview y la puerta de /facturas/emitir) y `_guia_referencia_de_factura`
        # ARMA el documento que viaja al SII (vía _armar_payload_factura, en el emitir y en
        # el reintento). Si uno se arregla y el otro no, el guard dice "puede emitir" y el
        # SII recibe otra fecha. Probar el segundo con datos exige montar un DTE 33 entero;
        # esta sonda estructural cubre la deriva igual de bien: si alguien devuelve
        # `fecha_despacho` a cualquiera de los dos, el test falla.
        for nombre, fn in (("_guia_referencia_de_factura", _guia_referencia_de_factura),
                           ("_preparar_emision_factura", _preparar_emision_factura)):
            src = inspect.getsource(fn)
            check(f"4a {nombre} resuelve la fecha del papel por el helper único",
                  "_fecha_guia_papel(" in src)
            # Se ignoran las líneas de comentario: los comentarios EXPLICAN el bug viejo y
            # nombran fecha_despacho a propósito.
            codigo = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
            check(f"4b {nombre} ya no lee fecha_despacho para la referencia 52",
                  "fecha_despacho" not in codigo,
                  [l for l in codigo.splitlines() if "fecha_despacho" in l])

        # ── 5) El helper aislado ─────────────────────────────────────────────────────
        f_ok, p_ok = _fecha_guia_papel(SimpleNamespace(fecha_guia=FECHA_GUIA_REAL,
                                                       numero_guia="900123"))
        check("5a helper: con fecha devuelve la fecha y sin problema",
              f_ok == FECHA_GUIA_REAL and p_ok is None, (f_ok, p_ok))
        f_no, p_no = _fecha_guia_papel(SimpleNamespace(fecha_guia=None, numero_guia="900123"))
        check("5b helper: sin fecha devuelve problema y ninguna fecha",
              f_no is None and bool(p_no), (f_no, p_no))

        # ── 6) Validación de lo que teclea el operador (PUT /despachos) ──────────────
        check("6a acepta AAAA-MM-DD", _parse_fecha_guia("2026-07-15") == date(2026, 7, 15))
        check("6b vacío = borrar la fecha (estado legítimo)",
              _parse_fecha_guia("") is None and _parse_fecha_guia(None) is None)
        for malo in ("15-07-2026", "hoy", "2026-13-40", "2026/07/15", "2026-07-155", "2026-02-31"):
            try:
                _parse_fecha_guia(malo)
                check(f"6c rechaza formato inválido '{malo}'", False, "no lanzó")
            except HTTPException as e:
                check(f"6c rechaza formato inválido '{malo}'", e.status_code == 400, e.detail)
        try:
            _parse_fecha_guia((hoy_chile() + timedelta(days=1)).isoformat())
            check("6d rechaza fecha futura", False, "no lanzó")
        except HTTPException as e:
            check("6d rechaza fecha futura", e.status_code == 400, e.detail)
        try:
            _parse_fecha_guia((hoy_chile() - timedelta(days=3000)).isoformat())
            check("6e rechaza un año absurdo (tipeo)", False, "no lanzó")
        except HTTPException as e:
            check("6e rechaza un año absurdo (tipeo)", e.status_code == 400, e.detail)
        check("6f hoy es válido (guía emitida hoy)",
              _parse_fecha_guia(hoy_chile().isoformat()) == hoy_chile())

        # ── 7) Sin cruce entre marcas ────────────────────────────────────────────────
        # MachParts y MonzaParts resuelven esto con código PROPIO sobre tablas PROPIAS.
        # Que compartan nombre de función no puede volverse un import cruzado.
        from monza_wasabil_dte import router as mz_router
        check("7a cada marca tiene su propio helper (módulos distintos)",
              ga_router._fecha_guia_papel.__module__ != mz_router._fecha_guia_papel.__module__,
              (ga_router._fecha_guia_papel.__module__, mz_router._fecha_guia_papel.__module__))
        check("7b el resolver de MachParts no toca tablas de Monza",
              "Monza" not in inspect.getsource(_guia_referencia_de_factura))
        check("7c el resolver de Monza no toca tablas de MachParts",
              "MonzaDespacho" in inspect.getsource(mz_router._referencia_guia_de_despacho))

        # ── 8) El PUT /despachos guarda de verdad (texto → columna DATE) ─────────────
        # No basta con que el validador funcione: el endpoint recorre los campos con un
        # setattr genérico y, sin la conversión, SQLAlchemy guardaría el string crudo.
        r = _client.put(f"/despachos/{desp.id}", json={"fecha_guia": "2026-07-15"})
        check("8a PUT acepta la fecha", r.status_code == 200, (r.status_code, r.text[:200]))
        db.rollback()
        fresco = db.query(Despacho).filter(Despacho.id == desp.id).first()
        check("8b la fecha queda persistida como DATE, no como texto",
              fresco.fecha_guia == date(2026, 7, 15), fresco.fecha_guia)
        r = _client.put(f"/despachos/{desp.id}", json={"fecha_guia": "15-07-2026"})
        check("8c PUT rechaza formato inválido con 400", r.status_code == 400, r.status_code)
        r = _client.put(f"/despachos/{desp.id}", json={"fecha_guia": None})
        db.rollback()
        fresco = db.query(Despacho).filter(Despacho.id == desp.id).first()
        check("8d PUT con null borra la fecha",
              r.status_code == 200 and fresco.fecha_guia is None,
              (r.status_code, fresco.fecha_guia))
        # Tri-estado: un PUT que NO menciona fecha_guia no puede borrarla.
        fresco.fecha_guia = FECHA_GUIA_REAL
        db.commit()
        r = _client.put(f"/despachos/{desp.id}", json={"transportista": "Starken"})
        db.rollback()
        fresco = db.query(Despacho).filter(Despacho.id == desp.id).first()
        check("8e un PUT que no menciona la fecha NO la pisa",
              fresco.fecha_guia == FECHA_GUIA_REAL, fresco.fecha_guia)

        # ── 9) La referencia 52 no puede fecharse DESPUÉS de la factura ──────────────
        # Lo encontró el enjambre adversarial: `fecha_emision` de la factura es un campo
        # LIBRE sin tope, así que backdatando la factura se llegaba a un DTE 33 REAL que
        # declara haberse emitido ANTES que la guía que dice amparar. Ninguna de las dos
        # fechas es inválida por sí sola; lo inválido es el orden, y nadie las cruzaba.
        refs_ok, prob_ok = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 7, 1), guia_folio="900123",
            guia_fecha=date(2026, 7, 15), fecha_documento=date(2026, 7, 20))
        check("9a guía ANTERIOR a la factura: se emite normal",
              not prob_ok and any(str(r.get("documentType")) == "52" for r in refs_ok),
              (refs_ok, prob_ok))
        refs_mismo, prob_mismo = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 7, 1), guia_folio="900123",
            guia_fecha=date(2026, 7, 20), fecha_documento=date(2026, 7, 20))
        check("9b guía del MISMO día que la factura: válido (el borde no bloquea)",
              not prob_mismo, prob_mismo)
        refs_mal, prob_mal = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 7, 1), guia_folio="900123",
            guia_fecha=date(2026, 7, 25), fecha_documento=date(2026, 7, 20))
        check("9c guía POSTERIOR a la factura: bloquea", bool(prob_mal), prob_mal)
        check("9d y la referencia 52 NO se arma (no sale a medias)",
              not any(str(r.get("documentType")) == "52" for r in refs_mal), refs_mal)
        check("9e el mensaje nombra las DOS fechas, para saber cuál corregir",
              prob_mal and "2026-07-25" in prob_mal[0] and "2026-07-20" in prob_mal[0],
              prob_mal)
        # Control: sin fecha_documento el cruce NO corre (llamadores viejos intactos).
        _r, prob_sin = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 7, 1), guia_folio="900123",
            guia_fecha=date(2026, 7, 25))
        check("9f sin fecha_documento el control no corre (compatibilidad)",
              not prob_sin, prob_sin)
    finally:
        wasabil_client.buscar_documentos = _orig_buscar
        wasabil_client.obtener_documento = _orig_obtener
        _limpiar(db)
        db.close()

    assert not _fails, f"{len(_fails)} fallos: {_fails}"


def test_factura_cita_la_guia_de_papel_con_su_fecha_de_emision(): run()


if __name__ == "__main__":
    run()
    print("TODO OK")
