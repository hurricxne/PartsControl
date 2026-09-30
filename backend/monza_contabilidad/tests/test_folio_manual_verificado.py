"""REGISTRO MANUAL VERIFICADO de facturas y boletas (2026-09-29) — suite propia.

Lo que se fija acá (ver monza_contabilidad/verificacion_folio.py):
  1. Todo registro manual declara ORIGEN: 'wasabil' (el folio se verifica: existe, tipo,
     emitido, total y —en factura— RUT) o 'externo' (declaración obligatoria; si el folio
     SÍ está en Wasabil se bloquea). Wasabil caído → 503 para 'wasabil'; el 'externo'
     pasa con su declaración marcada como no comprobada (dueño 2026-09-30).
  2. Folio OBLIGATORIO también para boleta, y único POR TIPO: la boleta N° X y la
     factura N° X conviven (uq_monza_cont_factura_tipo_folio).
  3. BOLETA sin guía firmada (venta B2C): se boletea un despacho 'despachado' con la
     guía sin firmar; la FACTURA sigue exigiendo la firma (control).
  4. La fecha de un documento verificado es la de Wasabil; la auditoría queda en la
     factura (origen, uuid, quién, cuándo, declaración).
  5. POST /facturas/verificar-folio NO persiste nada.
  6. Con MONZA_FOLIO_VERIFICACION=false rige la regla anterior (boleta sin folio → 200) y
     verificar-folio responde ok sin consultar Wasabil (el modal no queda trabado).

ESTILO de la casa: datos MARCADOS, limpieza total en `finally`, auth realista (una lectura
en la misma sesión del request) y `check()` que acumula. Wasabil SIMULADO: se reemplaza
`verificacion_folio.buscar_emitidos_por_folio` — la suite NUNCA toca el API real.

Requiere la BD local con `python -m migrations.monza_factura_folio_por_tipo` corrido.

Corre con:
  cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_folio_manual_verificado.py -q
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session, selectinload  # noqa: E402

from config import settings  # noqa: E402
from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad import verificacion_folio  # noqa: E402
from monza_contabilidad.router import router as router_contab  # noqa: E402
from monza_contabilidad.models import MonzaContFacturaCliente, MonzaContAdelanto  # noqa: E402
import monza_wasabil_compras.client as wc  # noqa: E402
from monza_wasabil_compras.client import WasabilComprasError  # noqa: E402

MARK = "__TEST_FMV__"
EMAIL = f"test-{MARK}@monza.test"
RUT_CLIENTE = "76.543.210-3"  # DV válido: la factura exige un RUT válido (_validar_receptor_factura)
BASE = "/api/monza/contabilidad"
# Folios altos para no chocar con datos de desarrollo. El MISMO número se usa como boleta
# y como factura a propósito (unicidad por tipo).
F_BOLETA_A = "987650001"
F_FACTURA_C = F_BOLETA_A
F_BOLETA_B = "987650002"
F_EXTERNO_D = "987650003"
F_EN_WASABIL = "987650004"
FECHA_WASABIL = "2026-09-20"

app = FastAPI()
app.include_router(router_contab)


def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))  # auth realista: el read view nace antes de los locks
    return SimpleNamespace(id=None, empresa="automotriz", email=EMAIL)


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Wasabil simulado ─────────────────────────────────────────────────────────────
# (codigo_sii, folio int) -> lista de documentos. WASABIL["caido"] = True simula timeout.
WASABIL = {"docs": {}, "caido": False}


def _doc(tipo, folio, total, rut="66.666.666-6", status=3, uuid=None):
    return {"uuid": uuid or f"uuid-{tipo}-{folio}", "sii_document_type_id": tipo,
            "folio": str(folio), "status_id": status, "sandbox": False, "received": False,
            "document_date": FECHA_WASABIL, "receiver_rut": rut, "receiver_name": "Receptor",
            "sent_nsubtotal": round(total / 1.19), "sent_niva": total - round(total / 1.19),
            "sent_ntotal": total}


def _fake_buscar(codigo, folio):
    if WASABIL["caido"]:
        raise WasabilComprasError("Wasabil no respondió a tiempo (timeout)")
    return list(WASABIL["docs"].get((str(codigo), int(folio)), []))


_S = {"cli": None, "cots": {}, "items": {}, "desps": {}, "desp_items": {}}


def _venta(db, key, qty=10, precio=1000):
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-{key}", cliente_id=_S["cli"], estado="vendida",
        total_neto=qty * precio, iva_monto=round(qty * precio * 0.19),
        total_bruto=round(qty * precio * 1.19), iva_pct=19,
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


def _despacho(db, key, qty=10, firmada=0, dkey=None):
    dkey = dkey or key
    d = mm.MonzaDespacho(numero=f"{MARK}-D{dkey}", cotizacion_id=_S["cots"][key],
                         estado="despachado", numero_guia=f"G-{dkey}", guia_firmada=firmada)
    db.add(d); db.flush()
    di = mm.MonzaDespachoItem(despacho_id=d.id, item_id=_S["items"][key], qty_despachada=qty)
    db.add(di); db.flush()
    _S["desps"][dkey] = d.id
    _S["desp_items"][dkey] = di.id


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut=RUT_CLIENTE)
        db.add(cli); db.flush()
        _S["cli"] = cli.id
        _venta(db, "A"); _despacho(db, "A", firmada=0)   # B2C: guía SIN firmar
        _venta(db, "B")                                  # boleta por retiro
        _venta(db, "C")                                  # factura por retiro (RUT)
        _venta(db, "D")                                  # documento externo
        _venta(db, "E")                                  # flag apagado
        _venta(db, "F")                                  # errores varios (nada se crea)
        _venta(db, "H")                                  # externo con Wasabil caído
        # G: mismo ítem en DOS guías — G1 firmada (5) y G2 sin firmar (5). La boleta de
        # G2 no puede comerse el cupo de la factura de G1 (hallazgo de la revisión).
        _venta(db, "G")
        _despacho(db, "G", qty=5, firmada=1, dkey="G1")
        _despacho(db, "G", qty=5, firmada=0, dkey="G2")
        db.commit()
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        cot_ids = [c for c in _S["cots"].values() if c]
        if cot_ids:
            for f in db.query(MonzaContFacturaCliente).filter(
                    MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all():
                db.delete(f)
            db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
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
        db.commit()
    finally:
        db.close()
    # Verificación con SESIÓN NUEVA: no quedó nada marcado.
    db = SessionLocal()
    try:
        quedan = db.execute(text("SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE :m"),
                            {"m": f"{MARK}%"}).scalar()
        print("Cleanup BD OK" if quedan == 0 else f"Cleanup INCOMPLETO: {quedan} cotizaciones")
    finally:
        db.close()


def _facturas(key):
    # selectinload: las filas se leen después de cerrar la sesión (sin él, `.items`
    # revienta con DetachedInstanceError).
    db = SessionLocal()
    try:
        return (db.query(MonzaContFacturaCliente)
                .options(selectinload(MonzaContFacturaCliente.items))
                .filter(MonzaContFacturaCliente.cotizacion_id == _S["cots"][key]).all())
    finally:
        db.close()


def _post(body):
    return client.post(f"{BASE}/facturas", json=body)


def run():
    # ══ 1 · B2C: boleta sobre guía SIN firmar (la factura sigue exigiendo firma) ══
    r = _post({"cotizacion_id": _S["cots"]["A"], "despacho_id": _S["desps"]["A"],
               "tipo_doc": "factura", "numero_factura": "987650099", "origen_folio": "wasabil"})
    check("1a control: FACTURA sobre guía sin firmar sigue en 400",
          r.status_code == 400 and "FIRMADA" in r.text, r.text)

    WASABIL["docs"][("39", int(F_BOLETA_A))] = [_doc(39, F_BOLETA_A, 11900)]
    body_a = {"cotizacion_id": _S["cots"]["A"], "despacho_id": _S["desps"]["A"],
              "tipo_doc": "boleta", "numero_factura": F_BOLETA_A, "origen_folio": "wasabil",
              "fecha_emision": "2026-01-01"}
    r = client.post(f"{BASE}/facturas/verificar-folio", json=body_a)
    check("1b verificar-folio: boleta sobre guía sin firmar → verificado",
          r.status_code == 200 and r.json()["ok"] and r.json()["estado"] == "verificado", r.text)
    check("1c verificar-folio NO persiste nada", len(_facturas("A")) == 0)

    r = _post(body_a)
    check("1d registrar la boleta → 200", r.status_code == 200, r.text)
    fa = _facturas("A")
    check("1e quedó UNA boleta con origen wasabil y uuid",
          len(fa) == 1 and fa[0].tipo_doc == "boleta" and fa[0].origen_folio == "wasabil"
          and fa[0].wasabil_uuid == f"uuid-39-{F_BOLETA_A}" and fa[0].folio_verificado_at is not None,
          [(f.tipo_doc, f.origen_folio, f.wasabil_uuid) for f in fa])
    check("1f la fecha es la de Wasabil, no la tecleada",
          len(fa) == 1 and str(fa[0].fecha_emision) == FECHA_WASABIL,
          fa[0].fecha_emision if fa else None)
    check("1g advierte la guía sin firmar",
          r.status_code == 200 and any("SIN firmar" in a for a in r.json().get("advertencias", [])), r.text)

    # ══ 2 · Boleta por retiro: monto distinto bloquea, el correcto pasa ══
    WASABIL["docs"][("39", int(F_BOLETA_B))] = [_doc(39, F_BOLETA_B, 12500)]
    body_b = {"cotizacion_id": _S["cots"]["B"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True,
              "tipo_doc": "boleta", "numero_factura": F_BOLETA_B, "origen_folio": "wasabil"}
    r = _post(body_b)
    check("2a total de Wasabil distinto → 409 con los dos montos",
          r.status_code == 409 and "no coincide" in r.text, r.text)
    check("2b no nació ninguna boleta", len(_facturas("B")) == 0)
    WASABIL["docs"][("39", int(F_BOLETA_B))] = [_doc(39, F_BOLETA_B, 11900)]
    r = _post(body_b)
    check("2c con el total correcto → 200", r.status_code == 200, r.text)

    # ══ 3 · Factura por retiro: RUT del receptor + folio repetido de OTRO tipo ══
    WASABIL["docs"][("33", int(F_FACTURA_C))] = [_doc(33, F_FACTURA_C, 11900, rut="11.111.111-1")]
    body_c = {"cotizacion_id": _S["cots"]["C"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True,
              "tipo_doc": "factura", "numero_factura": F_FACTURA_C, "origen_folio": "wasabil"}
    r = _post(body_c)
    check("3a factura emitida a OTRO RUT → 409", r.status_code == 409 and "RUT" in r.text, r.text)
    WASABIL["docs"][("33", int(F_FACTURA_C))] = [_doc(33, F_FACTURA_C, 11900, rut="76543210-3")]
    r = _post(body_c)
    check("3b factura N° X convive con la boleta N° X (unicidad por tipo) → 200",
          r.status_code == 200, r.text)
    r = client.post(f"{BASE}/facturas/verificar-folio", json=body_c)
    check("3c el mismo folio+tipo otra vez → 'duplicado'",
          r.status_code == 200 and not r.json()["ok"] and r.json()["estado"] == "duplicado", r.text)
    r = _post(body_c)
    check("3d registrarlo otra vez → 409 folio duplicado (no «ya facturada»)",
          r.status_code == 409 and "ya existe" in r.text, r.text)

    # ══ 4 · Documento emitido FUERA de Wasabil ══
    body_d = {"cotizacion_id": _S["cots"]["D"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True,
              "tipo_doc": "boleta", "numero_factura": F_EXTERNO_D, "origen_folio": "externo",
              "declaracion_externo": "corta"}
    r = _post(body_d)
    check("4a declaración demasiado corta → 400", r.status_code == 400, r.text)
    WASABIL["docs"][("39", int(F_EXTERNO_D))] = [_doc(39, F_EXTERNO_D, 11900)]
    body_d["declaracion_externo"] = "Boleta emitida en el POS de la tienda"
    r = _post(body_d)
    check("4b 'externo' pero el folio SÍ está en Wasabil → 409", r.status_code == 409, r.text)
    WASABIL["docs"].pop(("39", int(F_EXTERNO_D)))
    r = _post(body_d)
    check("4c externo con declaración → 200", r.status_code == 200, r.text)
    fd = _facturas("D")
    check("4d auditoría del externo (origen, declaración, sin uuid)",
          len(fd) == 1 and fd[0].origen_folio == "externo" and fd[0].wasabil_uuid is None
          and "POS" in (fd[0].declaracion_externo or ""),
          [(f.origen_folio, f.declaracion_externo) for f in fd])

    # ══ 5 · Errores que NO deben crear nada (venta F) ══
    base_f = {"cotizacion_id": _S["cots"]["F"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True,
              "tipo_doc": "boleta"}
    r = _post({**base_f, "numero_factura": "987650010"})
    check("5a sin origen → 400", r.status_code == 400 and "origen" in r.text.lower(), r.text)
    r = _post({**base_f, "origen_folio": "wasabil"})
    check("5b boleta sin folio → 400", r.status_code == 400, r.text)
    r = _post({**base_f, "numero_factura": "B-35", "origen_folio": "wasabil"})
    check("5c folio no numérico → 400", r.status_code == 400, r.text)
    r = _post({**base_f, "numero_factura": "987650011", "origen_folio": "wasabil"})
    check("5d folio que no existe en Wasabil → 409", r.status_code == 409, r.text)
    WASABIL["docs"][("39", 987650012)] = [_doc(39, 987650012, 11900, status=4)]
    r = _post({**base_f, "numero_factura": "987650012", "origen_folio": "wasabil"})
    check("5e documento FALLIDO en Wasabil → 409", r.status_code == 409, r.text)
    WASABIL["caido"] = True
    r = _post({**base_f, "numero_factura": "987650013", "origen_folio": "wasabil"})
    check("5f Wasabil caído → 503 (no se registra sin verificar)", r.status_code == 503, r.text)
    WASABIL["caido"] = False
    check("5h ningún error dejó facturas", len(_facturas("F")) == 0,
          [f.numero_factura for f in _facturas("F")])

    # ══ 5-ter · Wasabil caído + documento EXTERNO (venta H): pasa marcado (dueño 2026-09-30) ══
    WASABIL["caido"] = True
    r = _post({"cotizacion_id": _S["cots"]["H"], "sin_guia": True, "confirmar_retiro_sin_adelanto": True,
               "tipo_doc": "boleta", "numero_factura": "987650013", "origen_folio": "externo",
               "declaracion_externo": "Boleta antigua emitida en el portal del SII"})
    WASABIL["caido"] = False
    check("5g Wasabil caído + externo → 200 (la declaración es el respaldo)", r.status_code == 200, r.text)
    check("5g-bis advierte que el folio no se comprobó contra Wasabil",
          r.status_code == 200 and any("no comprobado" in a for a in r.json().get("advertencias", [])), r.text)
    fh = _facturas("H")
    check("5g-ter la declaración queda con la marca del sistema",
          len(fh) == 1 and fh[0].origen_folio == "externo"
          and (fh[0].declaracion_externo or "").startswith("Boleta antigua emitida en el portal del SII\n[sistema ")
          and "no se comprobó" in fh[0].declaracion_externo,
          [f.declaracion_externo for f in fh])

    # ══ 5-bis · Guía firmada + guía sin firmar del MISMO ítem (venta G) ══
    # Sonda: sin _qty_boleta_en_guia_sin_firma_por_item, 5-bis-b responde 409 «ya fue
    # facturado por completo» con la guía G1 intacta.
    WASABIL["docs"][("39", 987650020)] = [_doc(39, 987650020, 5950)]
    r = _post({"cotizacion_id": _S["cots"]["G"], "despacho_id": _S["desps"]["G2"],
               "tipo_doc": "boleta", "numero_factura": "987650020", "origen_folio": "wasabil"})
    check("5-bis-a boleta de la guía SIN firmar (5 u) → 200", r.status_code == 200, r.text)
    WASABIL["docs"][("33", 987650021)] = [_doc(33, 987650021, 5950, rut=RUT_CLIENTE)]
    r = _post({"cotizacion_id": _S["cots"]["G"], "despacho_id": _S["desps"]["G1"],
               "tipo_doc": "factura", "numero_factura": "987650021", "origen_folio": "wasabil"})
    check("5-bis-b factura de la guía FIRMADA sigue disponible (5 u) → 200",
          r.status_code == 200, r.text)
    WASABIL["docs"][("33", 987650022)] = [_doc(33, 987650022, 1190, rut=RUT_CLIENTE)]
    r = _post({"cotizacion_id": _S["cots"]["G"], "tipo_doc": "factura", "numero_factura": "987650022",
               "origen_folio": "wasabil",
               "items": [{"item_cotizacion_id": _S["items"]["G"], "cantidad": 1}]})
    check("5-bis-c y ya no queda NADA por facturar (techo global) → 409/400",
          r.status_code in (400, 409), r.text)
    check("5-bis-d la venta G tiene exactamente 10 u facturadas",
          sum(float(i.cantidad) for f in _facturas("G") for i in f.items) == 10.0)

    # ══ 6 · Interruptor apagado: regla anterior (boleta sin folio → 200) ══
    settings.MONZA_FOLIO_VERIFICACION = False
    WASABIL["caido"] = True   # si se consultara Wasabil, 6b respondería 503
    try:
        r = client.post(f"{BASE}/facturas/verificar-folio",
                        json={"cotizacion_id": _S["cots"]["E"], "sin_guia": True,
                              "confirmar_retiro_sin_adelanto": True, "tipo_doc": "boleta",
                              "numero_factura": "987650030", "origen_folio": "wasabil"})
        check("6b flag apagado: verificar-folio responde ok sin consultar Wasabil (la pantalla no se traba)",
              r.status_code == 200 and r.json()["ok"] and r.json()["estado"] == "sin_verificacion", r.text)
        r = _post({"cotizacion_id": _S["cots"]["E"], "sin_guia": True,
                   "confirmar_retiro_sin_adelanto": True, "tipo_doc": "boleta"})
        check("6a flag apagado: boleta sin folio ni origen → 200", r.status_code == 200, r.text)
    finally:
        settings.MONZA_FOLIO_VERIFICACION = True
        WASABIL["caido"] = False

    # ══ 7 · Cliente de consulta: si Wasabil ignora un filtro, es «no sé», no «no existe» ══
    # (con «no existe» el origen 'externo' pasaría como si el folio no estuviera en Wasabil)
    original_post = wc._post_query
    try:
        def _resp(items):
            return lambda body, params=None, timeout=None: {
                "success": True, "data": {"list": {"items": items, "total": len(items), "lastPage": 1}}}
        enviados = []

        def _registra(body, params=None, timeout=None):
            enviados.append(body)
            return _resp([_doc(39, 987650040, 11900)])(body)
        wc._post_query = _registra
        check("7a el documento que calza vuelve", len(wc.buscar_emitidos_por_folio("39", 987650040)) == 1)
        # Regresión PROD 2026-09-30: Wasabil ignora `folio`; solo filtra por `search`.
        check("7a-bis el folio viaja en search «folio:N» (el filtro `folio` Wasabil lo ignora)",
              enviados and enviados[0].get("search") == "folio:987650040"
              and enviados[0].get("siiDocumentTypeCode") == "39", enviados)
        wc._post_query = _resp([_doc(33, 111, 11900)])
        try:
            wc.buscar_emitidos_por_folio("39", 987650040)
            check("7b filtro ignorado → WasabilComprasError", False, "no lanzó")
        except WasabilComprasError:
            check("7b filtro ignorado → WasabilComprasError", True)
    finally:
        wc._post_query = original_post

    print()
    ok = not _fails
    print("RESULTADO:", "TODO OK" if ok else f"{len(_fails)} fallas: {_fails}")
    assert ok, f"fallas: {_fails}"


def test_folio_manual_verificado():
    original_buscar = verificacion_folio.buscar_emitidos_por_folio
    original_flag = settings.MONZA_FOLIO_VERIFICACION
    verificacion_folio.buscar_emitidos_por_folio = _fake_buscar
    settings.MONZA_FOLIO_VERIFICACION = True
    seed()
    try:
        run()
    finally:
        cleanup()
        verificacion_folio.buscar_emitidos_por_folio = original_buscar
        settings.MONZA_FOLIO_VERIFICACION = original_flag


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
