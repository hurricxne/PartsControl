"""Cliente del lead: edición con rastro, vínculo a lead huérfano y LTV al cliente
FACTURADO (arreglos 1, D4 y 5 del equipo, 2026-08-21).

QUÉ CIERRA
    · Arreglo 1: PATCH /leads/{id}/cliente existía sin UI y editaba la ficha COMPARTIDA
      sin dejar rastro. Ahora registra actividad en el lead + MonzaLog, y "" limpia un
      campo (None se descarta por exclude_none: ese es el contrato).
    · D4: un lead huérfano no tenía NINGÚN camino para ganar cliente. El PATCH acepta
      cliente_id SOLO cuando el lead no tiene (409 si ya tiene: la historia no se
      re-vincula).
    · Arreglo 5 (LTV): con «Cotizar a», el cliente de la cotización puede ser OTRO que
      el del lead. El LTV del despacho se suma —y la reversión lo resta— en la ficha
      del cliente FACTURADO (cot.cliente_id), jamás en la del lead.

SONDAS DE PODER DISCRIMINANTE
    · §5: cotización con cliente_id ≠ lead.cliente_id → al despachar, el LTV sube en la
      ficha FACTURADA y la del lead queda intacta (el código viejo acreditaba al lead:
      los dos checks caen con él). La reversión resta de la MISMA ficha (simetría).

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_cliente_lead_arreglos.py -q
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
    MonzaCliente, MonzaCotizacion, MonzaCotizacionCierre, MonzaCotizacionItem,
    MonzaLead, MonzaLeadActividad, MonzaLeadItem, MonzaLog, MonzaNotificacion,
)
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402
from monza_router_leads import router as leads_router  # noqa: E402

MARK = "test-mzclilead"
LEAD_MARK = "L-MZCL"
COT_MARK = "CCLI"
EMAIL = f"{MARK}@test.invalid"
TOTAL_BRUTO = 300000

app = FastAPI()
app.include_router(leads_router)
app.include_router(cotizaciones_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz", rol="admin")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _nuevo_cliente(nombre, rut=None):
    db = SessionLocal()
    try:
        c = MonzaCliente(nombre=nombre, rut=rut, ltv=0, leads_total=0)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _nuevo_lead(cliente_id):
    db = SessionLocal()
    try:
        lead = MonzaLead(numero=f"{LEAD_MARK}-{uuid.uuid4().hex[:6].upper()}",
                         cliente_id=cliente_id, estado="pendiente")
        db.add(lead)
        db.commit()
        return lead.id
    finally:
        db.close()


def _cliente(cid):
    db = SessionLocal()
    try:
        return db.query(MonzaCliente).filter(MonzaCliente.id == cid).first()
    finally:
        db.close()


def _actividades(lead_id):
    db = SessionLocal()
    try:
        return [a.descripcion for a in db.query(MonzaLeadActividad)
                .filter(MonzaLeadActividad.lead_id == lead_id).all()]
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        lead_ids = [r[0] for r in db.query(MonzaLead.id)
                    .filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).all()]
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .filter(MonzaCotizacion.numero.like(f"{COT_MARK}%")).all()]
        db.query(MonzaCotizacionCierre).filter(
            MonzaCotizacionCierre.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.titulo.like(f"%{COT_MARK}-%")).delete(synchronize_session=S)
        db.query(MonzaLeadActividad).filter(
            MonzaLeadActividad.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLeadItem).filter(
            MonzaLeadItem.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLead).filter(MonzaLead.id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(
            synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (db.query(MonzaLead).filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).count()
                  + db.query(MonzaCotizacion).filter(
                      MonzaCotizacion.numero.like(f"{COT_MARK}%")).count()
                  + db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        c1 = _nuevo_cliente(f"{MARK} CONTACTO", rut="11.111.111-1")
        lead_id = _nuevo_lead(c1)

        # ── 1) Edición de la ficha deja RASTRO (actividad + log) ──────────────────────
        r = client.patch(f"/api/monza/leads/{lead_id}/cliente",
                         json={"telefono": "+56 9 5555 5555", "email": "nuevo@mail.invalid"})
        check("1a editar responde 200", r.status_code == 200, r.text[:200])
        c = _cliente(c1)
        check("1b los campos quedaron", (c.telefono, c.email) == ("+56 9 5555 5555", "nuevo@mail.invalid"),
              (c.telefono, c.email))
        acts = _actividades(lead_id)
        check("1c SONDA: quedó actividad del cambio (teléfono, email)",
              any("Datos del cliente actualizados" in a and "teléfono" in a for a in acts), acts)
        db = SessionLocal()
        try:
            n_log = db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL,
                                              MonzaLog.entidad == "cliente").count()
        finally:
            db.close()
        check("1d y MonzaLog registró la edición", n_log >= 1, n_log)

        # ── 2) "" LIMPIA un campo (el contrato de exclude_none) ───────────────────────
        r = client.patch(f"/api/monza/leads/{lead_id}/cliente", json={"email": ""})
        check("2a limpiar responde 200", r.status_code == 200, r.text[:200])
        check("2b el email quedó vacío", (_cliente(c1).email or "") == "", _cliente(c1).email)
        # Un PATCH que no cambia nada no ensucia la actividad.
        antes = len(_actividades(lead_id))
        client.patch(f"/api/monza/leads/{lead_id}/cliente", json={"telefono": "+56 9 5555 5555"})
        check("2c re-enviar el MISMO valor no agrega actividad",
              len(_actividades(lead_id)) == antes, (antes, len(_actividades(lead_id))))

        # ── 3) VINCULAR a un lead huérfano (D4) ───────────────────────────────────────
        huerfano = _nuevo_lead(None)
        c2 = _nuevo_cliente(f"{MARK} EMPRESA", rut="76.222.222-2")
        base_leads_total = _cliente(c2).leads_total or 0
        r = client.patch(f"/api/monza/leads/{huerfano}/cliente", json={"cliente_id": c2})
        check("3a vincular responde 200", r.status_code == 200, r.text[:200])
        db = SessionLocal()
        try:
            ld = db.query(MonzaLead).filter(MonzaLead.id == huerfano).first()
            check("3b el lead quedó vinculado", ld.cliente_id == c2, ld.cliente_id)
        finally:
            db.close()
        check("3c leads_total del cliente subió (mismo contador que crear lead)",
              (_cliente(c2).leads_total or 0) == base_leads_total + 1,
              _cliente(c2).leads_total)
        check("3d quedó actividad del vínculo",
              any("vinculado al lead" in a for a in _actividades(huerfano)),
              _actividades(huerfano))

        # ── 4) Un lead CON cliente no se re-vincula ───────────────────────────────────
        r = client.patch(f"/api/monza/leads/{lead_id}/cliente", json={"cliente_id": c2})
        check("4a re-vincular responde 409", r.status_code == 409, (r.status_code, r.text[:200]))
        check("4b y el lead conserva su cliente original",
              client.get(f"/api/monza/leads/{lead_id}").json()["cliente"]["id"] == c1, "")

        # ── 5) LTV al cliente FACTURADO (cot.cliente_id), no al del lead ──────────────
        db = SessionLocal()
        try:
            cot = MonzaCotizacion(
                numero=f"{COT_MARK}-{uuid.uuid4().hex[:6].upper()}",
                lead_id=lead_id,          # el lead es de c1...
                cliente_id=c2,            # ...pero la venta se facturó a c2 («Cotizar a»)
                estado="vendida",
                total_neto=round(TOTAL_BRUTO / 1.19),
                iva_monto=TOTAL_BRUTO - round(TOTAL_BRUTO / 1.19),
                total_bruto=TOTAL_BRUTO,
                oc_cliente="OC-LTV",
            )
            db.add(cot)
            db.flush()
            # Línea ya despachada: el PATCH administrativo a 'despachado' no re-valida
            # cobertura sobre líneas que ya están en ese estado (hallazgo #8).
            db.add(MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Kit embrague",
                                       cantidad=1, precio_unitario_clp=TOTAL_BRUTO,
                                       subtotal_clp=TOTAL_BRUTO, estado_linea="despachado"))
            db.commit()
            cot_id = cot.id
        finally:
            db.close()

        ltv1_base = _cliente(c1).ltv or 0
        ltv2_base = _cliente(c2).ltv or 0
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "despachado"})
        check("5a el flip a despachado responde 200", r.status_code == 200, r.text[:300])
        check("5b SONDA: el LTV subió en la ficha FACTURADA (c2)",
              (_cliente(c2).ltv or 0) == ltv2_base + TOTAL_BRUTO,
              (ltv2_base, _cliente(c2).ltv, "el código viejo acreditaba al cliente del lead"))
        check("5c y la ficha del cliente del LEAD (c1) quedó intacta",
              (_cliente(c1).ltv or 0) == ltv1_base, (_cliente(c1).ltv, ltv1_base))

        # Reversión: la resta sale de la MISMA ficha (simetría; sin plata/logística
        # colgando el retroceso está permitido).
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}",
                         json={"estado": "enviada", "motivo_reversion": "prueba LTV"})
        check("5d la reversión responde 200", r.status_code == 200, r.text[:300])
        check("5e el LTV volvió a la base en c2 (resta simétrica)",
              (_cliente(c2).ltv or 0) == ltv2_base, (_cliente(c2).ltv, ltv2_base))
        check("5f y c1 sigue intacto", (_cliente(c1).ltv or 0) == ltv1_base, _cliente(c1).ltv)

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_cliente_lead_arreglos():
    run()


if __name__ == "__main__":
    run()
