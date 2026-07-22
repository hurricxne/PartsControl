"""Guard: la factura NO debe referenciar el N° de guía manual viejo mientras el folio
del SII de la guía electrónica todavía viene en camino."""
import sys, os
sys.path.insert(0, os.path.abspath("."))
from database import SessionLocal
from sqlalchemy import text
from models.models import Cotizacion, ItemCotizacion, OcCliente, Despacho
from wasabil_dte.models import WasabilDte, STATUS_PENDIENTE, STATUS_PROCESANDO, STATUS_EMITIDO
from wasabil_dte.router import _guia_electronica_en_proceso, _guia_referencia_de_factura
from types import SimpleNamespace
from datetime import datetime

MARK = "__TEST_GUARD52__"
fails = []
def check(n, c, e=""):
    print(("OK   | " if c else "FAIL | ") + n + ("" if c else f"  -> {e}"))
    if not c: fails.append(n)


def test_guia_en_proceso_no_cae_al_numero_manual():
  db = SessionLocal()
  try:
      cot = Cotizacion(numero=f"{MARK}-C", cliente=f"{MARK}", rut_cliente="78.279.030-7")
      db.add(cot); db.flush()
      oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK[-6:]}", fecha_oc="2026-07-01")
      db.add(oc); db.flush()
      desp = Despacho(numero_despacho=f"{MARK}-D", oc_cliente_id=oc.id, estado="despachado",
                      guia_firmada=1, numero_guia="G-VIEJA-777", fecha_despacho=datetime.now())
      db.add(desp); db.flush(); db.commit()
      fac = SimpleNamespace(despacho_id=desp.id, despacho=desp)

      # 1) sin guía electrónica → usa el N° manual (comportamiento legítimo)
      folio, _ = _guia_referencia_de_factura(db, fac)
      check("1 sin guía electrónica: usa el N° manual", folio == "G-VIEJA-777", folio)

      # 2) guía electrónica EN PROCESO (status 2, con uuid) → NO debe dar el manual
      dte = WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp.id,
                       status_id=STATUS_PROCESANDO, uuid="uuid-en-proceso")
      db.add(dte); db.commit()
      check("2 guía en PROCESO → se detecta", _guia_electronica_en_proceso(db, desp.id) is True)
      folio, _ = _guia_referencia_de_factura(db, fac)
      check("2 guía en PROCESO → NO devuelve el N° manual viejo", folio is None, folio)

      # 3) borrador pendiente en Wasabil (status 6 con uuid) → igual bloquea
      dte.status_id = STATUS_PENDIENTE; db.commit()
      folio, _ = _guia_referencia_de_factura(db, fac)
      check("3 guía PENDIENTE en Wasabil → tampoco cae al manual", folio is None, folio)

      # 4) ya EMITIDA con folio → usa el folio del SII
      dte.status_id = STATUS_EMITIDO; dte.folio = "555"
      dte.payload_json = '{"documentDate": "2026-07-15"}'
      db.commit()
      folio, fecha = _guia_referencia_de_factura(db, fac)
      check("4 guía EMITIDA → usa el folio del SII (no el manual)",
            folio == "555" and str(fecha) == "2026-07-15", (folio, fecha))

      # 5) guía FALLIDA (sin uuid, sin claim) → NO hay guía electrónica: el manual es válido
      dte.status_id = 4; dte.folio = None; dte.uuid = None; dte.en_vuelo_desde = None
      db.commit()
      check("5 guía FALLIDA sin uuid → no bloquea (no existe guía electrónica)",
            _guia_electronica_en_proceso(db, desp.id) is False)
      folio, _ = _guia_referencia_de_factura(db, fac)
      check("5 guía FALLIDA → vuelve a usar el N° manual", folio == "G-VIEJA-777", folio)
  finally:
      db.rollback()
      db.execute(text("DELETE FROM wasabil_dte WHERE despacho_id IN "
                      "(SELECT id FROM despachos WHERE numero_despacho LIKE :m)"), {"m": f"{MARK}%"})
      db.execute(text("DELETE FROM despachos WHERE numero_despacho LIKE :m"), {"m": f"{MARK}%"})
      db.execute(text("DELETE FROM oc_cliente WHERE cotizacion_id IN "
                      "(SELECT id FROM cotizaciones WHERE numero LIKE :m)"), {"m": f"{MARK}%"})
      db.execute(text("DELETE FROM cotizaciones WHERE numero LIKE :m"), {"m": f"{MARK}%"})
      db.commit(); db.close()
  assert not fails, f"{len(fails)} fallos: {fails}"


if __name__ == "__main__":
    test_guia_en_proceso_no_cae_al_numero_manual()
    print("TODO OK")
