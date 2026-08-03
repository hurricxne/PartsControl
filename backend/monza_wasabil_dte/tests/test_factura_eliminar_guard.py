"""Fase 6 — BORRADO de una factura con emisión electrónica (DTE 33) de MonzaParts.

╔══════════════════════════════════════════════════════════════════════════════╗
║ ESTA SUITE ES LA ESPECIFICACIÓN DE UN GUARD QUE HOY NO EXISTE.               ║
║                                                                              ║
║ `eliminar_factura` (monza_contabilidad/router.py) no mira el módulo DTE. Se  ║
║ verificó empíricamente contra la BD local: borrar una factura que ya tiene   ║
║ fila `monza_wasabil_dte` revienta con                                        ║
║   IntegrityError (1451) fk_monza_wasabil_dte_factura_id                      ║
║ → 500 en producción. La FK es RESTRICT (sin ON DELETE), igual que en GA.     ║
║                                                                              ║
║ Falta portar el bloque de Grupo AM (routers/contabilidad.py:2212-2246):      ║
║   · DTE EMITIDO            → 409 «anúlala primero en Wasabil»                ║
║   · claim vigente / uuid en estado 2|6 → 409 «emisión en curso»              ║
║   · uuid IS NULL AND en_vuelo_desde IS NOT NULL (AMBIGUO, claim vencido)     ║
║       → 409 a propósito: la respuesta se perdió y el documento PUDO nacer    ║
║         con folio real; borrar el ancla FACT-<id> lo vuelve INADOPTABLE y    ║
║         libera el cupo para una SEGUNDA emisión. La salida es Reintentar.    ║
║   · fallido CONFIRMADO / que nunca llegó a Wasabil → se PERMITE borrar, y    ║
║       hay que hacer `db.delete(dte)` junto con la factura o el DELETE choca. ║
╚══════════════════════════════════════════════════════════════════════════════╝

Mientras el guard no exista, esta suite queda EN ROJO y señala exactamente qué
falta. No la borres para poner la batería en verde: el rojo es el hallazgo.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_factura_eliminar_guard.py -q
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import SessionLocal  # noqa: E402
from monza_contabilidad.models import MonzaContFacturaCliente  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    CLAIM_TTL_SEGUNDOS, MonzaWasabilDte, STATUS_EMITIDO, STATUS_FALLIDO,
    STATUS_PENDIENTE, STATUS_PROCESANDO,
)
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    Checker, FakeWasabil, crear_venta, dte_de_factura, facturas_de, limpiar,
    montar_app, verificar_limpieza,
)

# MARK corto: MonzaCotizacion.numero es String(20) (ver crear_venta).
MARK = "__MWF33C__"
CURRENT = {"empresa": "automotriz", "id": None}

client = montar_app(CURRENT)
fake = FakeWasabil(MARK)
fake.install()
check = Checker()

BASE = "/api/monza/wasabil/facturas"
CONTAB = "/api/monza/contabilidad"


def _delete(factura_id):
    """DELETE que NO tumba la suite si el endpoint revienta.

    TestClient re-lanza las excepciones del server, y hoy este endpoint lanza
    IntegrityError. Se captura para que cada caso quede reportado como un check
    fallido (con la causa a la vista) en vez de abortar toda la corrida."""
    try:
        r = client.delete(f"{CONTAB}/facturas/{factura_id}")
        try:
            detalle = r.json().get("detail", r.text)
        except ValueError:
            detalle = r.text
        return r.status_code, str(detalle)
    except Exception as exc:                       # noqa: BLE001 (es el punto del test)
        return 500, f"EXCEPCIÓN DEL SERVER: {type(exc).__name__}: {exc}"[:400]


def _factura_con_dte(db, **kw_dte):
    """Factura por la VÍA MANUAL (folio digitado) + una fila DTE 33 fabricada en el
    estado que se quiere probar. La vía manual evita depender de la emisión simulada
    y deja el escenario legible: lo que se prueba es el BORRADO, no la emisión."""
    cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-1")
    r = client.post(f"{CONTAB}/facturas", json={
        "cotizacion_id": cot.id, "despacho_id": desp.id,
        "numero_factura": f"{MARK[-6:]}-{desp.id}"})
    assert r.status_code == 200, r.text
    factura_id = r.json()["id"]
    if kw_dte is not None:
        base = dict(empresa="automotriz", tipo_dte=33, factura_id=factura_id,
                    uuid=None, status_id=None, en_vuelo_desde=None, folio=None, error=None)
        base.update(kw_dte)
        db.add(MonzaWasabilDte(**base))
        db.commit()
    return cot, factura_id


def run():
    db = SessionLocal()
    fake.install()
    limpiar(db, MARK)
    try:
        CURRENT["empresa"] = "automotriz"

        # ── 0. CONTROL: sin DTE el borrado sigue funcionando como siempre ────────
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-1")
        r = client.post(f"{CONTAB}/facturas", json={
            "cotizacion_id": cot.id, "despacho_id": desp.id,
            "numero_factura": f"CTRL-{desp.id}"})
        check("control: factura manual creada", r.status_code == 200, r.text)
        codigo, detalle = _delete(r.json()["id"])
        check("control: factura SIN emisión electrónica se borra igual que siempre",
              codigo == 200, (codigo, detalle))
        limpiar(db, MARK)

        # ── 1. DTE EMITIDO → 409 (el documento vive ante el SII) ─────────────────
        _cot, factura_id = _factura_con_dte(
            db, uuid="uuid-emitida", status_id=STATUS_EMITIDO, folio="9001")
        codigo, detalle = _delete(factura_id)
        check("DTE EMITIDO → 409 (no se borra una factura viva ante el SII)",
              codigo == 409, (codigo, detalle))
        check("DTE EMITIDO → el mensaje manda a anularla en Wasabil",
              "Wasabil" in detalle or "anul" in detalle.lower(), detalle)
        db.rollback()
        check("DTE EMITIDO → la factura sigue existiendo",
              db.get(MonzaContFacturaCliente, factura_id) is not None)
        limpiar(db, MARK)

        # ── 2. Claim VIGENTE (emisión en vuelo) → 409 ────────────────────────────
        _cot, factura_id = _factura_con_dte(db, en_vuelo_desde=datetime.utcnow())
        codigo, detalle = _delete(factura_id)
        check("claim VIGENTE → 409 (hay una emisión en curso)", codigo == 409,
              (codigo, detalle))
        limpiar(db, MARK)

        # ── 3. uuid en PROCESO (2) o BORRADOR (6) en Wasabil → 409 ───────────────
        for etiqueta, status in (("procesando (2)", STATUS_PROCESANDO),
                                 ("borrador en Wasabil (6)", STATUS_PENDIENTE)):
            _cot, factura_id = _factura_con_dte(db, uuid="uuid-vivo", status_id=status)
            codigo, detalle = _delete(factura_id)
            check(f"uuid {etiqueta} → 409 (resolver en Wasabil antes de borrar)",
                  codigo == 409, (codigo, detalle))
            limpiar(db, MARK)

        # ── 4. AMBIGUO: sin uuid pero con claim (vencido) → 409 A PROPÓSITO ──────
        # Parece un bug («no puedo borrar una factura que nunca se emitió») y ya fue
        # reportado como tal en Grupo AM. NO lo "arregles" por comodidad: la respuesta
        # se perdió, el documento pudo nacer con folio real, y borrar el ancla
        # FACT-<id> lo vuelve inadoptable y habilita un SEGUNDO DTE por la misma
        # mercadería. La salida correcta es Reintentar, que consulta a Wasabil.
        _cot, factura_id = _factura_con_dte(
            db, error="timeout simulado",
            en_vuelo_desde=datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60))
        codigo, detalle = _delete(factura_id)
        check("AMBIGUO (uuid None + en_vuelo_desde no nulo) → 409: imborrable adrede",
              codigo == 409, (codigo, detalle))
        check("AMBIGUO → el mensaje ofrece Reintentar como salida",
              "eintent" in detalle, detalle)
        db.rollback()
        check("AMBIGUO → la factura y su ancla siguen vivas",
              db.get(MonzaContFacturaCliente, factura_id) is not None
              and dte_de_factura(db, factura_id) is not None)
        limpiar(db, MARK)

        # ── 5. CONFIRMADO que nunca llegó a Wasabil → SÍ se puede borrar ─────────
        # (uuid None, sin claim, con error registrado: el caso simétrico. Si esto no
        # se permitiera, la factura quedaría imborrable para siempre y secuestraría el
        # cupo facturable de la mercadería.)
        cot, factura_id = _factura_con_dte(
            db, error="conexión rechazada", en_vuelo_desde=None)
        codigo, detalle = _delete(factura_id)
        check("intento que NUNCA llegó a Wasabil → 200 (no secuestra el cupo)",
              codigo == 200, (codigo, detalle))
        db.rollback()
        check("al borrar la factura se borra TAMBIÉN su fila DTE (FK RESTRICT)",
              dte_de_factura(db, factura_id) is None
              and db.get(MonzaContFacturaCliente, factura_id) is None)
        check("borrada la factura, la mercadería vuelve a estar facturable",
              len(facturas_de(db, cot.id)) == 0, facturas_de(db, cot.id))
        limpiar(db, MARK)

        # ── 6. DTE FALLIDO en el SII (rechazado, con uuid) → SE PUEDE BORRAR ────
        # Espejo de GA: solo bloquean los estados VIVOS (emitido, procesando,
        # pendiente, claim vigente). Un rechazo del SII no genera folio ni documento
        # tributario válido; bloquearlo dejaba la factura imborrable PARA SIEMPRE
        # consumiendo el cupo facturable de la venta, y la única salida ofrecida
        # ("Reintentar") reenvía el MISMO payload que el SII ya rechazó
        # (hallazgo del multienjambre F6, 2026-07-29).
        _cot, factura_id = _factura_con_dte(
            db, uuid="uuid-rechazada", status_id=STATUS_FALLIDO,
            error="RUT del receptor no autorizado")
        codigo, detalle = _delete(factura_id)
        check("DTE FALLIDO (rechazado por el SII) → se puede borrar y re-facturar",
              codigo == 200, (codigo, detalle))
        check("el ancla DTE del rechazo se limpia con la factura",
              db.query(MonzaWasabilDte).filter(
                  MonzaWasabilDte.factura_id == factura_id).first() is None)
        limpiar(db, MARK)

    finally:
        limpiar(db, MARK)
        db.close()
        verificar_limpieza(MARK)

    check.finish()


def test_monza_factura_eliminar_guard():
    run()


if __name__ == "__main__":
    run()
