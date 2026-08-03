"""Auditoría integral F1-F6 — hallazgos #4 y #12: DTE «EMITIDO pero SIN folio».

Wasabil puede responder al POST /documents con {"uuid": ..., "status_id": 3} y SIN la
clave `folio` (forma que el README del módulo declara PENDIENTE de confirmar contra el
API real; MonzaParts todavía no hace su primera emisión real). Antes de estos arreglos
eso producía dos daños:

  · #4  La guía quedaba 'emitida' con folio NULL y `despacho.numero_guia` conservaba el
        N° TECLEADO a mano. `_guia_electronica_en_proceso` devolvía False (solo miraba
        status == 3), así que la factura 33 salía al SII referenciando ese N° manual —
        un folio 52 que el SII no reconoce, irreversible salvo nota de crédito. Además
        contradecía a `_guia_sii_en_proceso` (monza_router_despachos), que ya exigía
        folio no vacío.
  · #12 El estado era PERMANENTE: el sondeo cortaba en cuanto status era 3, /reintentar
        respondía 409 'ya está emitida' y el N° manual no se podía editar (guard
        anti-pisado). El despacho nunca recibía su N° de guía y la factura local nunca
        recibía su `numero_factura` (con el adelanto diferido sin aplicar).

Esta suite cubre las tres capas del arreglo, en las DOS direcciones (guía 52 y factura
33), y protege la direccionalidad que NO debe cambiar (una guía fallida que nunca llegó
a Wasabil sigue sin bloquear; una guía emitida CON folio sigue sin bloquear).

Wasabil SIMULADO por monkeypatch del client MONZA — JAMÁS el API real (emitir al SII es
IRREVERSIBLE). Datos con MARK propio y limpieza verificada con sesión nueva.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_aud_folio.py -q
(también:   ./venv/bin/python monza_wasabil_dte/tests/test_aud_folio.py)
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import SessionLocal  # noqa: E402
from monza_models import MonzaDespacho  # noqa: E402
from monza_wasabil_dte.models import STATUS_EMITIDO, STATUS_FALLIDO  # noqa: E402
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    Checker, FakeWasabil, crear_venta, dte_de_factura, dte_guia, facturas_de,
    limpiar, montar_app, verificar_limpieza,
)

# MARK corto A PROPÓSITO: MonzaCotizacion.numero es String(20) y el número de prueba
# es f"{MARK}-COT-{n}".
MARK = "__MWAUD__"
CURRENT = {"empresa": "automotriz", "id": None}

client = montar_app(CURRENT)
fake = FakeWasabil(MARK)
fake.install()
check = Checker()

GUIAS = "/api/monza/wasabil/despachos"
FACTURAS = "/api/monza/wasabil/facturas"


def _dte_guia_de(db, desp_id):
    """Fila DTE 52 del despacho, releída fresca (el test escribe por HTTP)."""
    from monza_wasabil_dte.models import MonzaWasabilDte
    db.rollback()  # sesión propia del test: descarta su snapshot viejo
    return (db.query(MonzaWasabilDte)
            .filter(MonzaWasabilDte.despacho_id == desp_id,
                    MonzaWasabilDte.tipo_dte == 52).first())


def run():
    db = SessionLocal()
    # Re-instalar NUESTRO fake al empezar: si pytest importó otras suites que instalan
    # fakes a nivel de módulo, la última instalación gana (anti-flaky).
    fake.install()
    limpiar(db, MARK)  # por si una corrida anterior murió a medias
    try:
        CURRENT["empresa"] = "automotriz"

        # ═══ 1. GUÍA 52, ruta A (#4 ORIGEN) — el POST vuelve EMITIDO sin folio y la
        #        emisión misma lo rescata con obtener_documento ═══════════════════
        fake.status_respuesta = STATUS_EMITIDO   # crear_documento responde 3 SIN folio
        fake.estado_final = STATUS_EMITIDO
        fake.folio_emitido = "52777"             # el documento completo SÍ lo trae
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion",
                                          numero_guia_manual="G-TECLEADO-H")
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("guía: emitir 200 aunque el POST volvió sin folio", r.status_code == 200, r.text)
        check("guía: la emisión RESCATA el folio en el acto (obtener_documento)",
              r.json().get("folio") == "52777", r.text)
        check("guía: se envió UN solo documento (no hay doble emisión)",
              len(fake.creados) == creados_antes + 1, len(fake.creados))
        db.rollback()
        check("guía: el folio pisa el N° tecleado en MonzaDespacho.numero_guia",
              db.get(MonzaDespacho, desp.id).numero_guia == "52777",
              db.get(MonzaDespacho, desp.id).numero_guia)
        limpiar(db, MARK)

        # ═══ 2. GUÍA 52, ruta B (#12 CALLEJÓN + #4 CINTURÓN) — ni el POST ni el
        #        documento completo traen folio todavía ═════════════════════════════
        # Wasabil confirma Emitida pero el folio del SII aún no está asignado.
        fake.status_respuesta = STATUS_EMITIDO
        fake.estado_final = STATUS_EMITIDO
        fake.folio_emitido = None                # obtener_documento tampoco lo trae
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion",
                                          numero_guia_manual="G-TECLEADO-H")
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("guía sin folio: emitir 200 (el documento SÍ salió)", r.status_code == 200, r.text)
        check("guía sin folio: el DTE queda emitido y SIN folio",
              r.json().get("folio") in (None, ""), r.text)
        db.rollback()
        check("guía sin folio: el despacho conserva el N° tecleado (aún no hay folio)",
              db.get(MonzaDespacho, desp.id).numero_guia == "G-TECLEADO-H",
              db.get(MonzaDespacho, desp.id).numero_guia)

        # El despacho se cierra (flujo real: la guía se emite en preparación y el
        # cierre lo pasa a 'despachado', que es el único estado facturable). Así el
        # ÚNICO bloqueo posible de la 33 más abajo es la guía sin folio.
        d = db.get(MonzaDespacho, desp.id)
        d.estado = "despachado"
        db.commit()

        # CINTURÓN (#4): con la guía emitida SIN folio, la factura 33 NO puede salir
        # referenciando el N° tecleado a mano.
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview", json={"cotizacion_id": cot.id,
                                                     "despacho_id": desp.id}).json()
        check("factura 33: la guía emitida SIN folio cuenta como EN PROCESO y BLOQUEA",
              p["puede_emitir"] is False and len(p["problemas"]) == 1
              and "EN PROCESO" in p["problemas"][0],
              p["problemas"])
        r = client.post(f"{FACTURAS}/emitir", json={"cotizacion_id": cot.id,
                                                    "despacho_id": desp.id})
        check("factura 33: emitir 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        db.rollback()
        check("factura 33: tampoco quedó factura local zombi",
              len(facturas_de(db, cot.id)) == 0, facturas_de(db, cot.id))

        # DESATASCO (#12): Wasabil ya asignó el folio → el sondeo lo rescata solo,
        # sin re-emitir nada.
        fake.folio_emitido = "52999"
        creados_antes = len(fake.creados)
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        check("guía sin folio: el SONDEO re-consulta y rescata el folio",
              r.status_code == 200 and r.json().get("folio") == "52999", r.text)
        check("guía sin folio: el sondeo NO re-emite (0 documentos nuevos)",
              len(fake.creados) == creados_antes, len(fake.creados))
        db.rollback()
        check("guía sin folio: el folio rescatado llega a MonzaDespacho.numero_guia",
              db.get(MonzaDespacho, desp.id).numero_guia == "52999",
              db.get(MonzaDespacho, desp.id).numero_guia)

        # Y recién ahí la factura 33 se destraba, referenciando el FOLIO REAL.
        p = client.post(f"{FACTURAS}/preview", json={"cotizacion_id": cot.id,
                                                     "despacho_id": desp.id}).json()
        ref52 = [x for x in p["referencias"] if x["tipo"] == "52"]
        check("factura 33: con el folio rescatado ya puede emitir",
              p["puede_emitir"] is True, p["problemas"])
        check("factura 33: referencia 52 con el FOLIO REAL, no el N° tecleado",
              len(ref52) == 1 and ref52[0]["folio"] == "52999", p["referencias"])
        limpiar(db, MARK)

        # ═══ 3. DIRECCIONALIDAD que NO cambia (control anti sobre-bloqueo) ════════
        # (a) Guía EMITIDA CON folio → no bloquea (era el caso feliz de siempre).
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-VIEJA")
        dte_guia(db, desp, uuid="uuid-guia-ok", status_id=STATUS_EMITIDO, folio="777",
                 payload_json='{"documentDate": "2026-06-20"}')
        p = client.post(f"{FACTURAS}/preview", json={"cotizacion_id": cot.id,
                                                     "despacho_id": desp.id}).json()
        check("control: guía emitida CON folio no bloquea y referencia el folio",
              p["puede_emitir"] is True
              and [x["folio"] for x in p["referencias"] if x["tipo"] == "52"] == ["777"],
              (p["problemas"], p["referencias"]))
        limpiar(db, MARK)

        # (b) Guía FALLIDA que NUNCA llegó a Wasabil (sin uuid ni claim) → no bloquea:
        #     ahí no hay guía electrónica y el N° manual es la referencia legítima.
        #     CORREGIDO (re-auditoría MEDIO-5): el N° manual era "G-MANUAL-9" y este check
        #     FIJABA COMO CORRECTO que un folio NO numérico viajara como FolioRef de la
        #     referencia 52. El folio de una guía —también en papel— es un correlativo
        #     numérico del SII, así que ahora eso BLOQUEA (test_aud_paridad_sii, esc. E).
        #     La direccionalidad que este check protege sigue igual.
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9349")
        dte_guia(db, desp, status_id=STATUS_FALLIDO, error="rechazada", en_vuelo_desde=None)
        p = client.post(f"{FACTURAS}/preview", json={"cotizacion_id": cot.id,
                                                     "despacho_id": desp.id}).json()
        check("control: guía FALLIDA sin uuid sigue SIN bloquear (N° manual legítimo)",
              p["puede_emitir"] is True
              and [x["folio"] for x in p["referencias"] if x["tipo"] == "52"] == ["9349"],
              (p["problemas"], p["referencias"]))
        limpiar(db, MARK)

        # ═══ 4. FACTURA 33 (#12) — mismo hueco: emitida sin folio ═════════════════
        # 4a) Ruta A: el documento completo trae el folio → la emisión lo escribe ya.
        fake.status_respuesta = STATUS_EMITIDO
        fake.estado_final = STATUS_EMITIDO
        fake.folio_emitido = "F5001"
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9101")
        r = client.post(f"{FACTURAS}/emitir", json={"cotizacion_id": cot.id,
                                                    "despacho_id": desp.id})
        check("factura: emitir 200 con el POST sin folio", r.status_code == 200, r.text)
        check("factura: la emisión rescata el folio en el acto",
              r.json().get("folio") == "F5001", r.text)
        db.rollback()
        facturas = facturas_de(db, cot.id)
        check("factura: el folio rescatado se escribe en numero_factura",
              len(facturas) == 1 and (facturas[0].numero_factura or "") == "F5001",
              [(f.id, f.numero_factura) for f in facturas])
        limpiar(db, MARK)

        # 4b) Ruta B: tampoco hay folio al emitir → el SONDEO lo rescata (antes la
        #     factura quedaba sin numero_factura para siempre).
        fake.folio_emitido = None
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9101")
        r = client.post(f"{FACTURAS}/emitir", json={"cotizacion_id": cot.id,
                                                    "despacho_id": desp.id})
        check("factura sin folio: emitir 200 (el documento salió)", r.status_code == 200, r.text)
        factura_id = r.json().get("factura_id")
        check("factura sin folio: queda emitida sin folio y sin numero_factura",
              r.json().get("folio") in (None, ""), r.text)
        db.rollback()
        f0 = [f for f in facturas_de(db, cot.id) if f.id == factura_id][0]
        check("factura sin folio: numero_factura sigue vacío",
              not (f0.numero_factura or "").strip(), f0.numero_factura)

        fake.folio_emitido = "F5002"   # Wasabil ya asignó el folio
        creados_antes = len(fake.creados)
        r = client.get(f"{FACTURAS}/{factura_id}/estado")
        check("factura sin folio: el SONDEO re-consulta y rescata el folio",
              r.status_code == 200 and r.json().get("folio") == "F5002", r.text)
        check("factura sin folio: el sondeo NO re-emite (0 documentos nuevos)",
              len(fake.creados) == creados_antes, len(fake.creados))
        db.rollback()
        f0 = [f for f in facturas_de(db, cot.id) if f.id == factura_id][0]
        check("factura sin folio: el folio rescatado llega a numero_factura",
              (f0.numero_factura or "") == "F5002", f0.numero_factura)
        check("factura sin folio: el DTE queda emitido con folio",
              (dte_de_factura(db, factura_id).folio or "") == "F5002")
        limpiar(db, MARK)

    finally:
        # Restaurar los defaults del fake: otras suites lo comparten por módulo.
        fake.status_respuesta = 2
        fake.estado_final = STATUS_EMITIDO
        fake.folio_emitido = "9001"
        limpiar(db, MARK)
        db.close()
        verificar_limpieza(MARK)

    check.finish()


def test_monza_wasabil_dte_emitido_sin_folio():
    run()


if __name__ == "__main__":
    run()
