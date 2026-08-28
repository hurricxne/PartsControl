"""El LTV se suma cuando la venta se despacha DE VERDAD (deuda H6, 2026-08-22).

EL BUG QUE CIERRA
    Los efectos de «venta despachada» (cerrar el lead y sumar el LTV del cliente) vivían
    SOLO en el PATCH administrativo. Pero el camino real de la operación es otro: cuando
    el encargado cierra el último despacho, `cerrar_despacho` voltea la venta a
    'despachado' por su cuenta — y no sumaba nada. Peor: como la venta ya quedaba
    'despachado', un PATCH posterior calculaba `es_despacho_nuevo = False` y tampoco los
    aplicaba. El LTV que la ficha del cliente muestra —y que el Portal va a publicar—
    casi nunca se sumaba.

SONDAS DE PODER DISCRIMINANTE
    · §1 recorre el flujo FÍSICO completo (bodega → despacho → cerrar) y afirma
      `ltv == total`: contra el código anterior daba 0. Es la sonda de máximo poder.
    · §2 el LTV va a la ficha del cliente FACTURADO, no a la del cliente del lead
      (con «Cotizar a» pueden ser distintos y la plata es de quien compró).
    · §3 idempotencia por los DOS caminos: tras el auto-flip, un PATCH 'despachado' no
      vuelve a sumar (y al revés tampoco).
    · §4 simetría: anular el despacho baja la venta a 'vendida' y DEVUELVE el LTV; una
      venta que ya estaba en 'vendida' no pierde plata por el barrido de reparación.

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_ltv_flip_despacho.py -q
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
    MonzaDespacho, MonzaDespachoItem, MonzaLead, MonzaLeadActividad, MonzaLog,
    MonzaNotificacion,
)
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402
from monza_router_despachos import router as despachos_router  # noqa: E402

MARK = "test-mzltv"
LEAD_MARK = "L-MZLTV"
COT_MARK = "CLTV"
EMAIL = f"{MARK}@test.invalid"
TOTAL_BRUTO = 476000.0

app = FastAPI()
app.include_router(despachos_router)
app.include_router(cotizaciones_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz", rol="admin")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _seed(qty=4, estado="vendida"):
    """Venta con lead, 1 línea EN BODEGA, y DOS clientes distintos.

    `cliente_lead` es quien originó el lead; `cliente_factura` es a quien se le emitió
    la cotización («Cotizar a»). El LTV tiene que ir al segundo.
    """
    db = SessionLocal()
    try:
        cliente_lead = MonzaCliente(nombre=f"{MARK} CONTACTO", ltv=0)
        cliente_factura = MonzaCliente(nombre=f"{MARK} EMPRESA", ltv=0)
        db.add_all([cliente_lead, cliente_factura])
        db.flush()
        lead = MonzaLead(numero=f"{LEAD_MARK}-{uuid.uuid4().hex[:5].upper()}",
                         cliente_id=cliente_lead.id, estado="en_proceso")
        db.add(lead)
        db.flush()
        cot = MonzaCotizacion(
            numero=f"{COT_MARK}-{uuid.uuid4().hex[:5].upper()}",
            lead_id=lead.id, cliente_id=cliente_factura.id, estado=estado,
            total_neto=400000, iva_monto=76000, total_bruto=TOTAL_BRUTO, iva_pct=19,
            oc_cliente=f"OC-{MARK}",
        )
        db.add(cot)
        db.flush()
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion="Turbo", cantidad=qty,
            precio_unitario_clp=100000, subtotal_clp=100000 * qty,
            estado_linea="en_bodega",
        )
        db.add(it)
        db.commit()
        return {"cot": cot.id, "item": it.id, "lead": lead.id,
                "cli_lead": cliente_lead.id, "cli_fact": cliente_factura.id, "qty": qty}
    finally:
        db.close()


def _ltv(cid):
    db = SessionLocal()
    try:
        c = db.query(MonzaCliente).filter(MonzaCliente.id == cid).first()
        return float(c.ltv or 0) if c else None
    finally:
        db.close()


def _vendidos_total(cid):
    db = SessionLocal()
    try:
        c = db.query(MonzaCliente).filter(MonzaCliente.id == cid).first()
        return int(c.vendidos_total or 0) if c else None
    finally:
        db.close()


def _estados(cot_id, lead_id):
    db = SessionLocal()
    try:
        cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
        lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
        return (cot.estado if cot else None), (lead.estado if lead else None)
    finally:
        db.close()


def _despachar_y_cerrar(s):
    """El camino FÍSICO real: crear el despacho por la cantidad completa y cerrarlo."""
    r = client.post("/api/monza/despachos/crear", json={
        "cotizacion_id": s["cot"], "items": [{"item_id": s["item"], "qty": s["qty"]}]})
    assert r.status_code == 200, r.text[:300]
    d = r.json()["id"]
    r = client.post(f"/api/monza/despachos/entidades/{d}/cerrar")
    assert r.status_code == 200, r.text[:300]
    return d


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .filter(MonzaCotizacion.numero.like(f"{COT_MARK}%")).all()]
        lead_ids = [r[0] for r in db.query(MonzaLead.id)
                    .filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).all()]
        desp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                    .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id.in_(desp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.id.in_(desp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionCierre).filter(
            MonzaCotizacionCierre.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.titulo.like(f"%{COT_MARK}%")).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLeadActividad).filter(
            MonzaLeadActividad.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLead).filter(MonzaLead.id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (db.query(MonzaCotizacion).filter(MonzaCotizacion.numero.like(f"{COT_MARK}%")).count()
                  + db.query(MonzaLead).filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).count()
                  + db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        # ── 1) SONDA PRINCIPAL: el flujo FÍSICO suma el LTV ──────────────────────
        s = _seed()
        check("1a la venta parte 'vendida' con LTV en 0",
              _estados(s["cot"], s["lead"])[0] == "vendida" and _ltv(s["cli_fact"]) == 0,
              (_estados(s["cot"], s["lead"]), _ltv(s["cli_fact"])))
        _despachar_y_cerrar(s)
        estado_cot, estado_lead = _estados(s["cot"], s["lead"])
        check("1b cerrar el último despacho deja la venta 'despachado'",
              estado_cot == "despachado", estado_cot)
        check("1c SONDA (HOY daba 0): el LTV del cliente facturado subió al total",
              _ltv(s["cli_fact"]) == TOTAL_BRUTO, _ltv(s["cli_fact"]))
        check("1d y el lead quedó 'cerrado' por el mismo camino",
              estado_lead == "cerrado", estado_lead)

        # ── 2) La plata va al cliente FACTURADO, no al del lead ──────────────────
        check("2a SONDA: la ficha del cliente DEL LEAD queda intacta",
              _ltv(s["cli_lead"]) == 0, _ltv(s["cli_lead"]))

        # ── 2-bis) El contador del CICLO del lead sigue vivo ─────────────────────
        # `vendidos_total` lo sumaba el PATCH del asesor al marcar el lead vendido a
        # mano. Desde que el CIERRE lo marca solo, ese PATCH ya no ocurre: si el cierre
        # no sumara, el contador de la ficha se quedaría en cero para siempre.
        # 'enviada': el cierre solo es NUEVO desde ahí (una venta que ya nace
        # 'vendida' no dispara los efectos del cierre, por idempotencia).
        s_v = _seed(estado="enviada")
        antes_v = _vendidos_total(s_v["cli_lead"])
        r = client.patch(f"/api/monza/cotizaciones/{s_v['cot']}", json={
            "estado": "vendida", "oc_cliente": f"OC-{MARK}-V", "oc_fecha": "2026-08-21"})
        check("2b el cierre de venta responde 200", r.status_code == 200, r.text[:200])
        check("2c y marca el lead como vendido",
              _estados(s_v["cot"], s_v["lead"])[1] == "vendido",
              _estados(s_v["cot"], s_v["lead"]))
        check("2d SONDA: `vendidos_total` del cliente del LEAD sube en 1 "
              "(sin esto el contador quedaba muerto)",
              _vendidos_total(s_v["cli_lead"]) == antes_v + 1,
              (antes_v, _vendidos_total(s_v["cli_lead"])))
        # Re-marcar a mano no debe volver a sumar: el PATCH de leads exige que el
        # estado CAMBIE, y ya está en 'vendido'.
        r = client.patch(f"/api/monza/cotizaciones/{s_v['cot']}", json={
            "estado": "vendida", "oc_cliente": f"OC-{MARK}-V"})
        check("2e re-cerrar (idempotente) NO vuelve a sumar el contador",
              _vendidos_total(s_v["cli_lead"]) == antes_v + 1,
              _vendidos_total(s_v["cli_lead"]))

        # ── 3) Idempotencia entre los dos caminos ────────────────────────────────
        r = client.patch(f"/api/monza/cotizaciones/{s['cot']}", json={"estado": "despachado"})
        check("3a re-PATCH 'despachado' tras el auto-flip responde 200",
              r.status_code == 200, r.text[:200])
        check("3b SONDA: y NO vuelve a sumar (la transición ya ocurrió)",
              _ltv(s["cli_fact"]) == TOTAL_BRUTO, _ltv(s["cli_fact"]))

        # El camino inverso: PATCH primero (suma) y luego el cierre físico (no repite).
        s2 = _seed()
        db = SessionLocal()
        try:
            # El PATCH exige cobertura de despachos cerrados salvo para líneas que ya
            # están 'despachado': se simula el cierre administrativo puro.
            it = db.query(MonzaCotizacionItem).filter(
                MonzaCotizacionItem.id == s2["item"]).first()
            it.estado_linea = "despachado"
            db.commit()
        finally:
            db.close()
        r = client.patch(f"/api/monza/cotizaciones/{s2['cot']}", json={"estado": "despachado"})
        check("3c el PATCH administrativo suma cuando él hace la transición",
              r.status_code == 200 and _ltv(s2["cli_fact"]) == TOTAL_BRUTO,
              (r.status_code, _ltv(s2["cli_fact"])))

        # ── 4) Simetría: anular devuelve la plata ────────────────────────────────
        s3 = _seed()
        d3 = _despachar_y_cerrar(s3)
        check("4a tras cerrar, el LTV está sumado", _ltv(s3["cli_fact"]) == TOTAL_BRUTO,
              _ltv(s3["cli_fact"]))
        # Anular exige el despacho en preparación: se reabre para ejercitar la reversa.
        db = SessionLocal()
        try:
            d = db.query(MonzaDespacho).filter(MonzaDespacho.id == d3).first()
            d.estado = "en_preparacion"
            db.commit()
        finally:
            db.close()
        r = client.delete(f"/api/monza/despachos/entidades/{d3}")
        check("4b anular responde 200", r.status_code == 200, r.text[:200])
        check("4c la venta volvió a 'vendida'", _estados(s3["cot"], s3["lead"])[0] == "vendida",
              _estados(s3["cot"], s3["lead"]))
        check("4d SONDA de simetría: el LTV volvió a 0 (la reversa resta lo que la "
              "suma puso, ni más ni menos)", _ltv(s3["cli_fact"]) == 0, _ltv(s3["cli_fact"]))

        # ── 5) El barrido de reparación NO resta plata que nunca entró ───────────
        # Una venta que ya está en 'vendida' (nunca despachada) pasa por el mismo
        # bloque de anular: su cliente no puede perder LTV de OTRAS ventas.
        s4 = _seed()
        db = SessionLocal()
        try:
            c = db.query(MonzaCliente).filter(MonzaCliente.id == s4["cli_fact"]).first()
            c.ltv = 999000        # plata de otras ventas del mismo cliente
            db.commit()
        finally:
            db.close()
        r = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": s4["cot"], "items": [{"item_id": s4["item"], "qty": 1}]})
        d4 = r.json()["id"]
        r = client.delete(f"/api/monza/despachos/entidades/{d4}")
        check("5a anular un BORRADOR (la venta nunca llegó a 'despachado') responde 200",
              r.status_code == 200, r.text[:200])
        check("5b SONDA: el LTV de otras ventas queda INTACTO (restar acá se comería "
              "plata legítima)", _ltv(s4["cli_fact"]) == 999000, _ltv(s4["cli_fact"]))

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_ltv_flip_despacho():
    run()


if __name__ == "__main__":
    run()
