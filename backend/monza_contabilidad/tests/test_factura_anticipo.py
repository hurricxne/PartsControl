"""Tests de la FACTURA DE ANTICIPO (vía B) de MonzaParts y su descuento automático — F7.

Contrato: los 17 puntos del §3 de la spec de la fase, traducidos uno a uno desde la
suite viva de Grupo AM (backend/tests_contabilidad/test_factura_anticipo.py) al modelo
de Monza. Cubre: emisión SIN guía (única excepción a la regla rectora), boleta 400,
receptor incompleto, factoring 409, tope contra el total de la venta, aplicación del
adelanto a la factura de anticipo, descuento automático en la factura del despacho real
(línea negativa con el folio, neto/IVA recalculados, Σ brutos == total de la venta),
persistencia de esa línea, borrados y sus reversiones derivadas, factura final en $0 con
advertencia, anticipo sin folio SII que BLOQUEA, y el EXCEDENTE del adelanto.

ADAPTACIONES al modelo de Monza (conscientes, ver la spec §1 y models.py):
  · La VENTA es la cotización (no hay OcCliente).
  · El adelanto NO se "informa" como fila: Comercial pone `pct_adelanto` en la venta y la
    fila MonzaContAdelanto nace cuando TESORERÍA aprueba
    (POST /api/monza/tesoreria/aprobaciones/{cot_id}/aprobar). Se prueba también la
    gemela de Contabilidad (POST /ventas/{cot}/adelanto/verificar), declaradas "en sync".
  · NO existe MonzaContAdelanto.factura_anticipo_id: el vínculo adelanto ↔ factura de
    anticipo es DERIVADO (`factura_anticipo_folio` sale de las facturas es_anticipo=1).
  · El receptor NO viaja en el payload (no hay rut_cliente/razon_social como en GA): sale
    de la ficha del cliente de la venta, así que el bloqueo se prueba sobre esa ficha.
  · El "preview" de Contabilidad no es un endpoint (vive en monza_wasabil_dte y llama a
    Wasabil por la ficha del receptor): aquí se ejercita el MISMO constructor que ese
    preview usa —_construir_factura_anticipo / _construir_factura— vía `_preview`, sin
    red. La capa SII se prueba en monza_wasabil_dte/tests.

POR QUÉ el test del EXCEDENTE assertea el saldo de CADA factura por separado: el
invariante `monto_aplicado == Σ cobranzas medio='adelanto'` se cumple IGUAL cuando el
ruteo del dinero está mal (la plata cae en la factura equivocada). Por eso los escenarios
D y F emiten la factura del despacho real ANTES que la de anticipo: con el orden viejo
(`id.asc()` a secas) la plata entraría primero a la factura del despacho y los saldos
quedarían cruzados — con la misma suma total. Sin saldos por factura, la regresión que
motivó el `order_by(es_anticipo.desc())` sería invisible.

Datos MARCADOS + limpieza TOTAL en `finally` (orden seguro de FKs) + verificación de la
limpieza con SESIÓN NUEVA (COUNT(*) == 0). Requiere la BD local.

Corre con:
  cd backend && ./venv/bin/python -m pytest monza_contabilidad/tests/test_factura_anticipo.py -q
  (o directo: ./venv/bin/python monza_contabilidad/tests/test_factura_anticipo.py)
"""
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
    router as contab_router, _cargar_venta, _construir_factura, _construir_factura_anticipo,
)
from monza_contabilidad.schemas import FacturaCreate  # noqa: E402
from monza_tesoreria.router import router as tes_router  # noqa: E402
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesConciliacion, MonzaTesConciliacionIngreso,
)

# monza_cotizaciones.numero es String(20): el MARK se mantiene corto a propósito.
MARK = "__TEST_MFANT__"
BASE = "/api/monza/contabilidad"
TES = "/api/monza/tesoreria"

# Folio NUMÉRICO para las facturas de ANTICIPO. Registrar un anticipo con un folio que
# no sea el correlativo del SII ('__TEST_MFANT__-ANT1', 'N/A-99', 'FAC 123') responde 400
# desde el cierre de la fase: la factura de la mercadería lo cita en una referencia tipo
# 33 y el SII solo acepta folios numéricos (ver crear_factura). Banda 9981xxxx: no choca
# con folios reales (los del SII son correlativos chicos), no choca con la banda 997xxxx
# de monza_wasabil_dte/tests ni con la 9982xxxx de test_regresiones_bloque_a, y la
# limpieza los borra igual porque barre por numero_cotizacion, no por folio.
_FOLIO_ANT = {"n": 99810000}


def _folio_ant() -> str:
    _FOLIO_ANT["n"] += 1
    return str(_FOLIO_ANT["n"])


Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()


# Auth REALISTA (espejo de monza_tests/test_viaje_de_la_plata.py): además de devolver el
# usuario hace una lectura en la MISMA sesión del request, como auth.get_current_user en
# producción. Ese SELECT abre el read view de MySQL ANTES de cualquier with_for_update();
# con un lambda "seco" el lock sería la PRIMERA sentencia, el snapshot nacería DESPUÉS y
# las carreras de plata serían invisibles para el test.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz")


app.include_router(contab_router)
app.include_router(tes_router)
app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []
# IDs sembrados: la limpieza borra por ID (no solo por MARK) porque un escenario deja
# temporalmente la razón social del cliente en blanco.
_IDS = {"cots": [], "clientes": [], "despachos": []}


def check(name, cond, extra=""):
    print(("OK   | " if cond else "FAIL | ") + name + ("" if cond else f"  -> {str(extra)[:300]}"))
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
                "monto_pagado,saldo,estado_pago,tipo_doc "
                "FROM monza_cont_factura_cliente WHERE id=:i", i=fid)
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
    return {"id": rows[0][0], "monto": float(rows[0][1] or 0),
            "aplicado": float(rows[0][2] or 0)}


def _cobs_adelanto(cot_id):
    """Cobranzas medio='adelanto' de TODA la venta (el otro lado del invariante)."""
    return [{"factura_id": r[0], "monto": float(r[1])} for r in _sql(
        "SELECT c.factura_id,c.monto FROM monza_cont_cobranza c "
        "JOIN monza_cont_factura_cliente f ON f.id=c.factura_id "
        "WHERE f.cotizacion_id=:c AND c.medio='adelanto' ORDER BY c.id", c=cot_id)]


def _brutos_de(cot_id):
    return sum(float(r[0] or 0) for r in _sql(
        "SELECT monto_bruto FROM monza_cont_factura_cliente WHERE cotizacion_id=:c", c=cot_id))


def _preview(**kw):
    """Equivalente LOCAL del preview: llama al MISMO constructor que usa el preview del
    módulo SII (monza_wasabil_dte._preparar_emision_factura), sin red ni Wasabil."""
    db = SessionLocal()
    try:
        payload = FacturaCreate(**kw)
        cot = _cargar_venta(db, payload.cotizacion_id, lock=False)
        datos = (_construir_factura_anticipo(db, payload, cot) if payload.es_anticipo
                 else _construir_factura(db, payload, cot, acumular=True))
        return {
            "puede_emitir": not datos["problemas"],
            "problemas": datos["problemas"],
            "advertencias": datos["advertencias"],
            "lineas": datos["lineas"],
            "totales": {"neto": datos["neto"], "iva": datos["iva"], "bruto": datos["bruto"]},
            "descuentos": datos["descuentos"],
            "receptor": datos["receptor"],
        }
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
                             cliente_nombre=cli.nombre,
                             # regla 2026-08-06: sin firma no se factura (el gate tiene suite propia)
                             guia_firmada=1)
        db.add(d); db.flush()
        db.add(mm.MonzaDespachoItem(despacho_id=d.id, item_id=it.id, qty_despachada=qty))
        desps.append(d.id)
    db.commit()
    _IDS["cots"].append(cot.id)
    _IDS["clientes"].append(cli.id)
    _IDS["despachos"].extend(desps)
    return SimpleNamespace(cot=cot.id, cli=cli.id, item=it.id, desps=desps)


def _limpiar(db):
    """Borra TODO lo del MARK en orden seguro de FK (idempotente).

    Las líneas de factura se borran ANTES que las facturas a propósito: la FK
    `anticipo_factura_id` NO lleva ondelete (segundo cinturón del 409), así que borrar
    primero una factura de anticipo aún referenciada reventaría con 1451."""
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
    cots, clis = _IDS["cots"], _IDS["clientes"]
    desps = _IDS["despachos"]
    faltan = []
    if _sql("SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE :m", m=f"{MARK}%")[0][0]:
        faltan.append("cotizaciones")
    if _sql("SELECT COUNT(*) FROM monza_clientes WHERE nombre LIKE :m", m=f"{MARK}%")[0][0]:
        faltan.append("clientes(nombre)")
    if _sql("SELECT COUNT(*) FROM monza_despachos WHERE numero LIKE :m", m=f"{MARK}%")[0][0]:
        faltan.append("despachos")
    if _sql("SELECT COUNT(*) FROM monza_cont_factura_cliente "
            "WHERE numero_factura LIKE :m OR numero_cotizacion LIKE :m", m=f"{MARK}%")[0][0]:
        faltan.append("facturas")
    if clis and _sql("SELECT COUNT(*) FROM monza_clientes WHERE id IN :i",
                     i=tuple(clis) if len(clis) > 1 else (clis[0], clis[0]))[0][0]:
        faltan.append("clientes(id)")
    if cots:
        ids = tuple(cots) if len(cots) > 1 else (cots[0], cots[0])
        for tabla, col in (("monza_cont_adelanto", "cotizacion_id"),
                           ("monza_cont_factura_cliente", "cotizacion_id"),
                           ("monza_cotizacion_items", "cotizacion_id")):
            if _sql(f"SELECT COUNT(*) FROM {tabla} WHERE {col} IN :i", i=ids)[0][0]:
                faltan.append(tabla)
    if desps:
        ids = tuple(desps) if len(desps) > 1 else (desps[0], desps[0])
        if _sql("SELECT COUNT(*) FROM monza_despacho_items WHERE despacho_id IN :i", i=ids)[0][0]:
            faltan.append("despacho_items")
    check("LIMPIEZA: no queda NINGÚN dato del test en la BD (COUNT(*) == 0)",
          not faltan, faltan)


# ═══════════════════════════ ESCENARIOS ════════════════════════════════════════
def _escenario_a(db):
    """Venta A — el recorrido completo de la vía B (puntos 1-14 del contrato).
    10 u × $10.000 → neto 100.000 / bruto 119.000, adelanto informado 50%."""
    print("\n═════ A · CICLO COMPLETO DE LA VÍA B (neto 100.000 / bruto 119.000) ═════")
    v = _crear_venta(db, "CA")

    # ── 1) Preview del anticipo ────────────────────────────────────────────────
    p = _preview(cotizacion_id=v.cot, es_anticipo=True, monto_neto_anticipo=50_000)
    check("A1 preview anticipo SIN guía: puede emitir", p["puede_emitir"] is True, p["problemas"])
    check("A1 preview anticipo: totales neto 50.000 / IVA 9.500 / bruto 59.500",
          p["totales"] == {"neto": 50000.0, "iva": 9500.0, "bruto": 59500.0}, p["totales"])
    check("A1 preview anticipo: UNA sola línea 'ANTICIPO'",
          len(p["lineas"]) == 1 and p["lineas"][0]["numero_parte"] == "ANTICIPO"
          and p["lineas"][0]["total_neto"] == 50000.0, p["lineas"])

    # ── 2) El anticipo debe ser FACTURA (DTE 33), nunca boleta ─────────────────
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "tipo_doc": "boleta",
        "numero_factura": f"{MARK}-ANTB", "monto_neto_anticipo": 50_000})
    check("A2 anticipo como boleta → 400", r.status_code == 400, r.text)
    check("A2 mensaje explica que debe ser tipo 'factura'", "factura" in r.text.lower(), r.text)

    # ── 3) Receptor incompleto bloquea IGUAL que una factura normal ────────────
    # (la única excepción de la vía B es la GUÍA, no la completitud del receptor)
    cli = db.query(mm.MonzaCliente).filter(mm.MonzaCliente.id == v.cli).first()
    nombre_ok, rut_ok = cli.nombre, cli.rut
    cli.nombre = "   "
    db.commit()
    p = _preview(cotizacion_id=v.cot, es_anticipo=True, monto_neto_anticipo=50_000)
    check("A3 anticipo sin razón social: preview BLOQUEA",
          p["puede_emitir"] is False and any("razón social" in x for x in p["problemas"]), p)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": f"{MARK}-ANTR",
        "monto_neto_anticipo": 50_000})
    check("A3 anticipo sin razón social → 400 y manda a la ficha del cliente",
          r.status_code == 400 and "ficha del cliente" in r.text, r.text)
    cli.nombre, cli.rut = nombre_ok, None
    db.commit()
    p = _preview(cotizacion_id=v.cot, es_anticipo=True, monto_neto_anticipo=50_000)
    check("A3 anticipo sin RUT: preview BLOQUEA",
          p["puede_emitir"] is False and any("RUT" in x for x in p["problemas"]), p)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": f"{MARK}-ANTR",
        "monto_neto_anticipo": 50_000})
    check("A3 anticipo sin RUT → 400", r.status_code == 400, r.text)
    cli.rut = rut_ok
    db.commit()
    p = _preview(cotizacion_id=v.cot, es_anticipo=True, monto_neto_anticipo=50_000)
    check("A3 con la ficha del cliente completa: DESBLOQUEA",
          p["puede_emitir"] is True and p["receptor"]["rut"] == rut_ok, p)

    # ── 4) Emisión de la factura de anticipo ───────────────────────────────────
    folio_ant1 = _folio_ant()
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": folio_ant1,
        "monto_neto_anticipo": 50_000, "descripcion_anticipo": "Anticipo 50% pedido"})
    check("A4 emitir factura de anticipo → 200", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    fant_id = r.json()["id"]
    check("A4 respuesta: es_anticipo=True y despacho_id None",
          r.json()["es_anticipo"] is True and r.json()["despacho_id"] is None, r.json())
    fa = _fac(fant_id)
    check("A4 PERSISTIDA: es_anticipo=1, SIN guía, bruto 59.500 y saldo == bruto",
          fa["es_anticipo"] == 1 and fa["despacho_id"] is None and fa["bruto"] == 59500.0
          and fa["saldo"] == 59500.0 and fa["estado"] == "por_cobrar", fa)
    check("A4 línea única 'ANTICIPO' SIN ítem físico (no consume mercadería)",
          len(fa["items"]) == 1 and fa["items"][0]["np"] == "ANTICIPO"
          and fa["items"][0]["item_cotizacion_id"] is None, fa["items"])

    # Agregados de la venta (§4.9): la mercadería sigue ENTERA por facturar y el
    # anticipo aparece como "por descontar" (base FÍSICA intacta, regla de oro G15).
    d = client.get(f"{BASE}/ventas/{v.cot}").json()
    check("A4 detalle: mercadería pendiente 119.000 y anticipo por descontar 59.500",
          d["resumen"]["mercaderia_pendiente_clp"] == 119000
          and d["resumen"]["anticipo_por_descontar_clp"] == 59500
          and d["resumen"]["por_facturar_clp"] == 59500, d["resumen"])
    fila = next((x for x in client.get(f"{BASE}/ventas", params={"q": f"{MARK}-CA"}).json()
                 if x["cotizacion_id"] == v.cot), None)
    check("A4 listado publica los MISMOS agregados que el detalle (sin N+1)",
          fila and fila["anticipo_por_descontar_clp"] == 59500
          and fila["mercaderia_pendiente_clp"] == 119000
          and fila["por_facturar_clp"] == 59500, fila)

    # ── 5) Factoring sobre una factura de anticipo → 409 ───────────────────────
    r = client.post(f"{BASE}/facturas/{fant_id}/factoring", json={"monto_adelantado": 10_000})
    check("A5 factoring sobre la factura de anticipo → 409",
          r.status_code == 409 and "anticipo" in r.text.lower(), r.text)

    # ── 6) Tope: un 2° anticipo que excede lo aún no facturado → 409 "excede" ──
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 60_000})
    check("A6 2° anticipo que excede la venta → 409 con la palabra 'excede'",
          r.status_code == 409 and "excede" in r.json().get("detail", "").lower(), r.text)
    check("A6 el mensaje trae el disponible BRUTO (59.500 de 119.000)",
          r.status_code == 409 and "59.500" in r.json().get("detail", "")
          and "119.000" in r.json().get("detail", ""), r.text)

    # ── 7) Tesorería aprueba → la plata cae en la factura de ANTICIPO ──────────
    r = client.post(f"{TES}/aprobaciones/{v.cot}/aprobar", json={
        "monto": 59_500, "fecha_pago": "2026-07-10", "banco": "BCI",
        "numero_operacion": f"{MARK}-OP1"})
    check("A7 Tesorería aprueba y aplica 59.500 AHORA",
          r.status_code == 200 and r.json().get("aplicado_ahora_clp") == 59500.0, r.text)
    fa = _fac(fant_id)
    check("A7 la factura de anticipo queda PAGADA con cobranza medio='adelanto'",
          fa["saldo"] == 0 and fa["estado"] == "pagada"
          and [c["medio"] for c in fa["cobranzas"]] == ["adelanto"]
          and fa["cobranzas"][0]["monto"] == 59500.0, fa)
    d = client.get(f"{BASE}/ventas/{v.cot}").json()
    check("A7 el adelanto publica el folio DERIVADO de su factura de anticipo",
          d["adelanto"] and d["adelanto"]["factura_anticipo_folio"] == folio_ant1,
          d.get("adelanto"))

    # ── 8) Preview de la factura del despacho real: descuento automático ───────
    p = _preview(cotizacion_id=v.cot, despacho_id=v.desps[0])
    dsc = [ln for ln in p["lineas"] if ln["numero_parte"] == "DESCUENTO"]
    check("A8 preview final: línea DESCUENTO negativa con el FOLIO del anticipo",
          len(dsc) == 1 and dsc[0]["total_neto"] == -50000.0
          and folio_ant1 in dsc[0]["descripcion"], p["lineas"])
    check("A8 preview final: neto 50.000 / IVA 9.500 sobre el neto YA DESCONTADO",
          p["totales"] == {"neto": 50000.0, "iva": 9500.0, "bruto": 59500.0}, p["totales"])

    # ── 9/10/11) Emisión de la factura del despacho real ──────────────────────
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-F1"})
    check("A9 emitir la factura del despacho real → 200 "
          "(el tope Σ brutos usa el neto YA DESCONTADO)", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    ffin_id = r.json()["id"]
    ff = _fac(ffin_id)
    check("A9 la final NO lleva cobranza 'adelanto' (esa plata quedó en el anticipo)",
          not any(c["medio"] == "adelanto" for c in ff["cobranzas"]), ff["cobranzas"])
    check("A9 final descontada: bruto 59.500 y saldo por cobrar del cliente",
          ff["bruto"] == 59500.0 and ff["saldo"] == 59500.0, ff)
    check("A10 INVARIANTE: Σ brutos de la venta == total de la venta (119.000)",
          abs(_brutos_de(v.cot) - 119000.0) < 0.01, _brutos_de(v.cot))
    lin = [i for i in ff["items"] if i["anticipo_factura_id"] == fant_id]
    check("A11 la línea de descuento PERSISTE apuntando a la factura de anticipo",
          len(lin) == 1 and lin[0]["total_neto"] == -50000.0
          and lin[0]["np"] == "DESCUENTO", ff["items"])
    check("A11 la línea de descuento NO consume cantidad física "
          "(sin item_cotizacion_id → no rompe el tope físico)",
          lin and lin[0]["item_cotizacion_id"] is None, lin)
    d = client.get(f"{BASE}/ventas/{v.cot}").json()
    check("A11 venta 100% facturada: mercadería pendiente 0 y nada por descontar",
          d["resumen"]["mercaderia_pendiente_clp"] == 0
          and d["resumen"]["anticipo_por_descontar_clp"] == 0
          and d["resumen"]["por_facturar_clp"] == 0, d["resumen"])

    # ── 12/13/14) Borrados y reversión DERIVADA ───────────────────────────────
    r = client.delete(f"{BASE}/facturas/{fant_id}")
    check("A14 borrar el anticipo CON cobranza → 409 (revierta las cobranzas primero)",
          r.status_code == 409 and "cobranza" in r.json().get("detail", "").lower(), r.text)
    cob_id = _fac(fant_id)["cobranzas"][0]["id"]
    r = client.delete(f"{BASE}/facturas/{fant_id}/cobranzas/{cob_id}")
    check("A14 revertir la cobranza del adelanto → 200", r.status_code == 200, r.text)
    r = client.delete(f"{BASE}/facturas/{fant_id}")
    check("A12 borrar una factura de anticipo YA DESCONTADA → 409",
          r.status_code == 409 and "descontada" in r.json().get("detail", "").lower(), r.text)
    r = client.delete(f"{BASE}/facturas/{ffin_id}")
    check("A13 borrar la factura final → 200", r.status_code == 200, r.text)
    p = _preview(cotizacion_id=v.cot, despacho_id=v.desps[0])
    check("A13 el descuento vuelve a estar DISPONIBLE (derivado, sin código de reversión)",
          any(ln["numero_parte"] == "DESCUENTO" for ln in p["lineas"])
          and p["totales"]["neto"] == 50000.0, p["lineas"])
    r = client.delete(f"{BASE}/facturas/{fant_id}")
    check("A14 borrar el anticipo sin pagos ni descuentos → 200", r.status_code == 200, r.text)
    a = _adel(v.cot)
    d = client.get(f"{BASE}/ventas/{v.cot}").json()
    check("A14 el adelanto vuelve a la vía A: aplicado 0 y sin folio de respaldo",
          a and a["aplicado"] == 0 and a["monto"] == 59500.0
          and d["adelanto"]["factura_anticipo_folio"] is None, (a, d.get("adelanto")))


def _escenario_b(db):
    """Venta B — punto 15: anticipo por el TOTAL ⇒ la factura final queda en $0 con
    ADVERTENCIA (la vía manual NO bloquea) y nace 'pagada'."""
    print("\n═════ B · ANTICIPO POR EL TOTAL → FACTURA FINAL EN $0 ═════")
    v = _crear_venta(db, "CB", pct_adelanto=0)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 100_000})
    check("B15 anticipo por el total de la venta → 200", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    p = _preview(cotizacion_id=v.cot, despacho_id=v.desps[0])
    check("B15 preview de la final: $0 PERMITIDO con advertencia (no bloquea)",
          p["puede_emitir"] is True and p["totales"]["bruto"] == 0
          and any("$0" in x for x in p["advertencias"]), p)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-F0"})
    check("B15 emitir la final en $0 → 200", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    f0 = _fac(r.json()["id"])
    check("B15 la final en $0 nace 'pagada' (nada por cobrar) y sin IVA",
          f0["bruto"] == 0 and f0["iva"] == 0 and f0["estado"] == "pagada", f0)
    check("B15 INVARIANTE: Σ brutos == total de la venta (119.000)",
          abs(_brutos_de(v.cot) - 119000.0) < 0.01, _brutos_de(v.cot))


def _escenario_c(db):
    """Venta C — punto 17: un anticipo SIN folio del SII (emisión electrónica en vuelo o
    rechazada) BLOQUEA la factura del despacho: ni se descuenta ni se ignora."""
    print("\n═════ C · ANTICIPO SIN FOLIO SII BLOQUEA LA FACTURA DEL DESPACHO ═════")
    v = _crear_venta(db, "CC", pct_adelanto=0)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 50_000})
    check("C17 anticipo emitido → 200", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    fant_id = r.json()["id"]
    # Simula el estado real de una emisión electrónica en curso: la factura de anticipo
    # existe localmente pero el SII todavía no le dio folio.
    db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.id == fant_id).update({"numero_factura": None})
    db.commit()
    p = _preview(cotizacion_id=v.cot, despacho_id=v.desps[0])
    check("C17 preview: BLOQUEA por el anticipo sin folio del SII",
          p["puede_emitir"] is False
          and any("folio del SII" in x for x in p["problemas"]), p)
    check("C17 el bloqueo NO deja pasar el descuento a medias (sin líneas DESCUENTO)",
          not any(ln["numero_parte"] == "DESCUENTO" for ln in p["lineas"]), p["lineas"])
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-F4"})
    check("C17 emitir la factura del despacho → 409 (ni descuenta ni ignora)",
          r.status_code == 409 and "folio del SII" in r.json().get("detail", ""), r.text)
    check("C17 no nació ninguna factura del despacho (no hay zombi)",
          _sql("SELECT COUNT(*) FROM monza_cont_factura_cliente "
               "WHERE cotizacion_id=:c AND es_anticipo=0", c=v.cot)[0][0] == 0)


def _escenario_excedente(db, sufijo, via):
    """Ventas D/F — punto 16 (EXCEDENTE) con el ruteo del dinero bajo la lupa.

    La factura del DESPACHO REAL se emite ANTES que la de anticipo (id menor), así que
    con el orden viejo `id.asc()` la plata entraría primero a la del despacho: misma
    suma, mismo invariante, saldos CRUZADOS. Por eso se assertea el saldo de CADA
    factura por separado — es la única forma de ver la regresión.

    `via`: 'tesoreria' (POST /aprobaciones/{cot}/aprobar) o 'contabilidad'
    (POST /ventas/{cot}/adelanto/verificar), las dos gemelas declaradas "en sync"."""
    print(f"\n═════ {sufijo} · EXCEDENTE DEL ADELANTO (aprobación por {via}) ═════")
    v = _crear_venta(db, sufijo, guias=(5, 5))
    # 1ª factura REAL (5 u → bruto 59.500), emitida ANTES del anticipo.
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0],
        "numero_factura": f"{MARK}-{sufijo}F"})
    check(f"{sufijo}16 factura del despacho real (5 u) emitida ANTES del anticipo → 200",
          r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    freal_id = r.json()["id"]
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 50_000})
    check(f"{sufijo}16 anticipo de 50.000 neto emitido DESPUÉS → 200 (id mayor)",
          r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    fant_id = r.json()["id"]
    check(f"{sufijo}16 el anticipo tiene id MAYOR que la factura real "
          "(si no, el test no probaría el orden)", fant_id > freal_id, (freal_id, fant_id))
    # El cliente depositó 70.000: 59.500 son el anticipo y 10.500 el EXCEDENTE.
    if via == "tesoreria":
        r = client.post(f"{TES}/aprobaciones/{v.cot}/aprobar",
                        json={"monto": 70_000, "fecha_pago": "2026-07-11"})
        aplicado = r.json().get("aplicado_ahora_clp") if r.status_code == 200 else None
        check(f"{sufijo}16 Tesorería aprueba 70.000 y los aplica TODOS en una pasada",
              r.status_code == 200 and aplicado == 70000.0, r.text)
    else:
        r = client.post(f"{BASE}/ventas/{v.cot}/adelanto/verificar",
                        json={"monto": 70_000, "fecha_pago": "2026-07-11"})
        check(f"{sufijo}16 Contabilidad verifica 70.000 → 200", r.status_code == 200, r.text)
    fa, fr = _fac(fant_id), _fac(freal_id)
    # ⚠ RUTEO: los saldos POR FACTURA, no solo la suma.
    check(f"{sufijo}16 RUTEO: la factura de ANTICIPO se salda primero (saldo 0, pagada)",
          fa["saldo"] == 0 and fa["estado"] == "pagada"
          and any(c["medio"] == "adelanto" and c["monto"] == 59500.0 for c in fa["cobranzas"]),
          fa)
    check(f"{sufijo}16 RUTEO: el EXCEDENTE (10.500) va a la factura del despacho real "
          "(saldo 49.000)",
          fr["saldo"] == 49000.0
          and any(c["medio"] == "adelanto" and c["monto"] == 10500.0 for c in fr["cobranzas"]),
          fr)
    a = _adel(v.cot)
    cobs = _cobs_adelanto(v.cot)
    check(f"{sufijo}16 INVARIANTE: monto_aplicado == Σ cobranzas medio='adelanto' (70.000)",
          a and abs(a["aplicado"] - 70000.0) < 0.01
          and abs(sum(c["monto"] for c in cobs) - 70000.0) < 0.01, (a, cobs))
    check(f"{sufijo}16 INVARIANTE: Σ brutos == total de la venta (119.000)",
          abs(_brutos_de(v.cot) - 119000.0) < 0.01, _brutos_de(v.cot))


def _escenario_e(db):
    """Venta E — punto 16 en la otra dirección: el adelanto se aprueba con SOLO el
    anticipo emitido y el excedente lo recoge la factura del despacho, emitida después
    (la aplica crear_factura). Es el caso literal de la suite de Grupo AM."""
    print("\n═════ E · EXCEDENTE QUE ESPERA A LA FACTURA DEL DESPACHO ═════")
    v = _crear_venta(db, "CE")
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "es_anticipo": True, "numero_factura": _folio_ant(),
        "monto_neto_anticipo": 50_000})
    check("E16 anticipo de 50.000 neto emitido → 200", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    fant_id = r.json()["id"]
    r = client.post(f"{TES}/aprobaciones/{v.cot}/aprobar",
                    json={"monto": 70_000, "fecha_pago": "2026-07-12"})
    check("E16 aprobar 70.000 aplica SOLO el bruto del anticipo (59.500)",
          r.status_code == 200 and r.json().get("aplicado_ahora_clp") == 59500.0, r.text)
    r = client.post(f"{BASE}/facturas", json={
        "cotizacion_id": v.cot, "despacho_id": v.desps[0], "numero_factura": f"{MARK}-F5"})
    check("E16 factura del despacho real → 200", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    ff, fa = _fac(r.json()["id"]), _fac(fant_id)
    check("E16 RUTEO: el anticipo sigue pagado (saldo 0) y no recibe de más",
          fa["saldo"] == 0 and fa["suma_cob"] == 59500.0, fa)
    check("E16 RUTEO: el excedente (10.500) rebaja la final → saldo 49.000",
          ff["saldo"] == 49000.0
          and any(c["medio"] == "adelanto" and c["monto"] == 10500.0 for c in ff["cobranzas"]),
          ff)
    a = _adel(v.cot)
    cobs = _cobs_adelanto(v.cot)
    check("E16 el adelanto queda aplicado COMPLETO (70.000, pendiente 0)",
          a and abs(a["aplicado"] - a["monto"]) < 0.01 and a["aplicado"] == 70000.0, a)
    check("E16 INVARIANTE: monto_aplicado == Σ cobranzas medio='adelanto'",
          a and abs(a["aplicado"] - sum(c["monto"] for c in cobs)) < 0.01, (a, cobs))
    check("E16 INVARIANTE: Σ brutos == total de la venta (119.000)",
          abs(_brutos_de(v.cot) - 119000.0) < 0.01, _brutos_de(v.cot))


def _correr():
    del _fails[:]
    for k in _IDS:
        del _IDS[k][:]
    db = SessionLocal()
    try:
        _limpiar(db)
        _escenario_a(db)
        _escenario_b(db)
        _escenario_c(db)
        _escenario_excedente(db, "CD", "tesoreria")
        _escenario_excedente(db, "CF", "contabilidad")
        _escenario_e(db)
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


def test_factura_anticipo_monza():
    assert _correr(), f"{len(_fails)} checks fallaron: {_fails}"


if __name__ == "__main__":
    sys.exit(0 if _correr() else 1)
