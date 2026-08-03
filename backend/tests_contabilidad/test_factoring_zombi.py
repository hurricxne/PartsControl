"""SONDAS DE PODER DISCRIMINANTE — la factura ZOMBI del factoring, el criterio ÚNICO de
«documento vivo ante el SII» y la línea LIGADA a su guía.

Tres daños que la ronda anterior dejó abiertos, y que las sondas de esa ronda NO podían
ver porque su escenario era el CÓMODO (un DTE 'emitido' se sembraba con folio NULL y se
fijaba como correcto que la plata entrara; ningún escenario construía el legado ya cedido
al factor). Acá el escenario es el ADVERSO:

  A · «EMITIDA SIN FOLIO» (status 3 · folio NULL) — el estado CONTRADICTORIO: Wasabil dice
      que el SII lo aceptó y no tenemos el correlativo. Las CUATRO puertas de plata
      (cobranza manual, cesión al factor, liquidación y aplicación de adelantos) tienen que
      bloquear con UN solo criterio (_dte_emitido_ante_sii: status EMITIDO **y** folio),
      el mismo que usa el módulo que arma las referencias tributarias.
      Control anti sobre-bloqueo: con el folio puesto, las mismas tres operaciones entran.

  B · El LEGADO atrapado: factura sin folio, DTE rechazado y una cesión al factor
      registrada ANTES de los guards. Estaba cerrada por los tres lados (no liquidar, no
      editar a 0, no eliminar) con la plata del factor adentro y el cupo facturable
      secuestrado. Ahora tiene UNA salida explícita y auditada: revertir la cesión con
      motivo obligatorio. Controles: no destruye una cesión LEGÍTIMA (factura sí emitida),
      no se lleva un abono ya conciliado con el banco, y después de revertir la factura
      se puede eliminar de verdad (la plata sale y el cupo se libera).

  C · La línea LIGADA a SU guía: `despacho_item_id` es opcional, y sin él el tope por GUÍA
      no se aplicaba y la línea se persistía con NULL — la 52 del DTE 33 citaba una guía
      que no trasladó esa mercadería, el cinturón del reintento se quedaba ciego y el cupo
      de la guía no se descontaba. Se prueba con DOS guías de la misma parte (4 + 4).

Cero introspección de código: todo por HTTP contra el router real (o la función real de
plata), y los efectos se miran en la BD por DELTA de filas. NO se emite ni se toca ningún
documento tributario real: las filas `wasabil_dte` se siembran LOCALES (jamás se llama a
Wasabil ni al SII). Datos MARCADOS con __TEST_FZ__ y limpieza verificada por delta.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_factoring_zombi.py -q
(también:   ./venv/bin/python tests_contabilidad/test_factoring_zombi.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContFactoring, ContAdelanto,
)
from tesoreria.models import ConciliacionIngreso  # noqa: E402
from wasabil_dte.models import (  # noqa: E402
    WasabilDte, STATUS_EMITIDO, STATUS_FALLIDO,
)
import routers.contabilidad as cont  # noqa: E402

MARK = "__TEST_FZ__"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(cont.router, prefix="/api")


def _current_user_realista(db: Session = Depends(get_db)):
    """Auth REALISTA (molde de las otras suites de plata): hace una lectura en la MISMA
    sesión del request, como auth.get_current_user en producción. Ese SELECT abre la
    transacción ANTES de cualquier with_for_update(), que es la condición real."""
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _current_user_realista
client = TestClient(app)

PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    """Precios FIJOS (el motor real depende del dólar del día y haría flaky la suite)."""
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0)} for i in items}
    neto = sum(cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0)) for i in items)
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ─── Sembrado ─────────────────────────────────────────────────────────────────
def _crear_venta(db, sufijo, *, precio=10000.0, cantidad=10):
    """Venta de 1 ítem con UNA guía despachada y FIRMADA (neto 100.000 / bruto 119.000)."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} HEPI",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro", cantidad=cantidad, estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    estado="despachado", guia_firmada=1, numero_guia=f"G-{sufijo}")
    db.add(desp); db.flush()
    db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id, qty_despachada=cantidad))
    db.commit()
    PRECIOS.clear()
    PRECIOS.update({it.id: precio})
    return cot, oc, desp, it


def _crear_venta_dos_guias(db, sufijo, *, precio=10000.0):
    """Venta de 2 ítems y DOS guías firmadas: A lleva 4 del ítem 1, B lleva 4 del ítem 1
    y 5 del ítem 2. Es el escenario del daño: la misma parte salió en dos guías, y el
    ítem 2 NO salió en A."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} HEPI",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it1 = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                         descripcion="Filtro", cantidad=10, estado_item="en_bodega")
    it2 = ItemCotizacion(cotizacion_id=cot.id, item_num=2, numero_parte="6I-2503",
                         descripcion="Sello", cantidad=5, estado_item="en_bodega")
    db.add_all([it1, it2]); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    da = Despacho(numero_despacho=f"{MARK}-DSPA-{oc.id}", oc_cliente_id=oc.id,
                  estado="despachado", guia_firmada=1, numero_guia=f"G-{sufijo}-A")
    dbz = Despacho(numero_despacho=f"{MARK}-DSPB-{oc.id}", oc_cliente_id=oc.id,
                   estado="despachado", guia_firmada=1, numero_guia=f"G-{sufijo}-B")
    db.add_all([da, dbz]); db.flush()
    # Guía C: la MISMA parte PARTIDA en dos líneas de la misma guía (1 + 1) — el caso
    # AMBIGUO, donde adivinar de cuál de las dos salió sería inventar.
    dc = Despacho(numero_despacho=f"{MARK}-DSPC-{oc.id}", oc_cliente_id=oc.id,
                  estado="despachado", guia_firmada=1, numero_guia=f"G-{sufijo}-C")
    db.add(dc); db.flush()
    dia = DespachoItem(despacho_id=da.id, item_cotizacion_id=it1.id, qty_despachada=4)
    dib = DespachoItem(despacho_id=dbz.id, item_cotizacion_id=it1.id, qty_despachada=4)
    dib2 = DespachoItem(despacho_id=dbz.id, item_cotizacion_id=it2.id, qty_despachada=5)
    dic1 = DespachoItem(despacho_id=dc.id, item_cotizacion_id=it1.id, qty_despachada=1)
    dic2 = DespachoItem(despacho_id=dc.id, item_cotizacion_id=it1.id, qty_despachada=1)
    db.add_all([dia, dib, dib2, dic1, dic2]); db.commit()
    PRECIOS.clear()
    PRECIOS.update({it1.id: precio, it2.id: precio})
    return cot, oc, da, dbz, dc, it1, it2, dia.id, dib.id, dic1.id


def _dte_local(db, factura_id, status_id, folio=None):
    """Siembra/actualiza la fila del DTE 33 de la factura, SIN llamar a Wasabil ni al SII.
    `folio=None` es el estado ADVERSO cuando el status es EMITIDO: «emitida SIN folio»."""
    dte = db.query(WasabilDte).filter(WasabilDte.factura_id == factura_id).first()
    if not dte:
        dte = WasabilDte(tipo_dte=33, factura_id=factura_id, empresa="mineria")
        db.add(dte)
    dte.status_id = status_id
    dte.folio = folio
    dte.uuid = None
    dte.en_vuelo_desde = None  # fallo CONFIRMADO (no ambiguo): así el legado es borrable
    db.commit()
    return dte


def _sin_folio(db, factura_id):
    """Deja la factura como la deja la emisión electrónica: sin folio del SII."""
    f = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    f.numero_factura = None
    db.commit()
    return f


def _factoring_legado(db, factura_id, *, adelanto=100000.0, estado="vigente"):
    """El LEGADO exacto de producción: una cesión al factor registrada ANTES del guard SII
    (por eso se siembra a mano: hoy el endpoint la rechaza), con su cobranza de adelanto y
    la factura marcada 'factorizada'."""
    fac = ContFactoring(factura_id=factura_id, empresa_factoring=f"{MARK} Factor",
                        id_operacion=f"{MARK}-OP", fecha_operacion="2026-07-10",
                        monto_adelantado=adelanto, costo_factoring=0,
                        retencion=19000, banco="Santander", estado=estado)
    db.add(fac)
    cob = ContCobranza(factura_id=factura_id, fecha="2026-07-10", monto=adelanto,
                       medio=cont.MEDIO_FACT_ADELANTO, banco="Santander",
                       numero_operacion=f"{MARK}-OP",
                       observaciones=f"{MARK} adelanto factoring legado")
    db.add(cob)
    f = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    f.monto_pagado = adelanto
    f.saldo = round(cont._f(f.monto_bruto) - adelanto, 2)
    f.estado_pago = "factorizada"
    db.commit()
    return fac.id, cob.id


def _cobranzas(db, factura_id, medio=None):
    q = db.query(ContCobranza).filter(ContCobranza.factura_id == factura_id)
    if medio:
        q = q.filter(ContCobranza.medio == medio)
    return q.all()


def _n_factoring(db, factura_id):
    return db.query(ContFactoring).filter(ContFactoring.factura_id == factura_id).count()


def _lineas(db, factura_id):
    return (db.query(ContFacturaClienteItem)
            .filter(ContFacturaClienteItem.factura_id == factura_id).all())


def _conteos(db):
    """Foto de las tablas de plata, para verificar la limpieza por DELTA."""
    return {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("cont_factura_cliente", "cont_factura_cliente_item", "cont_cobranza",
                      "cont_factoring", "cont_adelanto", "wasabil_dte",
                      "conc_conciliacion_ingreso")}


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    oc_ids = [oc.id for oc in db.query(OcCliente)
              .filter(OcCliente.cotizacion_id.in_([c.id for c in cots])).all()] if cots else []
    if oc_ids:
        adel_ids = [a.id for a in db.query(ContAdelanto)
                    .filter(ContAdelanto.oc_cliente_id.in_(oc_ids)).all()]
        fac_ids = [f.id for f in db.query(ContFacturaCliente)
                   .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
        if adel_ids:
            db.query(ContAdelanto).filter(ContAdelanto.id.in_(adel_ids)).update(
                {"factura_anticipo_id": None}, synchronize_session=False)
        if fac_ids:
            cob_ids = [c.id for c in db.query(ContCobranza)
                       .filter(ContCobranza.factura_id.in_(fac_ids)).all()]
            if cob_ids:
                # El enlace de conciliación PRIMERO (su FK es CASCADE, pero la sonda B6
                # lo siembra a propósito y debe desaparecer con la limpieza)
                db.query(ConciliacionIngreso).filter(
                    ConciliacionIngreso.cobranza_id.in_(cob_ids)).delete(synchronize_session=False)
            db.query(WasabilDte).filter(
                WasabilDte.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFactoring).filter(
                ContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContCobranza).filter(
                ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFacturaClienteItem).filter(
                ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
        if adel_ids:
            db.query(ContAdelanto).filter(
                ContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(ContFacturaCliente).filter(
                ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
        if desp_ids:
            db.query(WasabilDte).filter(
                WasabilDte.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
    for cot in cots:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == cot.id).delete(synchronize_session=False)
    if cots:
        db.query(Cotizacion).filter(
            Cotizacion.id.in_([c.id for c in cots])).delete(synchronize_session=False)
    db.commit()


def _emitir(db, oc, desp, folio):
    """Factura de la guía + la deja en el estado de la emisión electrónica (sin folio)."""
    r = client.post("/api/contabilidad/facturas",
                    json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                          "numero_factura": folio})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    _sin_folio(db, fid)
    return fid


def run():
    cont._precios_de_cotizacion = _fake_precios
    db = SessionLocal()
    _limpiar(db)
    antes = _conteos(db)
    try:
        CURRENT["empresa"] = "mineria"

        # ═══ A · «EMITIDA SIN FOLIO»: UN solo criterio en las CUATRO puertas de plata ═══
        # Estado adverso REAL (el que la sonda anterior no construía): Wasabil responde
        # status 3 —el SII lo aceptó— pero sin correlativo. La factura no tiene folio en
        # NINGUNA parte: ni en numero_factura (se escribe desde ese folio) ni en el DTE.
        cot, oc, desp, it = _crear_venta(db, "A")
        fa = _emitir(db, oc, desp, f"{MARK}-FA")
        _dte_local(db, fa, STATUS_EMITIDO, folio=None)

        r = client.post(f"/api/contabilidad/facturas/{fa}/cobranzas",
                        json={"monto": 119000, "medio": "transferencia"})
        check("A1 · cobranza manual con el DTE 'emitido SIN folio' → 409 y 0 cobranzas",
              r.status_code == 409 and "SII" in r.json().get("detail", "")
              and len(_cobranzas(db, fa)) == 0,
              {"status": r.status_code, "body": r.text, "cobs": len(_cobranzas(db, fa))})

        # Venta APARTE para las otras dos puertas: si compartieran factura, el 200 de la
        # cobranza del mutante consumiría el cupo y el daño del factoring se vería como un
        # 400 de tope en vez del daño real (cesión aceptada). Cada puerta, su escenario.
        cot2, oc2, desp2, it2v = _crear_venta(db, "A2")
        fa2 = _emitir(db, oc2, desp2, f"{MARK}-FA2")
        _dte_local(db, fa2, STATUS_EMITIDO, folio=None)

        r = client.post(f"/api/contabilidad/facturas/{fa2}/factoring",
                        json={"monto_adelantado": 100000, "empresa_factoring": f"{MARK} F",
                              "id_operacion": "OP-FZ-A", "fecha_operacion": "2026-07-20"})
        f2_db = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fa2).first()
        db.refresh(f2_db)
        check("A2 · cesión al factor con el DTE 'emitido SIN folio' → 409, 0 factoring, "
              "0 cobranza del factor y la factura NO queda 'factorizada'",
              r.status_code == 409 and "SII" in r.json().get("detail", "")
              and _n_factoring(db, fa2) == 0
              and len(_cobranzas(db, fa2, cont.MEDIO_FACT_ADELANTO)) == 0
              and f2_db.estado_pago != "factorizada",
              {"status": r.status_code, "body": r.text, "fact": _n_factoring(db, fa2),
               "estado": f2_db.estado_pago})

        # 4ª puerta: la aplicación AUTOMÁTICA de adelantos (misma regla, sin 409: el
        # adelanto queda esperando el folio). Se llama la función REAL con la OC y la
        # factura bloqueadas, como en producción.
        adel = ContAdelanto(oc_cliente_id=oc2.id, empresa="mineria", estado="aprobado",
                            monto=50000, monto_aplicado=0, fecha_pago="2026-07-10",
                            banco="Santander", numero_operacion=f"{MARK}-OP-A")
        db.add(adel); db.commit()
        oc_row = db.query(OcCliente).filter(OcCliente.id == oc2.id).with_for_update().first()
        f_row = (db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fa2)
                 .populate_existing().with_for_update().first())
        aplicado = cont._aplicar_adelantos_pendientes(db, oc_row, f_row)
        db.rollback()
        check("A3 · el adelanto NO se aplica contra un 'emitido SIN folio' (misma puerta)",
              aplicado == 0.0 and len(_cobranzas(db, fa2, cont.MEDIO_ADELANTO)) == 0,
              {"aplicado": aplicado})

        # Control anti sobre-bloqueo: llega el folio (el «Reintentar» que adopta el
        # documento) y las MISMAS operaciones entran. Sin este control, un guard que
        # bloquee siempre se vería igual de verde arriba.
        _dte_local(db, fa, STATUS_EMITIDO, folio="999601001")
        r = client.post(f"/api/contabilidad/facturas/{fa}/cobranzas",
                        json={"monto": 19000, "medio": "transferencia"})
        check("A4 · con el folio del SII la misma cobranza entra (200)",
              r.status_code == 200 and len(_cobranzas(db, fa)) == 1, r.text)
        r = client.post(f"/api/contabilidad/facturas/{fa}/factoring",
                        json={"monto_adelantado": 100000, "empresa_factoring": f"{MARK} F",
                              "id_operacion": "OP-FZ-A2", "fecha_operacion": "2026-07-20"})
        check("A5 · con el folio del SII la misma cesión entra (200)",
              r.status_code == 200 and _n_factoring(db, fa) == 1, r.text)
        r = client.post(f"/api/contabilidad/facturas/{fa}/factoring/liquidar")
        check("A6 · …y la liquidación también (200, cierra en 0)",
              r.status_code == 200 and r.json()["saldo"] == 0, r.text)

        # …y la liquidación SÍ bloquea cuando el folio se pierde (legado de la 2ª puerta)
        cot, oc, desp, it = _crear_venta(db, "B")
        fb = _emitir(db, oc, desp, f"{MARK}-FB")
        _dte_local(db, fb, STATUS_EMITIDO, folio=None)
        _factoring_legado(db, fb)
        r = client.post(f"/api/contabilidad/facturas/{fb}/factoring/liquidar")
        fac_row = db.query(ContFactoring).filter(ContFactoring.factura_id == fb).first()
        db.refresh(fac_row)
        check("A7 · liquidar con el DTE 'emitido SIN folio' → 409, sin retención y el "
              "factoring sigue vigente",
              r.status_code == 409 and "SII" in r.json().get("detail", "")
              and len(_cobranzas(db, fb, cont.MEDIO_FACT_RETENCION)) == 0
              and fac_row.estado == "vigente",
              {"status": r.status_code, "estado": fac_row.estado, "body": r.text})
        _limpiar(db)

        # ═══ B · El LEGADO atrapado y su ÚNICA salida auditada ═══
        # Zombi tal como quedó en producción: factura sin folio, el SII RECHAZÓ el
        # documento (status 4, fallo confirmado) y la cesión al factor ya registrada.
        cot, oc, desp, it = _crear_venta(db, "C")
        fc = _emitir(db, oc, desp, f"{MARK}-FC")
        _dte_local(db, fc, STATUS_FALLIDO)
        fac_id, cob_id = _factoring_legado(db, fc)

        # B1 · la trampa: los tres lados cerrados (esto es el diagnóstico, no el arreglo)
        r_liq = client.post(f"/api/contabilidad/facturas/{fc}/factoring/liquidar")
        r_edit = client.post(f"/api/contabilidad/facturas/{fc}/factoring",
                             json={"monto_adelantado": 0, "empresa_factoring": f"{MARK} F",
                                   "id_operacion": f"{MARK}-OP"})
        r_del = client.delete(f"/api/contabilidad/facturas/{fc}")
        check("B1 · el legado está cerrado por los 3 lados: liquidar 409, editar a 0 409, "
              "eliminar 409",
              r_liq.status_code == 409 and r_edit.status_code == 409
              and r_del.status_code == 409,
              {"liquidar": r_liq.status_code, "editar": r_edit.status_code,
               "eliminar": r_del.status_code})
        check("B2 · los 409 NOMBRAN la salida (revertir la cesión), no son un callejón",
              "reviert" in r_liq.json().get("detail", "").lower()
              and "reviert" in r_del.json().get("detail", "").lower(),
              {"liquidar": r_liq.text, "eliminar": r_del.text})

        # B3 · sin motivo no se revierte NADA (la traza es lo único que queda del hecho)
        r = client.post(f"/api/contabilidad/facturas/{fc}/factoring/revertir", json={})
        check("B3 · revertir SIN motivo → rechazado y la cesión sigue entera",
              r.status_code in (400, 422) and _n_factoring(db, fc) == 1
              and len(_cobranzas(db, fc, cont.MEDIO_FACT_ADELANTO)) == 1,
              {"status": r.status_code, "body": r.text})
        r = client.post(f"/api/contabilidad/facturas/{fc}/factoring/revertir",
                        json={"motivo": "      "})
        check("B4 · motivo en blanco → rechazado y la cesión sigue entera",
              r.status_code in (400, 422) and _n_factoring(db, fc) == 1,
              {"status": r.status_code, "body": r.text})

        # B5 · la salida: reversión con motivo
        r = client.post(f"/api/contabilidad/facturas/{fc}/factoring/revertir",
                        json={"motivo": f"{MARK} el SII rechazó el DTE; el factor devolvió el anticipo"})
        body = r.json() if r.status_code == 200 else {}
        f_db = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fc).first()
        db.refresh(f_db)
        check("B5 · revertir con motivo → 200, 0 factoring, 0 cobranza del factor, la "
              "factura vuelve a 'por_cobrar' con el saldo completo",
              r.status_code == 200 and _n_factoring(db, fc) == 0
              and len(_cobranzas(db, fc, cont.MEDIO_FACT_ADELANTO)) == 0
              and body.get("factoring") is None
              and body.get("estado_pago") == "por_cobrar"
              and body.get("saldo") == 119000.0 and body.get("monto_pagado") == 0.0
              and f_db.estado_pago == "por_cobrar" and cont._f(f_db.saldo) == 119000.0,
              {"status": r.status_code, "body": r.text,
               "estado_bd": f_db.estado_pago, "saldo_bd": cont._f(f_db.saldo)})
        check("B6 · la reversión deja TRAZA auditable en la factura (motivo + monto + "
              "operación) y devuelve el detalle de lo revertido",
              (MARK in (f_db.observaciones or "")
               and "REVERTIDO" in (f_db.observaciones or "")
               and "el factor devolvió el anticipo" in (f_db.observaciones or "")
               and body.get("factoring_revertido", {}).get("monto_adelantado") == 100000.0
               and body.get("factoring_revertido", {}).get("id_operacion") == f"{MARK}-OP"),
              {"observaciones": f_db.observaciones,
               "traza": body.get("factoring_revertido")})

        # B7 · y RECIÉN AHORA la factura se puede eliminar: el cupo facturable se libera
        r = client.delete(f"/api/contabilidad/facturas/{fc}")
        existe = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fc).count()
        check("B7 · después de revertir, la factura zombi SÍ se elimina (cupo liberado)",
              r.status_code == 200 and existe == 0, {"status": r.status_code, "body": r.text})
        # …y el cupo de verdad volvió: la misma guía se puede facturar otra vez
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FC2"})
        check("B8 · la mercadería vuelve a ser facturable (200), que era lo secuestrado",
              r.status_code == 200 and r.json()["monto_bruto"] == 119000.0, r.text)
        _limpiar(db)

        # B9 · CONTROL: la puerta es la INVERSA del guard, no una puerta de atrás.
        # Factura SÍ emitida ante el SII (folio) con una cesión LEGÍTIMA: revertir tiene
        # que rechazarse y no destruir nada. Un endpoint que borre factorings reales sería
        # peor que el zombi.
        cot, oc, desp, it = _crear_venta(db, "D")
        fd = _emitir(db, oc, desp, f"{MARK}-FD")
        _dte_local(db, fd, STATUS_EMITIDO, folio="999601002")
        r = client.post(f"/api/contabilidad/facturas/{fd}/factoring",
                        json={"monto_adelantado": 100000, "empresa_factoring": f"{MARK} F",
                              "id_operacion": "OP-FZ-D", "fecha_operacion": "2026-07-20"})
        check("B9a · cesión legítima registrada (base del control)",
              r.status_code == 200 and _n_factoring(db, fd) == 1, r.text)
        r = client.post(f"/api/contabilidad/facturas/{fd}/factoring/revertir",
                        json={"motivo": f"{MARK} intento de borrar una cesión real"})
        f_db = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fd).first()
        db.refresh(f_db)
        check("B9b · revertir una cesión de factura EMITIDA → 409 y NADA se destruye "
              "(factoring, cobranza del factor y observaciones intactas)",
              r.status_code == 409 and "SII" in r.json().get("detail", "")
              and _n_factoring(db, fd) == 1
              and len(_cobranzas(db, fd, cont.MEDIO_FACT_ADELANTO)) == 1
              and MARK not in (f_db.observaciones or ""),
              {"status": r.status_code, "body": r.text, "fact": _n_factoring(db, fd),
               "obs": f_db.observaciones})
        r = client.post(f"/api/contabilidad/facturas/{fd}/factoring/revertir",
                        json={"motivo": f"{MARK} sin factoring"})
        _limpiar(db)

        # B10 · CONTROL: no se lleva un abono ya CONCILIADO con la cartola (Tesorería)
        cot, oc, desp, it = _crear_venta(db, "E")
        fe = _emitir(db, oc, desp, f"{MARK}-FE")
        _dte_local(db, fe, STATUS_FALLIDO)
        fac_id, cob_id = _factoring_legado(db, fe)
        db.add(ConciliacionIngreso(empresa="mineria", cobranza_id=cob_id,
                                   monto_conciliado_clp=100000))
        db.commit()
        r = client.post(f"/api/contabilidad/facturas/{fe}/factoring/revertir",
                        json={"motivo": f"{MARK} intento con el abono conciliado"})
        n_conc = (db.query(ConciliacionIngreso)
                  .filter(ConciliacionIngreso.cobranza_id == cob_id).count())
        check("B10 · abono del factor CONCILIADO con el banco → 409, y el cruce bancario "
              "no se lo lleva el CASCADE",
              r.status_code == 409 and "Tesorer" in r.json().get("detail", "")
              and _n_factoring(db, fe) == 1 and n_conc == 1,
              {"status": r.status_code, "body": r.text, "conciliaciones": n_conc})
        # Desconciliado en Tesorería, la reversión procede (el 409 no es un muro)
        db.query(ConciliacionIngreso).filter(
            ConciliacionIngreso.cobranza_id == cob_id).delete(synchronize_session=False)
        db.commit()
        r = client.post(f"/api/contabilidad/facturas/{fe}/factoring/revertir",
                        json={"motivo": f"{MARK} ya desconciliado en Tesoreria"})
        check("B11 · desconciliado en Tesorería, la reversión procede (200)",
              r.status_code == 200 and _n_factoring(db, fe) == 0, r.text)
        _limpiar(db)

        # B12 · sin factoring registrado no hay nada que revertir (404, no un 500)
        cot, oc, desp, it = _crear_venta(db, "F")
        ff = _emitir(db, oc, desp, f"{MARK}-FF")
        _dte_local(db, ff, STATUS_FALLIDO)
        r = client.post(f"/api/contabilidad/facturas/{ff}/factoring/revertir",
                        json={"motivo": f"{MARK} no hay factoring"})
        check("B12 · factura sin factoring → 404 explícito", r.status_code == 404, r.text)
        _limpiar(db)

        # ═══ C · La línea facturada BAJO una guía queda LIGADA a esa guía ═══
        cot, oc, da, dbz, dc, it1, it2, dia_id, dib_id, dic1_id = _crear_venta_dos_guias(db, "G")

        # C1 · el daño: 8 unidades bajo la guía A, que sólo trasladó 4 (las otras 4
        # salieron en B). Antes pasaba: el tope por guía sólo se aplicaba «si se indicó
        # despacho_item_id», y el payload puede omitirlo.
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": da.id,
                              "numero_factura": f"{MARK}-G1",
                              "items": [{"item_cotizacion_id": it1.id, "cantidad": 8}]})
        n_fac = db.query(ContFacturaCliente).filter(
            ContFacturaCliente.oc_cliente_id == oc.id).count()
        check("C1 · 8 unidades bajo una guía que trasladó 4 → 409 y NO se persiste "
              "(la 52 citaría una guía que no llevó esa mercadería)",
              r.status_code == 409 and n_fac == 0,
              {"status": r.status_code, "body": r.text, "facturas": n_fac})

        # C2 · lo legítimo pasa Y la línea queda LIGADA al ítem de despacho de ESA guía
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": da.id,
                              "numero_factura": f"{MARK}-G2",
                              "items": [{"item_cotizacion_id": it1.id, "cantidad": 4}]})
        check("C2a · las 4 de la guía A se facturan (200)", r.status_code == 200, r.text)
        f_g2 = r.json()["id"] if r.status_code == 200 else None
        lns = _lineas(db, f_g2) if f_g2 else []
        check("C2b · la línea se PERSISTE con el despacho_item_id real de la guía A "
              "(sin eso, el cinturón del reintento de wasabil_dte queda ciego)",
              len(lns) == 1 and lns[0].despacho_item_id == dia_id,
              {"lineas": [(x.id, x.despacho_item_id) for x in lns], "esperado": dia_id})

        # C3 · el cupo de la guía se DESCUENTA (las líneas con NULL no se descontaban:
        # la misma guía se podía facturar dos veces mientras la otra quedaba sin facturar)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": da.id,
                              "numero_factura": f"{MARK}-G3",
                              "items": [{"item_cotizacion_id": it1.id, "cantidad": 4}]})
        check("C3 · segunda factura de la MISMA guía ya facturada → 409 (su cupo se "
              "descontó de verdad)",
              r.status_code == 409, {"status": r.status_code, "body": r.text})

        # C4 · una parte que NO salió en la guía declarada no se factura bajo ella
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": da.id,
                              "numero_factura": f"{MARK}-G4",
                              "items": [{"item_cotizacion_id": it2.id, "cantidad": 5}]})
        check("C4 · ítem que salió SÓLO en la guía B, declarado bajo A → 409",
              r.status_code == 409 and "guía" in r.json().get("detail", "").lower(),
              {"status": r.status_code, "body": r.text})

        # C5 · CONTROL anti sobre-bloqueo: la guía correcta sí factura, y también ligada
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": dbz.id,
                              "numero_factura": f"{MARK}-G5",
                              "items": [{"item_cotizacion_id": it1.id, "cantidad": 4}]})
        check("C5a · las 4 de la guía B se facturan bajo la guía B (200)",
              r.status_code == 200, r.text)
        f_g5 = r.json()["id"] if r.status_code == 200 else None
        lns = _lineas(db, f_g5) if f_g5 else []
        check("C5b · …y su línea queda ligada al ítem de despacho de la guía B",
              len(lns) == 1 and lns[0].despacho_item_id == dib_id,
              {"lineas": [(x.id, x.despacho_item_id) for x in lns], "esperado": dib_id})

        # C6 · AMBIGUO: la misma parte partida en dos líneas de la MISMA guía. No se
        # adivina de cuál salió: se pide el despacho_item_id explícito (fail closed)…
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": dc.id,
                              "numero_factura": f"{MARK}-G6",
                              "items": [{"item_cotizacion_id": it1.id, "cantidad": 2}]})
        check("C6a · parte en 2 líneas de la misma guía, sin declarar cuál → 409",
              r.status_code == 409
              and "despacho_item_id" in r.json().get("detail", ""),
              {"status": r.status_code, "body": r.text})
        # …y declarándolo, factura la cantidad de ESA línea (1) y queda ligada a ella
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": dc.id,
                              "numero_factura": f"{MARK}-G6",
                              "items": [{"item_cotizacion_id": it1.id,
                                         "despacho_item_id": dic1_id, "cantidad": 1}]})
        f_g6 = r.json()["id"] if r.status_code == 200 else None
        lns = _lineas(db, f_g6) if f_g6 else []
        check("C6b · declarando el despacho_item_id, la línea ambigua sí se factura (200)",
              r.status_code == 200 and len(lns) == 1
              and lns[0].despacho_item_id == dic1_id,
              {"status": r.status_code, "body": r.text,
               "lineas": [(x.id, x.despacho_item_id) for x in lns]})

    finally:
        _limpiar(db)
        despues = _conteos(db)
        check("limpieza · las tablas de plata quedan como estaban (delta 0)",
              antes == despues, {"antes": antes, "despues": despues})
        db.close()
        cont._precios_de_cotizacion = _orig_precios
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_factoring_zombi():
    run()


if __name__ == "__main__":
    run()
