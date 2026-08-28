"""Paridad contable Monza ← MachParts (ola 2-A): ANULAR un adelanto, PREVIEW de la
factura manual, adelanto NO PACTADO y los dos bajos del núcleo.

Cada bloque reproduce el escenario que el reconocimiento encontró, no una versión
suavizada — corriendo esta suite contra el código anterior, falla:

  T2/H1/A4 ALTO (3 lentes)  Monza no podía revertir un adelanto verificado por error: los
        dos únicos escritores eran creación/upsert y `adelanto_verificado` solo se
        escribía = 1. Consecuencia dura: Abastecimiento seguía comprando al proveedor
        contra un 50% que nunca llegó, y el 409 de monza_router_cotizaciones.py («Revierta
        el adelanto en Contabilidad/Tesorería primero») era una instrucción SIN
        implementación. → POST /adelantos/{id}/anular con los 2 candados de Grupo AM
        (aplicado / conciliado con el banco), idempotente, que devuelve
        `adelanto_verificado` a 0 y CIERRA el cortafuego.
  M1 MEDIO   La vía MANUAL no tenía preview: el operador registraba a ciegas una factura
        que YA emitió ante el SII (los montos los calcula el backend, descuento de
        anticipo incluido). → POST /facturas/preview, sin persistir nada.
  M2 MEDIO   Un depósito de adelanto NO PACTADO no se podía registrar (400 si
        pct_adelanto ≤ 0) y la plata quedaba en el banco sin destino hasta que Comercial
        hiciera un PATCH a la cotización. → se acepta; el tope (≤ total de la venta) sigue.
  B7 BAJO    El serializador no marcaba la cobranza del adelanto (es_adelanto/adelanto_id):
        la UI lo inferían comparando el string del medio.
  B8 BAJO    `crear_factura` IGNORABA `payload.despacho_id` cuando venían ítems (cadena
        elif) → aceptaba un despacho de OTRA venta en silencio. Grupo AM lo valida siempre.

ESTILO de la casa (ver test_regresiones_bloque_a.py): datos MARCADOS, limpieza total en
`finally` verificada con SESIÓN NUEVA (COUNT(*) == 0), auth REALISTA (una lectura en la
misma sesión del request antes de devolver el usuario, para que el read view de MySQL
nazca ANTES de los `with_for_update`) y `check()` que ACUMULA fallos en vez de abortar en
el primero. NO toca el SII ni el API de Wasabil.

Requiere la BD local (y `python -m monza_contabilidad.init_db` para la columna `estado`).

Corre con:
  cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_anular_adelanto_y_preview.py -q
  (o directo: ./venv/bin/python monza_contabilidad/tests/test_anular_adelanto_y_preview.py)
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContFacturaClienteItem, MonzaContCobranza,
    MonzaContFactoring, MonzaContAdelanto,
)
from monza_contabilidad.router import router as contab_router  # noqa: E402
from monza_router_abastecimiento import router as abast_router  # noqa: E402
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesConciliacion, MonzaTesConciliacionIngreso, MonzaTesCuentaBancaria,
    MonzaTesMovimiento,
)
# monza_cont_egreso es el OTRO destino de monza_tes_conciliacion (cargo ↔ egreso de
# Compras). No se usa aquí, pero sin registrar su modelo en el metadata SQLAlchemy no
# puede resolver esa FK y el primer INSERT de una conciliación revienta con
# NoReferencedTableError. Mismo import que hace monza_tesoreria/tests/test_paridad_ga.py.
import monza_compras_contab.models  # noqa: E402,F401

MARK = "__TEST_ANU__"          # monza_cotizaciones.numero es String(20): sufijos cortos
BASE = "/api/monza/contabilidad"

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()


# Auth REALISTA: hace una lectura en la MISMA sesión del request (como
# auth.get_current_user en producción), así el read view de MySQL nace ANTES de los
# with_for_update() y las carreras de plata no quedan invisibles para el test.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz", email="t@monzaparts.cl")


app.include_router(contab_router)
app.include_router(abast_router)
app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []
_S = {"cli": None, "cots": {}, "items": {}, "desps": {}, "desp_items": {},
      "ocps": [], "cuenta": None, "movs": []}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Siembra ────────────────────────────────────────────────────────────────────
def _venta(db, key, *, pct=0, lineas=1, qty=1, precio=100000, iva_pct=19):
    neto = lineas * qty * precio
    bruto = round(neto * (1 + iva_pct / 100.0))
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-{key}", cliente_id=_S["cli"], estado="vendida",
        total_neto=neto, iva_monto=bruto - neto, total_bruto=bruto, iva_pct=iva_pct,
        forma_pago="credito", pct_adelanto=pct,
    )
    db.add(cot); db.flush()
    _S["cots"][key] = cot.id
    for n in range(lineas):
        it = mm.MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion=f"Repuesto {key}{n}",
            numero_parte=f"NP-{key}{n}", cantidad=qty, precio_unitario_clp=precio,
            subtotal_clp=qty * precio, estado_linea="por_comprar",
        )
        db.add(it); db.flush()
        _S["items"][f"{key}{n}"] = it.id
    return cot.id


def _despacho(db, key, cot_key, *, estado="despachado", item_key=None, qty=1):
    d = mm.MonzaDespacho(numero=f"{MARK}-D{key}", cotizacion_id=_S["cots"][cot_key],
                         estado=estado, numero_guia=f"G-{key}",
                         # regla 2026-08-06: sin firma no se factura (el gate tiene suite propia)
                         guia_firmada=1)
    db.add(d); db.flush()
    _S["desps"][key] = d.id
    if item_key is not None:
        di = mm.MonzaDespachoItem(despacho_id=d.id, item_id=_S["items"][item_key],
                                  qty_despachada=qty)
        db.add(di); db.flush()
        _S["desp_items"][key] = di.id
    return d.id


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
        _S["cli"] = cli.id
        # AN: anular + cortafuego de Abastecimiento (2 líneas 'por_comprar': una para el
        # control con el adelanto vivo y otra para el intento con el adelanto anulado).
        _venta(db, "AN", pct=50, lineas=2)
        # AP: adelanto YA APLICADO a una factura → 409 al anular.
        _venta(db, "AP", pct=50)
        # CO: adelanto CONCILIADO con un abono del banco → 409 al anular.
        _venta(db, "CO", pct=50)
        # AX: la PLATA de un adelanto anulado no debe entrar a una factura nueva.
        _venta(db, "AX", pct=50)
        # NP: sin % pactado → adelanto NO PACTADO (M2).
        _venta(db, "NP", pct=0)
        # PV: preview + B8 (guía propia despachada, y una guía en preparación).
        _venta(db, "PV", pct=0, qty=2, precio=50000)
        _despacho(db, "PV", "PV", item_key="PV0", qty=2)
        _despacho(db, "PV2", "PV", estado="en_preparacion")
        # OT: otra venta con su propia guía despachada (el despacho AJENO de B8).
        _venta(db, "OT", pct=0, precio=50000)
        _despacho(db, "OT", "OT", item_key="OT0", qty=1)
        db.commit()
    finally:
        db.close()


# ── Lecturas de verificación (SESIÓN NUEVA: lo PERSISTIDO, no el eco del request) ──
def _adel_db(cot_key):
    db = SessionLocal()
    try:
        a = (db.query(MonzaContAdelanto)
             .filter(MonzaContAdelanto.cotizacion_id == _S["cots"][cot_key]).first())
        return None if a is None else {
            "id": a.id, "estado": a.estado, "monto": float(a.monto or 0),
            "monto_aplicado": float(a.monto_aplicado or 0),
        }
    finally:
        db.close()


def _n_adel_db(cot_key):
    db = SessionLocal()
    try:
        return (db.query(MonzaContAdelanto)
                .filter(MonzaContAdelanto.cotizacion_id == _S["cots"][cot_key]).count())
    finally:
        db.close()


def _cot_db(cot_key):
    db = SessionLocal()
    try:
        c = db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.id == _S["cots"][cot_key]).first()
        return {"adelanto_verificado": int(c.adelanto_verificado or 0),
                "pct_adelanto": int(c.pct_adelanto or 0)}
    finally:
        db.close()


def _n_facturas_db(cot_key):
    db = SessionLocal()
    try:
        return (db.query(MonzaContFacturaCliente)
                .filter(MonzaContFacturaCliente.cotizacion_id == _S["cots"][cot_key])
                .count())
    finally:
        db.close()


def _comprar(*item_keys):
    return client.post("/api/monza/abastecimiento/comprar",
                       json={"item_ids": [_S["items"][k] for k in item_keys],
                             "proveedor_nombre": f"{MARK} Proveedor"})


# ── Bloques ────────────────────────────────────────────────────────────────────
def _bloque_anular_y_cortafuego():
    """T2/H1/A4: anular devuelve la venta a 'por_verificar' y CIERRA el cortafuego."""
    cot = _S["cots"]["AN"]
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar",
                    json={"monto": 119000, "fecha_pago": "2026-07-01", "banco": "BancoX"})
    check("AN: verificar adelanto -> 200", r.status_code == 200, r.text)
    adel = _adel_db("AN")
    check("AN: el adelanto nace 'aprobado'", adel and adel["estado"] == "aprobado", adel)
    # El campo `estado` viaja al frontend (lo necesita el botón Anular).
    rv = client.get(f"{BASE}/ventas/{cot}")
    check("AN: detalle publica adelanto.estado = 'aprobado'",
          rv.status_code == 200 and (rv.json().get("adelanto") or {}).get("estado") == "aprobado",
          rv.text[:300])

    # CONTROL: con el adelanto vivo, Abastecimiento SÍ puede comprar.
    rc = _comprar("AN0")
    check("AN: control — comprar con adelanto verificado -> 200", rc.status_code == 200, rc.text)
    if rc.status_code == 200:
        _S["ocps"].append(rc.json()["ocp_id"])

    aid = adel["id"] if adel else 0
    ra = client.post(f"{BASE}/adelantos/{aid}/anular")
    check("AN: anular -> 200", ra.status_code == 200, ra.text)
    if ra.status_code == 200:
        j = ra.json()
        check("AN: la respuesta trae el registro anulado",
              (j.get("adelanto_anulado") or {}).get("estado") == "anulado", j)
        check("AN: la venta vuelve a 'por_verificar' y sin adelanto vivo",
              j.get("estado_adelanto") == "por_verificar" and j.get("adelanto") is None, j)
    check("AN: en BD el adelanto queda 'anulado' (la fila se conserva)",
          (_adel_db("AN") or {}).get("estado") == "anulado", _adel_db("AN"))
    check("AN: adelanto_verificado vuelve a 0 en la cotización",
          _cot_db("AN")["adelanto_verificado"] == 0, _cot_db("AN"))

    # SONDA: el cortafuego de Abastecimiento vuelve a frenar la compra al proveedor.
    rc2 = _comprar("AN1")
    check("AN: SONDA — comprar con el adelanto ANULADO -> 409", rc2.status_code == 409, rc2.text)
    if rc2.status_code == 200:  # no debería: se limpia igual para no dejar basura
        _S["ocps"].append(rc2.json()["ocp_id"])

    # El detalle y el LISTADO (que usa otra query, _adelantos_by_cot) coinciden.
    rv = client.get(f"{BASE}/ventas/{cot}")
    check("AN: detalle esconde el adelanto anulado",
          rv.status_code == 200 and rv.json().get("adelanto") is None
          and rv.json().get("estado_adelanto") == "por_verificar", rv.text[:300])
    rl = client.get(f"{BASE}/ventas", params={"q": f"{MARK}-AN"})
    fila = next((x for x in rl.json() if x["cotizacion_id"] == cot), None) if rl.status_code == 200 else None
    check("AN: el LISTADO también lo esconde",
          fila is not None and fila.get("adelanto") is None
          and fila.get("estado_adelanto") == "por_verificar", fila)

    # Idempotente (espejo GA) y 404 para un id que no existe.
    ra2 = client.post(f"{BASE}/adelantos/{aid}/anular")
    check("AN: re-anular es idempotente -> 200", ra2.status_code == 200, ra2.text)
    r404 = client.post(f"{BASE}/adelantos/999888777/anular")
    check("adelanto inexistente -> 404", r404.status_code == 404, r404.text)

    # REACTIVACIÓN: el cliente sí depositó → se REUSA la misma fila (el UNIQUE por
    # cotización no admite una segunda) y el cortafuego se vuelve a abrir.
    rr = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar", json={"monto": 100000})
    check("AN: re-verificar tras anular -> 200 (sin choque del UNIQUE)",
          rr.status_code == 200, rr.text)
    adel2 = _adel_db("AN")
    check("AN: SONDA — el adelanto queda REACTIVADO ('aprobado')",
          adel2 and adel2["estado"] == "aprobado", adel2)
    check("AN: es la MISMA fila (no se creó una segunda)",
          adel2 and adel2["id"] == aid and _n_adel_db("AN") == 1,
          (adel2, _n_adel_db("AN")))
    check("AN: y el monto nuevo quedó grabado", adel2 and adel2["monto"] == 100000, adel2)
    check("AN: adelanto_verificado vuelve a 1", _cot_db("AN")["adelanto_verificado"] == 1,
          _cot_db("AN"))


def _bloque_anular_bloqueado_por_plata():
    """Candado 1: la plata ya está dentro de una factura como cobranza 'adelanto'."""
    cot = _S["cots"]["AP"]
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cot, "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-FAP"})
    check("AP: factura previa -> 200", r.status_code == 200, r.text)
    fid = r.json()["id"] if r.status_code == 200 else None
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar", json={"monto": 59500})
    check("AP: verificar (aplica retroactivo) -> 200", r.status_code == 200, r.text)
    adel = _adel_db("AP")
    check("AP: monto_aplicado = 59.500", adel and adel["monto_aplicado"] == 59500, adel)

    # B7: la cobranza de adelanto va MARCADA (no hay que adivinar por el string del medio).
    rv = client.get(f"{BASE}/ventas/{cot}")
    cob = None
    if rv.status_code == 200 and rv.json()["facturas"]:
        cob = next((c for c in rv.json()["facturas"][0]["cobranzas"]
                    if c["medio"] == "adelanto"), None)
    check("B7: la cobranza trae es_adelanto=True", bool(cob and cob["es_adelanto"] is True), cob)
    check("B7: y adelanto_id apunta al adelanto de la venta",
          bool(cob and adel and cob["adelanto_id"] == adel["id"]), (cob, adel))
    check("B7: una cobranza que NO es de adelanto no lo trae",
          all(c["adelanto_id"] is None for c in rv.json()["facturas"][0]["cobranzas"]
              if c["medio"] != "adelanto") if rv.status_code == 200 else False)

    ra = client.post(f"{BASE}/adelantos/{adel['id']}/anular") if adel else None
    check("AP: SONDA — anular con plata aplicada -> 409",
          ra is not None and ra.status_code == 409, ra.text if ra else "sin adelanto")
    check("AP: el 409 dice qué hacer (revertir la cobranza)",
          ra is not None and "revierta esa cobranza" in ra.text, ra.text if ra else "")
    check("AP: y NO anuló nada", (_adel_db("AP") or {}).get("estado") == "aprobado", _adel_db("AP"))

    # Revertir la cobranza devuelve el monto y ahí sí se puede anular.
    if fid and cob:
        rd = client.delete(f"{BASE}/facturas/{fid}/cobranzas/{cob['id']}")
        check("AP: revertir la cobranza del adelanto -> 200", rd.status_code == 200, rd.text)
        check("AP: monto_aplicado vuelve a 0",
              (_adel_db("AP") or {}).get("monto_aplicado") == 0, _adel_db("AP"))
        ra = client.post(f"{BASE}/adelantos/{adel['id']}/anular")
        check("AP: ahora sí anular -> 200", ra.status_code == 200, ra.text)


def _bloque_plata_del_anulado_no_entra():
    """Lo que de verdad protege el filtro de anulados: la PLATA. Un adelanto anulado no
    puede aplicarse como cobranza a una factura nueva de la venta (si se aplicara, la
    factura nacería 'pagada' con dinero que nunca entró)."""
    cot = _S["cots"]["AX"]
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar", json={"monto": 59500})
    check("AX: verificar -> 200", r.status_code == 200, r.text)
    adel = _adel_db("AX")
    ra = client.post(f"{BASE}/adelantos/{adel['id']}/anular") if adel else None
    check("AX: anular -> 200", ra is not None and ra.status_code == 200,
          ra.text if ra else "sin adelanto")
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cot, "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-FAX"})
    check("AX: factura posterior al anulado -> 200", r.status_code == 200, r.text)
    if r.status_code == 200:
        j = r.json()
        check("AX: SONDA — NINGUNA cobranza de adelanto en la factura nueva",
              not [c for c in j["cobranzas"] if c["medio"] == "adelanto"], j["cobranzas"])
        check("AX: SONDA — la factura nace por cobrar por el bruto completo",
              j["saldo"] == 119000 and j["estado_pago"] != "pagada",
              (j["saldo"], j["estado_pago"]))
    check("AX: SONDA — monto_aplicado del anulado sigue en 0",
          (_adel_db("AX") or {}).get("monto_aplicado") == 0, _adel_db("AX"))


def _bloque_anular_bloqueado_por_banco():
    """Candado 2: hay un abono de la cartola cruzado con este adelanto."""
    cot = _S["cots"]["CO"]
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar", json={"monto": 59500})
    check("CO: verificar -> 200", r.status_code == 200, r.text)
    adel = _adel_db("CO")
    if not adel:
        check("CO: adelanto sembrado", False, "no se creó el adelanto")
        return
    db = SessionLocal()
    try:
        cta = MonzaTesCuentaBancaria(banco=f"{MARK} Banco", nombre=f"{MARK} Cta", moneda="CLP")
        db.add(cta); db.flush()
        _S["cuenta"] = cta.id
        mov = MonzaTesMovimiento(cuenta_id=cta.id, fecha=date(2026, 7, 1),
                                 glosa=f"{MARK} abono", tipo="abono", monto=59500,
                                 conciliado=True)
        db.add(mov); db.flush()
        _S["movs"].append(mov.id)
        db.add(MonzaTesConciliacion(movimiento_id=mov.id, adelanto_id=adel["id"],
                                    monto_conciliado_clp=59500))
        db.commit()
    finally:
        db.close()
    ra = client.post(f"{BASE}/adelantos/{adel['id']}/anular")
    check("CO: SONDA — anular un adelanto CONCILIADO -> 409", ra.status_code == 409, ra.text)
    check("CO: el 409 manda a desconciliar en Tesorería",
          "desconc" in ra.text.lower() and "Tesorer" in ra.text, ra.text)
    check("CO: y NO anuló nada", (_adel_db("CO") or {}).get("estado") == "aprobado", _adel_db("CO"))


def _bloque_adelanto_no_pactado():
    """M2: un depósito de adelanto que Comercial no pactó SE PUEDE registrar."""
    cot = _S["cots"]["NP"]
    check("NP: la venta no tiene % pactado", _cot_db("NP")["pct_adelanto"] == 0, _cot_db("NP"))
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar",
                    json={"monto": 30000, "fecha_pago": "2026-07-02", "banco": "BancoY"})
    check("NP: SONDA — verificar un adelanto NO PACTADO -> 200", r.status_code == 200, r.text)
    if r.status_code == 200:
        j = r.json()
        check("NP: la venta pasa a 'verificado' aunque pct sea 0",
              j.get("estado_adelanto") == "verificado" and j.get("pct_adelanto") == 0
              and j.get("requiere_adelanto") is True, j)
    # El tope real (≤ total de la venta, bruto 119.000) sigue vivo.
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar", json={"monto": 200000})
    check("NP: monto por sobre el total de la venta -> 400", r.status_code == 400, r.text)
    check("NP: el adelanto sigue en 30.000", (_adel_db("NP") or {}).get("monto") == 30000,
          _adel_db("NP"))
    # Glosa de la cobranza: sin % pactado NO puede decir «Adelanto 0% aplicado».
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cot, "sin_guia": True, "confirmar_retiro_sin_adelanto": True, "numero_factura": f"{MARK}-FNP"})
    check("NP: factura -> 200 (y aplica el adelanto)", r.status_code == 200, r.text)
    if r.status_code == 200:
        cobs = [c for c in r.json()["cobranzas"] if c["medio"] == "adelanto"]
        check("NP: la cobranza del adelanto es de 30.000",
              len(cobs) == 1 and cobs[0]["monto"] == 30000, cobs)
        check("NP: la glosa no inventa un 0%",
              bool(cobs) and "0%" not in (cobs[0]["observaciones"] or "")
              and "no pactado" in (cobs[0]["observaciones"] or ""), cobs)


def _bloque_preview():
    """M1: preview de la vía manual — muestra lo que va a registrar y NO persiste."""
    cot = _S["cots"]["PV"]
    r = client.post(f"{BASE}/facturas/preview",
                    json={"cotizacion_id": cot, "despacho_id": _S["desps"]["PV"]})
    check("PV: preview desde la guía -> 200", r.status_code == 200, r.text)
    if r.status_code == 200:
        j = r.json()
        check("PV: puede_emitir True y sin problemas",
              j["puede_emitir"] is True and j["problemas"] == [], j)
        check("PV: una línea por 100.000 neto",
              len(j["lineas"]) == 1 and j["lineas"][0]["total_neto"] == 100000, j["lineas"])
        check("PV: totales congelados 100.000 / 19.000 / 119.000",
              j["totales"]["neto"] == 100000 and j["totales"]["iva"] == 19000
              and j["totales"]["bruto"] == 119000, j["totales"])
        check("PV: publica la tasa REAL de la venta (no un 19% escrito a mano)",
              abs(float(j["totales"]["iva_rate"]) - 0.19) < 1e-9, j["totales"])
        check("PV: receptor con razón social y RUT del cliente",
              (j["receptor"] or {}).get("rut") == "11.111.111-1", j["receptor"])
        check("PV: eco de la guía que va a quedar registrada",
              (j["guia"] or {}).get("numero_guia") == "G-PV", j["guia"])
    # SONDA del preview: NO persiste nada.
    check("PV: SONDA — el preview no creó ninguna factura", _n_facturas_db("PV") == 0,
          _n_facturas_db("PV"))

    # Con problemas: los ACUMULA en la lista (200 + puede_emitir False), no un 409 seco —
    # es la diferencia con POST /facturas y lo que permite mostrarlos todos antes de emitir.
    r = client.post(f"{BASE}/facturas/preview", json={
        "cotizacion_id": cot,
        "items": [{"item_cotizacion_id": _S["items"]["PV0"], "cantidad": 999}]})
    check("PV: cantidad imposible -> 200 con problemas (no 409)",
          r.status_code == 200 and r.json()["puede_emitir"] is False
          and len(r.json()["problemas"]) >= 1, r.text[:400])
    # Preview de la factura de ANTICIPO (vía B): misma forma de salida.
    r = client.post(f"{BASE}/facturas/preview", json={
        "cotizacion_id": cot, "es_anticipo": True, "monto_neto_anticipo": 10000})
    check("PV: preview de anticipo -> 200", r.status_code == 200, r.text)
    if r.status_code == 200:
        j = r.json()
        check("PV: anticipo — línea ANTICIPO y totales 10.000 / 1.900",
              j["es_anticipo"] is True and j["lineas"][0]["numero_parte"] == "ANTICIPO"
              and j["totales"]["neto"] == 10000 and j["totales"]["iva"] == 1900, j)
    check("PV: SONDA — el preview del anticipo tampoco persistió",
          _n_facturas_db("PV") == 0, _n_facturas_db("PV"))
    r = client.post(f"{BASE}/facturas/preview", json={"cotizacion_id": 999888777})
    check("preview de una venta inexistente -> 404", r.status_code == 404, r.text)


def _bloque_b8_guia_ajena():
    """B8: con ítems explícitos la guía del payload también se valida."""
    cot = _S["cots"]["PV"]
    linea = {"item_cotizacion_id": _S["items"]["PV0"], "cantidad": 1}
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cot, "items": [linea], "despacho_id": _S["desps"]["OT"],
        "numero_factura": f"{MARK}-FB8A"})
    check("B8: SONDA — ítems + guía de OTRA venta -> 404", r.status_code == 404, r.text)
    check("B8: el 404 nombra el despacho", "Despacho no encontrado" in r.text, r.text)
    check("B8: y no persistió nada", _n_facturas_db("PV") == 0, _n_facturas_db("PV"))
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cot, "items": [linea], "despacho_id": _S["desps"]["PV2"],
        "numero_factura": f"{MARK}-FB8B"})
    check("B8: ítems + guía propia AÚN NO despachada -> 400", r.status_code == 400, r.text)
    # El preview lo reporta igual (misma fuente de verdad), acumulado en la lista.
    r = client.post(f"{BASE}/facturas/preview", json={
        "cotizacion_id": cot, "items": [linea], "despacho_id": _S["desps"]["OT"]})
    check("B8: el preview también lo bloquea",
          r.status_code == 200 and r.json()["puede_emitir"] is False, r.text[:300])
    # CONTROL: con su propia guía despachada, la factura sale.
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cot, "items": [dict(linea, despacho_item_id=_S["desp_items"]["PV"])],
        "despacho_id": _S["desps"]["PV"], "numero_factura": f"{MARK}-FB8OK"})
    check("B8: control — ítems + su propia guía despachada -> 200", r.status_code == 200, r.text)
    check("B8: control — quedó UNA factura", _n_facturas_db("PV") == 1, _n_facturas_db("PV"))


def run():
    _bloque_anular_y_cortafuego()
    _bloque_anular_bloqueado_por_plata()
    _bloque_plata_del_anulado_no_entra()
    _bloque_anular_bloqueado_por_banco()
    _bloque_adelanto_no_pactado()
    _bloque_preview()
    _bloque_b8_guia_ajena()
    print()
    if _fails:
        print(f"=== {len(_fails)} FALLO(S): {_fails} ===")
        return False
    print("=== TODO OK ===")
    return True


# ── Limpieza + verificación por DELTAS ─────────────────────────────────────────
def cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        for oid in _S["ocps"]:
            db.query(mm.MonzaOcProveedor).filter(mm.MonzaOcProveedor.id == oid).delete()
        cot_ids = [i for i in _S["cots"].values() if i]
        if cot_ids:
            adel_ids = [a.id for a in db.query(MonzaContAdelanto)
                        .filter(MonzaContAdelanto.cotizacion_id.in_(cot_ids)).all()]
            fac_ids = [f.id for f in db.query(MonzaContFacturaCliente)
                       .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all()]
            if adel_ids:
                db.query(MonzaTesConciliacion).filter(
                    MonzaTesConciliacion.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
            if fac_ids:
                cob_ids = [c.id for c in db.query(MonzaContCobranza)
                           .filter(MonzaContCobranza.factura_id.in_(fac_ids)).all()]
                if cob_ids:
                    db.query(MonzaTesConciliacionIngreso).filter(
                        MonzaTesConciliacionIngreso.cobranza_id.in_(cob_ids)
                    ).delete(synchronize_session=False)
                # Las LÍNEAS antes que las facturas: la FK anticipo_factura_id no lleva
                # ondelete (es el segundo cinturón del 409 del borrado).
                db.query(MonzaContFacturaClienteItem).filter(
                    MonzaContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(MonzaContCobranza).filter(
                    MonzaContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(MonzaContFactoring).filter(
                    MonzaContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.flush()
                db.query(MonzaContFacturaCliente).filter(
                    MonzaContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
            if adel_ids:
                db.query(MonzaContAdelanto).filter(
                    MonzaContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
        desp_ids = [i for i in _S["desps"].values() if i]
        if desp_ids:
            db.query(mm.MonzaDespachoItem).filter(
                mm.MonzaDespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaDespacho).filter(
                mm.MonzaDespacho.id.in_(desp_ids)).delete(synchronize_session=False)
        if cot_ids:
            db.query(mm.MonzaCotizacionItem).filter(
                mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaCotizacion).filter(
                mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        if _S["movs"]:
            db.query(MonzaTesMovimiento).filter(
                MonzaTesMovimiento.id.in_(_S["movs"])).delete(synchronize_session=False)
        if _S["cuenta"]:
            db.query(MonzaTesCuentaBancaria).filter(
                MonzaTesCuentaBancaria.id == _S["cuenta"]).delete(synchronize_session=False)
        db.query(mm.MonzaCliente).filter(
            mm.MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=False)
        db.commit()
        print("Cleanup OK")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print("Cleanup parcial:", e)
    finally:
        db.close()


def _verificar_limpieza():
    """DELTA final con SESIÓN NUEVA: ni una fila marcada sobrevive (ni sus dependientes)."""
    db = SessionLocal()
    faltan = []
    try:
        if db.query(mm.MonzaCotizacion).filter(
                mm.MonzaCotizacion.numero.like(f"{MARK}%")).count():
            faltan.append("monza_cotizaciones")
        if db.query(mm.MonzaCliente).filter(
                mm.MonzaCliente.nombre.like(f"{MARK}%")).count():
            faltan.append("monza_clientes")
        if db.query(mm.MonzaDespacho).filter(
                mm.MonzaDespacho.numero.like(f"{MARK}%")).count():
            faltan.append("monza_despachos")
        if db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.numero_factura.like(f"{MARK}%")).count():
            faltan.append("monza_cont_factura_cliente")
        if db.query(MonzaTesCuentaBancaria).filter(
                MonzaTesCuentaBancaria.banco.like(f"{MARK}%")).count():
            faltan.append("monza_tes_cuenta_bancaria")
        if db.query(MonzaTesMovimiento).filter(
                MonzaTesMovimiento.glosa.like(f"{MARK}%")).count():
            faltan.append("monza_tes_movimiento")
    finally:
        db.close()
    return faltan


# ── Reglas PURAS del estado del adelanto (sin BD) ──────────────────────────────
def test_estado_adelanto_puro():
    """El lector del estado es la última defensa cuando el adelanto llega de OTRO módulo
    (monza_tesoreria trae el suyo de sus propias queries, sin filtrar anulados)."""
    from monza_contabilidad.service import (
        adelanto_activo, estado_adelanto, estado_de_adelanto,
        reactivar_adelanto, ADEL_ANULADO, ADEL_APROBADO,
    )
    cot = SimpleNamespace(id=1, pct_adelanto=50)
    vivo = SimpleNamespace(id=9, estado=ADEL_APROBADO, monto=100, monto_aplicado=0,
                           fecha_pago=None, banco=None, numero_operacion=None,
                           observaciones=None, fecha_verificacion=None)
    anulado = SimpleNamespace(id=9, estado=ADEL_ANULADO, monto=100, monto_aplicado=0,
                              fecha_pago=None, banco=None, numero_operacion=None,
                              observaciones=None, fecha_verificacion=None)
    legado = SimpleNamespace(id=9, estado=None, monto=100, monto_aplicado=0,
                             fecha_pago=None, banco=None, numero_operacion=None,
                             observaciones=None, fecha_verificacion=None)

    # Fila LEGADA (sin la columna / NULL) = adelanto verificado, JAMÁS anulado: leerlo al
    # revés apagaría el cortafuego de Abastecimiento de todas las ventas viejas.
    assert estado_de_adelanto(legado) == ADEL_APROBADO
    assert adelanto_activo(legado) and adelanto_activo(vivo)
    assert not adelanto_activo(anulado) and not adelanto_activo(None)

    assert estado_adelanto(cot, vivo)["estado_adelanto"] == "verificado"
    assert estado_adelanto(cot, legado)["estado_adelanto"] == "verificado"
    # ANULADO se sirve como si no existiera → la venta vuelve a 'por_verificar'.
    out = estado_adelanto(cot, anulado)
    assert out["estado_adelanto"] == "por_verificar" and out["adelanto"] is None
    # …y sin % pactado, a 'no_aplica' (que es lo que cierra el cortafuego).
    assert estado_adelanto(SimpleNamespace(id=1, pct_adelanto=0), anulado)["estado_adelanto"] \
        == "no_aplica"
    # El estado viaja al frontend cuando el adelanto está vivo.
    assert estado_adelanto(cot, vivo)["adelanto"]["estado"] == ADEL_APROBADO

    # Reactivar: solo transiciona desde 'anulado' y es idempotente.
    assert reactivar_adelanto(anulado) is True and anulado.estado == ADEL_APROBADO
    assert reactivar_adelanto(anulado) is False
    assert reactivar_adelanto(None) is False


def test_anular_adelanto_y_preview():
    """Wrapper de UNA LÍNEA para pytest: sin él la suite sería INVISIBLE al gate."""
    seed()
    ok = False
    try:
        ok = run()
    finally:
        cleanup()
        residuos = _verificar_limpieza()
    assert not residuos, f"la limpieza dejó residuos en: {residuos}"
    assert ok, f"fallas: {_fails}"


if __name__ == "__main__":
    seed()
    ok = False
    try:
        ok = run()
    finally:
        cleanup()
        _res = _verificar_limpieza()
        if _res:
            print("RESIDUOS:", _res)
            ok = False
    sys.exit(0 if ok else 1)
