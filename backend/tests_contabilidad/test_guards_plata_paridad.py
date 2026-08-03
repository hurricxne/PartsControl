"""SONDAS DE PODER DISCRIMINANTE de los guards de plata de la paridad Monza ↔ MachParts.

Un guard sin sonda es un guard muerto: el próximo refactor lo borra y el gate sigue verde.
Estos checks ejercitan el COMPORTAMIENTO (HTTP contra el router real, o la función real de
plata) — nunca leen el código fuente ni cuentan strings, porque un tripwire de texto se
burla inyectando el bug por otra vía y se pone rojo con cualquier refactor inocente.

Guards cubiertos (quitar cualquiera de ellos del router pone ROJO su check):

  1. SII · cobranza manual   — registrar_cobranza sobre factura electrónica sin folio
  2. SII · factoring         — set_factoring: la plata que ENTREGA el factor
  3. SII · liquidación       — liquidar_factoring: la retención que libera el factor
  4. Folio NUMÉRICO del anticipo (la factura de la mercadería lo cita en referencia 33)
  5. BOLETA con anticipo pendiente de descuento (una boleta no referencia folios)
  6. 2° anticipo sólo con confirmación EXPLÍCITA — y que el flag SÍ desbloquea el legítimo
  7. Regla de lecturas de plata: la lectura BAJO LOCK de los adelantos trae valores
     FRESCOS aunque la fila ya esté en el identity map de SQLAlchemy

Cada guard se prueba en los DOS sentidos: bloquea el caso peligroso (y no deja rastro —
se verifica por DELTA de filas) y NO bloquea el legítimo de al lado (un guard demasiado
ancho es tan malo como uno ausente).

Alcance honesto del check 7: NO reclama la carrera "el mismo depósito aplicado dos veces"
(con el engine en READ COMMITTED y el candado de la OC tomado por los cuatro escritores no
se pudo construir). Prueba la regla de la casa de docs/regla-lecturas-de-plata.md, que es
lo que el arreglo garantiza: la lectura que DECIDE plata no puede servir un valor viejo del
identity map. Ese camino sí es alcanzable hoy — cualquier serialización previa del adelanto
en el mismo request lo mete al identity map.

NO emite ni toca documentos tributarios reales: la fila `wasabil_dte` se siembra LOCAL
(nunca se llama a Wasabil ni al SII). Datos MARCADOS con __TEST_GPP__ y limpieza verificada.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_guards_plata_paridad.py -q
(también:   ./venv/bin/python tests_contabilidad/test_guards_plata_paridad.py)
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
from wasabil_dte.models import (  # noqa: E402
    WasabilDte, STATUS_PROCESANDO, STATUS_EMITIDO, STATUS_FALLIDO,
)
import routers.contabilidad as cont  # noqa: E402

MARK = "__TEST_GPP__"

# Folios de ANTICIPO: NUMÉRICOS obligatorios (el guard 4 es justamente eso) y en un rango
# altísimo que nunca colisiona con los folios reales (van en los cientos).
FOLIO_ANT_OK = "999500021"
FOLIO_ANT_1 = "999500031"
FOLIO_ANT_2 = "999500032"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(cont.router, prefix="/api")


# Auth REALISTA (mismo molde que las otras suites de plata): además de devolver el usuario
# hace una lectura en la MISMA sesión del request, igual que auth.get_current_user en
# producción. Ese SELECT abre la transacción ANTES de cualquier with_for_update(), que es la
# condición real bajo la que corren los endpoints.
def _current_user_realista(db: Session = Depends(get_db)):
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


def _crear_venta(db, sufijo, *, precio=10000.0, cantidad=10):
    """Venta de 1 ítem con guía despachada y FIRMADA (neto 100.000 / bruto 119.000)."""
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


def _dte_local(db, factura_id, status_id, folio=None):
    """Siembra/actualiza la fila del DTE 33 de la factura, SIN llamar a Wasabil ni al SII.
    Es la ventana real de la emisión electrónica: la factura nace sin folio y la fila queda
    en 'procesando' (2) hasta que el SII responde 'emitido' (3) o 'fallido' (4).

    CORRECCIÓN 2026 (re-auditoría MEDIO-4): el FOLIO va de la mano del status. Un
    'emitido' (3) SIN folio no es un documento identificado ante el SII —es un estado
    contradictorio— y la plata sigue bloqueada (criterio único `_dte_emitido_ante_sii`).
    Esta sonda dejaba `folio = NULL` en los tres casos 'emitido', así que fijaba como
    CORRECTO justo el bug: cobranza 200 y factoring 200 contra una factura sin folio en
    ninguna parte. El caso 'emitido sin folio' se prueba a propósito —y bloqueando— en
    tests_contabilidad/test_factoring_zombi.py."""
    dte = db.query(WasabilDte).filter(WasabilDte.factura_id == factura_id).first()
    if not dte:
        dte = WasabilDte(tipo_dte=33, factura_id=factura_id, empresa="mineria")
        db.add(dte)
    dte.status_id = status_id
    dte.folio = folio if folio is not None else (
        f"9994{factura_id}" if status_id == STATUS_EMITIDO else None)
    db.commit()
    return dte


def _sin_folio(db, factura_id):
    """Deja la factura como la deja la emisión electrónica en vuelo: sin folio del SII."""
    f = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    f.numero_factura = None
    db.commit()
    return f


def _cobranzas(db, factura_id, medio=None):
    q = db.query(ContCobranza).filter(ContCobranza.factura_id == factura_id)
    if medio:
        q = q.filter(ContCobranza.medio == medio)
    return q.all()


def _conteos(db):
    """Foto de las tablas de plata, para verificar la limpieza por DELTA."""
    return {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("cont_factura_cliente", "cont_factura_cliente_item", "cont_cobranza",
                      "cont_factoring", "cont_adelanto", "wasabil_dte")}


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
            # Desliga los adelantos de sus facturas de anticipo antes de borrar facturas
            db.query(ContAdelanto).filter(ContAdelanto.id.in_(adel_ids)).update(
                {"factura_anticipo_id": None}, synchronize_session=False)
        if fac_ids:
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


def run():
    cont._precios_de_cotizacion = _fake_precios
    db = SessionLocal()
    _limpiar(db)
    antes = _conteos(db)
    try:
        CURRENT["empresa"] = "mineria"

        # ═══ 1 · Guard SII de la COBRANZA MANUAL (H2) ═══
        # La factura vive la ventana de la emisión electrónica: sin folio y con su DTE
        # esperando respuesta del SII. Nada de plata hasta que llegue el folio.
        cot, oc, desp, it = _crear_venta(db, "A")
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FA"})
        check("1a · factura del despacho emitida (base de la sonda)", r.status_code == 200, r.text)
        fa = r.json()["id"]
        _sin_folio(db, fa)
        _dte_local(db, fa, STATUS_PROCESANDO)

        r = client.post(f"/api/contabilidad/facturas/{fa}/cobranzas",
                        json={"monto": 119000, "medio": "transferencia"})
        check("1b · cobranza manual con DTE EN VUELO → 409 (guard SII)",
              r.status_code == 409 and "SII" in r.json().get("detail", ""), r.text)
        check("1c · …y no dejó rastro: 0 cobranzas en la factura",
              len(_cobranzas(db, fa)) == 0, _cobranzas(db, fa))

        # El SII confirma (status 3): la MISMA cobranza pasa. Sin este check, un guard que
        # bloquee siempre (o un `return True` de paso) también se vería verde arriba.
        _dte_local(db, fa, STATUS_EMITIDO)
        r = client.post(f"/api/contabilidad/facturas/{fa}/cobranzas",
                        json={"monto": 119000, "medio": "transferencia"})
        check("1d · con el DTE EMITIDO la misma cobranza entra (200)",
              r.status_code == 200 and r.json()["saldo"] == 0, r.text)

        # ═══ 2 · Guard SII del FACTORING (el crítico: misma plata, función de al lado) ═══
        # Peor caso: el SII RECHAZÓ el documento (status 4). Ceder eso al factor mete plata
        # real contra un documento que no existe y deja la factura zombi IMBORRABLE.
        cot, oc, desp, it = _crear_venta(db, "B")
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FB"})
        check("2a · factura del despacho emitida (base de la sonda)", r.status_code == 200, r.text)
        fb = r.json()["id"]
        _sin_folio(db, fb)
        _dte_local(db, fb, STATUS_FALLIDO)

        r = client.post(f"/api/contabilidad/facturas/{fb}/factoring",
                        json={"monto_adelantado": 100000, "empresa_factoring": f"{MARK} Factor",
                              "id_operacion": "OP-GPP", "fecha_operacion": "2026-07-20"})
        check("2b · ceder al factor una factura RECHAZADA por el SII → 409",
              r.status_code == 409 and "SII" in r.json().get("detail", ""), r.text)
        fac_rows = db.query(ContFactoring).filter(ContFactoring.factura_id == fb).all()
        f_db = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fb).first()
        db.refresh(f_db)
        check("2c · …y no dejó rastro: 0 factoring, 0 cobranza de adelanto del factor, "
              "la factura NO queda 'factorizada'",
              len(fac_rows) == 0
              and len(_cobranzas(db, fb, "factoring_adelanto")) == 0
              and f_db.estado_pago != "factorizada",
              {"factoring": len(fac_rows), "estado_pago": f_db.estado_pago})
        # …y la factura sigue BORRABLE (el zombi era: factorizada + imborrable para siempre)
        r = client.delete(f"/api/contabilidad/facturas/{fb}")
        check("2d · la factura no quedó secuestrada por un factoring fantasma "
              "(el borrado no responde «tiene factoring»)",
              r.status_code != 409 or "factoring" not in r.json().get("detail", "").lower(),
              r.text)

        # El SII confirma → la MISMA cesión se acepta (el guard no bloquea lo legítimo)
        cot, oc, desp, it = _crear_venta(db, "C")
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FC"})
        fc = r.json()["id"]
        _sin_folio(db, fc)
        _dte_local(db, fc, STATUS_EMITIDO)
        r = client.post(f"/api/contabilidad/facturas/{fc}/factoring",
                        json={"monto_adelantado": 100000, "empresa_factoring": f"{MARK} Factor",
                              "id_operacion": "OP-GPP2", "fecha_operacion": "2026-07-20"})
        check("2e · con el DTE EMITIDO la cesión al factor entra (200)",
              r.status_code == 200
              and len(_cobranzas(db, fc, "factoring_adelanto")) == 1, r.text)

        # ═══ 3 · Guard SII de la LIQUIDACIÓN del factoring ═══
        # Estado LEGADO: cesión registrada antes de este candado y el SII terminó
        # rechazando el documento. Liquidar cerraría la factura en 0 contra un fantasma.
        _dte_local(db, fc, STATUS_FALLIDO)
        r = client.post(f"/api/contabilidad/facturas/{fc}/factoring/liquidar")
        fac_row = db.query(ContFactoring).filter(ContFactoring.factura_id == fc).first()
        db.refresh(fac_row)
        check("3a · liquidar el factoring de una factura que el SII no conoce → 409, "
              "sin cobranza de retención y el factoring sigue vigente",
              r.status_code == 409 and "SII" in r.json().get("detail", "")
              and len(_cobranzas(db, fc, "factoring_retencion")) == 0
              and fac_row.estado == "vigente",
              {"status": r.status_code, "estado": fac_row.estado, "body": r.text})

        _dte_local(db, fc, STATUS_EMITIDO)
        r = client.post(f"/api/contabilidad/facturas/{fc}/factoring/liquidar")
        db.refresh(fac_row)
        check("3b · con el DTE EMITIDO la liquidación entra (200) y cierra en 0",
              r.status_code == 200 and r.json()["saldo"] == 0
              and fac_row.estado == "liquidada", r.text)

        # ═══ 4 · Folio NUMÉRICO de la factura de ANTICIPO ═══
        # El SII exige que el FolioRef de la referencia 33 sea el correlativo (un número):
        # un folio con letras se descubría semanas después, al facturar el despacho.
        cot, oc, desp, it = _crear_venta(db, "D")
        for folio_malo in ("N/A-99", "FAC 123", "0"):
            r = client.post("/api/contabilidad/facturas",
                            json={"oc_cliente_id": oc.id, "es_anticipo": True,
                                  "numero_factura": folio_malo, "monto_neto_anticipo": 20000})
            n_fac = db.query(ContFacturaCliente).filter(
                ContFacturaCliente.oc_cliente_id == oc.id).count()
            check(f"4a · folio de anticipo '{folio_malo}' → 400 y NO se persiste",
                  r.status_code == 400 and "folio" in r.json().get("detail", "").lower()
                  and n_fac == 0, r.text)

        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": FOLIO_ANT_OK, "monto_neto_anticipo": 20000})
        check("4b · con el folio correlativo NUMÉRICO el anticipo se emite (200)",
              r.status_code == 200 and r.json()["monto_bruto"] == 23800, r.text)
        f_ant = r.json()["id"]

        # ═══ 5 · BOLETA con anticipo pendiente de descuento ═══
        # El descuento del anticipo es una línea que CITA el folio de una factura; una
        # boleta no puede referenciar facturas, y ese descuento consumía el pendiente.
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "tipo_doc": "boleta"})
        n_fac = db.query(ContFacturaCliente).filter(
            ContFacturaCliente.oc_cliente_id == oc.id).count()
        check("5a · boleta de la mercadería con anticipo por descontar → 400 y NO persiste",
              r.status_code == 400 and "factura" in r.json().get("detail", "").lower()
              and n_fac == 1, r.text)

        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FD"})
        lineas_dsc = [ln for ln in r.json().get("items", [])
                      if (ln.get("numero_parte") or "") == "DESCUENTO"] if r.status_code == 200 else []
        check("5b · la MISMA mercadería como FACTURA sí se emite, con la línea de descuento",
              r.status_code == 200 and len(lineas_dsc) == 1
              and float(lineas_dsc[0]["total_neto"]) == -20000, r.text)

        # ═══ 6 · 2° anticipo sólo con confirmación EXPLÍCITA (y el flag desbloquea) ═══
        # En Grupo AM el anticipo parcial pactado es LEGÍTIMO: el guard no prohíbe, exige
        # decirlo a propósito (emitir un DTE 33 es irreversible). El cupo NO es el freno
        # acá: 23.800 + 23.800 = 47.600 de una venta de 119.000.
        cot, oc, desp, it = _crear_venta(db, "E")
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": FOLIO_ANT_1, "monto_neto_anticipo": 20000})
        check("6a · 1er anticipo emitido (base de la sonda)", r.status_code == 200, r.text)

        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": FOLIO_ANT_2, "monto_neto_anticipo": 20000})
        n_ant = db.query(ContFacturaCliente).filter(
            ContFacturaCliente.oc_cliente_id == oc.id,
            ContFacturaCliente.es_anticipo == 1).count()
        check("6b · 2° anticipo SIN confirmar → 409 (y sigue habiendo uno solo)",
              r.status_code == 409 and "anticipo" in r.json().get("detail", "").lower()
              and n_ant == 1, r.text)
        # El preview lo dice ANTES del clic (es lo que pinta la casilla del modal)
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "monto_neto_anticipo": 20000})
        p = r.json()
        check("6c · el preview avisa y bloquea el botón",
              p["puede_emitir"] is False
              and any("segundo anticipo" in x for x in p["problemas"]), p)
        # …y la casilla del modal (confirmar_segundo_anticipo) lo DESBLOQUEA
        r = client.post("/api/contabilidad/facturas/preview",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "monto_neto_anticipo": 20000,
                              "confirmar_segundo_anticipo": True})
        check("6d · con la casilla marcada el preview habilita el botón",
              r.json()["puede_emitir"] is True, r.text)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "numero_factura": FOLIO_ANT_2, "monto_neto_anticipo": 20000,
                              "confirmar_segundo_anticipo": True})
        n_ant = db.query(ContFacturaCliente).filter(
            ContFacturaCliente.oc_cliente_id == oc.id,
            ContFacturaCliente.es_anticipo == 1).count()
        check("6e · con la casilla marcada el 2° anticipo LEGÍTIMO se emite (200)",
              r.status_code == 200 and n_ant == 2, r.text)

        # ═══ 7 · Regla de lecturas de plata en _aplicar_adelantos_pendientes ═══
        # La función se llama con la OC bloqueada, igual que en producción. El adelanto ya
        # está en el identity map por una lectura PLANA previa del MISMO request (lo mete
        # cualquier serialización de adelantos), y mientras tanto su aplicación se commiteó
        # en otra transacción. Sin populate_existing, la lectura bajo lock devuelve el
        # objeto cacheado con monto_aplicado VIEJO y el mismo depósito se aplica DOS VECES.
        cot, oc, desp, it = _crear_venta(db, "F")
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FF"})
        check("7a · factura del despacho emitida (base de la sonda)", r.status_code == 200, r.text)
        ff = r.json()["id"]
        adel = ContAdelanto(oc_cliente_id=oc.id, empresa="mineria", estado="aprobado",
                            monto=50000, monto_aplicado=0, fecha_pago="2026-07-10",
                            banco="Santander", numero_operacion="OP-GPP7")
        db.add(adel); db.commit()
        adel_id = adel.id
        # (i) lectura PLANA: el adelanto entra al identity map con monto_aplicado = 0
        cont._adelantos_de_oc(db, oc.id)
        # (ii) otra transacción aplica ese depósito y COMMITEA (Tesorería, o la emisión
        #      electrónica al recibir el folio)
        db2 = SessionLocal()
        db2.add(ContCobranza(factura_id=ff, adelanto_id=adel_id, monto=50000,
                             medio=cont.MEDIO_ADELANTO, fecha="2026-07-10",
                             observaciones=f"{MARK} aplicado en paralelo"))
        db2.query(ContAdelanto).filter(ContAdelanto.id == adel_id).update(
            {"monto_aplicado": 50000}, synchronize_session=False)
        db2.commit(); db2.close()
        # (iii) la función real, con la OC y la factura bloqueadas como en producción
        oc_row = db.query(OcCliente).filter(OcCliente.id == oc.id).with_for_update().first()
        f_row = (db.query(ContFacturaCliente).filter(ContFacturaCliente.id == ff)
                 .populate_existing().with_for_update().first())
        aplicado = cont._aplicar_adelantos_pendientes(db, oc_row, f_row)
        check("7 · el adelanto YA aplicado no se aplica de nuevo (lectura de plata fresca)",
              aplicado == 0.0, {"aplicado": aplicado})
        db.rollback()
        cobs_adel = _cobranzas(db, ff, cont.MEDIO_ADELANTO)
        check("7b · invariante intacto: monto_aplicado == Σ cobranzas 'adelanto'",
              len(cobs_adel) == 1
              and float(db.query(ContAdelanto).filter(
                  ContAdelanto.id == adel_id).first().monto_aplicado) == 50000.0,
              [(c.id, float(c.monto)) for c in cobs_adel])

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


def test_guards_plata_paridad():
    run()


if __name__ == "__main__":
    run()
