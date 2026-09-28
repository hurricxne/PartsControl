"""Factura de un despacho cuya guía se emitió FUERA del sistema — paridad con MachParts
(`verificado_sin_guia_electronica`). Escenarios: features/monza/guia-emitida-fuera-del-sistema.feature

EL CASO (MTK-2026-0008, 2026-09-28): el SII rechazó la guía electrónica del despacho, el
documento quedó en Wasabil con uuid y la mercadería salió con una guía emitida a mano. La
factura se bloqueaba (MEDIO-5) y solo soporte la destrababa con SQL. Ahora el operador
repite la referencia del despacho después de revisar Wasabil, y eso queda auditado.

Lo que esta suite fija:
  · el bloqueo sigue SIN la declaración, y pide exactamente lo que la pantalla necesita;
  · con la declaración la 33 sale citando el N° tecleado, y la auditoría se escribe en la
    guía ANTES del POST al SII;
  · una guía EMITIDA en Wasabil con esa referencia bloquea sin excepción;
  · ningún otro estado de la guía cambia (papel, emitida, en proceso, ambigua);
  · la declaración vale solo para el estado exacto de MEDIO-5 (alineación con el predicado);
  · candado de empresa y de sesión.

JAMÁS se llama al API real de Wasabil: todo corre contra `FakeWasabil` (factura_harness).
Datos MARCADOS y limpieza verificada con sesión nueva.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_guia_emitida_fuera.py -q
"""
import json
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from database import SessionLocal
from monza_contabilidad.router import router as contab_router
from monza_wasabil_dte import client as monza_client
from monza_wasabil_dte import router as R
from monza_wasabil_dte.models import (CLAIM_TTL_SEGUNDOS, MonzaWasabilDte, STATUS_EMITIDO,
                                      STATUS_FALLIDO, STATUS_PROCESANDO)
from monza_wasabil_dte.router import router as wasabil_router
from monza_wasabil_dte.tests.factura_harness import (
    Checker, FakeWasabil, FECHA_GUIA_PAPEL, crear_venta, dte_guia, facturas_de, limpiar,
    montar_app, verificar_limpieza,
)

MARK = "__MWFUERA__"
CURRENT = {"empresa": "automotriz", "id": None}
FACTURAS = "/api/monza/wasabil/facturas"
PARAM = R.PARAM_VERIF_52

client = montar_app(CURRENT)
check = Checker()
fake = FakeWasabil(MARK)


def _buscar_405(_search):
    """Lo que hoy responde el API real al listado de documentos."""
    raise monza_client.WasabilError("HTTP 405 Method Not Allowed en GET /documents",
                                    ambiguo=False, status_code=405)


def _preparar_fake(*, buscar=None, docs=None):
    fake.install()
    fake.crear_falla = None
    fake.antes_de_crear = None
    fake.docs_buscables = list(docs or [])
    fake.busqueda_completa = True
    if buscar is not None:
        monza_client.buscar_documentos = buscar


def _venta_con_guia_rechazada(db, numero="8601"):
    cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual=numero)
    dte_guia(db, desp, status_id=STATUS_FALLIDO, uuid="uuid-guia-rechazada", folio=None,
             en_vuelo_desde=None, error=None)
    return cot, desp


def _payload(cot, desp):
    return {"cotizacion_id": cot.id, "despacho_id": desp.id}


def _ref52(preview) -> list:
    return [x["folio"] for x in preview.get("referencias", []) if x["tipo"] == "52"]


def _error_guia(despacho_id):
    s = SessionLocal()
    try:
        fila = (s.query(MonzaWasabilDte)
                .filter(MonzaWasabilDte.despacho_id == despacho_id,
                        MonzaWasabilDte.tipo_dte == 52).first())
        return (fila.error or "") if fila else ""
    finally:
        s.close()


def run():
    db = SessionLocal()
    limpiar(db, MARK)
    try:
        CURRENT["empresa"] = "automotriz"

        # ═══ 1 · Sin declaración: bloquea y pide la verificación con sus datos ═══════════
        _preparar_fake(buscar=_buscar_405)
        cot, desp = _venta_con_guia_rechazada(db)
        p = client.post(f"{FACTURAS}/preview", json=_payload(cot, desp)).json()
        v = p.get("verificacion_52") or {}
        check("1a sin declaración la factura NO puede emitir", p["puede_emitir"] is False,
              p["problemas"])
        check("1b pide la verificación con la referencia del despacho",
              v.get("referencia") == desp.numero, v)
        check("1c y dice qué guía va a citar (N° y fecha del despacho)",
              v.get("numero_guia") == "8601" and v.get("fecha_guia") == FECHA_GUIA_PAPEL.isoformat(), v)
        check("1d el mensaje conserva el motivo (ya NO se acepta como referencia 52)",
              any("ya NO se acepta como referencia 52" in x for x in p["problemas"]), p["problemas"])
        check("1e no arma referencia 52 todavía", _ref52(p) == [], p.get("referencias"))
        check("1e-bis el mensaje de la verificación es EXACTAMENTE uno de los problemas "
              "(la pantalla lo reemplaza por el paso guiado por igualdad)",
              v.get("mensaje") in p["problemas"], (v.get("mensaje"), p["problemas"]))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir", json=_payload(cot, desp))
        check("1f emitir sin declaración: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados, r.text)
        db.rollback()
        check("1g y no queda factura local zombi", len(facturas_de(db, cot.id)) == 0)

        # ═══ 2 · Con la referencia correcta: emite citando el N° tecleado y audita antes ═══
        visto_al_crear = {}

        def _antes(_payload_rest):
            visto_al_crear["error_guia"] = _error_guia(desp.id)
        fake.antes_de_crear = _antes
        p = client.post(f"{FACTURAS}/preview", json=_payload(cot, desp),
                        params={PARAM: desp.numero}).json()
        check("2a con la referencia correcta la vista previa deja emitir",
              p["puede_emitir"] is True and p.get("verificacion_52") is None, p["problemas"])
        check("2b y cita la guía N° 8601 como referencia 52", _ref52(p) == ["8601"],
              p.get("referencias"))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir", json=_payload(cot, desp),
                        params={PARAM: desp.numero})
        check("2c emitir con la declaración: 200 y UN documento al SII",
              r.status_code == 200 and len(fake.creados) == creados + 1, r.text)
        enviado = json.dumps(fake.creados[-1]) if fake.creados else ""
        check("2d el documento enviado cita la guía 8601 con SU fecha (la del despacho)",
              "8601" in enviado and FECHA_GUIA_PAPEL.isoformat() in enviado, enviado[:600])
        aud = visto_al_crear.get("error_guia", "")
        check("2e la auditoría estaba escrita en la guía ANTES del POST",
              "VERIFICACIÓN HUMANA" in aud and desp.numero in aud and "8601" in aud, aud)
        check("2f y dice quién declaró", "test-mwfac@monza.cl" in _error_guia(desp.id),
              _error_guia(desp.id))
        s2 = SessionLocal()
        try:
            fila = (s2.query(MonzaWasabilDte).filter(MonzaWasabilDte.despacho_id == desp.id,
                                                     MonzaWasabilDte.tipo_dte == 52).first())
            copias = json.loads(fila.respuesta_json or "{}").get("_verificaciones_guia_fuera", [])
        finally:
            s2.close()
        check("2f-bis y queda una copia estructurada y durable en respuesta_json",
              len(copias) == 1 and copias[0]["referencia"] == desp.numero
              and copias[0]["numero_guia"] == "8601" and copias[0]["usuario_email"], copias)
        limpiar(db, MARK)

        # ═══ 2-bis · El reintento de la factura exige la misma declaración ═══════════════
        # Búsqueda SANA (lista vacía y completa): con el 405 el reintento se detiene antes,
        # en su propio rescate por referencia FACT-<id>, y nunca llega a la guía.
        _preparar_fake()
        cot, desp = _venta_con_guia_rechazada(db, numero="8602")
        fake.crear_falla = monza_client.WasabilError("422 validación", ambiguo=False,
                                                      status_code=422)
        r = client.post(f"{FACTURAS}/emitir", json=_payload(cot, desp),
                        params={PARAM: desp.numero})
        db.rollback()
        fac = facturas_de(db, cot.id)
        check("2g (montaje) la 33 quedó creada y fallida", r.status_code == 502 and len(fac) == 1,
              r.text)
        fake.crear_falla = None
        if fac:
            creados = len(fake.creados)
            r = client.post(f"{FACTURAS}/{fac[0].id}/reintentar")
            check("2h reintentar SIN declaración: 409 y nada sale al SII",
                  r.status_code == 409 and len(fake.creados) == creados
                  and "ya NO se acepta como referencia 52" in r.text, r.text)
            r = client.post(f"{FACTURAS}/{fac[0].id}/reintentar", params={PARAM: desp.numero})
            check("2i reintentar CON declaración: re-emite una vez",
                  r.status_code == 200 and len(fake.creados) == creados + 1, r.text)
        limpiar(db, MARK)

        # ═══ 3 · Referencia que no coincide ════════════════════════════════════════════
        _preparar_fake(buscar=_buscar_405)
        cot, desp = _venta_con_guia_rechazada(db)
        p = client.post(f"{FACTURAS}/preview", json=_payload(cot, desp),
                        params={PARAM: "DSP-2026-0043"}).json()
        check("3a otra referencia: no puede emitir y avisa que no coincide",
              p["puede_emitir"] is False
              and (p.get("verificacion_52") or {}).get("referencia_no_coincide") is True
              and any("no coincide" in x for x in p["problemas"]), p)
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir", json=_payload(cot, desp),
                        params={PARAM: "DSP-2026-0043"})
        db.rollback()
        check("3b emitir con otra referencia: 409, nada al SII, sin factura zombi",
              r.status_code == 409 and len(fake.creados) == creados
              and len(facturas_de(db, cot.id)) == 0, r.text)
        limpiar(db, MARK)

        # ═══ 4 · Wasabil tiene una guía EMITIDA con esa referencia: bloqueo absoluto ═══════
        cot, desp = _venta_con_guia_rechazada(db)
        _preparar_fake(docs=[{"uuid": "u-91", "invoice_reference": desp.numero,
                              "status_id": STATUS_EMITIDO, "folio": "91"}])
        p = client.post(f"{FACTURAS}/preview", json=_payload(cot, desp),
                        params={PARAM: desp.numero}).json()
        check("4a con la declaración igual NO puede emitir y nombra el folio real 91",
              p["puede_emitir"] is False and any("91" in x and "EMITIDA" in x for x in p["problemas"]),
              p["problemas"])
        check("4b y no ofrece verificar (no hay nada que declarar)",
              p.get("verificacion_52") is None, p.get("verificacion_52"))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir", json=_payload(cot, desp),
                        params={PARAM: desp.numero})
        check("4c emitir: 409 y nada al SII", r.status_code == 409 and len(fake.creados) == creados,
              r.text)
        limpiar(db, MARK)

        # ═══ 4-bis · Diferencia deliberada con MachParts: aunque Wasabil CONFIRME que no
        # hay ninguna emitida, se pide la declaración (el N° tecleado puede ser el viejo). ═══
        cot, desp = _venta_con_guia_rechazada(db)
        _preparar_fake(docs=[{"uuid": "uuid-guia-rechazada", "invoice_reference": desp.numero,
                              "status_id": STATUS_FALLIDO, "folio": None}])
        p = client.post(f"{FACTURAS}/preview", json=_payload(cot, desp)).json()
        check("4d sin emitidas confirmadas, igual pide la declaración",
              p["puede_emitir"] is False and (p.get("verificacion_52") or {}).get("referencia") == desp.numero,
              p["problemas"])
        limpiar(db, MARK)

        # ═══ 5 · Guía en papel de siempre (sin guía electrónica): nada cambia ════════════
        _preparar_fake(buscar=_buscar_405)
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="8603")
        p = client.post(f"{FACTURAS}/preview", json=_payload(cot, desp)).json()
        check("5a papel sin guía electrónica: emite sin pedir verificación",
              p["puede_emitir"] is True and p.get("verificacion_52") is None
              and _ref52(p) == ["8603"], p)
        limpiar(db, MARK)

        # ═══ 6 · Guía electrónica emitida con folio: cita el folio del SII ════════════════
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="8604")
        dte_guia(db, desp, status_id=STATUS_EMITIDO, uuid="u-emitida", folio="9090",
                 payload_json=json.dumps({"documentDate": "2026-06-19"}))
        p = client.post(f"{FACTURAS}/preview", json=_payload(cot, desp),
                        params={PARAM: desp.numero}).json()
        check("6a emitida con folio: cita el 9090, no el N° tecleado, sin verificación",
              p["puede_emitir"] is True and _ref52(p) == ["9090"]
              and p.get("verificacion_52") is None, p)
        limpiar(db, MARK)

        # ═══ 7 · En proceso / ambigua / emitida sin folio: la declaración NO destraba ═════
        for nombre, estado in (
                ("en proceso", dict(status_id=STATUS_PROCESANDO, uuid="u-proc")),
                ("ambigua", dict(status_id=STATUS_FALLIDO, uuid=None,
                                 en_vuelo_desde=_vencido())),
                ("emitida sin folio", dict(status_id=STATUS_EMITIDO, uuid="u-sinfolio"))):
            cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="8605")
            dte_guia(db, desp, folio=None, **estado)
            p = client.post(f"{FACTURAS}/preview", json=_payload(cot, desp),
                            params={PARAM: desp.numero}).json()
            check(f"7 guía {nombre}: sigue bloqueando aunque se declare",
                  p["puede_emitir"] is False and p.get("verificacion_52") is None
                  and _ref52(p) == [], p["problemas"])
            limpiar(db, MARK)

        # ═══ 8 · Alineación: la declaración vale SOLO para el estado MEDIO-5 ══════════════
        estados = [
            dict(status_id=STATUS_FALLIDO, uuid="u4"),
            dict(status_id=STATUS_FALLIDO, uuid=None),
            dict(status_id=STATUS_FALLIDO, uuid=None, en_vuelo_desde=_vencido()),
            dict(status_id=STATUS_PROCESANDO, uuid="u2"),
            dict(status_id=STATUS_EMITIDO, uuid="u3", folio=None),
            dict(status_id=STATUS_EMITIDO, uuid="u3", folio="777"),
            dict(status_id=None, uuid=None),
        ]
        desalineados = []
        for est in estados:
            cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="8606")
            fila = dte_guia(db, desp, **{"folio": None, **est})
            motivo = R._guia_no_referenciable(db, desp.id)
            es_medio5 = motivo is not None and motivo == R._msg_guia_rechazada_con_documento(fila)
            if R._es_rechazo_con_documento(fila) != es_medio5:
                desalineados.append(est)
            limpiar(db, MARK)
        check("8a _es_rechazo_con_documento ⇔ el predicado responde MEDIO-5, en todos los estados",
              not desalineados, desalineados)

        # ═══ 9 · El aviso del selector de guías no cambia ═════════════════════════════════
        cot, desp = _venta_con_guia_rechazada(db)
        check("9a la guía rechazada con documento sigue contando como NO referenciable",
              R._guia_electronica_en_proceso(db, desp.id) is True)

        # ═══ 10 · La vía manual de Contabilidad no ofrece la verificación ═════════════════
        pm = client.post("/api/monza/contabilidad/facturas/preview",
                         json=_payload(cot, desp)).json()
        check("10a la vista previa manual no trae verificacion_52", "verificacion_52" not in pm,
              list(pm)[:12] if isinstance(pm, dict) else pm)
        limpiar(db, MARK)

        # ═══ 11 · Candado de empresa ═════════════════════════════════════════════════════
        _preparar_fake(buscar=_buscar_405)
        cot, desp = _venta_con_guia_rechazada(db)
        CURRENT["empresa"] = "mineria"
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir", json=_payload(cot, desp),
                        params={PARAM: desp.numero})
        check("11a usuario de otra empresa: 403 y nada al SII",
              r.status_code == 403 and len(fake.creados) == creados, r.text)
        CURRENT["empresa"] = "automotriz"

        # ═══ 12 · Sin sesión ═════════════════════════════════════════════════════════════
        sin_sesion = FastAPI()
        sin_sesion.include_router(contab_router)
        sin_sesion.include_router(wasabil_router, prefix="/api")
        r = TestClient(sin_sesion).post(f"{FACTURAS}/emitir", json=_payload(cot, desp),
                                        params={PARAM: desp.numero})
        # El esquema de auth del proyecto responde 403 "Not authenticated" (no 401).
        check("12a sin sesión: rechazado por no autenticado y nada al SII",
              r.status_code in (401, 403) and "Not authenticated" in r.text
              and len(fake.creados) == creados, (r.status_code, r.text))
    finally:
        CURRENT["empresa"] = "automotriz"
        fake.install()
        limpiar(db, MARK)
        db.close()
    verificar_limpieza(MARK)
    check.finish()


def _vencido():
    return datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)


def test_guia_emitida_fuera_del_sistema():
    run()


if __name__ == "__main__":
    run()
