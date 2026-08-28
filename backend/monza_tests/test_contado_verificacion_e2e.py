"""CONTADO exige verificación de Tesorería, de punta a punta (arreglo 8, 2026-08-21).

EL CIRCUITO QUE PINEA
    Cierre Contado (pct 100) → la venta entra a la cola de Tesorería por el TOTAL →
    Abastecimiento NO puede generar la OC de proveedor (409 del cortafuego) → Tesorería
    aprueba el pago recibido (adelanto_verificado=1) → la OC de proveedor pasa →
    factura final por el total → cobranza automática medio='adelanto' 100% → saldo 0.

LA MITAD NEGATIVA ES OBLIGATORIA (lección «sondas que no prueban nada»)
    El 409 del cortafuego se afirma ANTES de aprobar; y el cinturón backend nuevo
    (Contado ⇒ 100) se sondea con un cierre 'Contado' pct 0, que debe rebotar 409 —
    la ruta de escape del build viejo en caché o del API directo.
    Reparto de pines contra la mutación «contado vuelve a pct 0 en el frontend»: este
    e2e pinea el CIRCUITO backend dado pct=100 (que acá viaja hardcodeado, como lo
    manda la pantalla nueva); la CONSTANTE del frontend la pinea
    test_adelanto_espejo_frontend — juntos matan la mutación.

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_contado_verificacion_e2e.py -q
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
from monza_contabilidad.models import (  # noqa: E402
    MonzaContAdelanto, MonzaContCobranza, MonzaContFacturaCliente,
)
from monza_contabilidad.router import router as contab_router  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionCierre, MonzaCotizacionItem,
    MonzaDespacho, MonzaDespachoItem, MonzaLog, MonzaNotificacion, MonzaOcProveedor,
)
from monza_router_abastecimiento import router as abastecimiento_router  # noqa: E402
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402
from monza_tesoreria.router import router as tesoreria_router  # noqa: E402

MARK = "test-mzcontado"
COT_MARK = "CCTD"
EMAIL = f"{MARK}@test.invalid"
# El ítem se valoriza en NETO (la factura le suma el IVA); el bruto es lo que Tesorería
# aprueba y lo que la cobranza del adelanto cubre. Números redondos a propósito.
TOTAL_NETO = 500000
IVA = 95000
TOTAL_BRUTO = TOTAL_NETO + IVA  # 595.000

app = FastAPI()
app.include_router(cotizaciones_router)
app.include_router(abastecimiento_router)
app.include_router(tesoreria_router)
app.include_router(contab_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz", rol="admin")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _seed():
    """Cotización ENVIADA (lista para cerrar) con 1 ítem, sin lead (no lo necesita)."""
    db = SessionLocal()
    try:
        # RUT con dígito verificador VÁLIDO: la factura de §6 lo exige.
        cli = MonzaCliente(nombre=f"{MARK} SpA", rut="11.111.111-1")
        db.add(cli)
        db.flush()
        cot = MonzaCotizacion(
            numero=f"{COT_MARK}-{uuid.uuid4().hex[:6].upper()}",
            cliente_id=cli.id, estado="enviada",
            total_neto=TOTAL_NETO, iva_monto=IVA, total_bruto=TOTAL_BRUTO, iva_pct=19,
        )
        db.add(cot)
        db.flush()
        it = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Turbo completo",
                                 cantidad=1, precio_unitario_clp=TOTAL_NETO,
                                 subtotal_clp=TOTAL_NETO, estado_linea="cotizado")
        db.add(it)
        db.commit()
        return cot.id, cot.numero, it.id
    finally:
        db.close()


def _cot(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    finally:
        db.close()


def _item(item_id):
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == item_id).first()
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .filter(MonzaCotizacion.numero.like(f"{COT_MARK}%")).all()]
        ocp_ids = [r[0] for r in db.query(MonzaCotizacionItem.oc_proveedor_id)
                   .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0]),
                           MonzaCotizacionItem.oc_proveedor_id.isnot(None)).distinct().all()]
        # Plata de §6 primero (FK: cobranza → factura → adelanto/cotización).
        fac_ids = [r[0] for r in db.query(MonzaContFacturaCliente.id)
                   .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids or [0])).all()]
        db.query(MonzaContCobranza).filter(
            MonzaContCobranza.factura_id.in_(fac_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id.in_(fac_ids or [0])).delete(synchronize_session=S)
        desp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                    .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id.in_(desp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.id.in_(desp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionCierre).filter(
            MonzaCotizacionCierre.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaOcProveedor).filter(
            MonzaOcProveedor.id.in_(ocp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.titulo.like(f"%{COT_MARK}-%")).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(
            synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (db.query(MonzaCotizacion).filter(
            MonzaCotizacion.numero.like(f"{COT_MARK}%")).count()
            + db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count()
            # La plata de §6 se verifica directo (no solo de rebote por las FK).
            + db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.numero_factura.like(f"{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        cot_id, numero, item_id = _seed()

        # ── 0) CINTURÓN BACKEND: cerrar 'Contado' con pct < 100 rebota ────────────────
        # La ruta de escape real es un build viejo en caché tras el deploy (manda pct 0
        # para Contado, como antes del arreglo) o el API directo. El guard debe fallar
        # CERRADO en ambos sabores: pct 0 explícito y pct ausente sobre venta con 0.
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={
            "estado": "vendida", "pct_adelanto": 0, "forma_pago": "Contado",
            "oc_cliente": f"OC-{MARK}", "oc_fecha": "2026-08-21",
        })
        check("0a SONDA: cierre Contado con pct 0 explícito → 409 (build viejo)",
              r.status_code == 409 and "Contado exige" in r.text,
              (r.status_code, r.text[:200]))
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={
            "estado": "vendida", "forma_pago": "Contado",
            "oc_cliente": f"OC-{MARK}", "oc_fecha": "2026-08-21",
        })
        check("0b y Contado SIN pct sobre venta con 0 también rebota (API directo)",
              r.status_code == 409, (r.status_code, r.text[:200]))
        check("0c la venta sigue SIN cerrar", _cot(cot_id).estado == "enviada",
              _cot(cot_id).estado)

        # ── 1) Cierre CONTADO: pct 100 (lo que la pantalla manda desde este arreglo) ──
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={
            "estado": "vendida", "pct_adelanto": 100, "forma_pago": "Contado",
            "oc_cliente": f"OC-{MARK}", "oc_fecha": "2026-08-21",
            "fecha_entrega_est": "2026-09-15",
        })
        check("1a el cierre Contado-100 responde 200", r.status_code == 200, r.text[:300])
        cot = _cot(cot_id)
        check("1b pct_adelanto quedó en 100", int(cot.pct_adelanto or 0) == 100, cot.pct_adelanto)
        check("1c la línea quedó por_comprar", _item(item_id).estado_linea == "por_comprar",
              _item(item_id).estado_linea)
        check("1d el pago aún NO está verificado", not int(cot.adelanto_verificado or 0),
              cot.adelanto_verificado)

        # ── 1.5) BYPASS POST-CIERRE (hallazgo ALT1 de la ronda escéptica): el cinturón
        # también cubre la EDICIÓN sin `estado` — antes, {pct_adelanto: 0} sobre esta
        # venta respondía 200 y apagaba el cortafuego con la venta ya cerrada.
        r = client.patch(f"/api/monza/cotizaciones/{cot_id}", json={"pct_adelanto": 0})
        check("1e SONDA ALT1: bajar el pct SIN estado sobre la venta Contado → 409",
              r.status_code == 409 and "Contado exige" in r.text,
              (r.status_code, r.text[:200]))
        check("1f y el pct sigue en 100", int(_cot(cot_id).pct_adelanto or 0) == 100,
              _cot(cot_id).pct_adelanto)
        # La otra mitad del bypass: PONER forma Contado sobre una venta cerrada con
        # pct < 100 (quedaba Contado+50 sin pasar por guard alguno). Venta hermana.
        cot2_id, _, _ = _seed()
        r = client.patch(f"/api/monza/cotizaciones/{cot2_id}", json={
            "estado": "vendida", "pct_adelanto": 50, "forma_pago": "50% adelanto",
            "oc_cliente": f"OC2-{MARK}", "oc_fecha": "2026-08-21",
        })
        check("1g venta hermana cerrada al 50%", r.status_code == 200, r.text[:200])
        r = client.patch(f"/api/monza/cotizaciones/{cot2_id}", json={"forma_pago": "Contado"})
        check("1h SONDA ALT1: poner forma Contado post-cierre con pct 50 → 409",
              r.status_code == 409, (r.status_code, r.text[:200]))
        # Las salidas LEGÍTIMAS siguen abiertas: subir a Contado-100 completo, o
        # corregir a crédito (la forma efectiva deja de ser Contado).
        r = client.patch(f"/api/monza/cotizaciones/{cot2_id}", json={
            "forma_pago": "Contado", "pct_adelanto": 100})
        check("1i subir a Contado-100 post-cierre sí pasa", r.status_code == 200, r.text[:200])
        r = client.patch(f"/api/monza/cotizaciones/{cot2_id}", json={
            "forma_pago": "60 días contra factura", "pct_adelanto": 0})
        check("1j y corregir a crédito (sin plata registrada) también",
              r.status_code == 200, r.text[:200])

        # ── 2) La venta entra a la cola de Tesorería POR EL TOTAL ─────────────────────
        r = client.get("/api/monza/tesoreria/aprobaciones")
        check("2a la cola responde 200", r.status_code == 200, r.text[:200])
        fila = next((x for x in r.json()["por_aprobar"] if x["cotizacion_id"] == cot_id), None)
        check("2b SONDA (RED con pct 0): la venta Contado está POR APROBAR",
              fila is not None, "con contado=0 no entra a la cola")
        check("2c el monto sugerido es el TOTAL de la venta (100%)",
              fila is not None and fila.get("monto_sugerido_clp") == float(TOTAL_BRUTO), fila)

        # ── 3) MITAD NEGATIVA: Abastecimiento NO puede comprar sin verificar ──────────
        body_oc = {"item_ids": [item_id], "proveedor_nombre": f"{MARK} PROVEEDOR",
                   "pais": "Alemania", "moneda": "EUR"}
        r = client.post("/api/monza/abastecimiento/comprar", json=body_oc)
        check("3a SONDA (RED con pct 0): el cortafuego responde 409", r.status_code == 409,
              (r.status_code, r.text[:200]))
        check("3b y el 409 explica el porqué (adelanto no verificado)",
              "Adelanto no verificado" in r.text, r.text[:300])
        check("3c la línea sigue por_comprar (nada se compró)",
              _item(item_id).estado_linea == "por_comprar", _item(item_id).estado_linea)

        # ── 4) Tesorería aprueba el pago recibido → el cortafuego se abre ─────────────
        r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar", json={
            "monto": TOTAL_BRUTO, "banco": "BancoTest", "numero_operacion": "OP-1",
        })
        check("4a aprobar responde 200", r.status_code == 200, r.text[:300])
        cot = _cot(cot_id)
        check("4b adelanto_verificado quedó en 1", int(cot.adelanto_verificado or 0) == 1,
              cot.adelanto_verificado)
        db = SessionLocal()
        try:
            adel = db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.cotizacion_id == cot_id).first()
            check("4c el adelanto quedó registrado por el TOTAL",
                  adel is not None and float(adel.monto) == float(TOTAL_BRUTO),
                  getattr(adel, "monto", None))
        finally:
            db.close()

        # ── 5) Ahora la OC de proveedor SÍ pasa ───────────────────────────────────────
        r = client.post("/api/monza/abastecimiento/comprar", json=body_oc)
        check("5a comprar responde 200 tras la verificación", r.status_code == 200,
              (r.status_code, r.text[:300]))
        check("5b la OC quedó emitida con el ítem",
              r.status_code == 200 and r.json().get("ok") and r.json().get("items") == 1,
              r.text[:200])
        check("5c la línea pasó a comprado", _item(item_id).estado_linea == "comprado",
              _item(item_id).estado_linea)

        # ── 6) La COLA de la plata: factura final → cobranza 'adelanto' → saldo 0 ─────
        # Con Contado=100 este es el camino COTIDIANO de caja. Se salta la logística
        # física (mismo atajo de test_viaje_de_la_plata): la línea queda despachada con
        # su guía FIRMADA (regla 2026-08-06: sin firma no se factura) y se emite la
        # factura final por el total. El adelanto YA verificado debe aplicarse solo:
        # UNA cobranza medio='adelanto' por el 100% y la factura nace pagada, saldo 0.
        db = SessionLocal()
        try:
            it = db.query(MonzaCotizacionItem).filter(
                MonzaCotizacionItem.id == item_id).first()
            it.estado_linea = "despachado"
            d = MonzaDespacho(numero=f"{MARK}-DSP1", cotizacion_id=cot_id,
                              estado="despachado", numero_guia=f"G-{MARK}-1",
                              cliente_nombre=f"{MARK} SpA", guia_firmada=1)
            db.add(d)
            db.flush()
            db.add(MonzaDespachoItem(despacho_id=d.id, item_id=item_id, qty_despachada=1))
            db.commit()
            desp_id = d.id
        finally:
            db.close()
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_id, "despacho_id": desp_id,
            "numero_factura": f"{MARK}-F1", "plazo_dias": 3650,
            "fecha_emision": "2026-08-21",
        })
        check("6a la factura final se emite", r.status_code == 200, r.text[:300])
        fac_id = r.json().get("id")
        db = SessionLocal()
        try:
            f = db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.id == fac_id).first()
            cobs = db.query(MonzaContCobranza).filter(
                MonzaContCobranza.factura_id == fac_id).all()
            check("6b la factura es por el TOTAL de la venta",
                  f is not None and round(float(f.monto_bruto)) == TOTAL_BRUTO,
                  getattr(f, "monto_bruto", None))
            check("6c SONDA: nace con UNA cobranza medio='adelanto' por el 100%",
                  len(cobs) == 1 and cobs[0].medio == "adelanto"
                  and round(float(cobs[0].monto)) == TOTAL_BRUTO,
                  [(c.medio, c.monto) for c in cobs])
            check("6d y queda PAGADA con saldo 0 (no se cobra dos veces)",
                  f is not None and round(float(f.saldo or 0)) == 0
                  and (f.estado_pago or "") == "pagada",
                  (getattr(f, "saldo", None), getattr(f, "estado_pago", None)))
            adel = db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.cotizacion_id == cot_id).first()
            check("6e el adelanto quedó APLICADO por el total (consumido, sin resto)",
                  adel is not None and round(float(adel.monto_aplicado or 0)) == TOTAL_BRUTO,
                  getattr(adel, "monto_aplicado", None))
        finally:
            db.close()

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_contado_verificacion_e2e():
    run()


if __name__ == "__main__":
    run()
