"""Regresión de la AUDITORÍA INTEGRAL Fases 1-6 de MonzaParts — reparador R3 (contabilidad).

Un bloque por hallazgo reparado, con el escenario numérico del auditor:

  #5  (MEDIUM, punto 3) iva_rate_de deja de ambiguar «sin dato» vs «0 explícito» y loguea
      el id de la cotización enferma. El RESULTADO no cambia (sigue IVA_DEFAULT): lo que
      se prueba es que la traza permita identificar qué venta reparar.
  #6  (MEDIUM) una cobranza MANUAL ya no entra en una factura electrónica que el SII
      todavía no conoce (guard espejo del de _aplicar_adelanto).
  #7  (MEDIUM) numero_guia se sirve EN VIVO desde el despacho: cuando el SII confirma el
      folio de la guía 52 y pisa el N° tecleado a mano, la factura deja de mostrar la
      guía vieja para siempre.
  #15 (LOW) la holgura de 1 CLP (TOL_PAGO) queda FIJADA como comportamiento deliberado
      (documentada en docs/regla-lecturas-de-plata.md): 1 peso de sobrepago pasa, 2 no.
  #16 (LOW) facturar por la vía manual un despacho SIN N° de guía deja advertencia.
  #19 (LOW) el docstring de _bloqueo_dte_factura prometía un bloqueo que el código no
      hace: se fija el comportamiento REAL (un DTE rechazado no deja la factura
      imborrable) para que nadie «arregle» el código hacia el texto viejo.
      · CORREGIDO (re-refutación ALTO-3): la otra mitad del invariante estaba fijada AL
        REVÉS —«el ancla DTE se borró con la factura»— y medida con una condición ciega
        (`factura_id == fid`, que se cumple igual si la fila se conserva desligada). Con
        uuid el documento EXISTE en Wasabil: la factura se borra, el ANCLA no. Ahora se
        mide por el id de la fila, con hermanos para el caso sin uuid y para el estado
        que no permite concluir.

Todo lo que crea lo borra al terminar (deja la BD intacta). Sin datos reales y sin una
sola llamada al API de Wasabil: los estados del SII se simulan escribiendo la fila
monza_wasabil_dte a mano, que es justo lo que el guard lee.

Corre con:  cd backend && ./venv/bin/python monza_contabilidad/tests/test_aud_guards.py
       o:   cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_aud_guards.py -q
"""
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.router import router, _construir_factura, _cargar_venta  # noqa: E402
from monza_contabilidad.schemas import FacturaCreate  # noqa: E402
from monza_contabilidad.service import iva_rate_de, IVA_DEFAULT  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContCobranza, MonzaContFactoring,
    MonzaContFacturaClienteItem, MonzaContAdelanto,
)

MARK = "__TEST_AUDG__"
CURRENT = {"empresa": "automotriz", "id": 1}

app = FastAPI()
app.include_router(router)


def _cu(db: Session = Depends(get_db)):
    # Lectura en la MISMA sesión del request (abre el read view antes de los locks),
    # igual que auth.get_current_user en producción — mismo patrón que test_integration.
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []
_seed = {}
_dte_ids = []
_factura_ids = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Acceso al módulo DTE: si la tabla no está migrada en esta BD, los bloques que la
# necesitan se SALTAN en vez de fallar (el módulo es opcional en un entorno a medias).
def _dte_disponible() -> bool:
    db = SessionLocal()
    try:
        from monza_wasabil_dte.models import MonzaWasabilDte  # noqa: F401
        db.execute(text("SELECT 1 FROM monza_wasabil_dte LIMIT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"SKIP | módulo monza_wasabil_dte no disponible en esta BD: {e}")
        return False
    finally:
        db.close()


def _crear_dte(factura_id: int, *, status_id, uuid=None, folio=None):
    """Fila monza_wasabil_dte a mano = estado del SII simulado (jamás se llama a Wasabil)."""
    from monza_wasabil_dte.models import MonzaWasabilDte
    db = SessionLocal()
    try:
        d = MonzaWasabilDte(empresa="automotriz", tipo_dte=33, factura_id=factura_id,
                            uuid=uuid, status_id=status_id, folio=folio)
        db.add(d)
        db.commit()
        _dte_ids.append(d.id)
        return d.id
    finally:
        db.close()


def _factura_electronica(cot_id: int, bruto: float, *, tipo_doc="factura", folio=None) -> int:
    """Factura persistida SIN folio, tal como la deja la vía SII antes de que llegue el
    folio (la vía manual exige folio, así que no se puede crear por HTTP)."""
    db = SessionLocal()
    try:
        f = MonzaContFacturaCliente(
            cotizacion_id=cot_id, numero_cotizacion=f"{MARK}-COT2",
            cliente_nombre=f"{MARK} Cliente", rut_cliente="11.111.111-1",
            numero_factura=folio, tipo_doc=tipo_doc,
            monto_neto=round(bruto / 1.19, 2), iva=round(bruto - bruto / 1.19, 2),
            monto_bruto=bruto, monto_pagado=0, saldo=bruto, estado_pago="por_cobrar",
        )
        db.add(f)
        db.commit()
        _factura_ids.append(f.id)
        return f.id
    finally:
        db.close()


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
        # Venta 1: 10 × 100.000 = 1.000.000 neto / 1.190.000 bruto. Guía con N° MANUAL.
        cot = mm.MonzaCotizacion(
            numero=f"{MARK}-COT", cliente_id=cli.id, estado="vendida",
            total_neto=1000000, iva_monto=190000, total_bruto=1190000, iva_pct=19,
            forma_pago="credito", oc_cliente="OC-AUDG",
        )
        db.add(cot); db.flush()
        item = mm.MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion="Filtro", numero_parte="FP-AUDG",
            cantidad=10, precio_unitario_clp=100000, subtotal_clp=1000000,
            estado_linea="despachado",
        )
        db.add(item); db.flush()
        desp = mm.MonzaDespacho(
            numero=f"{MARK}-DSP", cotizacion_id=cot.id, estado="despachado",
            numero_guia="GD-VIEJA", cliente_nombre=cli.nombre,
            guia_firmada=1,  # regla 2026-08-06: sin firma no se factura (gate con suite propia)
        )
        db.add(desp); db.flush()
        di = mm.MonzaDespachoItem(despacho_id=desp.id, item_id=item.id, qty_despachada=10)
        db.add(di); db.flush()
        # Venta 2: despacho SIN N° de guía (hallazgo #16) + facturas electrónicas (#6/#19).
        cot2 = mm.MonzaCotizacion(
            numero=f"{MARK}-COT2", cliente_id=cli.id, estado="vendida",
            total_neto=1000000, iva_monto=190000, total_bruto=1190000, iva_pct=19,
            forma_pago="credito",
        )
        db.add(cot2); db.flush()
        item2 = mm.MonzaCotizacionItem(
            cotizacion_id=cot2.id, descripcion="Bujía", numero_parte="BJ-AUDG",
            cantidad=10, precio_unitario_clp=100000, subtotal_clp=1000000,
            estado_linea="despachado",
        )
        db.add(item2); db.flush()
        desp2 = mm.MonzaDespacho(
            numero=f"{MARK}-DSP2", cotizacion_id=cot2.id, estado="despachado",
            numero_guia=None, cliente_nombre=cli.nombre,
            guia_firmada=1,  # regla 2026-08-06: sin firma no se factura (gate con suite propia)
        )
        db.add(desp2); db.flush()
        di2 = mm.MonzaDespachoItem(despacho_id=desp2.id, item_id=item2.id, qty_despachada=10)
        db.add(di2); db.flush()
        db.commit()
        _seed.update(cli_id=cli.id, cot_id=cot.id, item_id=item.id, desp_id=desp.id,
                     di_id=di.id, cot2_id=cot2.id, item2_id=item2.id, desp2_id=desp2.id,
                     di2_id=di2.id)
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        # ORDEN: el ancla DTE tiene FK RESTRICT a la factura → primero los DTE.
        try:
            from monza_wasabil_dte.models import MonzaWasabilDte
            if _dte_ids:
                db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id.in_(_dte_ids)).delete(
                    synchronize_session=False)
        except Exception:  # noqa: BLE001 — módulo/tabla ausente: nada que limpiar
            pass
        cot_ids = [v for k, v in _seed.items() if k in ("cot_id", "cot2_id") and v]
        if cot_ids:
            fids = [f.id for f in db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all()]
            fids = list(set(fids) | set(_factura_ids))
            if fids:
                db.query(MonzaContCobranza).filter(
                    MonzaContCobranza.factura_id.in_(fids)).delete(synchronize_session=False)
                db.query(MonzaContFactoring).filter(
                    MonzaContFactoring.factura_id.in_(fids)).delete(synchronize_session=False)
                db.query(MonzaContFacturaClienteItem).filter(
                    MonzaContFacturaClienteItem.factura_id.in_(fids)).delete(synchronize_session=False)
                db.query(MonzaContFacturaCliente).filter(
                    MonzaContFacturaCliente.id.in_(fids)).delete(synchronize_session=False)
            db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        for desp_key, item_key in (("desp_id", "item_id"), ("desp2_id", "item2_id")):
            if _seed.get(desp_key):
                db.query(mm.MonzaDespachoItem).filter(
                    mm.MonzaDespachoItem.despacho_id == _seed[desp_key]).delete(
                    synchronize_session=False)
                db.query(mm.MonzaDespacho).filter(
                    mm.MonzaDespacho.id == _seed[desp_key]).delete(synchronize_session=False)
            if _seed.get(item_key):
                db.query(mm.MonzaCotizacionItem).filter(
                    mm.MonzaCotizacionItem.id == _seed[item_key]).delete(synchronize_session=False)
        if cot_ids:
            db.query(mm.MonzaCotizacion).filter(
                mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        if _seed.get("cli_id"):
            db.query(mm.MonzaCliente).filter(
                mm.MonzaCliente.id == _seed["cli_id"]).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


class _CapturaLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.mensajes = []

    def emit(self, record):
        self.mensajes.append(record.getMessage())


def run():
    cot_id, cot2_id = _seed["cot_id"], _seed["cot2_id"]

    # ── #5 (punto 3): iva_rate_de distingue SIN DATO de 0 EXPLÍCITO y nombra la venta ──
    log = logging.getLogger("monza_contabilidad")
    cap = _CapturaLog()
    log.addHandler(cap)
    try:
        cap.mensajes.clear()
        r0 = iva_rate_de(SimpleNamespace(id=4242, iva_pct=0), SimpleNamespace(iva_pct=0))
        msg0 = " ".join(cap.mensajes)
        check("#5 iva 0 explícito -> IVA_DEFAULT (no exento)", r0 == IVA_DEFAULT, r0)
        check("#5 iva 0 explícito -> log dice INVÁLIDO y trae el id de la venta",
              "INVÁLIDO" in msg0 and "4242" in msg0, msg0)
        cap.mensajes.clear()
        rn = iva_rate_de(SimpleNamespace(id=99, iva_pct=None), None)
        msgn = " ".join(cap.mensajes)
        check("#5 sin dato -> IVA_DEFAULT", rn == IVA_DEFAULT, rn)
        check("#5 sin dato -> log dice SIN DATO (caso distinto)",
              "SIN DATO" in msgn and "INVÁLIDO" not in msgn, msgn)
        cap.mensajes.clear()
        check("#5 iva 19 normal -> 0.19 sin warnings",
              iva_rate_de(SimpleNamespace(id=1, iva_pct=19), None) == 0.19 and not cap.mensajes,
              cap.mensajes)
        # Cadena de resolución INTACTA: la venta sin iva_pct sigue cayendo a la config.
        check("#5 venta sin iva_pct cae a la config (comportamiento previo intacto)",
              iva_rate_de(SimpleNamespace(id=2, iva_pct=None), SimpleNamespace(iva_pct=19)) == 0.19)
    finally:
        log.removeHandler(cap)

    # ── #16: factura manual de un despacho SIN N° de guía deja ADVERTENCIA ──
    db = SessionLocal()
    try:
        cot2 = _cargar_venta(db, cot2_id, lock=False)
        datos = _construir_factura(
            db, FacturaCreate(cotizacion_id=cot2_id, despacho_id=_seed["desp2_id"]),
            cot2, acumular=True)
        adv = " ".join(datos["advertencias"])
        check("#16 despacho sin N° de guía -> advertencia",
              "no tiene N° de guía" in adv, datos["advertencias"])
        check("#16 la advertencia NO bloquea la factura", not datos["problemas"], datos["problemas"])
        cot1 = _cargar_venta(db, cot_id, lock=False)
        datos_ok = _construir_factura(
            db, FacturaCreate(cotizacion_id=cot_id, despacho_id=_seed["desp_id"]),
            cot1, acumular=True)
        check("#16 despacho CON guía -> sin advertencia", not datos_ok["advertencias"],
              datos_ok["advertencias"])
    finally:
        db.close()

    # ── #7: numero_guia VIVO (el folio del SII pisa el N° manual después de facturar) ──
    rf = client.post("/api/monza/contabilidad/facturas",
                     json={"cotizacion_id": cot_id, "despacho_id": _seed["desp_id"],
                           "numero_factura": f"{MARK}-F1", "plazo_dias": 30})
    check("#7 factura manual sobre la guía 'GD-VIEJA' 200", rf.status_code == 200, rf.text)
    f1 = rf.json() if rf.status_code == 200 else {}
    if f1.get("id"):
        _factura_ids.append(f1["id"])
    check("#7 al emitir muestra la guía vigente 'GD-VIEJA'", f1.get("numero_guia") == "GD-VIEJA", f1)
    # El SII confirma el folio de la guía 52 y el módulo DTE pisa el N° del despacho:
    db = SessionLocal()
    try:
        d = db.query(mm.MonzaDespacho).filter(mm.MonzaDespacho.id == _seed["desp_id"]).first()
        d.numero_guia = "52999"
        db.commit()
        snap = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id == f1.get("id", 0)).first()
        check("#7 la columna snapshot NO se toca (histórico)",
              snap is not None and snap.numero_guia == "GD-VIEJA",
              getattr(snap, "numero_guia", None))
    finally:
        db.close()
    rl = client.get("/api/monza/contabilidad/facturas")
    fl = next((x for x in rl.json()["facturas"] if x["id"] == f1.get("id")), None)
    check("#7 listado sirve el folio real '52999'", fl and fl["numero_guia"] == "52999", fl)
    rv = client.get(f"/api/monza/contabilidad/ventas/{cot_id}")
    fv = next((x for x in rv.json()["facturas"] if x["id"] == f1.get("id")), None)
    check("#7 detalle de la venta sirve el folio real '52999'",
          fv and fv["numero_guia"] == "52999", fv)
    # Aviso del selector de guías (no bloquea): la clave existe y es booleana.
    rdf = client.get(f"/api/monza/contabilidad/ventas/{cot2_id}/despachos-facturables")
    check("#7 selector de guías expone guia_sii_en_proceso",
          rdf.status_code == 200 and all("guia_sii_en_proceso" in e for e in rdf.json()),
          rdf.text)

    # ── #15: la holgura de 1 CLP es deliberada (queda FIJADA, ver docs) ──
    if f1.get("id"):
        saldo = f1["saldo"]
        r2 = client.post(f"/api/monza/contabilidad/facturas/{f1['id']}/cobranzas",
                         json={"monto": saldo + 2})
        check("#15 sobrepago de 2 CLP -> 400", r2.status_code == 400, r2.text)
        r1 = client.post(f"/api/monza/contabilidad/facturas/{f1['id']}/cobranzas",
                         json={"monto": saldo + 1})
        check("#15 sobrepago de 1 CLP (holgura) -> 200", r1.status_code == 200, r1.text)
        if r1.status_code == 200:
            d1 = r1.json()
            check("#15 el saldo se clampea a 0 y monto_pagado conserva lo registrado",
                  d1["saldo"] == 0 and d1["monto_pagado"] == saldo + 1, d1)
        r3 = client.post(f"/api/monza/contabilidad/facturas/{f1['id']}/cobranzas",
                         json={"monto": 1})
        check("#15 el desvío NO es acumulable (segundo peso -> 400)", r3.status_code == 400, r3.text)

    # ── #6 / #19: guards del SII sobre facturas electrónicas ──
    if _dte_disponible():
        from monza_wasabil_dte.models import MonzaWasabilDte
        fid = _factura_electronica(cot2_id, 1190000)
        _crear_dte(fid, status_id=2, uuid="uuid-audg-1")  # procesando: sin folio aún
        rc = client.post(f"/api/monza/contabilidad/facturas/{fid}/cobranzas",
                         json={"monto": 1000000})
        check("#6 cobranza en factura sin folio con DTE en curso -> 409",
              rc.status_code == 409, rc.text)
        db = SessionLocal()
        try:
            n = db.query(MonzaContCobranza).filter(MonzaContCobranza.factura_id == fid).count()
            check("#6 no quedó NINGUNA cobranza registrada", n == 0, n)
        finally:
            db.close()
        # El SII confirma el folio → la MISMA cobranza se acepta.
        db = SessionLocal()
        try:
            dte = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.factura_id == fid).first()
            dte.status_id = 3
            dte.folio = "33001"
            db.commit()
        finally:
            db.close()
        rc2 = client.post(f"/api/monza/contabilidad/facturas/{fid}/cobranzas",
                          json={"monto": 1000000})
        check("#6 tras el folio del SII la cobranza entra -> 200", rc2.status_code == 200, rc2.text)
        # Una factura CON folio (vía manual) nunca consulta el módulo DTE: sigue cobrable.
        fid_folio = _factura_electronica(cot2_id, 100000, folio=f"{MARK}-FMAN")
        rc3 = client.post(f"/api/monza/contabilidad/facturas/{fid_folio}/cobranzas",
                          json={"monto": 1000})
        check("#6 factura con folio manual no la toca el guard -> 200",
              rc3.status_code == 200, rc3.text)

        # #19: un DTE RECHAZADO por el SII (uuid, status 4, sin claim) NO deja la factura
        # imborrable — comportamiento REAL que el docstring ahora describe bien.
        #
        # CORRECCIÓN (re-refutación, ALTO-3): este bloque fijaba «el ancla DTE se borró
        # con la factura» como el invariante CORRECTO, y además lo medía con
        # `factura_id == fid` — una condición que se cumple igual si la fila se conserva
        # DESLIGADA, así que no distinguía destruir de conservar. El invariante correcto
        # tiene dos mitades: la factura SE BORRA (no secuestrar el cupo facturable) y el
        # ancla NO se destruye cuando hay uuid (es la única llave del documento que
        # EXISTE en Wasabil; el status 4 local es una foto que puede quedar obsoleta).
        # Se mide por el id de la fila, que es lo que discrimina.
        fid_rech = _factura_electronica(cot2_id, 119000)
        dte_rech_id = _crear_dte(fid_rech, status_id=4, uuid="uuid-audg-rechazado")
        rd = client.delete(f"/api/monza/contabilidad/facturas/{fid_rech}")
        check("#19 factura con DTE rechazado se puede eliminar -> 200", rd.status_code == 200, rd.text)
        db = SessionLocal()
        try:
            fac_viva = db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.id == fid_rech).count()
            fila = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == dte_rech_id).first()
            check("#19 la factura sí desapareció (el cupo facturable se libera)",
                  fac_viva == 0, fac_viva)
            check("#19 el ANCLA sobrevive DESLIGADA: la fila existe, con su uuid, "
                  "factura_id NULL y la nota que dice dónde buscar el documento",
                  fila is not None and fila.factura_id is None
                  and fila.uuid == "uuid-audg-rechazado"
                  and "ANCLA CONSERVADA" in (fila.error or "")
                  and f"FACT-{fid_rech}" in (fila.error or ""),
                  None if fila is None else {"factura_id": fila.factura_id,
                                             "uuid": fila.uuid, "error": fila.error})
        finally:
            db.close()
        # Hermano del anterior: SIN uuid el documento NUNCA nació, no hay llave que
        # perder → ahí sí se borra la fila junto con la factura.
        fid_sin_uuid = _factura_electronica(cot2_id, 119000)
        dte_sin_uuid_id = _crear_dte(fid_sin_uuid, status_id=4, uuid=None)
        rdu = client.delete(f"/api/monza/contabilidad/facturas/{fid_sin_uuid}")
        db = SessionLocal()
        try:
            fila = db.query(MonzaWasabilDte).filter(
                MonzaWasabilDte.id == dte_sin_uuid_id).first()
            check("#19 rechazo SIN uuid: la factura se borra (200) y el ancla se limpia "
                  "con ella (no hay documento que anclar)",
                  rdu.status_code == 200 and fila is None,
                  {"status": rdu.status_code, "fila": fila is not None, "body": rdu.text})
        finally:
            db.close()
        # Y el estado que NO permite concluir: hay documento (uuid) y el estado local no
        # dice en qué quedó ante el SII → FALLA CERRADO nombrando el uuid.
        fid_incognita = _factura_electronica(cot2_id, 119000)
        _crear_dte(fid_incognita, status_id=None, uuid="uuid-audg-incognita")
        rdi = client.delete(f"/api/monza/contabilidad/facturas/{fid_incognita}")
        check("#19 uuid con estado DESCONOCIDO -> 409 que nombra el identificador",
              rdi.status_code == 409 and "uuid-audg-incognita" in rdi.json().get("detail", ""),
              rdi.text)
        # Y el que SÍ debe seguir bloqueando: DTE procesando con uuid.
        fid_viva = _factura_electronica(cot2_id, 119000)
        _crear_dte(fid_viva, status_id=2, uuid="uuid-audg-viva")
        rdv = client.delete(f"/api/monza/contabilidad/facturas/{fid_viva}")
        check("#19 factura con DTE en curso sigue protegida -> 409", rdv.status_code == 409, rdv.text)

    print()
    if _fails:
        print(f"=== {len(_fails)} FALLO(S): {_fails} ===")
        return False
    print("=== TODO OK ===")
    return True


def test_monza_contabilidad_aud_guards():
    """Wrapper pytest: sin él la suite sería INVISIBLE al gate rutinario 'pytest verde'."""
    seed()
    ok = False
    try:
        ok = run()
    finally:
        cleanup()
    assert ok, f"fallas: {_fails}"


if __name__ == "__main__":
    seed()
    ok = False
    try:
        ok = run()
    finally:
        cleanup()
    sys.exit(0 if ok else 1)
