"""Fase 6 — ANTI DOBLE EMISIÓN y recuperación de la factura electrónica (DTE 33) de
MonzaParts. Wasabil SIMULADO (JAMÁS el API real).

Un segundo DTE 33 por la misma mercadería es IRREVERSIBLE (solo se deshace con nota
de crédito), así que esta suite ejerce las tres capas del candado y las dos reglas de
la recuperación:

  · Candado de INTENCIÓN por venta (`_emision_33_en_vuelo_de_cot`): el UNIQUE por
    factura_id NO cubre "emitir factura NUEVA" — cada request crearía una factura con
    id distinto. Se prueba secuencialmente Y con dos hilos de verdad.
  · UNIQUE `uq_monza_wasabil_dte_factura` a nivel de BD (última línea de defensa).
  · Claim en vuelo (`en_vuelo_desde`) durante la ventana HTTP.
  · Fallo AMBIGUO (timeout: el documento PUDO nacer) → el claim se queda puesto y el
    reintento VERIFICA en Wasabil antes de re-crear; si no puede verificar, ABORTA.
  · Fallo CLARO (conexión rechazada) → el claim se libera y el reintento re-crea.

Y el invariante de plata de la Fase 6 desde el otro lado: mientras el DTE no está
EMITIDO, el adelanto verificado NO se aplica a la factura ni siquiera cuando
Contabilidad lo verifica retroactivamente — una factura que el SII puede rechazar no
debe haber movido plata.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_factura_concurrencia.py -q
(también:   ./venv/bin/python monza_wasabil_dte/tests/test_factura_concurrencia.py)
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy.exc import IntegrityError  # noqa: E402

from database import SessionLocal  # noqa: E402
from monza_contabilidad.models import MonzaContFacturaCliente  # noqa: E402
from monza_wasabil_dte import client as monza_client  # noqa: E402
from monza_wasabil_dte.models import MonzaWasabilDte, STATUS_PENDIENTE  # noqa: E402
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    Checker, FakeWasabil, cobranzas_de, crear_venta, despacho_extra, dte_de_factura,
    facturas_de, limpiar, montar_app, verificar_limpieza,
)

# MARK corto: MonzaCotizacion.numero es String(20) (ver crear_venta).
MARK = "__MWF33B__"
CURRENT = {"empresa": "automotriz", "id": None}

client = montar_app(CURRENT)
fake = FakeWasabil(MARK)
fake.install()
check = Checker()

BASE = "/api/monza/wasabil/facturas"
CONTAB = "/api/monza/contabilidad"


def _emitir(cot_id, desp_id):
    return client.post(f"{BASE}/emitir", json={"cotizacion_id": cot_id,
                                               "despacho_id": desp_id})


def run():
    db = SessionLocal()
    fake.install()   # re-instalar: si pytest importó otras suites, la última ganó
    limpiar(db, MARK)
    try:
        CURRENT["empresa"] = "automotriz"

        # ═══ 1. FALLO AMBIGUO (timeout): el claim se queda puesto ════════════════
        cot, desp, it1, _i2 = crear_venta(db, MARK, numero_guia_manual="9101",
                                          pct_adelanto=50)
        fake.crear_falla = monza_client.WasabilError("timeout simulado", ambiguo=True)
        creados_antes = len(fake.creados)
        r = _emitir(cot.id, desp.id)
        check("fallo ambiguo → 502", r.status_code == 502, r.text)
        db.rollback()  # cierra el snapshot del test para ver los commits del router
        facturas = facturas_de(db, cot.id)
        check("la factura local SÍ quedó creada (el ancla se commitea ANTES del HTTP)",
              len(facturas) == 1, facturas)
        factura_id = facturas[0].id
        fila = dte_de_factura(db, factura_id)
        check("DTE con error, SIN uuid y con el claim puesto",
              fila is not None and fila.uuid is None
              and "timeout" in (fila.error or "") and fila.en_vuelo_desde is not None,
              fila and (fila.uuid, fila.error, fila.en_vuelo_desde))
        fake.crear_falla = None

        # Candado de INTENCIÓN por VENTA: el segundo "emitir factura nueva" no puede
        # pasar aunque crearía una factura con otro id (el UNIQUE no lo cubriría).
        # Se usa un SEGUNDO despacho de la misma venta a propósito: con el mismo
        # despacho el tope "ya fue facturado por completo" frenaría antes y el candado
        # nunca se ejercitaría (es el candado el que se está probando, no el tope).
        desp_b = despacho_extra(db, MARK, cot, {it1.id: 6}, numero_guia="9102")
        r = _emitir(cot.id, desp_b.id)
        check("claim fresco → emitir OTRA factura de la MISMA venta 409 'EN CURSO'",
              r.status_code == 409 and "EN CURSO" in r.json()["detail"], r.text)
        db.rollback()
        check("el emitir bloqueado NO dejó una segunda factura",
              len(facturas_de(db, cot.id)) == 1, facturas_de(db, cot.id))
        check("nada llegó a Wasabil en toda la ventana del claim",
              len(fake.creados) == creados_antes, fake.creados[creados_antes:])
        r = client.post(f"{BASE}/{factura_id}/reintentar")
        check("claim fresco → reintentar 409 'EN CURSO'",
              r.status_code == 409 and "EN CURSO" in r.json()["detail"], r.text)

        # ═══ 2. INVARIANTE DE PLATA: el adelanto NO cae en una factura sin emitir ══
        # Contabilidad verifica el adelanto AHORA (aplicación retroactiva a todas las
        # facturas de la venta). La factura existe y tiene saldo íntegro, pero su DTE
        # no está emitido: sin el guard quedaría 'pagada' un documento que el SII
        # todavía puede rechazar (y encima bloquearía su borrado por "tiene cobranzas").
        r = client.post(f"{CONTAB}/ventas/{cot.id}/adelanto/verificar",
                        json={"monto": 50000, "fecha_pago": "2026-06-15", "banco": "BCI"})
        check("verificar adelanto 200", r.status_code == 200, r.text)
        db.rollback()
        check("ADELANTO NO APLICADO mientras el DTE no está emitido",
              len(cobranzas_de(db, factura_id)) == 0, cobranzas_de(db, factura_id))
        fac = db.get(MonzaContFacturaCliente, factura_id)
        check("la factura sin folio conserva su saldo íntegro",
              float(fac.monto_pagado) == 0 and float(fac.saldo) == float(fac.monto_bruto),
              (fac.monto_pagado, fac.saldo))

        # ═══ 3. UNIQUE por factura_id (última línea de defensa, a nivel de BD) ════
        try:
            db.add(MonzaWasabilDte(empresa="automotriz", tipo_dte=33,
                                   factura_id=factura_id, status_id=STATUS_PENDIENTE))
            db.commit()
            check("UNIQUE uq_monza_wasabil_dte_factura rechaza el DTE gemelo", False,
                  "el INSERT duplicado pasó: el UNIQUE no está en la BD "
                  "(¿falta correr monza_wasabil_dte/init_db.py?)")
        except IntegrityError:
            db.rollback()
            check("UNIQUE uq_monza_wasabil_dte_factura rechaza el DTE gemelo", True)

        # ═══ 4. Claim vencido + documento PERDIDO en Wasabil → se ADOPTA ══════════
        # El claim expira solo. El reintento busca la referencia interna FACT-<id> y,
        # si el documento existe, lo adopta con SU folio real en vez de re-crear.
        fila = dte_de_factura(db, factura_id)
        fila.en_vuelo_desde = datetime.utcnow() - timedelta(seconds=600)
        db.commit()
        fake.docs_buscables = [{"uuid": "uuid-perdido", "status_id": 3, "folio": "8888",
                                "invoice_reference": f"FACT-{factura_id}",
                                "document_pdf_url": "https://api.wasabil.com/pdf/8888"}]
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/{factura_id}/reintentar")
        check("reintento ADOPTA el documento perdido (match EXACTO) y NO re-crea",
              r.status_code == 200 and r.json()["folio"] == "8888"
              and len(fake.creados) == creados_antes, r.text)
        db.rollback()
        fac = db.get(MonzaContFacturaCliente, factura_id)
        check("el folio ADOPTADO se escribe en numero_factura",
              fac.numero_factura == "8888", fac.numero_factura)
        cobs = cobranzas_de(db, factura_id)
        check("el adelanto diferido cae RECIÉN al adoptarse el documento emitido",
              len(cobs) == 1 and cobs[0].medio == "adelanto" and float(cobs[0].monto) == 50000,
              [(c.medio, float(c.monto)) for c in cobs])
        fake.docs_buscables = []
        limpiar(db, MARK)

        # ═══ 5. Referencia AJENA no se adopta: se re-crea tras confirmar la búsqueda ══
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9101")
        fake.crear_falla = monza_client.WasabilError("conexión rechazada", ambiguo=False)
        _emitir(cot.id, desp.id)
        fake.crear_falla = None
        db.rollback()
        factura_id = facturas_de(db, cot.id)[0].id
        fila = dte_de_factura(db, factura_id)
        check("fallo NO ambiguo (seguro que no se creó) → claim LIBERADO",
              fila is not None and fila.en_vuelo_desde is None, fila and fila.en_vuelo_desde)
        fake.docs_buscables = [{"uuid": "uuid-ajeno", "status_id": 3, "folio": "9999",
                                "invoice_reference": f"FACT-{factura_id + 987654}"}]
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/{factura_id}/reintentar")
        check("documento de OTRA factura no se adopta: se re-crea (delta 1)",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1
              and r.json()["folio"] != "9999", r.text)
        fake.docs_buscables = []
        limpiar(db, MARK)

        # ═══ 6. El reintento NUNCA re-crea a ciegas ══════════════════════════════
        # (a) La verificación en Wasabil se cae → 502, sin re-crear.
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9101")
        fake.crear_falla = monza_client.WasabilError("conexión rechazada", ambiguo=False)
        _emitir(cot.id, desp.id)
        fake.crear_falla = None
        db.rollback()
        factura_id = facturas_de(db, cot.id)[0].id
        creados_antes = len(fake.creados)
        _orig_buscar = monza_client.buscar_documentos
        monza_client.buscar_documentos = lambda s: (_ for _ in ()).throw(
            monza_client.WasabilError("red caída"))
        r = client.post(f"{BASE}/{factura_id}/reintentar")
        check("verificación caída → 502 SIN re-crear",
              r.status_code == 502 and len(fake.creados) == creados_antes, r.text)
        monza_client.buscar_documentos = _orig_buscar
        fake.install()   # restaura el buscar_documentos del FAKE, nunca el real

        # (b) La búsqueda quedó truncada por paginación: "no lo encontré" NO prueba que
        #     no exista — re-emitir aquí arriesga una segunda factura legal.
        fake.busqueda_completa = False
        r = client.post(f"{BASE}/{factura_id}/reintentar")
        check("búsqueda paginada incompleta → 502 SIN re-crear",
              r.status_code == 502 and "incompleta" in r.json()["detail"]
              and len(fake.creados) == creados_antes, r.text)
        fake.busqueda_completa = True

        # (c) Confirmado que no existe (búsqueda completa y vacía) → recién ahí re-crea.
        r = client.post(f"{BASE}/{factura_id}/reintentar")
        check("búsqueda completa y vacía → SÍ re-crea (delta 1)",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
        limpiar(db, MARK)

        # (d) CON uuid pero sin poder consultar su estado → 502, tampoco re-crea.
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9101")
        _emitir(cot.id, desp.id)          # queda "procesando" con uuid
        db.rollback()
        factura_id = facturas_de(db, cot.id)[0].id
        creados_antes = len(fake.creados)
        fila = dte_de_factura(db, factura_id)
        fila.en_vuelo_desde = None        # claim vencido: el reintento es alcanzable
        db.commit()
        _orig_estado = monza_client.estado_documento
        monza_client.estado_documento = lambda u: (_ for _ in ()).throw(
            monza_client.WasabilError("respuesta inesperada", ambiguo=True))
        r = client.post(f"{BASE}/{factura_id}/reintentar")
        check("con uuid y estado no verificable → 502 SIN re-crear",
              r.status_code == 502 and len(fake.creados) == creados_antes, r.text)
        # El sondeo, en cambio, DEGRADA en vez de romper: devuelve 200 con error_consulta.
        r = client.get(f"{BASE}/{factura_id}/estado")
        check("el sondeo degrada elegante (200 con error_consulta, no 500)",
              r.status_code == 200 and r.json().get("error_consulta")
              and r.json()["estado"] != "emitido", r.text)
        monza_client.estado_documento = _orig_estado
        fake.install()
        limpiar(db, MARK)

        # ═══ 7. CARRERA REAL: dos hilos emitiendo la MISMA venta ═════════════════
        # Es la prueba del `db.rollback()` ANTES del `with_for_update()` en
        # emitir_factura_sii: `_preparar_emision_factura` abre el snapshot (SELECTs +
        # el HTTP de la ficha del cliente) ANTES del lock; bajo REPEATABLE READ, sin
        # ese rollback la re-validación no vería la factura/claim que el request gemelo
        # acaba de commitear y saldrían DOS documentos reales al SII.
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9101")
        # IDs como enteros ANTES de cerrar: los hilos no pueden tocar la sesión del
        # test (una Session de SQLAlchemy no es thread-safe), y tras el close los
        # objetos quedan detached.
        cot_id, desp_id = cot.id, desp.id
        db.close()   # el test no debe retener locks mientras corren los hilos
        creados_antes = len(fake.creados)
        # Ensancha la ventana HTTP del ganador para que el perdedor llegue al lock
        # mientras el claim ya está commiteado pero la respuesta aún no vuelve.
        fake.antes_de_crear = lambda payload: time.sleep(0.4)
        barrera = threading.Barrier(2)
        resultados: list = []

        def _correr():
            barrera.wait()
            try:
                r = _emitir(cot_id, desp_id)
                resultados.append((r.status_code, r.text[:200]))
            except Exception as exc:                     # pragma: no cover
                resultados.append((-1, repr(exc)[:200]))

        hilos = [threading.Thread(target=_correr) for _ in range(2)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=90)
        fake.antes_de_crear = None
        codigos = sorted(c for c, _t in resultados)
        # El perdedor sale con 409: o lo frena el candado de intención ("EN CURSO"), o
        # —si el ganador ya cerró su transacción— el tope "ya fue facturado por
        # completo". Nunca un 200 ni un 500.
        check("carrera: exactamente UN 200 y UN 409 (nunca dos emisiones)",
              codigos == [200, 409], resultados)
        # Que el rechazo sea el del CANDADO prueba que los hilos se solaparon de verdad
        # (si el perdedor hubiera llegado después del cierre del ganador, el mensaje
        # sería el del tope y el test no estaría ejerciendo la carrera).
        check("carrera: al perdedor lo frena el CANDADO DE INTENCIÓN por venta",
              any("EN CURSO" in t for c, t in resultados if c == 409), resultados)
        check("carrera: UN SOLO documento salió a Wasabil",
              len(fake.creados) == creados_antes + 1,
              [x.get("invoice_reference") for x in fake.creados[creados_antes:]])
        db = SessionLocal()
        check("carrera: UNA SOLA factura local persistida (el perdedor no deja zombi)",
              len(facturas_de(db, cot_id)) == 1, facturas_de(db, cot_id))

    finally:
        limpiar(db, MARK)
        db.close()
        verificar_limpieza(MARK)

    check.finish()


def test_monza_factura_concurrencia():
    run()


if __name__ == "__main__":
    run()
