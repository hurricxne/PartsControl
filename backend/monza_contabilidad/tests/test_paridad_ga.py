"""Tests de PARIDAD Monza ← GA del módulo Contabilidad MonzaParts (M-PAR 2026-07).

Cubre los arreglos espejados desde routers/contabilidad.py y tesoreria/router.py:
  1. Tope Σ brutos ≤ total venta con precio explícito (excede → 409, dentro → 200).
  2. Precio $0: explícito → 422 (schema gt=0); derivado del ítem → 400 (nunca una
     factura en $0 que nace 'pagada').
  3. Folio SII obligatorio para tipo 'factura' (400 si falta; boleta no lo exige).
  4. Folio duplicado → 409.
  5. Aplicación RETROACTIVA del adelanto en verificar_adelanto: factura previa con
     saldo queda descontada; cap por SALDO actual (no por bruto); factura con
     factoring VIGENTE se salta.
  6. observaciones del adelanto se PRESERVAN si el payload las omite.
  7. fecha_pago no parseable → 400 (no cae en silencio a hoy).
  8. Redondeo half-up por línea e IVA (criterio SII, no el round() banker's).

Datos MARCADOS (__TEST_MPAR__) + limpieza total en finally; verificación de los
invariantes de plata con SESIÓN NUEVA. Requiere la BD local (igual que las suites GA).

Corre con:
  cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_paridad_ga.py -q
  (o directo: ./venv/bin/python monza_contabilidad/tests/test_paridad_ga.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.router import router  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContCobranza, MonzaContAdelanto,
)
from monza_contabilidad.service import _total_linea, _iva_clp, _precio2  # noqa: E402

MARK = "__TEST_MPAR__"
CURRENT = {"empresa": "automotriz", "id": 1}

app = FastAPI()
app.include_router(router)


# Auth REALISTA (mismo patrón que test_integration.py): hace una lectura en la MISMA
# sesión del request, para que el read view de MySQL nazca ANTES del primer lock,
# igual que en producción.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []
# Ventas sembradas: A=tope, B=precio $0 derivado, C=folios, G=boleta,
# D=retro básica, E=cap por saldo, F=factoring saltado + observaciones.
_seed = {"cli_id": None, "cots": {}, "items": {}, "desps": {}, "desp_items": {}}
BASE = "/api/monza/contabilidad"


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _venta(db, key, numero, neto, bruto, qty, precio, pct_adelanto=0, con_despacho=False):
    cot = mm.MonzaCotizacion(
        numero=numero, cliente_id=_seed["cli_id"], estado="vendida",
        total_neto=neto, iva_monto=bruto - neto, total_bruto=bruto, iva_pct=19,
        forma_pago="credito", pct_adelanto=pct_adelanto,
    )
    db.add(cot); db.flush()
    it = mm.MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion=f"Item {key}", numero_parte=f"NP-{key}",
        cantidad=qty, precio_unitario_clp=precio, subtotal_clp=neto,
        estado_linea="despachado" if con_despacho else "en_bodega",
    )
    db.add(it); db.flush()
    _seed["cots"][key] = cot.id
    _seed["items"][key] = it.id
    if con_despacho:
        d = mm.MonzaDespacho(numero=f"{MARK}-DSP-{key}", cotizacion_id=cot.id,
                             estado="despachado", numero_guia=f"G-{key}",
                             # regla 2026-08-06: sin firma no se factura (el gate tiene suite propia)
                             guia_firmada=1)
        db.add(d); db.flush()
        di = mm.MonzaDespachoItem(despacho_id=d.id, item_id=it.id, qty_despachada=qty)
        db.add(di); db.flush()
        _seed["desps"][key] = d.id
        _seed["desp_items"][key] = di.id


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
        _seed["cli_id"] = cli.id
        # A: tope Σ brutos con precio explícito (necesita despacho para modo items)
        _venta(db, "A", f"{MARK}-A", 1000000, 1190000, 10, 100000, con_despacho=True)
        # B: ítem con precio $0 (derivado) — retiro en oficina
        _venta(db, "B", f"{MARK}-B", 0, 0, 1, 0)
        # C: folios (obligatorio / duplicado) — retiro en oficina
        _venta(db, "C", f"{MARK}-C", 100000, 119000, 2, 50000)
        # G: boleta sin folio — retiro en oficina
        _venta(db, "G", f"{MARK}-G", 50000, 59500, 1, 50000)
        # D/E/F: adelanto 50% sobre bruto 119.000 — retiro en oficina
        for k in ("D", "E", "F"):
            _venta(db, k, f"{MARK}-{k}", 100000, 119000, 1, 100000, pct_adelanto=50)
        db.commit()
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        cot_ids = [i for i in _seed["cots"].values() if i]
        if cot_ids:
            # ORM delete: el cascade borra items/cobranzas/factoring de cada factura.
            for f in db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)
            ).all():
                db.delete(f)
            db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.cotizacion_id.in_(cot_ids)
            ).delete(synchronize_session=False)
            db.flush()
        for did in _seed["desp_items"].values():
            db.query(mm.MonzaDespachoItem).filter(mm.MonzaDespachoItem.id == did).delete()
        for did in _seed["desps"].values():
            db.query(mm.MonzaDespacho).filter(mm.MonzaDespacho.id == did).delete()
        for iid in _seed["items"].values():
            db.query(mm.MonzaCotizacionItem).filter(mm.MonzaCotizacionItem.id == iid).delete()
        for cid in cot_ids:
            db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id == cid).delete()
        if _seed["cli_id"]:
            db.query(mm.MonzaCliente).filter(mm.MonzaCliente.id == _seed["cli_id"]).delete()
        db.commit()
        print("Cleanup OK")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print("Cleanup parcial:", e)
    finally:
        db.close()


def _factura_db(factura_id):
    """Relee la factura con SESIÓN NUEVA (verifica lo PERSISTIDO, no el eco del request)."""
    db = SessionLocal()
    try:
        f = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id == factura_id).first()
        cobs = db.query(MonzaContCobranza).filter(
            MonzaContCobranza.factura_id == factura_id).all()
        return {
            "saldo": float(f.saldo or 0), "monto_pagado": float(f.monto_pagado or 0),
            "estado_pago": f.estado_pago, "numero_factura": f.numero_factura,
            "cobranzas": [{"medio": c.medio, "monto": float(c.monto or 0)} for c in cobs],
        }
    finally:
        db.close()


def _adelanto_db(cot_id):
    db = SessionLocal()
    try:
        a = db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id == cot_id).first()
        return None if a is None else {
            "monto": float(a.monto or 0), "monto_aplicado": float(a.monto_aplicado or 0),
            "observaciones": a.observaciones,
        }
    finally:
        db.close()


def run():
    CURRENT["empresa"] = "automotriz"
    cots = _seed["cots"]

    # ── 1) Tope Σ brutos ≤ total venta con PRECIO EXPLÍCITO (venta A, bruto 1.190.000) ──
    linea_a = {"item_cotizacion_id": _seed["items"]["A"],
               "despacho_item_id": _seed["desp_items"]["A"], "cantidad": 10}
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["A"], "numero_factura": f"{MARK}-FA-X",
        "items": [{**linea_a, "precio_unit_neto": 200000}],
    })
    check("tope: precio explícito que excede la venta -> 409", r.status_code == 409, r.text)
    check("tope: mensaje 'excede el total de la venta'",
          "excede el total de la venta" in r.text, r.text)
    # dentro de la venta → 200 (neto 500.000, bruto 595.000 ≤ 1.190.000)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["A"], "numero_factura": f"{MARK}-FA",
        "items": [{**linea_a, "precio_unit_neto": 50000}],
    })
    check("tope: precio explícito dentro de la venta -> 200", r.status_code == 200, r.text)
    if r.status_code == 200:
        fa = r.json()
        check("tope: neto/bruto correctos (500.000 / 595.000)",
              fa["monto_neto"] == 500000 and fa["monto_bruto"] == 595000, fa)

    # ── 2) Precio $0 ──
    # explícito → 422 (schema gt=0, espejo GA contabilidad.py:1217)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["A"], "numero_factura": f"{MARK}-F0",
        "items": [{**linea_a, "precio_unit_neto": 0}],
    })
    check("precio $0 explícito -> 422", r.status_code == 422, r.text)
    # derivado de un ítem en $0 → 400 (antes '< 0': la factura $0 nacía 'pagada')
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["B"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-FB"})
    check("precio $0 derivado del ítem -> 400", r.status_code == 400, r.text)
    check("precio $0: mensaje 'no se puede facturar en $0'",
          "no se puede facturar en $0" in r.text, r.text)

    # ── 3) Folio SII obligatorio para tipo 'factura' (venta C) ──
    r = client.post(f"{BASE}/facturas", json={"cotizacion_id": cots["C"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True})
    check("factura sin folio -> 400", r.status_code == 400, r.text)
    check("folio: mensaje espejo GA", "Ingresa el folio SII" in r.text, r.text)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["C"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": "   "})
    check("folio de puros espacios -> 400", r.status_code == 400, r.text)
    # boleta SIN folio → 200 (venta G) y persiste numero_factura NULL
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["G"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "tipo_doc": "boleta"})
    check("boleta sin folio -> 200", r.status_code == 200, r.text)
    if r.status_code == 200:
        check("boleta persiste folio NULL",
              _factura_db(r.json()["id"])["numero_factura"] is None, r.json())

    # ── 4) Folio duplicado -> 409 ──
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["C"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-DUP"})
    check("factura con folio -> 200", r.status_code == 200, r.text)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["C"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-DUP"})
    check("folio duplicado -> 409", r.status_code == 409, r.text)
    check("folio duplicado: mensaje menciona el folio", "folio" in r.text.lower(), r.text)

    # ── 7) fecha_pago no parseable en verificar_adelanto -> 400 (venta D) ──
    r = client.post(f"{BASE}/ventas/{cots['D']}/adelanto/verificar",
                    json={"monto": 59500, "fecha_pago": "banana"})
    check("verificar con fecha inválida -> 400", r.status_code == 400, r.text)

    # ── 5a) RETRO básica (venta D): facturar PRIMERO, verificar DESPUÉS ──
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["D"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-FD"})
    check("D: factura previa 200", r.status_code == 200, r.text)
    fd_id = r.json()["id"] if r.status_code == 200 else None
    check("D: factura previa saldo completo", r.status_code == 200 and r.json()["saldo"] == 119000, r.text)
    r = client.post(f"{BASE}/ventas/{cots['D']}/adelanto/verificar",
                    json={"monto": 59500, "fecha_pago": "2026-07-01", "banco": "BancoX",
                          "observaciones": "obs-original"})
    check("D: verificar tras facturar -> 200", r.status_code == 200, r.text)
    if fd_id:
        fdb = _factura_db(fd_id)
        adb = _adelanto_db(cots["D"])
        check("D: retro aplica cobranza medio='adelanto' de 59.500",
              any(c["medio"] == "adelanto" and c["monto"] == 59500 for c in fdb["cobranzas"]), fdb)
        check("D: saldo persistido descontado (59.500)", fdb["saldo"] == 59500, fdb)
        check("D: monto_aplicado == Σ cobranzas adelanto",
              adb and adb["monto_aplicado"] == 59500, adb)

    # ── 5b) Cap por SALDO actual, no por bruto (venta E) ──
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["E"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-FE"})
    check("E: factura previa 200", r.status_code == 200, r.text)
    fe_id = r.json()["id"] if r.status_code == 200 else None
    if fe_id:
        r = client.post(f"{BASE}/facturas/{fe_id}/cobranzas", json={"monto": 100000})
        check("E: cobranza parcial 100.000 -> 200", r.status_code == 200, r.text)
    r = client.post(f"{BASE}/ventas/{cots['E']}/adelanto/verificar", json={"monto": 59500})
    check("E: verificar -> 200", r.status_code == 200, r.text)
    if fe_id:
        fdb = _factura_db(fe_id)
        adb = _adelanto_db(cots["E"])
        check("E: cap por saldo — aplica solo 19.000 (no 59.500)",
              adb and adb["monto_aplicado"] == 19000, adb)
        check("E: factura queda saldada (saldo 0, pagada)",
              fdb["saldo"] == 0 and fdb["estado_pago"] == "pagada", fdb)
        check("E: cobranza adelanto por 19.000",
              any(c["medio"] == "adelanto" and c["monto"] == 19000 for c in fdb["cobranzas"]), fdb)

    # ── 5c) Factoring VIGENTE se salta (venta F) + 6) observaciones preservadas ──
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cots["F"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-FF"})
    check("F: factura previa 200", r.status_code == 200, r.text)
    ff_id = r.json()["id"] if r.status_code == 200 else None
    if ff_id:
        r = client.post(f"{BASE}/facturas/{ff_id}/factoring", json={"monto_adelantado": 50000})
        check("F: factoring vigente 200", r.status_code == 200 and r.json()["estado_pago"] == "factorizada", r.text)
    r = client.post(f"{BASE}/ventas/{cots['F']}/adelanto/verificar",
                    json={"monto": 59500, "observaciones": "OBS-F"})
    check("F: verificar con factoring vigente -> 200", r.status_code == 200, r.text)
    if ff_id:
        fdb = _factura_db(ff_id)
        adb = _adelanto_db(cots["F"])
        check("F: retro SALTA la factura cedida (monto_aplicado 0)",
              adb and adb["monto_aplicado"] == 0, adb)
        check("F: sin cobranza medio='adelanto'",
              not any(c["medio"] == "adelanto" for c in fdb["cobranzas"]), fdb)
        # 6) re-verificar SIN observaciones no pisa las existentes (permitido: aplicado=0)
        r = client.post(f"{BASE}/ventas/{cots['F']}/adelanto/verificar", json={"monto": 59500})
        check("F: re-verificar sin observaciones -> 200", r.status_code == 200, r.text)
        adb2 = _adelanto_db(cots["F"])
        check("F: observaciones preservadas al omitirlas",
              adb2 and adb2["observaciones"] == "OBS-F", adb2)

    print()
    if _fails:
        print(f"=== {len(_fails)} FALLO(S): {_fails} ===")
        return False
    print("=== TODO OK ===")
    return True


# ── 8) Redondeo half-up (puro, sin BD; espejo GA _total_linea/_iva_clp) ──────────
def test_redondeo_half_up():
    assert _total_linea(100.5, 1) == 101          # round() banker's daría 100
    assert _total_linea(100.4, 1) == 100
    assert _precio2(100.005) == 100.0 or _precio2(100.005) == 100.01  # 2 dec estables
    assert _iva_clp(150, 0.19) == 29              # 28.5 → half-up 29 (round() daría 28)
    assert _iva_clp(100, 0.19) == 19
    # precio a 2 decimales ANTES de multiplicar (criterio guía/factura)
    assert _total_linea(0.335, 1000) == round(_precio2(0.335) * 1000)


# Wrapper pytest (espejo del patrón GA tesoreria/tests/test_integration.py).
def test_paridad_ga():
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
