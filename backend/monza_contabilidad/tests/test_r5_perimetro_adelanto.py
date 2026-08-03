"""Perímetro del adelanto Monza (refutaciones A1 y A2 de la auditoría adversarial).

Los dos hallazgos son de plata y los dos se reprodujeron por HTTP antes de arreglarlos:

  A1 ALTO  El 409 «Revierta el adelanto en Contabilidad/Tesorería primero» MENTÍA: los dos
        conteos de monza_router_cotizaciones.py (:612 al des-cerrar la venta y :634 al
        bajar pct_adelanto) usan `COUNT(*)` de adelantos de la venta, y anular NO borra la
        fila (es a propósito: deja la traza 'anulado'). Entonces el operador obedecía la
        instrucción, anulaba, y el 409 salía IDÉNTICO para siempre. → DELETE
        /adelantos/{id} (eliminar_adelanto): la reversión completa, con los MISMOS candados
        de anular, que es la mitad «elimina» que esos mensajes ya prometían.
  A2 ALTO  Se podía anular el adelanto que RESPALDA una factura de ANTICIPO ya emitida,
        siguiendo la remediación que el propio 409 recomienda: el 409 decía «revierta esa
        cobranza antes de anularlo», se borraba la cobranza (monto_aplicado → 0) y anular
        pasaba a 200. Quedaba el anticipo ante el SII por cobrar, el adelanto invisible
        (`adelanto: null`) y el depósito del cliente sin destino. → tercer candado
        (_bloqueo_anticipo_del_adelanto) en anular Y en eliminar, con la salida correcta:
        borrar primero la factura de anticipo (ese camino sí existe y él sabe juzgar el
        SII). La suite comprueba que esa salida FUNCIONA, no solo que el 409 la nombra.

SONDAS: cada arreglo se ejercita por HTTP (nunca leyendo el código fuente). Quitando el
arreglo del producto, los checks marcados «SONDA» se ponen ROJOS — está medido, no supuesto.

ESTILO de la casa (test_anular_adelanto_y_preview.py, test_regresiones_bloque_a.py): datos
MARCADOS, `cleanup()` ANTES de sembrar (un corte anterior no puede dejar la suite roja para
siempre) y también en `finally`, verificación por DELTAS con SESIÓN NUEVA, auth REALISTA y
`check()` que ACUMULA fallos. NO toca el SII ni el API de Wasabil: las facturas de anticipo
se registran por la vía MANUAL (`issue`/emisión electrónica jamás entra en juego).

Requiere la BD local (y `python -m monza_contabilidad.init_db` para la columna `estado`).

Corre con:
  cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_r5_perimetro_adelanto.py -q
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
# El router de VENTAS (cotizaciones) entra a la app de prueba a propósito: los dos 409 de
# A1 viven ahí y la remediación tiene que probarse END-TO-END, con el PATCH real.
from monza_router_cotizaciones import router as cot_router  # noqa: E402
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesConciliacion, MonzaTesConciliacionIngreso, MonzaTesCuentaBancaria,
    MonzaTesMovimiento,
)
# monza_cont_egreso es el OTRO destino de monza_tes_conciliacion (cargo ↔ egreso de
# Compras). No se usa acá, pero sin registrar su modelo en el metadata SQLAlchemy no puede
# resolver esa FK y el primer INSERT de una conciliación revienta con NoReferencedTableError.
import monza_compras_contab.models  # noqa: E402,F401

MARK = "__TEST_R5C__"          # monza_cotizaciones.numero es String(20): sufijos cortos
BASE = "/api/monza/contabilidad"
VENTAS = "/api/monza/cotizaciones"

# Folio NUMÉRICO para las facturas de ANTICIPO: el folio de un anticipo lo referencia la
# factura de la mercadería (referencia tipo 33) y el SII solo acepta dígitos, así que
# crear_factura rechaza un folio marcado. Banda 99843xxx: no choca con folios reales (los
# del SII son correlativos chicos) ni con la 9981xxxx de test_factura_anticipo.py. La
# limpieza los barre igual porque va por cotizacion_id, no por folio.
_FOLIO = {"n": 99843000}


def _folio_ant() -> str:
    _FOLIO["n"] += 1
    return str(_FOLIO["n"])


Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()


# Auth REALISTA: hace una lectura en la MISMA sesión del request (como
# auth.get_current_user en producción), así el read view de MySQL nace ANTES de los
# with_for_update() y las carreras de plata no quedan invisibles para el test.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz", email="t@monzaparts.cl", rol="admin")


app.include_router(contab_router)
app.include_router(cot_router)
app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)


# App GEMELA con un usuario de MINERÍA (MachParts): el endpoint NUEVO tiene que quedar
# dentro del candado de empresa del router, como todos los demás.
def _cu_mineria(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=2, empresa="mineria", email="t@machparts.cl", rol="admin")


app_min = FastAPI()
app_min.include_router(contab_router)
app_min.dependency_overrides[get_current_user] = _cu_mineria
client_min = TestClient(app_min)

_fails = []
_S = {"cli": None, "cots": {}, "items": {}, "cuenta": None, "movs": []}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Siembra ────────────────────────────────────────────────────────────────────
def _venta(db, key, *, pct=0, precio=100000, iva_pct=19):
    neto = precio
    bruto = round(neto * (1 + iva_pct / 100.0))
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-{key}", cliente_id=_S["cli"], estado="vendida",
        total_neto=neto, iva_monto=bruto - neto, total_bruto=bruto, iva_pct=iva_pct,
        forma_pago="credito", pct_adelanto=pct,
        # N° y fecha de OC del cliente: el PATCH de Ventas los exige para CERRAR y la
        # venta nace cerrada. Sin ellos, el PATCH de A1 se cae por otro motivo.
        oc_cliente=f"OC-{key}", oc_fecha=date(2026, 7, 1),
    )
    db.add(cot); db.flush()
    _S["cots"][key] = cot.id
    it = mm.MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion=f"Repuesto {key}", numero_parte=f"NP-{key}",
        cantidad=1, precio_unitario_clp=precio, subtotal_clp=precio,
        estado_linea="por_comprar",
    )
    db.add(it); db.flush()
    _S["items"][key] = it.id
    return cot.id


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
        _S["cli"] = cli.id
        _venta(db, "RM", pct=50)   # A1: la remediación de punta a punta
        _venta(db, "AT", pct=50)   # A2: adelanto que respalda una factura de ANTICIPO
        _venta(db, "EA", pct=50)   # candado 1 del DELETE: plata ya aplicada
        _venta(db, "EC", pct=50)   # candado 2 del DELETE: conciliado con el banco
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
    """El COUNT(*) que usan los dos 409 de Ventas: es EXACTAMENTE la pregunta de A1."""
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
        return None if c is None else {
            "estado": c.estado, "pct_adelanto": int(c.pct_adelanto or 0),
            "adelanto_verificado": int(c.adelanto_verificado or 0)}
    finally:
        db.close()


def _n_conc_db(adel_id):
    db = SessionLocal()
    try:
        return (db.query(MonzaTesConciliacion)
                .filter(MonzaTesConciliacion.adelanto_id == adel_id).count())
    finally:
        db.close()


def _cobranzas_de(fac_id):
    r = client.get(f"{BASE}/facturas")
    if r.status_code != 200:
        return []
    for f in r.json().get("facturas", []):
        if f.get("id") == fac_id:
            return f.get("cobranzas") or []
    return []


# ── A1: la remediación que el 409 promete se puede EJECUTAR ────────────────────
def _bloque_remediacion_ejecutable():
    """A1: anular deja la fila (traza) y por eso NO destraba los 409 de Ventas, que cuentan
    filas. Eliminar sí, y con los mismos candados."""
    cot = _S["cots"]["RM"]
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar",
                    json={"monto": 59500, "fecha_pago": "2026-07-01", "banco": "BancoX"})
    check("RM: verificar el adelanto -> 200", r.status_code == 200, r.text)
    adel = _adel_db("RM")
    check("RM: el adelanto existe y está 'aprobado'",
          bool(adel) and adel["estado"] == "aprobado", adel)
    aid = adel["id"] if adel else 0

    # Estado de partida: con el adelanto vivo, Ventas frena la corrección. Correcto.
    r = client.patch(f"{VENTAS}/{cot}", json={"pct_adelanto": 0})
    check("RM: con el adelanto vivo, bajar el % -> 409", r.status_code == 409, r.text[:200])
    check("RM: y el 409 manda a revertir el adelanto",
          "Revierta el adelanto" in r.text, r.text[:200])

    # Se OBEDECE la instrucción: anular. Sigue frenado (la fila queda, por diseño) — este
    # check documenta POR QUÉ hace falta eliminar; no es la sonda del arreglo.
    ra = client.post(f"{BASE}/adelantos/{aid}/anular")
    check("RM: anular -> 200", ra.status_code == 200, ra.text[:200])
    check("RM: anular NO borra la fila (traza 'anulado')",
          _n_adel_db("RM") == 1 and (_adel_db("RM") or {}).get("estado") == "anulado",
          (_n_adel_db("RM"), _adel_db("RM")))
    r = client.patch(f"{VENTAS}/{cot}", json={"pct_adelanto": 0})
    check("RM: tras anular, bajar el % sigue en 409 (la fila cuenta)",
          r.status_code == 409, r.text[:200])

    # SONDA A1: la reversión COMPLETA. Sin el endpoint nuevo, FastAPI responde 405/404 y
    # los tres checks siguientes se caen.
    rd = client.delete(f"{BASE}/adelantos/{aid}")
    check("RM: SONDA — DELETE /adelantos/{id} -> 200", rd.status_code == 200, rd.text[:250])
    if rd.status_code == 200:
        j = rd.json()
        check("RM: la respuesta trae la traza de lo eliminado",
              (j.get("adelanto_eliminado") or {}).get("monto") == 59500
              and j.get("estado_adelanto") == "por_verificar", j)
    check("RM: SONDA — la fila YA NO existe (el COUNT de Ventas vuelve a 0)",
          _n_adel_db("RM") == 0, _n_adel_db("RM"))
    check("RM: y adelanto_verificado queda en 0 (cortafuego de Abastecimiento cerrado)",
          (_cot_db("RM") or {}).get("adelanto_verificado") == 0, _cot_db("RM"))

    # SONDA A1 (la que importa al dueño): la instrucción del 409 AHORA se puede ejecutar.
    r = client.patch(f"{VENTAS}/{cot}", json={"pct_adelanto": 0})
    check("RM: SONDA — con el adelanto ELIMINADO, bajar el % -> 200",
          r.status_code == 200, r.text[:250])
    check("RM: el % quedó grabado en 0", (_cot_db("RM") or {}).get("pct_adelanto") == 0,
          _cot_db("RM"))
    # El OTRO 409 de A1: el des-cierre («… 1 adelanto(s) … Anula/elimina eso primero»).
    r = client.patch(f"{VENTAS}/{cot}", json={"estado": "enviada"})
    check("RM: SONDA — y el DES-CIERRE de la venta -> 200", r.status_code == 200, r.text[:250])
    check("RM: la venta volvió a 'enviada'", (_cot_db("RM") or {}).get("estado") == "enviada",
          _cot_db("RM"))

    # Contrato del DELETE sobre algo que no existe.
    r404 = client.delete(f"{BASE}/adelantos/999888777")
    check("RM: DELETE de un adelanto inexistente -> 404", r404.status_code == 404, r404.text)
    # Candado de empresa: el endpoint nuevo no puede ser la rendija por la que MachParts
    # toque plata de MonzaParts.
    r403 = client_min.delete(f"{BASE}/adelantos/{aid}")
    check("RM: un usuario de MINERÍA no puede eliminar el adelanto -> 403",
          r403.status_code == 403, r403.text[:160])


# ── A2: el adelanto que respalda un ANTICIPO no se anula ni se elimina ─────────
def _bloque_anticipo_protegido():
    """A2: la remediación que el 409 viejo recomendaba (borrar la cobranza) era el camino
    para dejar una factura de anticipo del SII sin respaldo."""
    cot = _S["cots"]["AT"]
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar",
                    json={"monto": 59500, "fecha_pago": "2026-07-01", "banco": "BancoX"})
    check("AT: verificar el adelanto -> 200", r.status_code == 200, r.text)
    adel = _adel_db("AT")
    aid = adel["id"] if adel else 0
    folio = _folio_ant()
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cot, "es_anticipo": True, "monto_neto_anticipo": 50000,
        "numero_factura": folio, "tipo_doc": "factura", "fecha_emision": "2026-07-02",
        "descripcion_anticipo": "Anticipo"})
    check("AT: factura de ANTICIPO (vía manual) -> 200", r.status_code == 200, r.text[:250])
    fac_id = r.json().get("id") if r.status_code == 200 else None
    check("AT: la plata del adelanto entró al anticipo como cobranza",
          (_adel_db("AT") or {}).get("monto_aplicado") == 59500, _adel_db("AT"))

    # Se OBEDECE al pie de la letra el 409 histórico: «revierta esa cobranza antes de
    # anularlo». Esto es lo que abría el agujero.
    cobs = [c for c in _cobranzas_de(fac_id) if c.get("medio") == "adelanto"]
    check("AT: el anticipo tiene la cobranza del adelanto", len(cobs) == 1, cobs)
    for c in cobs:
        rd = client.delete(f"{BASE}/facturas/{fac_id}/cobranzas/{c['id']}")
        check("AT: revertir esa cobranza -> 200", rd.status_code == 200, rd.text[:200])
    check("AT: monto_aplicado vuelve a 0 (los 2 candados viejos quedan satisfechos)",
          (_adel_db("AT") or {}).get("monto_aplicado") == 0, _adel_db("AT"))

    # SONDA A2 (anular): sin el tercer candado esto respondía 200 y la factura de anticipo
    # quedaba ante el SII por cobrar, con el adelanto invisible.
    ra = client.post(f"{BASE}/adelantos/{aid}/anular")
    check("AT: SONDA — anular con la factura de anticipo VIVA -> 409",
          ra.status_code == 409, ra.text[:300])
    check("AT: el 409 nombra el folio del anticipo", folio in ra.text, ra.text[:300])
    check("AT: y NO anuló nada", (_adel_db("AT") or {}).get("estado") == "aprobado",
          _adel_db("AT"))
    # SONDA A2 (eliminar): el candado tiene que estar en las DOS puertas — si no, la
    # reversión nueva sería el nuevo camino al mismo agujero.
    rd = client.delete(f"{BASE}/adelantos/{aid}")
    check("AT: SONDA — eliminar con la factura de anticipo VIVA -> 409",
          rd.status_code == 409, rd.text[:300])
    check("AT: la fila sigue ahí", _n_adel_db("AT") == 1, _n_adel_db("AT"))

    # SONDA de que el 409 NO manda a una acción imposible: la salida que nombra funciona.
    rdf = client.delete(f"{BASE}/facturas/{fac_id}")
    check("AT: SONDA — la salida que indica el 409 (borrar el anticipo) -> 200",
          rdf.status_code == 200, rdf.text[:250])
    ra = client.post(f"{BASE}/adelantos/{aid}/anular")
    check("AT: y recién ahí anular -> 200", ra.status_code == 200, ra.text[:200])
    check("AT: el adelanto queda 'anulado'", (_adel_db("AT") or {}).get("estado") == "anulado",
          _adel_db("AT"))


# ── Los candados del DELETE nuevo (no puede ser una puerta más floja) ──────────
def _bloque_eliminar_bloqueado_por_plata():
    """Candado 1 del DELETE: la plata ya está dentro de una factura como cobranza."""
    cot = _S["cots"]["EA"]
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": cot, "sin_guia": True, "numero_factura": f"{MARK}-FEA"})
    check("EA: factura previa (sin guía) -> 200", r.status_code == 200, r.text[:250])
    fac_id = r.json().get("id") if r.status_code == 200 else None
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar", json={"monto": 59500})
    check("EA: verificar (se aplica retroactivo) -> 200", r.status_code == 200, r.text[:200])
    adel = _adel_db("EA")
    check("EA: monto_aplicado = 59.500", (adel or {}).get("monto_aplicado") == 59500, adel)
    aid = adel["id"] if adel else 0

    rd = client.delete(f"{BASE}/adelantos/{aid}")
    check("EA: SONDA — eliminar con plata aplicada -> 409", rd.status_code == 409, rd.text[:250])
    check("EA: el 409 dice qué hacer (revertir la cobranza)",
          "revierta esa cobranza" in rd.text, rd.text[:250])
    check("EA: y no borró nada", _n_adel_db("EA") == 1, _n_adel_db("EA"))

    cobs = [c for c in _cobranzas_de(fac_id) if c.get("medio") == "adelanto"]
    for c in cobs:
        client.delete(f"{BASE}/facturas/{fac_id}/cobranzas/{c['id']}")
    rd = client.delete(f"{BASE}/adelantos/{aid}")
    check("EA: revertida la cobranza, eliminar -> 200", rd.status_code == 200, rd.text[:250])
    check("EA: la fila se fue", _n_adel_db("EA") == 0, _n_adel_db("EA"))
    # La factura NORMAL de la venta sigue viva: eliminar el adelanto no toca documentos.
    check("EA: la factura de la venta sigue existiendo",
          any(f.get("id") == fac_id for f in client.get(f"{BASE}/facturas").json().get("facturas", [])),
          fac_id)


def _bloque_eliminar_bloqueado_por_banco():
    """Candado 2 del DELETE, y es el más peligroso: monza_tes_conciliacion.adelanto_id es
    ON DELETE CASCADE, así que sin este candado el motor se llevaría el cruce bancario en
    silencio (el movimiento quedaría 'conciliado' apuntando a nada)."""
    cot = _S["cots"]["EC"]
    r = client.post(f"{BASE}/ventas/{cot}/adelanto/verificar", json={"monto": 59500})
    check("EC: verificar -> 200", r.status_code == 200, r.text[:200])
    adel = _adel_db("EC")
    if not adel:
        check("EC: adelanto sembrado", False, "no se creó el adelanto")
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

    rd = client.delete(f"{BASE}/adelantos/{adel['id']}")
    check("EC: SONDA — eliminar un adelanto CONCILIADO -> 409", rd.status_code == 409,
          rd.text[:250])
    check("EC: el 409 manda a desconciliar en Tesorería",
          "desconc" in rd.text.lower() and "Tesorer" in rd.text, rd.text[:250])
    check("EC: SONDA — la fila del adelanto sigue viva", _n_adel_db("EC") == 1, _n_adel_db("EC"))
    check("EC: SONDA — y el CRUCE BANCARIO no se lo llevó el CASCADE",
          _n_conc_db(adel["id"]) == 1, _n_conc_db(adel["id"]))


def run():
    _bloque_remediacion_ejecutable()
    _bloque_anticipo_protegido()
    _bloque_eliminar_bloqueado_por_plata()
    _bloque_eliminar_bloqueado_por_banco()
    print()
    if _fails:
        print(f"=== {len(_fails)} FALLO(S): {_fails} ===")
        return False
    print("=== TODO OK ===")
    return True


# ── Limpieza + verificación por DELTAS ─────────────────────────────────────────
def cleanup():
    """Barre por MARCA (no solo por los ids de este proceso): así un corte anterior no deja
    huérfanos que hagan fallar `_verificar_limpieza` en TODAS las corridas siguientes."""
    db = SessionLocal()
    try:
        db.rollback()
        cot_ids = [r[0] for r in db.query(mm.MonzaCotizacion.id)
                   .filter(mm.MonzaCotizacion.numero.like(f"{MARK}%")).all()]
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
            db.query(mm.MonzaCotizacionItem).filter(
                mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaCotizacion).filter(
                mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        # Cartola/movimientos y cliente: por MARCA, igual que arriba.
        movs = [m[0] for m in db.query(MonzaTesMovimiento.id)
                .filter(MonzaTesMovimiento.glosa.like(f"{MARK}%")).all()]
        if movs:
            db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.movimiento_id.in_(movs)).delete(synchronize_session=False)
            db.query(MonzaTesMovimiento).filter(
                MonzaTesMovimiento.id.in_(movs)).delete(synchronize_session=False)
        db.query(MonzaTesCuentaBancaria).filter(
            MonzaTesCuentaBancaria.banco.like(f"{MARK}%")).delete(synchronize_session=False)
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
        if db.query(MonzaTesCuentaBancaria).filter(
                MonzaTesCuentaBancaria.banco.like(f"{MARK}%")).count():
            faltan.append("monza_tes_cuenta_bancaria")
        if db.query(MonzaTesMovimiento).filter(
                MonzaTesMovimiento.glosa.like(f"{MARK}%")).count():
            faltan.append("monza_tes_movimiento")
        # Las facturas de anticipo llevan folio NUMÉRICO (no admiten la marca): se verifican
        # por su venta, que ya no existe si la limpieza anduvo bien.
        if db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.numero_cotizacion.like(f"{MARK}%")).count():
            faltan.append("monza_cont_factura_cliente")
    finally:
        db.close()
    return faltan


def test_r5_perimetro_adelanto():
    """Wrapper de UNA LÍNEA para pytest: sin él la suite sería INVISIBLE al gate."""
    cleanup()          # pre-limpieza: la suite nunca se auto-envenena
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
    cleanup()
    seed()
    ok = False
    try:
        ok = run()
    finally:
        cleanup()
        print("residuos:", _verificar_limpieza())
    sys.exit(0 if ok else 1)
