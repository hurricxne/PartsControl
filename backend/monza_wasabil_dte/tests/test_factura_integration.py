"""Fase 6 — flujo completo de la FACTURA electrónica (DTE 33) de MonzaParts contra la
BD local, con Wasabil SIMULADO (JAMÁS el API real: emitir al SII es IRREVERSIBLE).

Monta Contabilidad Monza + Wasabil DTE Monza en una app efímera (sin tocar main.py) y
recorre: preview que NO emite → emisión → sondeo → folio del SII en
`numero_factura` → adelanto DIFERIDO que cae recién ahí → referencia 52 con el FOLIO
REAL de la guía (no el N° tecleado) → guía en proceso que bloquea → retiro en oficina
sin referencia 52 → bloqueos del receptor → IVA por venta → candado de empresa.

Los fakes pisan `monza_wasabil_dte.client` (superficie MONZA), nunca la de Grupo AM.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_factura_integration.py -q
(también:   ./venv/bin/python monza_wasabil_dte/tests/test_factura_integration.py)
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import SessionLocal  # noqa: E402
from monza_contabilidad.models import MonzaContFacturaCliente  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    STATUS_EMITIDO, STATUS_FALLIDO, STATUS_PROCESANDO,
)
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    FECHA_GUIA_PAPEL, NETO_DESPACHO, NETO_VENTA, PRECIO_1, PRECIO_2, QTY_DESP_1, QTY_DESP_2,
    RUT_INVALIDO, RUT_SIN_FICHA, Checker, FakeWasabil, cobranzas_de, crear_venta,
    dte_de_factura, dte_guia, facturas_de, limpiar, montar_app, verificar_limpieza,
)

# MARK corto A PROPÓSITO: MonzaCotizacion.numero es String(20) y el número de prueba
# es f"{MARK}-COT-{n}" — un prefijo largo revienta con "Data too long".
MARK = "__MWF33A__"
CURRENT = {"empresa": "automotriz", "id": None}

client = montar_app(CURRENT)
fake = FakeWasabil(MARK)
fake.install()
check = Checker()

BASE = "/api/monza/wasabil/facturas"
CONTAB = "/api/monza/contabilidad"


def run():
    db = SessionLocal()
    # Re-instalar NUESTRO fake al empezar: si pytest importó otras suites que instalan
    # fakes a nivel de módulo, la última instalación gana (anti-flaky).
    fake.install()
    limpiar(db, MARK)  # por si una corrida anterior murió a medias
    try:
        CURRENT["empresa"] = "automotriz"

        # ═══ 1. PREVIEW FELIZ — y la TRAMPA del snapshot `numero_guia` ═══════════
        # El despacho conserva el N° tecleado a mano ("G-MANUAL-VIEJA") mientras su
        # guía electrónica YA está emitida con folio 777. La referencia 52 debe salir
        # del DTE de la guía, jamás del N° viejo: si saliera del snapshot, el SII
        # recibiría una factura apuntando a un folio que no reconoce.
        cot, desp, _it1, _it2 = crear_venta(db, MARK, numero_guia_manual="G-MANUAL-VIEJA",
                                            pct_adelanto=50)
        dte_guia(db, desp, uuid="uuid-guia-1", status_id=STATUS_EMITIDO, folio="777",
                 payload_json='{"documentDate": "2026-06-20"}')
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id})
        check("preview 200", r.status_code == 200, r.text)
        p = r.json()
        check("preview puede_emitir", p["puede_emitir"] is True, p["problemas"])
        check("preview receptor desde Wasabil (ficha anidada normalizada)",
              p["receptor"]["fuente"] == "wasabil" and p["receptor"]["comuna"] == "Las Condes",
              p["receptor"])
        check("preview 2 líneas del despacho con precio congelado",
              len(p["lineas"]) == 2
              and p["lineas"][0]["cantidad"] == QTY_DESP_1
              and p["lineas"][0]["precio_unit_neto"] == PRECIO_1
              and p["lineas"][1]["cantidad"] == QTY_DESP_2
              and p["lineas"][1]["precio_unit_neto"] == PRECIO_2, p["lineas"])
        check("preview totales con el IVA de la venta",
              p["totales"] == {"neto": NETO_DESPACHO, "iva": round(NETO_DESPACHO * 0.19),
                               "bruto": NETO_DESPACHO + round(NETO_DESPACHO * 0.19),
                               "iva_rate": 0.19}, p["totales"])
        check("preview ref 801 con N° y fecha de la OC",
              p["referencias"][0]["tipo"] == "801"
              and p["referencias"][0]["folio"] == "OC-4501"
              and p["referencias"][0]["fecha"] == "2026-06-10", p["referencias"])
        check("preview ref 52 con el FOLIO REAL del SII, NO el N° tecleado viejo",
              p["referencias"][1]["tipo"] == "52"
              and p["referencias"][1]["folio"] == "777"
              and p["referencias"][1]["fecha"] == "2026-06-20", p["referencias"])
        check("preview referencias SIN texto redundante (formato v3)",
              all(not x["descripcion"] for x in p["referencias"]), p["referencias"])
        check("PREVIEW NO EMITE: nada llegó a Wasabil",
              len(fake.creados) == creados_antes, fake.creados[creados_antes:])
        db.rollback()
        check("PREVIEW NO PERSISTE: no se creó factura local",
              len(facturas_de(db, cot.id)) == 0, facturas_de(db, cot.id))

        # ═══ 2. ADELANTO VERIFICADO ANTES DE EMITIR ═════════════════════════════
        # Contabilidad verifica el 50% informado por Comercial. Todavía no hay factura,
        # así que no se aplica nada: la aplicación es la que debe quedar DIFERIDA.
        r = client.post(f"{CONTAB}/ventas/{cot.id}/adelanto/verificar",
                        json={"monto": 50000, "fecha_pago": "2026-06-15",
                              "banco": "BCI", "numero_operacion": "OP-1"})
        check("adelanto verificado 200", r.status_code == 200, r.text)

        # ═══ 3. EMITIR — factura local SIN folio + claim, ANTES de cualquier HTTP ══
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot.id,
                                                "despacho_id": desp.id})
        check("emitir 200", r.status_code == 200, r.text)
        e = r.json()
        factura_id = e.get("factura_id")
        check("emitir devuelve factura_id (el modal sondea POR FACTURA)",
              isinstance(factura_id, int), e)
        check("emitir uuid persistido y estado no terminal",
              e["uuid"] == f"uuid-f{len(fake.creados)}" and e["estado"] == "procesando", e)
        enviado = fake.creados[-1]
        check("emitir issue=true SOLO en el camino confirmado", enviado["issue"] is True)
        check("emitir payload REST: tipo 33 + client_id + payment_method",
              enviado["sii_document_type_code"] == 33
              and enviado["client_id"] == 160065
              and enviado["payment_method"] in ("contado", "credito"), enviado)
        check("emitir invoice_reference = FACT-<id de la factura local>",
              enviado["invoice_reference"] == f"FACT-{factura_id}", enviado)
        check("emitir referencias 801 + 52 en snake_case, con el folio real",
              enviado["references"] == [
                  {"document_type": "801", "folio": "OC-4501", "date": "2026-06-10"},
                  {"document_type": "52", "folio": "777", "date": "2026-06-20"}],
              enviado["references"])
        check("emitir líneas con externalId = despacho_item_id (cruce 1:1 con la guía)",
              len(enviado["details"]) == 2
              and all(x["externalId"].isdigit() for x in enviado["details"]),
              enviado["details"])
        db.rollback()  # cierra el snapshot del test para ver los commits del router
        fac = db.get(MonzaContFacturaCliente, factura_id)
        check("factura local persistida SIN folio (lo asigna el SII)",
              fac is not None and not (fac.numero_factura or "").strip(),
              fac and fac.numero_factura)
        check("factura local con los montos congelados de la emisión",
              float(fac.monto_neto) == NETO_DESPACHO
              and float(fac.monto_bruto) == NETO_DESPACHO + round(NETO_DESPACHO * 0.19),
              (fac.monto_neto, fac.monto_bruto))
        check("el SNAPSHOT numero_guia conserva el N° viejo (por eso la referencia "
              "52 NO puede salir de ahí)", fac.numero_guia == "G-MANUAL-VIEJA",
              fac.numero_guia)
        fila = dte_de_factura(db, factura_id)
        check("fila DTE 33 ligada a la factura, con montos congelados",
              fila is not None and fila.tipo_dte == 33
              and float(fila.monto_neto) == NETO_DESPACHO, fila and fila.monto_neto)
        check("ADELANTO DIFERIDO: aún sin cobranza (el SII no ha confirmado nada)",
              len(cobranzas_de(db, factura_id)) == 0, cobranzas_de(db, factura_id))

        # ═══ 4. SONDEO → EMITIDO: folio a la factura y RECIÉN AHÍ el adelanto ═════
        r = client.get(f"{BASE}/{factura_id}/estado")
        s = r.json()
        check("estado emitido con folio", s["estado"] == "emitido" and s["folio"] == "9001", s)
        check("estado trae pdf/xml y factura_id", "pdf/9001" in (s["pdf_url"] or "")
              and s["factura_id"] == factura_id, s)
        db.rollback()
        fac = db.get(MonzaContFacturaCliente, factura_id)
        check("folio del SII escrito en numero_factura", fac.numero_factura == "9001",
              fac.numero_factura)
        cobs = cobranzas_de(db, factura_id)
        check("adelanto aplicado RECIÉN al confirmarse la emisión",
              len(cobs) == 1 and cobs[0].medio == "adelanto" and float(cobs[0].monto) == 50000,
              [(c.medio, float(c.monto)) for c in cobs])
        check("saldo de la factura recalculado con el adelanto",
              float(fac.monto_pagado) == 50000
              and float(fac.saldo) == float(fac.monto_bruto) - 50000,
              (fac.monto_pagado, fac.saldo))

        # Idempotencia: volver a sondear no duplica el adelanto ni cambia el folio.
        client.get(f"{BASE}/{factura_id}/estado")
        client.get(f"{BASE}/{factura_id}/estado")
        db.rollback()
        check("sondeo repetido es IDEMPOTENTE (ni folio distinto ni adelanto doble)",
              len(cobranzas_de(db, factura_id)) == 1
              and db.get(MonzaContFacturaCliente, factura_id).numero_factura == "9001")

        # ═══ 5. Anti doble emisión sobre lo YA facturado ═════════════════════════
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot.id,
                                                "despacho_id": desp.id})
        check("re-emitir el mismo despacho → 409 (ya facturado por completo)",
              r.status_code == 409, r.text)
        check("re-emitir bloqueado NO llegó a Wasabil",
              len(fake.creados) == creados_antes, fake.creados[creados_antes:])
        r = client.post(f"{BASE}/{factura_id}/reintentar")
        check("reintentar una factura ya emitida → 409 con el folio",
              r.status_code == 409 and "9001" in r.json()["detail"], r.text)
        r = client.get(f"{BASE}/estado-batch?ids={factura_id}")
        check("estado-batch trae el folio para el badge del listado",
              r.json().get(str(factura_id), {}).get("folio") == "9001", r.json())
        limpiar(db, MARK)

        # ═══ 6. RETIRO EN OFICINA (sin_guia): sin referencia 52 ══════════════════
        # Modo exclusivo de Monza (GA no lo tiene): la factura misma ampara el traslado.
        cot, _desp, _i1, _i2 = crear_venta(db, MARK, con_despacho=False)
        r = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id, "sin_guia": True})
        p = r.json()
        check("retiro en oficina: puede emitir", p["puede_emitir"] is True, p["problemas"])
        check("retiro en oficina: SOLO referencia 801 (sin 52)",
              [x["tipo"] for x in p["referencias"]] == ["801"], p["referencias"])
        check("retiro en oficina: el preview lo ADVIERTE explícitamente",
              any("Retiro en oficina" in a for a in p["advertencias"]), p["advertencias"])
        check("retiro en oficina factura el saldo VENDIDO completo",
              p["totales"]["neto"] == NETO_VENTA, p["totales"])
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot.id, "sin_guia": True})
        check("retiro en oficina emitir 200", r.status_code == 200, r.text)
        enviado = fake.creados[-1]
        check("retiro en oficina: el documento enviado NO lleva referencia 52",
              [x["document_type"] for x in enviado["references"]] == ["801"],
              enviado["references"])
        check("retiro en oficina: externalId 'fi-<id>' (no hay línea de despacho)",
              all(x["externalId"].startswith("fi-") for x in enviado["details"]),
              enviado["details"])
        limpiar(db, MARK)

        # ═══ 7. GUÍA 52 EN PROCESO → bloquea la factura ══════════════════════════
        # `despacho.numero_guia` todavía tiene el N° manual viejo; el folio real no
        # llega. Facturar aquí produce un DTE 33 real contra un folio inexistente.
        for etiqueta, kw in (
            ("uuid en proceso (status 2)", dict(uuid="uuid-proc", status_id=STATUS_PROCESANDO)),
            ("borrador en Wasabil (status 6)", dict(uuid="uuid-borr", status_id=6)),
            ("claim en vuelo sin respuesta", dict(en_vuelo_desde=datetime.utcnow())),
        ):
            cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-MANUAL-VIEJA")
            dte_guia(db, desp, **kw)
            creados_antes = len(fake.creados)
            p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                     "despacho_id": desp.id}).json()
            check(f"guía {etiqueta} → preview BLOQUEA",
                  p["puede_emitir"] is False
                  and any("EN PROCESO" in x for x in p["problemas"]), p["problemas"])
            r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot.id,
                                                    "despacho_id": desp.id})
            check(f"guía {etiqueta} → emitir 409 y NADA sale al SII",
                  r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
            db.rollback()
            check(f"guía {etiqueta} → tampoco quedó factura local zombi",
                  len(facturas_de(db, cot.id)) == 0, facturas_de(db, cot.id))
            limpiar(db, MARK)

        # Direccionalidad: una guía FALLIDA que nunca llegó a Wasabil NO bloquea — ahí
        # no hay guía electrónica y el N° manual es la referencia legítima.
        # CORREGIDO (re-auditoría MEDIO-5): el N° manual era "G-MANUAL-9" y este check
        # FIJABA COMO CORRECTO que un folio no numérico viajara como FolioRef de la
        # referencia 52. El folio de una guía —también en papel— es un correlativo
        # numérico del SII: ahora eso BLOQUEA (ver test_aud_paridad_sii, escenario E).
        # Lo que este check protege sigue intacto: el rechazo confirmado no bloquea y el
        # N° de la guía en papel se usa como referencia.
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9350")
        dte_guia(db, desp, status_id=STATUS_FALLIDO, error="rechazada", en_vuelo_desde=None)
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id}).json()
        # CORREGIDO otra vez (fecha de emisión de la guía, 2026-07-30): este check esperaba
        # "2026-06-20", que es la fecha en que el harness CIERRA el despacho. O sea, fijaba
        # como correcto el bug: la referencia 52 salía con la fecha del cierre en vez de la
        # de emisión de la guía. Ahora espera FECHA_GUIA_PAPEL (2026-06-18), que es la que
        # el harness le pone a la guía en papel. Lo que el check protege sigue intacto: el
        # rechazo confirmado no bloquea y el N° de la guía en papel se usa como referencia.
        check("guía FALLIDA sin uuid → NO bloquea y referencia el N° manual",
              p["puede_emitir"] is True
              and p["referencias"][1] == {"tipo": "52", "folio": "9350",
                                          "fecha": FECHA_GUIA_PAPEL.isoformat(),
                                          "descripcion": None},
              (p["problemas"], p["referencias"]))
        limpiar(db, MARK)

        # Despacho SIN guía de ninguna clase → bloquea (jamás un 33 sin la 52)
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual=None)
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id}).json()
        check("despacho sin folio ni N° manual → bloquea pidiendo emitir la guía",
              p["puede_emitir"] is False
              and any("guía" in x and "folio" in x for x in p["problemas"]), p["problemas"])
        limpiar(db, MARK)

        # ═══ 8. Referencia 801: N° y FECHA de OC obligatorios ════════════════════
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_oc="", oc_fecha=None,
                                          numero_guia_manual="G-1")
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id}).json()
        check("sin N° ni FECHA de OC → dos problemas bloqueantes",
              p["puede_emitir"] is False
              and any("N° de OC" in x for x in p["problemas"])
              and any("FECHA" in x for x in p["problemas"]), p["problemas"])
        limpiar(db, MARK)

        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_oc="OC-DEMASIADO-LARGA-123456",
                                          numero_guia_manual="G-1")
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id}).json()
        check("N° de OC > 18 chars → bloquea (el SII lo rechazaría)",
              p["puede_emitir"] is False
              and any("18" in x and "OC" in x for x in p["problemas"]), p["problemas"])
        limpiar(db, MARK)

        # ═══ 9. Bloqueos del modo SII y del receptor ═════════════════════════════
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-1")
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id,
                                                 "numero_factura": "12345"}).json()
        check("folio digitado en modo SII → bloquea (lo asigna el SII)",
              p["puede_emitir"] is False
              and any("folio lo asigna el SII" in x for x in p["problemas"]), p["problemas"])
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id,
                                                 "tipo_doc": "boleta"}).json()
        check("boleta en modo SII → bloquea (el DTE 33 es solo factura)",
              p["puede_emitir"] is False
              and any("DTE 33" in x for x in p["problemas"]), p["problemas"])
        limpiar(db, MARK)

        cot, desp, _i1, _i2 = crear_venta(db, MARK, rut=RUT_INVALIDO,
                                          numero_guia_manual="G-1")
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id}).json()
        check("RUT con DV inválido → bloquea y manda a la FICHA DEL CLIENTE",
              p["puede_emitir"] is False
              and any("ficha del cliente" in x for x in p["problemas"]), p["problemas"])
        limpiar(db, MARK)

        cot, desp, _i1, _i2 = crear_venta(db, MARK, rut=RUT_SIN_FICHA,
                                          numero_guia_manual="G-1")
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id}).json()
        check("cliente sin ficha en Wasabil → bloquea",
              p["puede_emitir"] is False
              and any("no existe en Wasabil" in x for x in p["problemas"]), p["problemas"])
        limpiar(db, MARK)

        # Ficha SIN comuna: en la guía 52 era solo advertencia; en la factura 33
        # BLOQUEA (el SII exige receptor completo y el rechazo consume el documento).
        addr = fake.cliente["addresses"]
        fake.cliente["addresses"] = [{"address": "AV. LAS CONDES 10000", "comuna": None,
                                      "city": "Santiago", "default": True}]
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-1")
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id}).json()
        check("ficha de Wasabil sin comuna → BLOQUEA la factura (asimetría vs. la guía)",
              p["puede_emitir"] is False
              and any("comuna" in x for x in p["problemas"]), p["problemas"])
        fake.cliente["addresses"] = addr
        limpiar(db, MARK)

        # Sin token configurado: se puede previsualizar, jamás emitir.
        fake.configurado = False
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-1")
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id})
        check("sin token: el preview funciona pero avisa",
              r.status_code == 200
              and any("no está configurado" in x for x in r.json()["problemas"]), r.text)
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot.id,
                                                "despacho_id": desp.id})
        check("sin token: emitir 409 y nada sale",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        fake.configurado = True
        limpiar(db, MARK)

        # ═══ 10. IVA POR VENTA (iva_pct congelado ≠ 19) ══════════════════════════
        cot, desp, _i1, _i2 = crear_venta(db, MARK, iva_pct=10.0,
                                          numero_guia_manual="G-1")
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id}).json()
        check("IVA de la venta (10%) gobierna los totales de la 33, no un 19% fijo",
              p["totales"] == {"neto": NETO_DESPACHO, "iva": round(NETO_DESPACHO * 0.10),
                               "bruto": NETO_DESPACHO + round(NETO_DESPACHO * 0.10),
                               "iva_rate": 0.10}, p["totales"])
        limpiar(db, MARK)

        # ═══ 11. Candado de empresa: minería queda fuera del módulo Monza ════════
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-1")
        CURRENT["empresa"] = "mineria"
        check("candado: preview de factura 403",
              client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                   "despacho_id": desp.id}).status_code == 403)
        check("candado: emitir 403",
              client.post(f"{BASE}/emitir", json={"cotizacion_id": cot.id,
                                                  "despacho_id": desp.id}).status_code == 403)
        check("candado: estado-batch 403",
              client.get(f"{BASE}/estado-batch?ids=1").status_code == 403)
        CURRENT["empresa"] = "automotriz"
        limpiar(db, MARK)

    finally:
        limpiar(db, MARK)
        db.close()
        verificar_limpieza(MARK)

    check.finish()


def test_monza_factura_integration():
    run()


if __name__ == "__main__":
    run()
