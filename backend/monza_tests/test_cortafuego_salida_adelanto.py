"""El adelanto sin verificar también frena la SALIDA de mercadería (V8-04, 2026-08-22).

QUÉ CAMBIA
    Hasta ahora, el pago pendiente solo frenaba la COMPRA al proveedor. El resto quedaba
    cubierto DE REBOTE por el camino físico (sin compra no hay nada que despachar), pero
    mercadería que llega a bodega por otra vía —una reposición, el remanente de otra
    línea— podía salir despachada, con guía al SII, con el adelanto sin cobrar.

    Ahora el mismo predicado protege las puertas por donde la mercadería SALE:
      · crear despacho          → 409
      · emisión de la guía 52   → problema bloqueante en el preview
      · factura de RETIRO EN OFICINA (canal sin_guia) → 409 con puerta de emergencia

    La factura CON GUÍA no lleva guard a propósito: facturar es cómo se le COBRA al
    cliente (el adelanto se aplica retroactivamente cuando Tesorería lo registra), y
    bloquearla sería circular — el cliente necesita la factura para pagar. La salida
    física de ESA mercadería ya está protegida por las dos puertas de arriba.

SONDAS DE PODER DISCRIMINANTE
    · §1/§2 el 409 se afirma ANTES de verificar y se comprueba que NO quedó fila creada
      (la ruta no se recorrió, no es un 4xx casual).
    · §3 tras la verificación de Tesorería, las mismas llamadas pasan.
    · §4 la factura CON GUÍA nunca se bloquea (si alguien "endurece" eso, cae).
    · §5 la puerta de emergencia del retiro deja rastro en el documento.

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_cortafuego_salida_adelanto.py -q
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
    MonzaDespacho, MonzaDespachoItem, MonzaLog, MonzaNotificacion,
)
from monza_router_despachos import router as despachos_router  # noqa: E402

MARK = "test-mzsalida"
COT_MARK = "CSAL"
EMAIL = f"{MARK}@test.invalid"
NETO = 200000
TOTAL_BRUTO = 238000

app = FastAPI()
app.include_router(despachos_router)
app.include_router(contab_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz", rol="admin")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _seed(pct_adelanto=50, verificado=0, qty=2):
    """Venta cerrada con adelanto informado y mercadería EN BODEGA."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA", rut="11.111.111-1")
        db.add(cli)
        db.flush()
        cot = MonzaCotizacion(
            numero=f"{COT_MARK}-{uuid.uuid4().hex[:5].upper()}",
            cliente_id=cli.id, estado="vendida",
            total_neto=NETO, iva_monto=TOTAL_BRUTO - NETO, total_bruto=TOTAL_BRUTO,
            iva_pct=19, oc_cliente=f"OC-{MARK}",
            pct_adelanto=pct_adelanto, adelanto_verificado=verificado,
        )
        db.add(cot)
        db.flush()
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion="Kit distribución", cantidad=qty,
            precio_unitario_clp=NETO / qty, subtotal_clp=NETO,
            estado_linea="en_bodega",
        )
        db.add(it)
        db.commit()
        return {"cot": cot.id, "item": it.id, "cli": cli.id, "qty": qty,
                "numero": cot.numero}
    finally:
        db.close()


def _despachos_de(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaDespacho).filter(MonzaDespacho.cotizacion_id == cot_id).count()
    finally:
        db.close()


def _facturas_de(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.cotizacion_id == cot_id).count()
    finally:
        db.close()


def _verificar_adelanto(cot_id):
    db = SessionLocal()
    try:
        cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
        cot.adelanto_verificado = 1
        db.commit()
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .filter(MonzaCotizacion.numero.like(f"{COT_MARK}%")).all()]
        fac_ids = [r[0] for r in db.query(MonzaContFacturaCliente.id)
                   .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids or [0])).all()]
        desp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                    .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        db.query(MonzaContCobranza).filter(
            MonzaContCobranza.factura_id.in_(fac_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id.in_(fac_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
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
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (db.query(MonzaCotizacion).filter(MonzaCotizacion.numero.like(f"{COT_MARK}%")).count()
                  + db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        # ── 1) DESPACHO: la mercadería no sale con el pago pendiente ─────────────
        s = _seed()
        r = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": s["cot"], "items": [{"item_id": s["item"], "qty": 1}]})
        check("1a SONDA: crear despacho con el adelanto sin verificar → 409",
              r.status_code == 409 and "Adelanto no verificado" in r.text,
              (r.status_code, r.text[:160]))
        check("1b y NO quedó despacho creado (la ruta no se recorrió)",
              _despachos_de(s["cot"]) == 0, _despachos_de(s["cot"]))

        # ── 2) RETIRO EN OFICINA: la otra puerta por donde sale la mercadería ────
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": s["cot"], "sin_guia": True,
            "numero_factura": f"{MARK}-F1", "plazo_dias": 30})
        check("2a SONDA: factura de RETIRO con el adelanto sin verificar → 409",
              r.status_code == 409 and "no se entrega mercadería en retiro" in r.text,
              (r.status_code, r.text[:160]))
        check("2b y NO quedó factura creada", _facturas_de(s["cot"]) == 0, _facturas_de(s["cot"]))

        # ── 3) Tras la verificación de Tesorería, las puertas se abren ───────────
        _verificar_adelanto(s["cot"])
        r = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": s["cot"], "items": [{"item_id": s["item"], "qty": 1}]})
        check("3a con el pago verificado, crear despacho pasa",
              r.status_code == 200, (r.status_code, r.text[:160]))
        check("3b y el despacho existe", _despachos_de(s["cot"]) == 1, _despachos_de(s["cot"]))

        # ── 4) La factura CON GUÍA nunca se bloquea (facturar es COBRAR) ─────────
        s2 = _seed()
        db = SessionLocal()
        try:
            it = db.query(MonzaCotizacionItem).filter(
                MonzaCotizacionItem.id == s2["item"]).first()
            it.estado_linea = "despachado"
            d = MonzaDespacho(numero=f"{MARK}-DSP", cotizacion_id=s2["cot"],
                              estado="despachado", numero_guia=f"G-{MARK}",
                              cliente_nombre=f"{MARK} SpA", guia_firmada=1)
            db.add(d)
            db.flush()
            db.add(MonzaDespachoItem(despacho_id=d.id, item_id=s2["item"],
                                     qty_despachada=s2["qty"]))
            db.commit()
            desp_id = d.id
        finally:
            db.close()
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": s2["cot"], "despacho_id": desp_id,
            "numero_factura": f"{MARK}-F2", "plazo_dias": 30})
        check("4a SONDA: la factura CON GUÍA se emite aunque el adelanto no esté "
              "verificado (facturar es cómo se COBRA; su salida física ya la frenó "
              "el guard del despacho)", r.status_code == 200, (r.status_code, r.text[:200]))

        # ── 5) Puerta de emergencia del retiro: pasa y DEJA RASTRO ───────────────
        s3 = _seed()
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": s3["cot"], "sin_guia": True,
            "numero_factura": f"{MARK}-F3", "plazo_dias": 30,
            "confirmar_retiro_sin_adelanto": True})
        check("5a con la puerta explícita, el retiro pasa", r.status_code == 200,
              (r.status_code, r.text[:200]))
        db = SessionLocal()
        try:
            f = db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.cotizacion_id == s3["cot"]).first()
            check("5b SONDA: y queda RASTRO en el documento de que se autorizó con el "
                  "adelanto pendiente",
                  f is not None and "AÚN NO verificado" in (f.observaciones or ""),
                  getattr(f, "observaciones", None))
        finally:
            db.close()

        # ── 6) Una venta SIN adelanto pactado no se ve afectada ──────────────────
        s4 = _seed(pct_adelanto=0)
        r = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": s4["cot"], "items": [{"item_id": s4["item"], "qty": 1}]})
        check("6a control: sin adelanto pactado, el despacho pasa como siempre",
              r.status_code == 200, (r.status_code, r.text[:160]))

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_cortafuego_salida_adelanto():
    run()


if __name__ == "__main__":
    run()
