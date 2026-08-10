"""GATE de la guía FIRMADA (regla 2026-08-06, paridad MachParts) — suite propia.

La regla que se fija acá, completa:
  1. La firma se marca en DESPACHOS (POST /api/monza/despachos/entidades/{id}/firmar):
     multipart con la foto/PDF y la FECHA de la firma, solo sobre despachos cerrados.
     No existe des-firmar; re-firmar corrige (reemplaza archivo y fecha).
  2. Contabilidad EXIGE la firma para facturar el flujo con guía: modo despacho_id,
     ítems explícitos ligados a un despacho_item, y el tope agregado de ítems sueltos
     cuentan SOLO despachos firmados.
  3. El RETIRO EN OFICINA (sin_guia) no es bypass: su tope excluye la mercadería
     comprometida en despachos vivos (borradores incluidos) — eso se factura desde su
     guía firmada.
  4. La factura de ANTICIPO sigue sin exigir guía (vía B, Fase 7): no nace de una.

Método: sondas de PODER DISCRIMINANTE — cada check está pensado para fallar si se
quita el guard que lo sostiene (verificado a mano al construir la suite: sin el gate
de _construir_factura, los checks 400 devuelven 200/201).

Todo lo que crea lo borra al terminar (BD y uploads/docs quedan intactos). Sin datos
reales y sin llamadas a Wasabil.

Corre con:  cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_guia_firmada_gate.py -q
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.router import router as router_contab  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContFacturaClienteItem, MonzaContAdelanto,
)
from monza_router_despachos import router as router_despachos, _DOCS_DIR  # noqa: E402

MARK = "__TEST_GFG__"
EMAIL = f"test-{MARK}@monza.test"
CURRENT = {"empresa": "automotriz", "id": 1}

app = FastAPI()
app.include_router(router_contab)
app.include_router(router_despachos)


def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"], email=EMAIL)


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []
_docs_subidos = []  # archivos que el firmar e2e dejó en uploads/docs (se borran al final)


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _firmar(desp_id, fecha=None, filename="guia-firmada.jpg", mime="image/jpeg",
            contenido=b"foto-firmada", numero_guia=None):
    """POST multipart al endpoint real. Registra el archivo subido para limpiarlo."""
    data = {"fecha_firma": fecha or date.today().isoformat()}
    if numero_guia:
        data["numero_guia"] = numero_guia
    r = client.post(f"/api/monza/despachos/entidades/{desp_id}/firmar",
                    files={"file": (filename, contenido, mime)}, data=data)
    if r.status_code == 200 and r.json().get("guia_firmada_archivo"):
        _docs_subidos.append(r.json()["guia_firmada_archivo"])
    return r


_S = {"cli": None, "cots": {}, "items": {}, "desps": {}, "desp_items": {}}


def _venta(db, key, qty=10, precio=1000, iva_pct=19):
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-{key}", cliente_id=_S["cli"], estado="vendida",
        total_neto=qty * precio, iva_monto=round(qty * precio * iva_pct / 100),
        total_bruto=round(qty * precio * (1 + iva_pct / 100)), iva_pct=iva_pct,
        forma_pago="contado", oc_cliente=f"OC-{key}",
    )
    db.add(cot); db.flush()
    it = mm.MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion=f"Pieza {key}", numero_parte=f"NP-{key}",
        cantidad=qty, precio_unitario_clp=precio, subtotal_clp=qty * precio,
        estado_linea="despachado",
    )
    db.add(it); db.flush()
    _S["cots"][key] = cot.id
    _S["items"][key] = it.id
    return cot, it


def _despacho(db, key, cot_key, qty, estado="despachado", firmada=0, fecha_guia=None):
    d = mm.MonzaDespacho(
        numero=f"{MARK}-D{key}", cotizacion_id=_S["cots"][cot_key], estado=estado,
        numero_guia=f"G-{key}", guia_firmada=firmada, fecha_guia=fecha_guia,
    )
    db.add(d); db.flush()
    di = mm.MonzaDespachoItem(despacho_id=d.id, item_id=_S["items"][cot_key], qty_despachada=qty)
    db.add(di); db.flush()
    _S["desps"][key] = d.id
    _S["desp_items"][key] = di.id
    return d, di


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
        _S["cli"] = cli.id
        # A: venta con guía cerrada SIN firmar (el corazón del gate).
        _venta(db, "A"); _despacho(db, "A", "A", qty=10)
        # B: venta con guía cerrada sin firmar de 6 + saldo libre de 4 (tope retiro).
        _venta(db, "B"); _despacho(db, "B", "B", qty=6)
        # C: venta con BORRADOR (en_preparacion) de 6 + saldo libre de 4.
        _venta(db, "C"); _despacho(db, "C", "C", qty=6, estado="en_preparacion")
        # D: venta SIN despachos (retiro legítimo + anticipo).
        _venta(db, "D")
        # E: guía en PAPEL cerrada, con fecha de emisión (para la malla fecha_firma >= fecha_guia).
        _venta(db, "E"); _despacho(db, "E", "E", qty=10, fecha_guia=date.today() - timedelta(days=5))
        # F: guía cerrada y FIRMADA de 6 — para la sonda inversa del canal (sueltos
        # consumen el cupo de la guía; el retiro del saldo libre NO debe bloquearse).
        _venta(db, "F"); _despacho(db, "F", "F", qty=6, firmada=1)
        db.commit()
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        cot_ids = [c for c in _S["cots"].values() if c]
        if cot_ids:
            for f in db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)
            ).all():
                db.delete(f)
            db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.cotizacion_id.in_(cot_ids)
            ).delete(synchronize_session=False)
            db.flush()
        for did in _S["desp_items"].values():
            db.query(mm.MonzaDespachoItem).filter(mm.MonzaDespachoItem.id == did).delete()
        for did in _S["desps"].values():
            db.query(mm.MonzaDespacho).filter(mm.MonzaDespacho.id == did).delete()
        for iid in _S["items"].values():
            db.query(mm.MonzaCotizacionItem).filter(mm.MonzaCotizacionItem.id == iid).delete()
        for cid in cot_ids:
            db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id == cid).delete()
        if _S["cli"]:
            db.query(mm.MonzaCliente).filter(mm.MonzaCliente.id == _S["cli"]).delete()
        db.query(mm.MonzaLog).filter(mm.MonzaLog.user_email == EMAIL).delete(synchronize_session=False)
        db.commit()
        print("Cleanup BD OK")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print("Cleanup parcial:", e)
    finally:
        db.close()
    for fn in _docs_subidos:
        ruta = os.path.join(_DOCS_DIR, fn)
        if os.path.isfile(ruta):
            os.remove(ruta)
    print("Cleanup docs OK")


def _desp_flag(desp_id):
    db = SessionLocal()
    try:
        d = db.query(mm.MonzaDespacho).filter(mm.MonzaDespacho.id == desp_id).first()
        return (bool(d.guia_firmada), d.fecha_firma, d.guia_firmada_archivo, d.usuario_firma_id, d.numero_guia)
    finally:
        db.close()


def run():
    hoy = date.today()

    # ══ 1 · EL GATE: facturar guía cerrada SIN firmar → 400 con mensaje accionable ══
    # (Sonda: sin el guard de _construir_factura este POST devuelve 201.)
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["A"], "despacho_id": _S["desps"]["A"],
        "numero_factura": f"F-{MARK}-A0"})
    check("gate: guía sin firmar no factura (400)", r.status_code == 400, r.text)
    check("gate: el error dice cómo salir (FIRMADA / Despachos)",
          r.status_code == 400 and "FIRMADA" in r.json()["detail"] and "Despachos" in r.json()["detail"], r.text)

    # El preview propaga el MISMO problema (misma función; el modal avisa antes del clic).
    r = client.post("/api/monza/contabilidad/facturas/preview", json={
        "cotizacion_id": _S["cots"]["A"], "despacho_id": _S["desps"]["A"]})
    check("preview: publica el problema de la firma",
          r.status_code == 200 and not r.json()["puede_emitir"]
          and any("FIRMADA" in str(p) for p in r.json()["problemas"]), r.text)

    # Ítems explícitos LIGADOS a la guía sin firmar → mismo 400 (no hay puerta lateral).
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["A"], "numero_factura": f"F-{MARK}-A1",
        "items": [{"item_cotizacion_id": _S["items"]["A"], "despacho_item_id": _S["desp_items"]["A"], "cantidad": 2}]})
    check("gate: ítems ligados a guía sin firmar → 400", r.status_code == 400, r.text)

    # Ítems explícitos SUELTOS (sin despacho_item_id) tampoco: el tope agregado
    # cuenta SOLO firmados, y con 0 firmado el ítem "no ha sido despachado con firma".
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["A"], "numero_factura": f"F-{MARK}-A2",
        "items": [{"item_cotizacion_id": _S["items"]["A"], "cantidad": 2}]})
    check("gate: ítems sueltos contra guía sin firmar → 400", r.status_code == 400, r.text)

    # despachos-facturables NO oculta la guía: viaja con guia_firmada=false (el
    # selector la deshabilita con el motivo).
    r = client.get(f"/api/monza/contabilidad/ventas/{_S['cots']['A']}/despachos-facturables")
    check("facturables: la guía sin firmar se lista con flag en falso",
          r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["guia_firmada"] is False
          and r.json()[0].get("fecha_firma") is None, r.text)

    # ══ 2 · FIRMAR: validaciones del endpoint ══
    # Borrador → 400 (primero confirmar).
    r = _firmar(_S["desps"]["C"])
    check("firmar borrador → 400", r.status_code == 400 and "confirma" in r.json()["detail"].lower(), r.text)
    # Sin archivo → 422 (multipart lo exige).
    r = client.post(f"/api/monza/despachos/entidades/{_S['desps']['A']}/firmar",
                    data={"fecha_firma": hoy.isoformat()})
    check("firmar sin archivo → 422", r.status_code == 422, r.text)
    # Extensión no permitida → 400.
    r = _firmar(_S["desps"]["A"], filename="guia.xlsx", mime="application/vnd.ms-excel")
    check("firmar .xlsx → 400", r.status_code == 400 and "no permitido" in r.json()["detail"], r.text)
    # Archivo vacío → 400.
    r = _firmar(_S["desps"]["A"], contenido=b"")
    check("firmar archivo vacío → 400", r.status_code == 400 and "vacío" in r.json()["detail"], r.text)
    # Fecha futura → 400.
    r = _firmar(_S["desps"]["A"], fecha=(hoy + timedelta(days=2)).isoformat())
    check("firmar con fecha futura → 400", r.status_code == 400 and "futura" in r.json()["detail"], r.text)
    # Fecha malformada → 400.
    r = _firmar(_S["desps"]["A"], fecha="07-08-2026")
    check("firmar con fecha malformada → 400", r.status_code == 400, r.text)
    # Fecha ANTERIOR a la emisión de la guía en papel → 400 (nadie firma antes de que exista).
    r = _firmar(_S["desps"]["E"], fecha=(hoy - timedelta(days=9)).isoformat())
    check("firma anterior a la emisión de la guía → 400",
          r.status_code == 400 and "ANTERIOR" in r.json()["detail"], r.text)

    # Firmar BIEN → 200; BD con flag + fecha + archivo + usuario; archivo en disco.
    r = _firmar(_S["desps"]["A"], fecha=hoy.isoformat())
    check("firmar ok → 200", r.status_code == 200 and r.json()["guia_firmada"] is True, r.text)
    flag, fecha_bd, archivo_bd, user_bd, _ng = _desp_flag(_S["desps"]["A"])
    check("firma persistida (flag+fecha+archivo+usuario)",
          flag and fecha_bd and fecha_bd.date() == hoy and archivo_bd and user_bd == CURRENT["id"],
          f"{flag}/{fecha_bd}/{archivo_bd}/{user_bd}")
    check("archivo físico en uploads/docs", archivo_bd and os.path.isfile(os.path.join(_DOCS_DIR, archivo_bd)))

    # El GET del doc lo sirve; el path traversal se corta.
    r = client.get(f"/api/monza/despachos/docs/{archivo_bd}")
    check("GET doc de la firma → 200", r.status_code == 200 and r.content == b"foto-firmada", r.status_code)
    r = client.get("/api/monza/despachos/docs/..%2F..%2Fmain.py")
    check("GET doc path traversal → 400/404", r.status_code in (400, 404), r.status_code)

    # RE-firmar corrige (reemplaza fecha y archivo); no existe des-firmar.
    r = _firmar(_S["desps"]["A"], fecha=(hoy - timedelta(days=1)).isoformat(), contenido=b"foto-v2")
    flag2, fecha2, archivo2, _u2, _ng2 = _desp_flag(_S["desps"]["A"])
    check("re-firmar corrige fecha y archivo",
          r.status_code == 200 and flag2 and fecha2.date() == hoy - timedelta(days=1) and archivo2 != archivo_bd,
          f"{fecha2}/{archivo2}")

    # El invariante firma>=emisión no se rompe por la puerta de atrás: editar la
    # fecha de EMISIÓN (PUT cabecera) a una posterior a la firma → 400.
    r = client.put(f"/api/monza/despachos/entidades/{_S['desps']['A']}/",
                   json={"fecha_guia": hoy.isoformat()})
    if r.status_code == 404:  # sin slash final, según cómo resuelva la ruta
        r = client.put(f"/api/monza/despachos/entidades/{_S['desps']['A']}",
                       json={"fecha_guia": hoy.isoformat()})
    check("editar emisión posterior a la firma → 400 (invariante protegido)",
          r.status_code == 400 and "FIRMADA" in r.json().get("detail", ""), r.text)

    # ══ 3 · CON la firma, la guía se factura (y el flujo del dinero sigue) ══
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["A"], "despacho_id": _S["desps"]["A"],
        "numero_factura": f"F-{MARK}-A9"})
    check("con firma: facturar → 201", r.status_code in (200, 201), r.text)
    check("con firma: monto correcto (10×1000×1.19)",
          r.status_code in (200, 201) and r.json()["monto_bruto"] == 11900, r.text)

    # ══ 4 · RETIRO EN OFICINA: el tope excluye lo comprometido en guías ══
    # B: vendido 10, guía cerrada SIN firmar por 6 → el retiro deriva SOLO 4.
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["B"], "sin_guia": True, "numero_factura": f"F-{MARK}-B1"})
    check("retiro con guía pendiente: deriva solo el saldo libre (4×1000×1.19)",
          r.status_code in (200, 201) and r.json()["monto_bruto"] == 4760, r.text)
    # Reintento del retiro: ya no queda nada libre — el 409 debe MANDAR a la guía.
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["B"], "sin_guia": True, "numero_factura": f"F-{MARK}-B2"})
    check("retiro agotado con guía pendiente → 409 que manda a la guía firmada",
          r.status_code == 409 and "guía" in r.json()["detail"].lower(), r.text)

    # ── CONTINUACIÓN de B (sonda del hallazgo HIGH del multienjambre 2026-08-07:
    # el orden retiro→firma→guía dejaba las unidades de la guía infacturables por el
    # DOBLE descuento entre canales). Con el canal (factura.sin_guia) el retiro de 4
    # ya NO come el cupo de la guía: firmarla debe facturar sus 6 u COMPLETAS y la
    # venta debe cerrar 100% facturada.
    r = _firmar(_S["desps"]["B"], fecha=hoy.isoformat())
    check("B: firmar la guía tras el retiro → 200", r.status_code == 200, r.text)
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["B"], "despacho_id": _S["desps"]["B"],
        "numero_factura": f"F-{MARK}-B3"})
    check("B: la guía factura sus 6 u completas (6×1000×1.19, sin doble descuento)",
          r.status_code in (200, 201) and r.json()["monto_bruto"] == 7140, r.text)
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["B"], "sin_guia": True, "numero_factura": f"F-{MARK}-B4"})
    check("B: venta 100% facturada (4760+7140=11900) → nuevo retiro 409",
          r.status_code == 409, r.text)

    # C: BORRADOR de 6 también bloquea (mercadería comprometida a salir con guía).
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["C"], "sin_guia": True, "numero_factura": f"F-{MARK}-C1"})
    check("retiro con BORRADOR comprometido: deriva solo el saldo libre (4×1000×1.19)",
          r.status_code in (200, 201) and r.json()["monto_bruto"] == 4760, r.text)

    # D: venta sin despachos — retiro legítimo COMPLETO.
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["D"], "sin_guia": True, "numero_factura": f"F-{MARK}-D1"})
    check("retiro legítimo (sin despachos) → factura el total",
          r.status_code in (200, 201) and r.json()["monto_bruto"] == 11900, r.text)

    # F: sonda INVERSA del canal (manifestación espejo del hallazgo HIGH): ítems
    # SUELTOS (sin despacho_item_id, vía API) consumen el cupo de la guía firmada;
    # el retiro del saldo libre real (4 u jamás comprometidas) NO debe bloquearse.
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["F"], "numero_factura": f"F-{MARK}-F1",
        "items": [{"item_cotizacion_id": _S["items"]["F"], "cantidad": 6}]})
    check("F: sueltos contra guía firmada → 6 u (7140)",
          r.status_code in (200, 201) and r.json()["monto_bruto"] == 7140, r.text)
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["F"], "sin_guia": True, "numero_factura": f"F-{MARK}-F2"})
    check("F: retiro del saldo libre tras sueltos → 4 u (4760), sin bloqueo falso",
          r.status_code in (200, 201) and r.json()["monto_bruto"] == 4760, r.text)

    # ══ 5 · La factura de ANTICIPO sigue SIN exigir guía (regresión Fase 7) ══
    db = SessionLocal()
    try:
        # El anticipo exige el adelanto verificado: se siembra directo (vía B real).
        db.add(MonzaContAdelanto(cotizacion_id=_S["cots"]["E"], monto=5950, estado="aprobado"))
        cot_e = db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id == _S["cots"]["E"]).first()
        cot_e.pct_adelanto = 50
        cot_e.adelanto_verificado = 1
        db.commit()
    finally:
        db.close()
    # Folio NUMÉRICO: la vía B lo exige (la factura de la mercadería lo referencia
    # ante el SII). Se genera único por corrida; la limpieza borra la factura igual.
    import uuid as _uuid
    folio_anticipo = str(900_000_000_000 + _uuid.uuid4().int % 10**11)
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": _S["cots"]["E"], "es_anticipo": True, "monto_neto_anticipo": 5000,
        "numero_factura": folio_anticipo})
    check("anticipo: no exige guía firmada (la guía E sigue sin firmar)",
          r.status_code in (200, 201), r.text)

    # ── SONDA del CINTURÓN DEL REINTENTO SII ──────────────────────────────────
    # El gate de creación no cubre el REINTENTO: ese camino re-arma el payload desde
    # las líneas ya congeladas de una factura y vuelve a emitir al SII (acto nuevo e
    # IRREVERSIBLE). Una factura pre-candado ligada a una guía jamás firmada no puede
    # re-emitirse. Sin esta sonda, borrar el cinturón dejaba la suite entera verde
    # (hallazgo MEDIUM de la ronda 2: "no tiene ninguna sonda discriminante").
    # Se prueba la FUNCIÓN (_armar_payload_factura), no el endpoint: emitir de verdad
    # exige credenciales y un documento real en Wasabil.
    from monza_wasabil_dte.router import _armar_payload_factura  # noqa: E402
    db = SessionLocal()
    try:
        # Factura "pre-candado": se persiste a mano ligada a la guía E (sin firmar),
        # que es justo lo que el gate de creación ya no deja nacer.
        f_legada = MonzaContFacturaCliente(
            cotizacion_id=_S["cots"]["E"], despacho_id=_S["desps"]["E"],
            numero_factura=f"{MARK}-RETRY", tipo_doc="factura",
            fecha_emision=hoy, cliente_nombre=MARK, monto_neto=1000,
            iva=190, monto_bruto=1190,
        )
        db.add(f_legada); db.flush()
        db.add(MonzaContFacturaClienteItem(
            factura_id=f_legada.id, item_cotizacion_id=_S["items"]["E"],
            numero_parte="P-1", descripcion="parte", cantidad=1,
            precio_unit_neto=1000, total_neto=1000,
        ))
        db.commit()
        db.refresh(f_legada)
        _doc, problemas = _armar_payload_factura(db, f_legada, client_id=1, issue=True)
        check("reintento SII: una factura ligada a guía SIN firmar NO se re-emite",
              any("FIRMADA" in p for p in problemas), problemas)
        # Espejo POSITIVO (poder discriminante): con la guía firmada, el mismo
        # payload deja de tener ESE problema — el cinturón no bloquea de más.
        db.query(mm.MonzaDespacho).filter(
            mm.MonzaDespacho.id == _S["desps"]["E"]).update({"guia_firmada": 1})
        db.commit()
        _doc2, problemas2 = _armar_payload_factura(db, f_legada, client_id=1, issue=True)
        check("reintento SII: con la guía firmada, el cinturón deja pasar",
              not any("FIRMADA" in p for p in problemas2), problemas2)
    finally:
        db.close()

    print()
    ok = not _fails
    print("RESULTADO:", "TODO OK" if ok else f"{len(_fails)} fallas: {_fails}")
    assert ok, f"fallas: {_fails}"


def test_guia_firmada_gate():
    cleanup_previo = dict(_S)  # noqa: F841 — checkpoint de depuración
    seed()
    try:
        run()
    finally:
        cleanup()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
