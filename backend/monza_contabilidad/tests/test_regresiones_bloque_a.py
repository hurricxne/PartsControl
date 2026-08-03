"""REGRESIONES del BLOQUE A (F7 · multienjambre adversarial) + los 2 puntos de cierre.

Cada escenario es el que REPRODUJO el hallazgo, no una versión suavizada: corriendo esta
suite contra el código anterior al arreglo, falla; contra el actual, pasa. Los ocho
hallazgos del bloque A (`monza_contabilidad`):

  A-1 CRÍTICO  N facturas de anticipo REALES por el mismo adelanto (dos clics, sin
               carrera: el candado del módulo SII solo dura mientras el HTTP está en
               vuelo). → 409 que NOMBRA la previa, y puerta explícita
               `confirmar_segundo_anticipo`.
  A-2 ALTO     El mismo depósito contado dos veces: cobranza MANUAL sobre la factura de
               anticipo. → 409 (la plata del adelanto entra por una sola puerta).
  A-3 ALTO     Σ brutos > total de la venta sin límite: la tolerancia escalaba por tanda
               y cada anticipo de $1 pagaba su propia holgura. → tolerancia PLANA.
  A-4 MEDIO    Tesorería aprueba ANTES de que se emita el anticipo (orden normal del
               negocio) y la plata queda en la factura equivocada. → RE-RUTEO, con
               advertencia accionable cuando los guards lo impiden.
  A-5 MEDIO    Una BOLETA del despacho descontaba un anticipo citando el folio de una
               FACTURA. → 400.
  A-6 BAJO     El preview publicaba un neto intermedio (ni descontado ni sin descontar)
               cuando un anticipo no tenía folio. → aborta el bloque de descuentos.
  A-7 MEDIO    `crear_factura` descartaba las advertencias por la vía manual. → las
               devuelve.
  A-8 MEDIO    `es_anticipo` sin DEFAULT en BD fresca: con NULLs el ORDER BY ... DESC
               invierte el FIFO y la plata entra a la factura equivocada.

y los dos puntos de COORDINACIÓN que cerró el agente de cierre:

  P-1 La advertencia del re-ruteo se perdía por la vía SII: `_aplicar_adelantos_pendientes`
      (el nombre-contrato que llama monza_wasabil_dte al confirmarse el folio) no
      propagaba `advertencias`. → las propaga y las DEVUELVE.
  P-3 El folio de una factura de ANTICIPO se valida NUMÉRICO al registrarla, no recién
      al armar la referencia 33 semanas después; y el 409 del borrado explica la salida
      de la colisión de folio.

(P-2 —`factura_anticipo_folio` en la RAÍZ de la fila de venta— se verifica en el mismo
escenario P-1/P-2: es un dato derivado del listado, sin escritura propia.)

ESTILO (el de la casa, ver test_factura_anticipo.py): datos MARCADOS, limpieza total en
`finally` verificada con SESIÓN NUEVA (COUNT(*) == 0), auth REALISTA (una lectura en la
misma sesión del request antes de devolver el usuario, para que el read view de MySQL
nazca ANTES de los `with_for_update`) y `check()` que ACUMULA fallos en vez de abortar en
el primero — un hallazgo que tapa a otro es exactamente lo que esta ronda vino a evitar.

Requiere la BD local. NO toca el SII ni el API de Wasabil (el único DTE que aparece es una
fila fabricada a mano para el guard del borrado).

Corre con:
  cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_regresiones_bloque_a.py -q
  (o directo: ./venv/bin/python monza_contabilidad/tests/test_regresiones_bloque_a.py)
"""
import inspect
import os
import sys
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
from monza_contabilidad.router import (  # noqa: E402
    router as contab_router, _aplicar_adelantos_pendientes, _cargar_venta,
    _cobranzas_bloqueadas, _construir_factura, _construir_factura_anticipo,
    _persistir_factura,
)
from monza_contabilidad.schemas import FacturaCreate  # noqa: E402
from monza_contabilidad.service import _recompute_factura  # noqa: E402
from monza_tesoreria.router import router as tes_router  # noqa: E402
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesConciliacion, MonzaTesConciliacionIngreso,
)

# El módulo de facturas electrónicas es OPCIONAL para esta suite: solo lo usa el caso de
# la colisión de folio. Si no estuviera instalado, ese bloque se salta con su aviso en vez
# de tumbar la corrida entera.
try:
    from monza_wasabil_dte.models import MonzaWasabilDte, STATUS_EMITIDO
except ImportError:                                        # pragma: no cover
    MonzaWasabilDte = None
    STATUS_EMITIDO = 3

# monza_cotizaciones.numero es String(20): el MARK se mantiene corto a propósito.
MARK = "__TEST_RBA__"
BASE = "/api/monza/contabilidad"
TES = "/api/monza/tesoreria"

# Folio NUMÉRICO para los anticipos (crear_factura lo exige desde el cierre de la fase:
# la referencia 33 apunta a un DTE y el SII solo acepta folios numéricos). Banda
# 9982xxxx, disjunta de la 9981xxxx de test_factura_anticipo y de la 997xxxx de
# monza_wasabil_dte/tests, para que dos suites en la misma BD no choquen con el UNIQUE.
_FOLIO = {"n": 99820000}


def _folio_ant() -> str:
    _FOLIO["n"] += 1
    return str(_FOLIO["n"])


Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()


# Auth REALISTA: además de devolver el usuario hace una lectura en la MISMA sesión del
# request, como auth.get_current_user en producción. Ese SELECT abre el read view de
# MySQL ANTES de cualquier with_for_update(); con un lambda "seco" el lock sería la
# PRIMERA sentencia, el snapshot nacería después y las carreras de plata serían
# invisibles para el test.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz")


app.include_router(contab_router)
app.include_router(tes_router)
app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []
# IDs sembrados: la limpieza borra por ID además de por MARK (las facturas del SII nacen
# sin folio y no llevan el MARK en ningún campo propio salvo numero_cotizacion).
_IDS = {"cots": [], "clientes": [], "despachos": [], "dtes": []}


def check(name, cond, extra=""):
    print(("OK   | " if cond else "FAIL | ") + name + ("" if cond else f"  -> {str(extra)[:400]}"))
    if not cond:
        _fails.append(name)


# ─── Lecturas de verificación: SIEMPRE con conexión nueva ─────────────────────
# La sesión del test arrastra su propio snapshot y puede mentir en ambas direcciones.
def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _fac(fid):
    """Factura PERSISTIDA (+ líneas y cobranzas), leída con conexión nueva."""
    rows = _sql("SELECT numero_factura,es_anticipo,despacho_id,monto_neto,iva,monto_bruto,"
                "monto_pagado,saldo,estado_pago,tipo_doc FROM monza_cont_factura_cliente "
                "WHERE id=:i", i=fid)
    if not rows:
        return None
    r = rows[0]
    items = _sql("SELECT id,numero_parte,descripcion,total_neto,anticipo_factura_id,"
                 "item_cotizacion_id FROM monza_cont_factura_cliente_item "
                 "WHERE factura_id=:i ORDER BY id", i=fid)
    cobs = _sql("SELECT id,monto,medio FROM monza_cont_cobranza WHERE factura_id=:i ORDER BY id",
                i=fid)
    return {
        "folio": r[0], "es_anticipo": int(r[1] or 0), "despacho_id": r[2],
        "neto": float(r[3] or 0), "iva": float(r[4] or 0), "bruto": float(r[5] or 0),
        "pagado": float(r[6] or 0), "saldo": float(r[7] or 0), "estado": r[8], "tipo": r[9],
        "items": [{"id": i[0], "np": i[1], "desc": i[2], "total_neto": float(i[3] or 0),
                   "anticipo_factura_id": i[4], "item_cotizacion_id": i[5]} for i in items],
        "cobranzas": [{"id": c[0], "monto": float(c[1]), "medio": c[2]} for c in cobs],
        "suma_cob": sum(float(c[1]) for c in cobs),
    }


def _adel(cot_id):
    rows = _sql("SELECT id,monto,monto_aplicado FROM monza_cont_adelanto WHERE cotizacion_id=:c",
                c=cot_id)
    if not rows:
        return None
    return {"id": rows[0][0], "monto": float(rows[0][1] or 0), "aplicado": float(rows[0][2] or 0)}


def _cobs_adelanto(cot_id):
    """Cobranzas medio='adelanto' de TODA la venta (el otro lado del invariante)."""
    return [{"factura_id": r[0], "monto": float(r[1])} for r in _sql(
        "SELECT c.factura_id,c.monto FROM monza_cont_cobranza c "
        "JOIN monza_cont_factura_cliente f ON f.id=c.factura_id "
        "WHERE f.cotizacion_id=:c AND c.medio='adelanto' ORDER BY c.id", c=cot_id)]


def _brutos_de(cot_id):
    return sum(float(r[0] or 0) for r in _sql(
        "SELECT monto_bruto FROM monza_cont_factura_cliente WHERE cotizacion_id=:c", c=cot_id))


def _n_anticipos(cot_id):
    return _sql("SELECT COUNT(*) FROM monza_cont_factura_cliente "
                "WHERE cotizacion_id=:c AND es_anticipo=1", c=cot_id)[0][0]


def _preview(**kw):
    """Equivalente LOCAL del preview: el MISMO constructor que usa el preview del módulo
    SII (monza_wasabil_dte._preparar_emision_factura), sin red ni Wasabil."""
    db = SessionLocal()
    try:
        payload = FacturaCreate(**kw)
        cot = _cargar_venta(db, payload.cotizacion_id, lock=False)
        datos = (_construir_factura_anticipo(db, payload, cot) if payload.es_anticipo
                 else _construir_factura(db, payload, cot, acumular=True))
        return {
            "puede_emitir": not datos["problemas"], "problemas": datos["problemas"],
            "advertencias": datos["advertencias"], "lineas": datos["lineas"],
            "totales": {"neto": datos["neto"], "iva": datos["iva"], "bruto": datos["bruto"]},
            "descuentos": datos["descuentos"],
        }
    finally:
        db.close()


def _anticipo_via_sii(cot_id, neto):
    """Factura de ANTICIPO nacida por la VÍA SII, tal como la crea emitir_factura_sii:
    SIN folio (lo asigna el SII) y con el adelanto DIFERIDO (aplicar_adelantos=False).
    Devuelve su id. Es el arreglo del escenario que perdía la advertencia (P-1)."""
    db = SessionLocal()
    try:
        payload = FacturaCreate(cotizacion_id=cot_id, es_anticipo=True,
                                monto_neto_anticipo=neto)
        cot = _cargar_venta(db, cot_id, lock=True)
        datos = _construir_factura_anticipo(db, payload, cot)
        assert not datos["problemas"], datos["problemas"]
        factura = _persistir_factura(db, payload, cot, datos, folio=None,
                                     tipo_doc="factura", aplicar_adelantos=False)
        db.commit()
        return factura.id
    finally:
        db.close()


def _confirmar_folio_sii(cot_id, factura_id, folio):
    """Lo que hace monza_wasabil_dte._finalizar_factura_emitida al confirmarse el folio:
    lock cotización → lock factura → escribir el folio → aplicar el adelanto DIFERIDO.
    Devuelve lo que devuelve `_aplicar_adelantos_pendientes` (las advertencias)."""
    db = SessionLocal()
    try:
        cot = _cargar_venta(db, cot_id, lock=True)
        factura = (db.query(MonzaContFacturaCliente)
                   .filter(MonzaContFacturaCliente.id == factura_id)
                   .populate_existing().with_for_update().first())
        factura.numero_factura = folio
        db.flush()
        avisos = _aplicar_adelantos_pendientes(db, cot, factura)
        db.flush()
        _recompute_factura(factura, cobranzas=_cobranzas_bloqueadas(db, factura.id))
        db.commit()
        return avisos
    finally:
        db.close()


# ─── Siembra / limpieza ────────────────────────────────────────────────────────
def _crear_venta(db, sufijo, *, cantidad=10, precio=10_000.0, pct_adelanto=50, guias=(10,)):
    """Venta de 1 ítem (neto 100.000 / bruto 119.000 con los defaults) y sus guías
    'despachado'. `guias` es la qty de cada despacho (permite facturar en tandas)."""
    cli = mm.MonzaCliente(nombre=f"{MARK} CLIENTE {sufijo}", rut="11.111.111-1")
    db.add(cli); db.flush()
    neto = cantidad * precio
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-{sufijo}", cliente_id=cli.id, estado="vendida",
        total_neto=neto, iva_monto=round(neto * 0.19, 0), total_bruto=round(neto * 1.19, 0),
        iva_pct=19, forma_pago="50_adelanto", pct_adelanto=pct_adelanto,
        adelanto_verificado=0, oc_cliente=f"OC-{sufijo}",
    )
    db.add(cot); db.flush()
    it = mm.MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion="FILTRO ACEITE", numero_parte=f"NP-{sufijo}",
        cantidad=cantidad, precio_unitario_clp=precio, subtotal_clp=neto,
        estado_linea="despachado",
    )
    db.add(it); db.flush()
    desps = []
    for n, qty in enumerate(guias, start=1):
        d = mm.MonzaDespacho(numero=f"{MARK}-D{sufijo}{n}", cotizacion_id=cot.id,
                             estado="despachado", numero_guia=f"G-{sufijo}-{n}",
                             cliente_nombre=cli.nombre)
        db.add(d); db.flush()
        db.add(mm.MonzaDespachoItem(despacho_id=d.id, item_id=it.id, qty_despachada=qty))
        desps.append(d.id)
    db.commit()
    _IDS["cots"].append(cot.id)
    _IDS["clientes"].append(cli.id)
    _IDS["despachos"].extend(desps)
    return SimpleNamespace(cot=cot.id, cli=cli.id, item=it.id, desps=desps)


def _limpiar(db):
    """Borra TODO lo sembrado, en orden seguro de FK (idempotente).

    Las líneas de factura van ANTES que las facturas: la FK `anticipo_factura_id` no
    lleva ondelete (segundo cinturón del 409), así que borrar primero una factura de
    anticipo aún referenciada reventaría con 1451. Las filas DTE van todavía antes: su
    FK a la factura es RESTRICT."""
    db.rollback()
    cots = db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = sorted({c.id for c in cots} | set(_IDS["cots"]))
    cli_ids = set(_IDS["clientes"]) | {c.cliente_id for c in cots if c.cliente_id}
    if cot_ids:
        adel_ids = [a.id for a in db.query(MonzaContAdelanto)
                    .filter(MonzaContAdelanto.cotizacion_id.in_(cot_ids)).all()]
        fac_ids = [f.id for f in db.query(MonzaContFacturaCliente)
                   .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all()]
        if adel_ids:
            db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            if MonzaWasabilDte is not None:
                db.query(MonzaWasabilDte).filter(
                    MonzaWasabilDte.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            cob_ids = [c.id for c in db.query(MonzaContCobranza)
                       .filter(MonzaContCobranza.factura_id.in_(fac_ids)).all()]
            if cob_ids:
                db.query(MonzaTesConciliacionIngreso).filter(
                    MonzaTesConciliacionIngreso.cobranza_id.in_(cob_ids)
                ).delete(synchronize_session=False)
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
    desp_ids = sorted({d.id for d in db.query(mm.MonzaDespacho).filter(
        mm.MonzaDespacho.numero.like(f"{MARK}%")).all()} | set(_IDS["despachos"]))
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
    if cli_ids:
        db.query(mm.MonzaCliente).filter(
            mm.MonzaCliente.id.in_(sorted(cli_ids))).delete(synchronize_session=False)
    db.query(mm.MonzaCliente).filter(
        mm.MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=False)
    db.commit()


def _verificar_limpieza():
    """COUNT(*) == 0 con SESIÓN NUEVA: la limpieza no dejó residuos en ninguna tabla."""
    faltan = []
    for tabla, col in (("monza_cotizaciones", "numero"), ("monza_clientes", "nombre"),
                       ("monza_despachos", "numero")):
        if _sql(f"SELECT COUNT(*) FROM {tabla} WHERE {col} LIKE :m", m=f"{MARK}%")[0][0]:
            faltan.append(tabla)
    if _sql("SELECT COUNT(*) FROM monza_cont_factura_cliente "
            "WHERE numero_factura LIKE :m OR numero_cotizacion LIKE :m", m=f"{MARK}%")[0][0]:
        faltan.append("facturas(MARK)")
    for banda in ("9982%",):
        if _sql("SELECT COUNT(*) FROM monza_cont_factura_cliente "
                "WHERE numero_factura LIKE :b", b=banda)[0][0]:
            faltan.append(f"facturas(folio {banda})")
    cots, clis, desps = _IDS["cots"], _IDS["clientes"], _IDS["despachos"]
    if cots:
        ids = tuple(cots) if len(cots) > 1 else (cots[0], cots[0])
        for tabla, col in (("monza_cont_adelanto", "cotizacion_id"),
                           ("monza_cont_factura_cliente", "cotizacion_id"),
                           ("monza_cotizacion_items", "cotizacion_id")):
            if _sql(f"SELECT COUNT(*) FROM {tabla} WHERE {col} IN :i", i=ids)[0][0]:
                faltan.append(tabla)
    if clis:
        ids = tuple(clis) if len(clis) > 1 else (clis[0], clis[0])
        if _sql("SELECT COUNT(*) FROM monza_clientes WHERE id IN :i", i=ids)[0][0]:
            faltan.append("clientes(id)")
    if desps:
        ids = tuple(desps) if len(desps) > 1 else (desps[0], desps[0])
        if _sql("SELECT COUNT(*) FROM monza_despacho_items WHERE despacho_id IN :i", i=ids)[0][0]:
            faltan.append("despacho_items")
    if _IDS["dtes"]:
        ids = tuple(_IDS["dtes"]) if len(_IDS["dtes"]) > 1 else (_IDS["dtes"][0],) * 2
        if _sql("SELECT COUNT(*) FROM monza_wasabil_dte WHERE id IN :i", i=ids)[0][0]:
            faltan.append("monza_wasabil_dte")
    check("LIMPIEZA: no queda NINGÚN dato de la suite en la BD (sesión nueva)",
          not faltan, faltan)


# ═══════════ A-1 · N facturas de anticipo REALES por el mismo adelanto ═════════
def _a1(db):
    print("\n═════ A-1 · ¿se pueden emitir DOS facturas de anticipo por la misma venta? ═════")
    v = _crear_venta(db, "A1")
    folio1 = _folio_ant()
    r1 = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": folio1,
        "monto_neto_anticipo": 30_000})
    check("A-1 primer anticipo → 200", r1.status_code == 200, r1.text)
    r2 = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 30_000})
    print(f"     · 2º anticipo (sin confirmar): HTTP {r2.status_code} · {r2.text[:220]}")
    check("A-1 el 2º anticipo se RECHAZA con 409", r2.status_code == 409, r2.text)
    det = r2.json().get("detail", "") if r2.status_code == 409 else ""
    check("A-1 el 409 NOMBRA la factura de anticipo previa (folio y monto)",
          folio1 in det and "35.700" in det, det)
    check("A-1 no nació una 2ª factura de anticipo", _n_anticipos(v.cot) == 1, _n_anticipos(v.cot))
    r3 = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 30_000, "confirmar_segundo_anticipo": True})
    print(f"     · 2º anticipo CON confirmar_segundo_anticipo: HTTP {r3.status_code}")
    check("A-1 con el flag explícito SÍ deja emitir un segundo anticipo",
          r3.status_code == 200 and _n_anticipos(v.cot) == 2, r3.text)
    # El flag es EXPLÍCITO, no pegajoso: el tercero vuelve a bloquear si no se repite.
    r4 = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 10_000})
    check("A-1 el flag NO queda 'pegado': el 3º sin flag vuelve a 409",
          r4.status_code == 409 and _n_anticipos(v.cot) == 2, r4.text)


# ═══════════ A-2 · el mismo depósito contado dos veces ════════════════════════
def _a2(db):
    print("\n═════ A-2 · cobranza MANUAL sobre una factura de anticipo (doble conteo) ═════")
    v = _crear_venta(db, "A2")
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 50_000})
    if r.status_code != 200:
        check("A-2 anticipo emitido", False, r.text)
        return
    fant = r.json()["id"]
    rc = client.post(f"{BASE}/facturas/{fant}/cobranzas", json={
        "monto": 59_500, "medio": "transferencia", "banco": "BCI"})
    print(f"     · cobranza manual sobre el anticipo: HTTP {rc.status_code} · {rc.text[:200]}")
    check("A-2 la cobranza manual sobre un anticipo se RECHAZA con 409", rc.status_code == 409, rc.text)
    check("A-2 el 409 manda a Tesorería (la plata del adelanto entra por una sola puerta)",
          "Tesorería" in rc.text, rc.text)
    fa = _fac(fant)
    check("A-2 la factura de anticipo NO quedó saldada a mano",
          fa["saldo"] == 59500.0 and not fa["cobranzas"], fa)
    # El guard NO arrincona: por la puerta correcta (Tesorería) el anticipo SÍ se salda.
    ra = client.post(f"{TES}/aprobaciones/{v.cot}/aprobar",
                     json={"monto": 59_500, "fecha_pago": "2026-07-14"})
    fa = _fac(fant)
    check("A-2 por la puerta CORRECTA (Tesorería aprueba) el anticipo sí queda pagado",
          ra.status_code == 200 and fa["saldo"] == 0
          and [c["medio"] for c in fa["cobranzas"]] == ["adelanto"], (ra.text, fa))
    # Y una factura NORMAL sigue aceptando la cobranza manual de siempre (no-regresión).
    rf = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-A2F"})
    if rf.status_code == 200:
        rc2 = client.post(f"{BASE}/facturas/{rf.json()['id']}/cobranzas", json={
            "monto": 1_000, "medio": "transferencia"})
        check("A-2 NO-REGRESIÓN: la cobranza manual sobre una factura normal sigue en 200",
              rc2.status_code == 200, rc2.text)
    else:
        check("A-2 factura del despacho para la no-regresión", False, rf.text)


# ═══════════ A-3 · Σ brutos > total de la venta, sin límite ═══════════════════
def _a3(db):
    print("\n═════ A-3 · anticipos de $1 sobre una venta YA facturada al 100% ═════")
    v = _crear_venta(db, "A3", pct_adelanto=0)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-A3F"})
    check("A-3 venta facturada al 100% (bruto 119.000)",
          r.status_code == 200 and abs(_brutos_de(v.cot) - 119000.0) < 0.01, r.text)
    colados, ultimo = 0, ""
    for _i in range(50):
        rr = client.post(f"{BASE}/facturas", json={
            "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
            "monto_neto_anticipo": 1, "confirmar_segundo_anticipo": True})
        if rr.status_code == 200:
            colados += 1
        else:
            ultimo = f"HTTP {rr.status_code} {rr.text[:150]}"
    print(f"     · anticipos de $1 COLADOS: {colados}/50 · Σ brutos {_brutos_de(v.cot):,.0f} "
          f"sobre un total de 119.000 · último rechazo: {ultimo}")
    check("A-3 la tolerancia NO se compra de a un peso por factura (≤ 1 colado)",
          colados <= 1, colados)
    check("A-3 Σ brutos no se despega del total más allá de la holgura PLANA de 1 CLP",
          _brutos_de(v.cot) <= 119_001.0 + 0.01, _brutos_de(v.cot))
    check("A-3 el rechazo explica el disponible (no un error mudo)",
          "excede" in ultimo and "disponible" in ultimo, ultimo)
    # Con el guard A-1 puesto (sin el flag explícito) no se cuela NINGUNO.
    v2 = _crear_venta(db, "A3B", pct_adelanto=0)
    client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v2.cot, "despacho_id": v2.desps[0], "numero_factura": f"{MARK}-A3BF"})
    client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v2.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 1, "confirmar_segundo_anticipo": True})
    colados2 = sum(1 for _i in range(20) if client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v2.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 1}).status_code == 200)
    print(f"     · anticipos de $1 COLADOS sobre una venta que YA tiene anticipo: {colados2}/20")
    check("A-3+A-1 sobre una venta que ya tiene anticipo no se cuela ninguno", colados2 == 0, colados2)


# ═══════════ A-4 · la plata del adelanto cae en la factura equivocada ═════════
def _a4(db):
    print("\n═════ A-4 · adelanto APROBADO ANTES de emitir el anticipo (orden normal) ═════")
    v = _crear_venta(db, "A4", guias=(5, 5))
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-A4F"})
    if r.status_code != 200:
        check("A-4 factura del despacho", False, r.text)
        return
    freal = r.json()["id"]
    r = client.post(f"{TES}/aprobaciones/{v.cot}/aprobar",
                    json={"monto": 59_500, "fecha_pago": "2026-07-10"})
    check("A-4 Tesorería aprueba 59.500 el lunes (cae en la factura del despacho)",
          r.status_code == 200, r.text)
    print(f"     · antes de emitir el anticipo: factura despacho saldo {_fac(freal)['saldo']:,.0f}")
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 50_000})
    if r.status_code != 200:
        check("A-4 anticipo emitido el martes", False, r.text)
        return
    fant = r.json()["id"]
    fa, fr = _fac(fant), _fac(freal)
    a, cobs = _adel(v.cot), _cobs_adelanto(v.cot)
    print(f"     · anticipo: saldo {fa['saldo']:,.0f} estado {fa['estado']} cobranzas {fa['cobranzas']}")
    print(f"     · factura despacho: saldo {fr['saldo']:,.0f} cobranzas {fr['cobranzas']}")
    print(f"     · adelanto aplicado {a['aplicado']:,.0f} · cobranzas 'adelanto' {cobs}")
    check("A-4 la factura de ANTICIPO queda PAGADA (la plata se re-rutea)",
          fa["saldo"] == 0 and fa["estado"] == "pagada"
          and any(c["medio"] == "adelanto" and c["monto"] == 59500.0 for c in fa["cobranzas"]), fa)
    check("A-4 la factura del despacho recupera su saldo (59.500 por cobrar)",
          fr["saldo"] == 59500.0 and not any(c["medio"] == "adelanto" for c in fr["cobranzas"]), fr)
    check("A-4 INVARIANTE monto_aplicado == Σ cobranzas 'adelanto' (59.500)",
          a and abs(a["aplicado"] - 59500.0) < 0.01
          and abs(sum(c["monto"] for c in cobs) - 59500.0) < 0.01, (a, cobs))
    check("A-4 INVARIANTE Σ brutos == total de la venta (119.000)",
          abs(_brutos_de(v.cot) - 119000.0) < 0.01, _brutos_de(v.cot))
    check("A-4 el re-ruteo NO inventa advertencias cuando pudo hacerse completo",
          (r.json().get("advertencias") or []) == [], r.json().get("advertencias"))

    print("\n═════ A-4b · mismo caso con EXCEDENTE (adelanto 70.000) ═════")
    v = _crear_venta(db, "A4B", guias=(5, 5))
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-A4BF"})
    if r.status_code != 200:
        check("A-4b factura del despacho", False, r.text)
        return
    freal = r.json()["id"]
    client.post(f"{TES}/aprobaciones/{v.cot}/aprobar",
                json={"monto": 70_000, "fecha_pago": "2026-07-11"})
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 50_000})
    if r.status_code != 200:
        check("A-4b anticipo emitido", False, r.text)
        return
    fa, fr = _fac(r.json()["id"]), _fac(freal)
    a, cobs = _adel(v.cot), _cobs_adelanto(v.cot)
    print(f"     · anticipo saldo {fa['saldo']:,.0f} · despacho saldo {fr['saldo']:,.0f} "
          f"· aplicado {a['aplicado']:,.0f} · cobranzas {cobs}")
    check("A-4b el anticipo queda pagado y el EXCEDENTE (10.500) se queda en la del despacho",
          fa["saldo"] == 0 and fr["saldo"] == 49000.0
          and any(c["medio"] == "adelanto" and c["monto"] == 10500.0 for c in fr["cobranzas"]),
          (fa, fr))
    check("A-4b INVARIANTE monto_aplicado (70.000) == Σ cobranzas 'adelanto'",
          a and abs(a["aplicado"] - 70000.0) < 0.01
          and abs(sum(c["monto"] for c in cobs) - 70000.0) < 0.01, (a, cobs))

    print("\n═════ A-4c · re-ruteo BLOQUEADO por factoring vigente → advertencia (A-7) ═════")
    v = _crear_venta(db, "A4C", guias=(5, 5))
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-A4CF"})
    if r.status_code != 200:
        check("A-4c factura del despacho", False, r.text)
        return
    freal = r.json()["id"]
    client.post(f"{TES}/aprobaciones/{v.cot}/aprobar",
                json={"monto": 59_500, "fecha_pago": "2026-07-12"})
    rf = client.post(f"{BASE}/facturas/{freal}/factoring", json={"monto_adelantado": 0})
    check("A-4c factoring vigente sobre la factura del despacho", rf.status_code == 200, rf.text)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 50_000})
    print(f"     · anticipo con la otra factura cedida al factor: HTTP {r.status_code} "
          f"· advertencias {r.json().get('advertencias') if r.status_code == 200 else '-'}")
    check("A-4c NO falla: el anticipo se emite igual", r.status_code == 200, r.text)
    if r.status_code == 200:
        adv = r.json().get("advertencias") or []
        check("A-4c devuelve una ADVERTENCIA explicando qué hacer",
              any("adelanto" in x.lower() for x in adv), adv)
        check("A-4c la advertencia NOMBRA el motivo real (factoring) y la salida",
              any("factor" in x.lower() for x in adv)
              and any("Tesorería" in x for x in adv), adv)
        a, cobs = _adel(v.cot), _cobs_adelanto(v.cot)
        check("A-4c INVARIANTE intacto pese al bloqueo",
              a and abs(a["aplicado"] - sum(c["monto"] for c in cobs)) < 0.01, (a, cobs))


# ═══════════ A-5 · BOLETA que descuenta un anticipo ═══════════════════════════
def _a5(db):
    print("\n═════ A-5 · una BOLETA del despacho descontando un anticipo ═════")
    v = _crear_venta(db, "A5", pct_adelanto=0)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 50_000})
    if r.status_code != 200:
        check("A-5 anticipo emitido", False, r.text)
        return
    p = _preview(cotizacion_id=v.cot, despacho_id=v.desps[0], tipo_doc="boleta")
    print(f"     · preview boleta: puede_emitir={p['puede_emitir']} · "
          f"líneas DESCUENTO={[ln['descripcion'] for ln in p['lineas'] if ln['numero_parte'] == 'DESCUENTO']}")
    check("A-5 el preview de la BOLETA bloquea", p["puede_emitir"] is False, p["problemas"])
    check("A-5 la boleta no arma ninguna línea que cite una FACTURA",
          not any(ln["numero_parte"] == "DESCUENTO" for ln in p["lineas"]), p["lineas"])
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "tipo_doc": "boleta"})
    print(f"     · emitir boleta: HTTP {r.status_code} · {r.text[:200]}")
    check("A-5 emitir la boleta se rechaza", r.status_code in (400, 409), r.text)
    check("A-5 no nació ninguna boleta con descuento de anticipo",
          _sql("SELECT COUNT(*) FROM monza_cont_factura_cliente "
               "WHERE cotizacion_id=:c AND tipo_doc='boleta'", c=v.cot)[0][0] == 0)
    # La misma mercadería, emitida como FACTURA, sí pasa (el bloqueo es del TIPO de doc).
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-A5F"})
    check("A-5 la MISMA mercadería como FACTURA sí se emite (el bloqueo es del tipo)",
          r.status_code == 200, r.text)


# ═══════════ A-6 · el preview inventa un neto intermedio ══════════════════════
def _a6(db):
    print("\n═════ A-6 · anticipo sin folio + otro con folio: el preview no debe inventar ═════")
    v = _crear_venta(db, "A6", pct_adelanto=0)
    r1 = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 30_000})
    r2 = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 30_000, "confirmar_segundo_anticipo": True})
    if r1.status_code != 200 or r2.status_code != 200:
        check("A-6 dos anticipos emitidos", False, (r1.text, r2.text))
        return
    # El PRIMERO se queda sin folio: es el estado real de una emisión electrónica en
    # vuelo o rechazada (la vía SII persiste sin folio hasta que el SII lo confirma).
    db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.id == r1.json()["id"]).update({"numero_factura": None})
    db.commit()
    p = _preview(cotizacion_id=v.cot, despacho_id=v.desps[0])
    dsc = [ln for ln in p["lineas"] if ln["numero_parte"] == "DESCUENTO"]
    print(f"     · preview: puede_emitir={p['puede_emitir']} · neto {p['totales']['neto']:,.0f} "
          f"· descuentos acumulados {len(dsc)} · problemas {p['problemas']}")
    check("A-6 el preview BLOQUEA por el anticipo sin folio",
          p["puede_emitir"] is False and any("folio del SII" in x for x in p["problemas"]), p)
    check("A-6 NO acumula el descuento del anticipo siguiente (0 líneas DESCUENTO)",
          len(dsc) == 0, dsc)
    check("A-6 el neto publicado es el SIN descontar (100.000), no una cifra intermedia",
          p["totales"]["neto"] == 100000.0, p["totales"])
    check("A-6 tampoco publica descuentos para las referencias 33",
          p["descuentos"] == [], p["descuentos"])
    # Con el folio de vuelta, la MISMA venta descuenta los DOS anticipos y cuadra.
    db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.id == r1.json()["id"]).update({"numero_factura": _folio_ant()})
    db.commit()
    p = _preview(cotizacion_id=v.cot, despacho_id=v.desps[0])
    check("A-6 recuperado el folio: descuenta los DOS anticipos (neto 40.000)",
          p["puede_emitir"] is True and len(p["descuentos"]) == 2
          and p["totales"]["neto"] == 40000.0, (p["problemas"], p["totales"]))


# ═══════════ A-7 · crear_factura descarta las advertencias ════════════════════
def _a7(db):
    print("\n═════ A-7 · las advertencias de la vía manual ═════")
    v = _crear_venta(db, "A7", pct_adelanto=0)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 100_000})
    if r.status_code != 200:
        check("A-7 anticipo por el total", False, r.text)
        return
    check("A-7 el campo `advertencias` es ADITIVO: viene SIEMPRE, aunque esté vacío",
          "advertencias" in r.json(), sorted(r.json().keys()))
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-A7F"})
    check("A-7 factura final en $0 → 200", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    body = r.json()
    print(f"     · respuesta de crear_factura: advertencias={body.get('advertencias')}")
    check("A-7 la respuesta TRAE las advertencias", "advertencias" in body, sorted(body.keys()))
    check("A-7 la advertencia explica la factura en $0",
          any("$0" in x for x in (body.get("advertencias") or [])), body.get("advertencias"))


# ═══════════ A-8 · esquema de es_anticipo ═════════════════════════════════════
def _a8():
    print("\n═════ A-8 · esquema de es_anticipo (BD migrada y BD fresca) ═════")
    row = _sql("SELECT is_nullable, column_default FROM information_schema.columns "
               "WHERE table_schema=DATABASE() AND table_name='monza_cont_factura_cliente' "
               "AND column_name='es_anticipo'")[0]
    print(f"     · information_schema: is_nullable={row[0]} column_default={row[1]}")
    check("A-8 la columna en la BD quedó NOT NULL DEFAULT 0",
          row[0] == "NO" and str(row[1]) == "0", row)
    from sqlalchemy.schema import CreateTable
    ddl = str(CreateTable(MonzaContFacturaCliente.__table__).compile(engine))
    linea = [x.strip() for x in ddl.splitlines() if "es_anticipo" in x]
    print(f"     · DDL que emitiría create_all en una BD FRESCA: {linea}")
    check("A-8 create_all (BD fresca) emite la columna con NOT NULL y DEFAULT 0",
          bool(linea) and "NOT NULL" in linea[0]
          and "DEFAULT 0" in linea[0].replace("'", "").replace('"', ""), linea)
    # Cinturón y tirantes: los ORDER BY de este módulo van blindados con COALESCE, así
    # que una fila legada con NULL no invierte el FIFO (en DESC MySQL manda los NULL al
    # final). El order_by gemelo de monza_tesoreria es de otro bloque.
    from monza_contabilidad import router as R
    src = inspect.getsource(R.verificar_adelanto)
    check("A-8 el order_by de verificar_adelanto va blindado con COALESCE",
          "coalesce" in src.lower() and "es_anticipo" in src, "")
    src2 = inspect.getsource(R._reencauzar_adelanto_al_anticipo)
    check("A-8 el filtro del re-ruteo también usa COALESCE (un NULL no pasa por anticipo)",
          "coalesce" in src2.lower() and "es_anticipo" in src2, "")


# ═══════ P-1 · la advertencia del re-ruteo por la VÍA SII + P-2 · el folio ═════
def _p1_p2(db):
    print("\n═════ P-1 · la vía SII (folio confirmado) NO puede perder la advertencia ═════")
    v = _crear_venta(db, "P1", guias=(5, 5))
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-P1F"})
    if r.status_code != 200:
        check("P-1 factura del despacho", False, r.text)
        return
    freal = r.json()["id"]
    ra = client.post(f"{TES}/aprobaciones/{v.cot}/aprobar",
                     json={"monto": 59_500, "fecha_pago": "2026-07-13"})
    check("P-1 Tesorería aprueba primero (la plata cae en la factura del despacho)",
          ra.status_code == 200 and _fac(freal)["saldo"] == 0, ra.text)
    rf = client.post(f"{BASE}/facturas/{freal}/factoring", json={"monto_adelantado": 0})
    check("P-1 esa factura se cede al factor (el re-ruteo NO va a poder)",
          rf.status_code == 200, rf.text)
    # La vía SII: la factura de anticipo nace SIN folio y con el adelanto DIFERIDO.
    fant = _anticipo_via_sii(v.cot, 50_000)
    fa = _fac(fant)
    check("P-1 la factura de anticipo nace sin folio y sin cobranzas (adelanto diferido)",
          fa["folio"] is None and not fa["cobranzas"], fa)
    # ...y al confirmarse el folio del SII se aplica el adelanto: AHÍ nace la advertencia.
    folio = _folio_ant()
    avisos = _confirmar_folio_sii(v.cot, fant, folio)
    print(f"     · _aplicar_adelantos_pendientes devolvió: {avisos}")
    check("P-1 _aplicar_adelantos_pendientes DEVUELVE la advertencia (no la descarta)",
          isinstance(avisos, list) and any("adelanto" in x.lower() for x in avisos), avisos)
    check("P-1 la advertencia dice el motivo (factoring) y la salida (Tesorería)",
          any("factor" in x.lower() for x in avisos) and any("Tesorería" in x for x in avisos),
          avisos)
    fa = _fac(fant)
    a, cobs = _adel(v.cot), _cobs_adelanto(v.cot)
    check("P-1 el anticipo queda POR COBRAR (es lo que la advertencia avisa), sin fuga",
          fa["saldo"] == 59500.0 and a and abs(a["aplicado"] - sum(c["monto"] for c in cobs)) < 0.01,
          (fa, a, cobs))
    # La firma acepta además una lista del llamador (los caminos que ya acumulaban).
    sig = inspect.signature(_aplicar_adelantos_pendientes)
    check("P-1 la firma expone `advertencias` para el módulo DTE",
          "advertencias" in sig.parameters, str(sig))

    print("\n═════ P-2 · `factura_anticipo_folio` en la RAÍZ de la fila de venta ═════")
    # Caso NORMAL de la vía B: el anticipo se emite ANTES de que Tesorería apruebe, así
    # que `adelanto` es None y el folio SOLO puede salir por la raíz.
    v2 = _crear_venta(db, "P2", pct_adelanto=0)
    folio2 = _folio_ant()
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v2.cot, "es_anticipo": True, "numero_factura": folio2,
        "monto_neto_anticipo": 50_000})
    if r.status_code != 200:
        check("P-2 anticipo emitido", False, r.text)
        return
    fila = next((x for x in client.get(f"{BASE}/ventas", params={"q": f"{MARK}-P2"}).json()
                 if x["cotizacion_id"] == v2.cot), None)
    print(f"     · fila del listado: adelanto={fila and fila.get('adelanto')} "
          f"· raíz={fila and fila.get('factura_anticipo_folio')}")
    check("P-2 el listado publica factura_anticipo_folio en la RAÍZ de la fila",
          bool(fila) and fila.get("factura_anticipo_folio") == folio2, fila)
    check("P-2 y lo hace con `adelanto` en None (Tesorería aún no aprueba: el caso normal)",
          bool(fila) and fila.get("adelanto") is None, fila and fila.get("adelanto"))
    det = client.get(f"{BASE}/ventas/{v2.cot}").json()
    check("P-2 el DETALLE publica el mismo dato en la raíz (coherencia listado/detalle)",
          det.get("factura_anticipo_folio") == folio2, det.get("factura_anticipo_folio"))
    # Sin factura de anticipo el campo es None (no una cadena vacía que el front pinte).
    v3 = _crear_venta(db, "P2B", pct_adelanto=0)
    fila3 = next((x for x in client.get(f"{BASE}/ventas", params={"q": f"{MARK}-P2B"}).json()
                  if x["cotizacion_id"] == v3.cot), None)
    check("P-2 una venta sin anticipo publica None (no una cadena vacía)",
          bool(fila3) and fila3.get("factura_anticipo_folio") is None, fila3)


# ═══════ P-3 · folio NUMÉRICO al registrar + salida de la colisión de folio ════
def _p3(db):
    print("\n═════ P-3 · el folio de un ANTICIPO se valida NUMÉRICO al registrarlo ═════")
    v = _crear_venta(db, "P3", pct_adelanto=0)
    # El último es el que reventaba el int() de la validación si no se acotara el largo
    # (y de todas formas el SII no acepta un FolioRef de más de 18 caracteres).
    for etiqueta, folio in (("N/A-99", "N/A-99"), ("con espacio", "FAC 123"),
                            ("N/A", "N/A"), ("cero", "0"), ("negativo", "-5"),
                            ("dígito árabe", "٣٤٥"), ("kilométrico", "9" * 40)):
        r = client.post(f"{BASE}/facturas", json={
            "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": folio,
            "monto_neto_anticipo": 10_000})
        ok = r.status_code == 400 and "numérico" in r.text
        print(f"     · folio {etiqueta!r}: HTTP {r.status_code}")
        check(f"P-3 folio {etiqueta} → 400 con mensaje claro", ok, r.text[:200])
    check("P-3 ninguno de esos intentos dejó factura registrada", _n_anticipos(v.cot) == 0)
    folio_ok = _folio_ant()
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": folio_ok,
        "monto_neto_anticipo": 50_000})
    check("P-3 con el folio NUMÉRICO del SII sí se registra", r.status_code == 200, r.text)
    # NO-REGRESIÓN: una factura NORMAL con folio legado no numérico se sigue registrando
    # (nadie la referencia con una 33; romper eso sería una regresión gratuita).
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-P3F"})
    check("P-3 NO-REGRESIÓN: una factura NORMAL con folio legado no numérico → 200",
          r.status_code == 200, r.text)

    print("\n═════ P-3b · colisión de folio: el 409 del borrado trae la SALIDA ═════")
    if MonzaWasabilDte is None:                              # pragma: no cover
        check("P-3b (saltado) el módulo monza_wasabil_dte no está instalado", True)
        return
    v2 = _crear_venta(db, "P3B", pct_adelanto=0, guias=(5, 5))
    ocupado = _folio_ant()
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v2.cot, "despacho_id": v2.desps[0], "numero_factura": ocupado})
    if r.status_code != 200:
        check("P-3b factura que OCUPA el folio", False, r.text)
        return
    ocupante = r.json()["id"]
    # La factura de anticipo emitida al SII: el DTE quedó EMITIDO con ese mismo folio y
    # el UNIQUE global impidió escribirlo → la factura local se quedó SIN N°.
    fant = _anticipo_via_sii(v2.cot, 50_000)
    dte = MonzaWasabilDte(empresa="automotriz", tipo_dte=33, factura_id=fant,
                          uuid="uuid-colision-f7", status_id=STATUS_EMITIDO, folio=ocupado)
    db.add(dte)
    db.commit()
    _IDS["dtes"].append(dte.id)
    rd = client.delete(f"{BASE}/facturas/{fant}")
    det = rd.json().get("detail", rd.text)
    print(f"     · borrar el anticipo trabado: HTTP {rd.status_code} · {str(det)[:260]}")
    check("P-3b el borrado responde 409 (el documento vive ante el SII)", rd.status_code == 409, det)
    check("P-3b el 409 NOMBRA la factura local que tiene el folio ocupado",
          f"#{ocupante}" in str(det), det)
    # La salida REAL es local (corregir el N° de la otra factura y volver a consultar el
    # estado); mandar a anular en Wasabil sería un consejo equivocado — el documento del
    # SII está bien. Por eso se exige que ese consejo NO aparezca en este caso.
    _d = str(det).lower()
    check("P-3b el 409 dice la salida REAL (corregir esa otra factura, no anular en Wasabil)",
          "corrige" in _d and "vuelve a consultar el estado" in _d and "anúlala" not in _d, det)
    check("P-3b la factura de anticipo sigue existiendo (no se borró nada)",
          _fac(fant) is not None)
    # NO-REGRESIÓN: con el folio ya grabado en la factura, el mensaje es el de siempre.
    db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.id == ocupante).update({"numero_factura": _folio_ant()})
    db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.id == fant).update({"numero_factura": ocupado})
    db.commit()
    rd = client.delete(f"{BASE}/facturas/{fant}")
    det = rd.json().get("detail", rd.text)
    check("P-3b NO-REGRESIÓN: con el N° ya grabado vuelve el mensaje de siempre (anular en Wasabil)",
          rd.status_code == 409 and "Wasabil" in str(det) and "anúlala" in str(det), det)


def _correr():
    del _fails[:]
    for k in _IDS:
        del _IDS[k][:]
    db = SessionLocal()
    try:
        _limpiar(db)
        _a1(db)
        _a2(db)
        _a3(db)
        _a4(db)
        _a5(db)
        _a6(db)
        _a7(db)
        _a8()
        _p1_p2(db)
        _p3(db)
    finally:
        _limpiar(db)
        _verificar_limpieza()
        db.close()
    print()
    if _fails:
        print(f"=== {len(_fails)} FALLO(S): {_fails} ===")
        return False
    print("=== TODO OK ===")
    return True


def test_regresiones_bloque_a():
    assert _correr(), f"{len(_fails)} checks fallaron: {_fails}"


if __name__ == "__main__":
    sys.exit(0 if _correr() else 1)
