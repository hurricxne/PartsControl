"""Los cuatro caminos por los que la factura 33 REAL podía salir citando una guía 52 que
el SII no reconoce — reproducidos de punta a punta y cerrados.

Todos terminan en el mismo daño IRREVERSIBLE (Grupo AM es la marca que ya emite de
verdad): un DTE 33 vivo con una referencia 52 falsa, que solo se arregla con nota de
crédito. La regla del módulo es "ante la duda, BLOQUEAR y pedir intervención humana",
nunca "seguir adelante y esperar que salga bien".

  1 · CALLEJÓN 'EMITIDA SIN FOLIO **Y SIN UUID**'. La variante con uuid ya estaba cubierta
      (test_aud_folio.py), pero basta una respuesta `{"status_id": 3}` pelada al POST
      /documents para reproducir el callejón COMPLETO: el rescate por uuid no aplica, el
      sondeo no re-consultaba (exigía uuid), /reintentar respondía 409 'ya está emitida
      (folio None)' y el N° manual no se puede editar (guard anti-pisado de despachos.py).
      Con el guard viejo (`bool(dte.uuid) or claim_vigente`) el estado NO bloqueaba, así
      que la 33 salía referenciando el N° de guía TECLEADO a mano. Ahora: el guard decide
      por "status 3 y folio vacío" SIN mirar el uuid, el rescate y el sondeo van por la
      REFERENCIA INTERNA (N° de despacho / FACT-<id>) y /reintentar es un rescate de solo
      lectura que JAMÁS re-crea (si no puede resolver: 409 con el remedio humano).

  2 · EL RESCATE NO PUEDE DEGRADAR EL UUID. `{**data, **completo}` conserva las claves
      ausentes pero no las presentes-con-null: un documento completo `{"uuid": None, ...}`
      BORRABA el uuid del POST y creaba el callejón del punto 1 — el rescate empeoraba el
      resultado. Ahora la fusión nunca pisa un dato útil con un vacío.

  3 · LA GUÍA AMBIGUA (POST enviado, respuesta perdida, claim VENCIDO por TTL) bloquea la
      33. Se llega con un timeout común: `_emitir_en_wasabil` conserva `en_vuelo_desde` a
      propósito porque el documento PUDO nacer con folio real. Decidir por TTL dejaba
      `puede_emitir=True` con la referencia 52 = N° tecleado a mano. Es el mismo
      `incluir_ambiguo` con el que routers/despachos.py protege el anular, y por este
      agujero ya se emitió una segunda guía real en el pasado.

  4 · LA 52 NO PUEDE CITAR LA GUÍA EQUIVOCADA. `_construir_factura` valida el
      `despacho_item_id` contra TODOS los despacho_items de la OC, pero la 52 se arma
      desde `payload.despacho_id`: unidades salidas en la guía B, facturadas declarando la
      guía A, salían con la 52 de A. Se bloquea en el preview (antes de persistir) y en el
      armado desde la factura congelada (camino del reintento).

Wasabil SIMULADO por monkeypatch de wasabil_dte.client — JAMÁS el API real (los documentos
vivos de Grupo AM son las guías 136/137 y la factura 116, y nada de acá los toca: todo
sale con issue=False o contra el fake). Datos MARCADOS, limpieza por MARK **y** por padre,
con verificación de deltas al final.

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_aud_callejon_w1.py -q
(también:   ./venv/bin/python wasabil_dte/tests/test_aud_callejon_w1.py)
"""
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from auth import get_current_user  # noqa: E402
from database import SessionLocal, engine, Base  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContAdelanto,
)
import routers.contabilidad as cont  # noqa: E402
from wasabil_dte import client as wasabil_client  # noqa: E402
from wasabil_dte.models import (  # noqa: E402
    WasabilDte, CLAIM_TTL_SEGUNDOS, STATUS_EMITIDO, STATUS_FALLIDO, STATUS_PENDIENTE,
)
from wasabil_dte.router import router as wasabil_router  # noqa: E402
import wasabil_dte.router as wr  # noqa: E402
from wasabil_dte.service import TIPO_DOC_FACTURA, TIPO_DOC_GUIA, claim_vigente  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_W1SU__"
# Fecha de EMISIÓN de la guía EN PAPEL. DISTINTA de la fecha de cierre del despacho a
# propósito: la referencia 52 de la factura cita ESTA, y si el código volviera a usar
# `fecha_despacho` el valor delataría cuál está leyendo.
FECHA_GUIA_PAPEL = date(2026, 6, 18)

CURRENT = {"empresa": "mineria", "id": None}

app = FastAPI()
app.include_router(wasabil_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

GUIAS = "/api/wasabil/despachos"
FACTURAS = "/api/wasabil/facturas"
# Claim ya vencido por TTL (el estado ambiguo que el TTL dejaba pasar)
VENCIDO = timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)

# N° de guía TECLEADO A MANO por el operador. Son NUMÉRICOS a propósito: el folio de una
# guía en papel es un correlativo autorizado por el SII, así que un N° numérico es
# perfectamente legítimo como referencia 52 — y por eso el único freno de estos escenarios
# tiene que ser el GUARD del estado de la guía electrónica. Con los valores viejos
# ("G-TECLEADO-A-MANO", "G-B") la validación de folio numérico de armar_referencias_factura
# bloqueaba sola y los checks del guard perdían poder discriminante (quedaban verdes con el
# guard muerto). El daño que se prueba es el mismo: un N° que el SII no reconoce COMO GUÍA
# ELECTRÓNICA de este despacho viajando en un DTE 33 real.
N_TECLEADO = "999001"
N_AMBIGUA = "550055"
N_GUIA_B = "800002"

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


# ─── Wasabil SIMULADO ───────────────────────────────────────────────────────────
class FakeWasabil:
    """Todo configurable, porque el hallazgo vive en las FORMAS de la respuesta:

      crear_respuesta   → lo que devuelve el POST /documents (por defecto: status 3
                          PELADO, sin uuid y sin folio — el callejón del punto 1)
      documento         → lo que devuelve obtener_documento(uuid)
      docs_buscables    → lo que devuelve buscar_documentos(search)
      buscar_falla      → si está, buscar_documentos LANZA (el API real da 405)
    """

    def __init__(self):
        self.configurado = True
        self.cliente = {"id": 158381, "rut": "78.279.030-7",
                        "name": "H-E PARTS INTERNATIONAL CHILE SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS", "default": True}],
                        "addresses": [{"address": "RUTA 26 KM 15 S/N",
                                       "comuna": "Antofagasta", "city": "Antofagasta",
                                       "default": True}]}
        self.crear_respuesta = {"status_id": STATUS_EMITIDO}   # ni uuid ni folio
        self.documento = None            # obtener_documento(uuid)
        self.docs_buscables: list = []
        self.buscar_falla = None
        # Si está, buscar_documentos RESPONDE a cualquier referencia con un documento
        # emitido con este folio (para ejercitar el rescate sin conocer el id de antemano)
        self.responder_ref_folio = None
        self.creados: list = []          # cada POST que habría salido al SII

    def install(self):
        wasabil_client.esta_configurado = lambda: self.configurado
        wasabil_client.buscar_cliente_por_rut = lambda rut: (
            wasabil_client._normalizar_cliente(self.cliente) if self.cliente and
            wasabil_client._normalizar_rut(rut) == wasabil_client._normalizar_rut(self.cliente["rut"])
            else None)
        wasabil_client.crear_documento = self._crear
        wasabil_client.estado_documento = self._estado
        wasabil_client.obtener_documento = self._obtener
        wasabil_client.buscar_documentos = self._buscar

    def _crear(self, payload):
        self.creados.append(payload)
        return dict(self.crear_respuesta)

    def _estado(self, uuid):
        doc = dict(self.documento or {})
        return {"uuid": doc.get("uuid", uuid), "status_id": doc.get("status_id", STATUS_EMITIDO)}

    def _obtener(self, uuid):
        return dict(self.documento or {"uuid": uuid, "status_id": STATUS_EMITIDO})

    def _buscar(self, search):
        if self.buscar_falla:
            raise self.buscar_falla
        if self.responder_ref_folio:
            return [{"uuid": f"uuid-ref-{self.responder_ref_folio}",
                     "status_id": STATUS_EMITIDO, "invoice_reference": search,
                     "folio": self.responder_ref_folio}], True
        return [dict(d) for d in self.docs_buscables], True


fake = FakeWasabil()

# Precios deterministas (el pricing tiene sus propios tests): neto 10.000/u
PRECIOS: dict = {}


def _precio_de(item_id) -> float:
    return float((PRECIOS.get(item_id) or {}).get("precio_venta_clp") or 0.0)


def _fake_precios_cont(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": _precio_de(i.id),
                   "total_venta_clp": _precio_de(i.id) * float(i.cantidad or 0)}
            for i in items}
    neto = sum(_precio_de(i.id) * float(i.cantidad or 0) for i in items)
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


# ─── Datos MARCADOS ─────────────────────────────────────────────────────────────
def _crear_venta(db, *, sufijo, estado_despacho="en_preparacion",
                 guia_manual=N_TECLEADO, cantidad=10, precio=10000.0):
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} HEPI",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro de aceite motor", cantidad=cantidad,
                        estado_item="despachado")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"W1SU-{sufijo}", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{sufijo}", oc_cliente_id=oc.id,
                    # Guía EN PAPEL: siempre trae fecha de emisión, y la referencia 52 de la
                    # factura la exige (sin ella se bloquea). Se pone SÓLO cuando hay N° manual
                    # y con una fecha DISTINTA del cierre, para que ninguna suite las confunda.
                    estado=estado_despacho, guia_firmada=1, numero_guia=guia_manual,
                    fecha_guia=(FECHA_GUIA_PAPEL if guia_manual else None),
                    contacto_destinatario="Juan Pérez", fecha_despacho=datetime.now())
    db.add(desp); db.flush()
    di = DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id, qty_despachada=cantidad)
    db.add(di)
    db.commit()
    PRECIOS[it.id] = {"precio_venta_clp": precio}
    return cot, oc, desp, it, di


def _agregar_despacho(db, oc, it, *, sufijo, qty, guia_manual):
    """Segunda guía de la MISMA venta (el escenario del punto 4)."""
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{sufijo}", oc_cliente_id=oc.id,
                    estado="despachado", guia_firmada=1, numero_guia=guia_manual,
                    fecha_guia=(FECHA_GUIA_PAPEL if guia_manual else None),
                    contacto_destinatario="Juan Pérez", fecha_despacho=datetime.now())
    db.add(desp); db.flush()
    di = DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id, qty_despachada=qty)
    db.add(di)
    db.commit()
    return desp, di


def _dte_guia(db, desp_id):
    db.rollback()  # sesión propia del test: descartar su snapshot viejo
    return (db.query(WasabilDte)
            .filter(WasabilDte.despacho_id == desp_id,
                    WasabilDte.tipo_dte == TIPO_DOC_GUIA).first())


def _facturas_de(db, oc_id):
    db.rollback()
    return (db.query(ContFacturaCliente)
            .filter(ContFacturaCliente.oc_cliente_id == oc_id)
            .order_by(ContFacturaCliente.id.asc()).all())


def _ref52(preview) -> list:
    return [x["folio"] for x in preview.get("referencias", []) if x["tipo"] == "52"]


def _cerrar(db, desp_id):
    """Cierra el despacho (la guía se emite EN PREPARACIÓN y el cierre lo pasa a
    'despachado', el único estado facturable)."""
    db.rollback()
    d = db.get(Despacho, desp_id)
    d.estado = "despachado"
    db.commit()


def _limpiar(db):
    """Por MARK **y** por padre: una corrida interrumpida no puede dejar la suite roja
    para siempre (es el modo de falla que envenenó otras suites del repo)."""
    db.rollback()
    db.execute(text("DELETE FROM wasabil_dte WHERE despacho_id IN "
                    "(SELECT id FROM despachos WHERE numero_despacho LIKE :m)"), {"m": f"{MARK}%"})
    db.commit()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    oc_ids = [o.id for o in db.query(OcCliente).filter(
        OcCliente.cotizacion_id.in_(cot_ids)).all()] if cot_ids else []
    if oc_ids:
        fac_ids = [f.id for f in db.query(ContFacturaCliente).filter(
            ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
        if fac_ids:
            db.query(WasabilDte).filter(WasabilDte.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContCobranza).filter(ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFacturaClienteItem).filter(ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
        db.query(ContAdelanto).filter(ContAdelanto.oc_cliente_id.in_(oc_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(ContFacturaCliente).filter(ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
    desp_ids = [d.id for d in db.query(Despacho).filter(
        Despacho.numero_despacho.like(f"{MARK}%")).all()]
    if oc_ids:
        desp_ids += [d.id for d in db.query(Despacho).filter(
            Despacho.oc_cliente_id.in_(oc_ids)).all()]
    if desp_ids:
        db.query(WasabilDte).filter(WasabilDte.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(DespachoItem).filter(DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
    if oc_ids:
        db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
    for c in cots:
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == c.id).delete(synchronize_session=False)
    if cot_ids:
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.commit()
    PRECIOS.clear()


def _verificar_limpieza():
    """Sesión NUEVA: deltas a cero (ni una fila marcada sobrevive)."""
    db2 = SessionLocal()
    try:
        n = db2.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
        n += db2.query(Despacho).filter(Despacho.numero_despacho.like(f"{MARK}%")).count()
        check("limpieza: 0 filas marcadas al final", n == 0, n)
    finally:
        db2.close()


# ═══════════════════════════════════════════════════════════════════════════════
def run():
    db = SessionLocal()
    fake.install()   # re-instalar acá: la última instalación de la sesión gana
    orig_precios_cont = cont._precios_de_cotizacion
    orig_precios_router = wr._precios
    cont._precios_de_cotizacion = _fake_precios_cont
    wr._precios = lambda db_, cot_: PRECIOS
    _limpiar(db)
    try:
        CURRENT["empresa"] = "mineria"

        # ═══ 1 · CALLEJÓN 'EMITIDA SIN FOLIO Y SIN UUID' ═══════════════════════════
        # El POST responde status 3 pelado y el listado de Wasabil da 405 (el API real):
        # no hay uuid que consultar ni referencia que rescatar → la fila queda atascada.
        fake.crear_respuesta = {"status_id": STATUS_EMITIDO}
        fake.documento = None
        fake.docs_buscables = []
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="A")
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("1 emitir 200 (el documento SÍ salió: Wasabil lo dio por emitido)",
              r.status_code == 200, r.text)
        check("1 se envió UN solo documento", len(fake.creados) == creados_antes + 1,
              len(fake.creados))
        fila = _dte_guia(db, desp.id)
        check("1 la fila reproduce el callejón: status 3 · folio NULL · uuid NULL",
              fila is not None and fila.status_id == STATUS_EMITIDO
              and fila.folio is None and fila.uuid is None,
              fila and (fila.status_id, fila.folio, fila.uuid))
        check("1 el claim quedó liberado (el TTL no protege nada acá)",
              fila is not None and claim_vigente(fila) is False, fila and fila.en_vuelo_desde)
        check("1 el despacho conserva el N° TECLEADO a mano (no hay folio que lo pise)",
              db.get(Despacho, desp.id).numero_guia == N_TECLEADO,
              db.get(Despacho, desp.id).numero_guia)

        # ── EL DAÑO: la 33 no puede salir citando ese N° tecleado ──
        check("1 GUARD: 'emitida sin folio' SIN uuid cuenta como guía en proceso",
              wr._guia_electronica_en_proceso(db, desp.id) is True)
        _cerrar(db, desp.id)
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("1 preview 33: BLOQUEA y explica el motivo",
              p["puede_emitir"] is False and any("EN PROCESO" in x for x in p["problemas"]),
              p["problemas"])
        check("1 preview 33: NO arma una referencia 52 con el N° tecleado a mano",
              _ref52(p) == [], p.get("referencias"))
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("1 emitir 33: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("1 emitir 33: tampoco quedó factura local zombi",
              len(_facturas_de(db, oc.id)) == 0, _facturas_de(db, oc.id))

        # ── SALIDAS: el sondeo y el reintento existen y NUNCA re-crean ──
        creados_antes = len(fake.creados)
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        check("1 sondeo con el listado caído: 200 informando el error, sin re-emitir",
              r.status_code == 200 and r.json().get("error_consulta")
              and len(fake.creados) == creados_antes, r.text)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("1 reintentar sin poder verificar: 502 y NADA sale al SII",
              r.status_code == 502 and len(fake.creados) == creados_antes, r.text)

        # El listado responde, pero el documento no aparece: 'no lo encontré' NO habilita
        # re-emitir (Wasabil ya lo dio por emitido) → fail closed con remedio humano.
        fake.buscar_falla = None
        fake.docs_buscables = []
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("1 reintentar con el documento no encontrado: 409, JAMÁS una segunda guía",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("1 el 409 pide intervención humana y dice que no se re-emite",
              "no se re-emite" in r.json().get("detail", "").lower(), r.text)

        # Wasabil ya asignó el folio: el sondeo lo rescata por la REFERENCIA INTERNA
        # (N° de despacho), sin uuid y sin re-emitir nada.
        fake.docs_buscables = [{"uuid": "uuid-rescatado", "status_id": STATUS_EMITIDO,
                                "invoice_reference": f"{MARK}-DSP-A", "folio": "52123"}]
        fake.documento = {"uuid": "uuid-rescatado", "status_id": STATUS_EMITIDO,
                          "folio": "52123",
                          "document_pdf_url": "https://api.wasabil.com/pdf/52123"}
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        check("1 el SONDEO rescata el folio por la referencia interna (sin uuid)",
              r.status_code == 200 and r.json().get("folio") == "52123", r.text)
        check("1 el rescate NO re-emite (0 documentos nuevos)",
              len(fake.creados) == creados_antes, len(fake.creados))
        db.rollback()
        check("1 el folio rescatado pisa el N° tecleado en despacho.numero_guia",
              db.get(Despacho, desp.id).numero_guia == "52123",
              db.get(Despacho, desp.id).numero_guia)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("1 con el folio rescatado la 33 se destraba y cita el FOLIO REAL",
              p["puede_emitir"] is True and _ref52(p) == ["52123"],
              (p["problemas"], p.get("referencias")))
        _limpiar(db)

        # ── 1-bis · si el listado SÍ responde, la EMISIÓN MISMA rescata el folio por la
        #    referencia interna: el operador nunca ve el callejón ──────────────────────
        fake.docs_buscables = []
        fake.buscar_falla = None
        fake.responder_ref_folio = "52456"
        fake.documento = None
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="F")
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("1-bis la emisión rescata folio Y uuid por la referencia, en el acto",
              r.status_code == 200 and r.json().get("folio") == "52456"
              and r.json().get("uuid") == "uuid-ref-52456", r.text)
        check("1-bis un solo documento enviado (el rescate es de solo lectura)",
              len(fake.creados) == creados_antes + 1, len(fake.creados))
        db.rollback()
        check("1-bis el folio pisa el N° tecleado en la misma emisión",
              db.get(Despacho, desp.id).numero_guia == "52456",
              db.get(Despacho, desp.id).numero_guia)
        fake.responder_ref_folio = None
        _limpiar(db)

        # ═══ 2 · EL RESCATE NO DEGRADA EL UUID ═════════════════════════════════════
        # El POST trae uuid; el documento completo responde con uuid PRESENTE-EN-NULL.
        # Con el merge plano ese null borraba el uuid y creaba el callejón del punto 1.
        fake.crear_respuesta = {"uuid": "uuid-w2", "status_id": STATUS_EMITIDO}
        fake.documento = {"uuid": None, "status_id": STATUS_EMITIDO, "folio": None}
        fake.docs_buscables = []
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)  # sin red de rescate
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="B")
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("2 emitir 200 aunque el documento completo vino con uuid null",
              r.status_code == 200, r.text)
        fila = _dte_guia(db, desp.id)
        check("2 el uuid del POST SOBREVIVE al rescate (no lo pisa el null)",
              fila is not None and fila.uuid == "uuid-w2", fila and fila.uuid)
        # Y por eso el sondeo puede curar: Wasabil ya asignó el folio.
        fake.documento = {"uuid": None, "status_id": STATUS_EMITIDO, "folio": "52222"}
        creados_antes = len(fake.creados)
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        check("2 con el uuid intacto el sondeo rescata el folio",
              r.status_code == 200 and r.json().get("folio") == "52222", r.text)
        check("2 el sondeo NO re-emite", len(fake.creados) == creados_antes, len(fake.creados))
        fila = _dte_guia(db, desp.id)
        check("2 el uuid sigue en la fila después del sondeo",
              fila is not None and fila.uuid == "uuid-w2", fila and fila.uuid)
        _limpiar(db)

        # ═══ 3 · LA GUÍA AMBIGUA BLOQUEA LA 33 ═════════════════════════════════════
        # Estado que deja un timeout: status 6 · uuid NULL · en_vuelo_desde puesto. A los
        # 180 s el claim vence y antes de esto el guard lo daba por "no hay guía".
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="C", estado_despacho="despachado",
                                                guia_manual=N_AMBIGUA)
        ambigua = WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp.id,
                             status_id=STATUS_PENDIENTE, uuid=None,
                             en_vuelo_desde=datetime.utcnow() - VENCIDO,
                             error="timeout simulado: no se pudo confirmar")
        db.add(ambigua); db.commit()
        check("3 el claim de la emisión ambigua YA venció por TTL",
              claim_vigente(ambigua) is False, ambigua.en_vuelo_desde)
        check("3 GUARD: la guía ambigua cuenta como en proceso (el doc PUDO existir)",
              wr._guia_electronica_en_proceso(db, desp.id) is True)
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3 preview 33: BLOQUEA y dice que no se pudo CONFIRMAR",
              p["puede_emitir"] is False
              and any("CONFIRMAR" in x for x in p["problemas"]), p["problemas"])
        check("3 preview 33: NO cita el N° de guía tecleado a mano",
              _ref52(p) == [], p.get("referencias"))
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("3 emitir 33 sobre guía ambigua: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("3 emitir 33 sobre guía ambigua: sin factura local zombi",
              len(_facturas_de(db, oc.id)) == 0, _facturas_de(db, oc.id))

        # CONTROL anti sobre-bloqueo (a): fallo CONFIRMADO (sin en_vuelo_desde) → el N°
        # manual es legítimo, porque ahí NO nació ningún documento.
        db.rollback()
        fila = _dte_guia(db, desp.id)
        fila.en_vuelo_desde = None
        db.commit()
        check("3a control: fallo confirmado sin claim NO bloquea",
              wr._guia_electronica_en_proceso(db, desp.id) is False)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3a control: vuelve a usar el N° manual y puede emitir",
              p["puede_emitir"] is True and _ref52(p) == [N_AMBIGUA],
              (p["problemas"], p.get("referencias")))

        # CONTROL anti sobre-bloqueo (b): el SII la RECHAZÓ de verdad → no existe guía
        # electrónica y el despacho no queda preso.
        # CORREGIDO EN ESTA RONDA (el check anterior fijaba un BUG como correcto): la forma
        # de un rechazo CONFIRMADO es `status 4 CON uuid` y sin claim — el SII contestó
        # sobre un documento concreto. El estado que ponía la versión anterior
        # (`status 4 · uuid NULL · en_vuelo puesto`) NO es un rechazo confirmado: es una
        # emisión AMBIGUA cuyo status 4 puede venir de una respuesta que contradice una
        # emisión real, y routers/despachos.py la considera guía VIVA (no deja anular).
        # Ese estado ahora BLOQUEA (ver 3c).
        #
        # CORREGIDO OTRA VEZ (ronda del cinturón cerrado): este control AFIRMA SU
        # PRECONDICIÓN. Antes corría con `buscar_falla` puesto desde la sección 2 (el 405
        # del API real), o sea que probaba "con el listado CAÍDO se cita el N° tecleado a
        # mano" — que es justo el agujero MEDIO-5. El control legítimo (un rechazo
        # confirmado no deja preso al despacho) es con el listado SANO y sin emitidos; el
        # caso peligroso está en 3b bis y 3b ter.
        db.rollback()
        fila = _dte_guia(db, desp.id)
        fila.status_id = STATUS_FALLIDO
        fila.uuid = "uuid-rechazo-confirmado"
        fila.en_vuelo_desde = None
        fila.error = "SII: rechazado (folio del receptor inválido)"
        db.commit()
        ref_desp = db.get(Despacho, desp.id).numero_despacho
        fake.buscar_falla = None          # PRECONDICIÓN del control: el listado responde…
        fake.docs_buscables = []          # …y consta que NO hay documentos emitidos
        fake.responder_ref_folio = None
        check("3b PRECONDICIÓN: el listado responde y no hay ningún emitido",
              wasabil_client.buscar_documentos(ref_desp) == ([], True),
              wasabil_client.buscar_documentos(ref_desp))
        check("3b control: rechazo CONFIRMADO (status 4 CON uuid) no bloquea",
              wr._guia_electronica_en_proceso(db, desp.id) is False,
              wr._guia_no_referenciable(db, desp.id))
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3b control: con el rechazo del SII la 33 sale con el N° manual",
              p["puede_emitir"] is True and _ref52(p) == [N_AMBIGUA],
              (p["problemas"], p.get("referencias")))

        # 3b bis · EL CASO PELIGROSO QUE EL CONTROL VIEJO TAPABA: mismo estado (rechazo
        # confirmado CON uuid) pero el listado NO permite concluir (405 del API real). No
        # se puede afirmar que no exista una 52 EMITIDA con esta referencia, así que citar
        # el N° tecleado a mano en un DTE 33 REAL es exactamente el daño. FALLA CERRADO.
        creados_antes = len(fake.creados)
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3b bis listado caído: la 33 NO cita el N° tecleado a mano",
              p["puede_emitir"] is False and _ref52(p) == [],
              (p["problemas"], p.get("referencias")))
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("3b bis emitir: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("3b bis sin factura local zombi",
              len(_facturas_de(db, oc.id)) == 0, _facturas_de(db, oc.id))
        # …y la salida humana AUDITADA destraba (repitiendo el N° de despacho EXACTO)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id},
                        params={"verificado_sin_guia_electronica": ref_desp}).json()
        check("3b bis con la verificación humana EXACTA vuelve a poder facturarse",
              p["puede_emitir"] is True and _ref52(p) == [N_AMBIGUA],
              (p["problemas"], p.get("referencias")))
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id},
                        params={"verificado_sin_guia_electronica": "true"}).json()
        check("3b bis una declaración que no es la referencia exacta NO destraba",
              p["puede_emitir"] is False, (p["problemas"], p.get("referencias")))

        # 3b ter · el listado SÍ responde y tiene una 52 EMITIDA con esta referencia: el
        # folio REAL existe y la 33 iba a citar el del papel. Bloqueo DURO (ninguna
        # declaración humana lo levanta: acá no hay duda, hay evidencia).
        fake.buscar_falla = None
        fake.docs_buscables = [{"uuid": "uuid-otra-52", "status_id": STATUS_EMITIDO,
                                "invoice_reference": ref_desp, "folio": "52777"}]
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3b ter con una 52 EMITIDA real: bloquea y nombra el folio verdadero",
              p["puede_emitir"] is False and _ref52(p) == []
              and any("52777" in x for x in p["problemas"]), p["problemas"])
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id},
                        params={"verificado_sin_guia_electronica": ref_desp}).json()
        check("3b ter la declaración humana NO levanta un bloqueo con evidencia",
              p["puede_emitir"] is False and _ref52(p) == [], p["problemas"])
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id},
                        params={"verificado_sin_guia_electronica": ref_desp})
        check("3b ter emitir: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        fake.docs_buscables = []
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)

        # 3c · el estado que el 3b viejo daba por inofensivo: status 4 · uuid NULL ·
        # en_vuelo puesto (claim vencido). Es AMBIGUO y tiene que bloquear.
        db.rollback()
        fila = _dte_guia(db, desp.id)
        fila.status_id = STATUS_FALLIDO
        fila.uuid = None
        fila.en_vuelo_desde = datetime.utcnow() - VENCIDO
        db.commit()
        check("3c 'status 4 · sin uuid · en vuelo' BLOQUEA (es ambiguo, no un rechazo)",
              wr._guia_electronica_en_proceso(db, desp.id) is True,
              wr._guia_no_referenciable(db, desp.id))
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3c preview 33: no cita el N° manual con la guía ambigua",
              p["puede_emitir"] is False and _ref52(p) == [],
              (p["problemas"], p.get("referencias")))
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("3c emitir 33: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        _limpiar(db)

        # ═══ 4 · LA 52 NO PUEDE CITAR LA GUÍA EQUIVOCADA ═══════════════════════════
        # Misma venta, dos guías FIRMADAS: A (folio 52111) y B (folio 52222). Se factura
        # declarando A pero con las unidades que salieron en B.
        fake.crear_respuesta = {"uuid": "uuid-f33", "status_id": STATUS_FALLIDO}
        fake.documento = {"uuid": "uuid-f33", "status_id": STATUS_FALLIDO}
        fake.buscar_falla = None
        fake.docs_buscables = []
        _cot, oc, desp_a, it, di_a = _crear_venta(db, sufijo="D",
                                                  estado_despacho="despachado",
                                                  guia_manual="800001", cantidad=4)
        desp_b, di_b = _agregar_despacho(db, oc, it, sufijo="E", qty=4, guia_manual=N_GUIA_B)
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp_a.id,
                          uuid="uuid-guia-a", status_id=STATUS_EMITIDO, folio="52111",
                          payload_json='{"documentDate": "2026-07-10"}'))
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp_b.id,
                          uuid="uuid-guia-b", status_id=STATUS_EMITIDO, folio="52222",
                          payload_json='{"documentDate": "2026-07-12"}'))
        db.commit()
        cruzado = {"oc_cliente_id": oc.id, "despacho_id": desp_a.id,
                   "items": [{"item_cotizacion_id": it.id, "despacho_item_id": di_b.id,
                              "cantidad": 4}]}
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview", json=cruzado).json()
        check("4 preview: líneas de la guía B declarando la guía A → BLOQUEA",
              p["puede_emitir"] is False
              and any("OTRA guía" in x for x in p["problemas"]), p["problemas"])
        # El preview igual muestra lo que SALDRÍA (la 52 de A) — por eso lo que importa es
        # que bloquee nombrando el ítem culpable, para que el operador sepa qué corregir.
        check("4 preview: el problema nombra el ítem de despacho ajeno",
              any(str(di_b.id) in x and "OTRA guía" in x for x in p["problemas"]),
              (di_b.id, p["problemas"]))
        r = client.post(f"{FACTURAS}/emitir", json=cruzado)
        check("4 emitir cruzado: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("4 emitir cruzado: sin factura local zombi",
              len(_facturas_de(db, oc.id)) == 0, _facturas_de(db, oc.id))

        # CONTROL: la MISMA cantidad, declarada en SU guía, sí puede y cita su folio
        correcto = {"oc_cliente_id": oc.id, "despacho_id": desp_a.id,
                    "items": [{"item_cotizacion_id": it.id, "despacho_item_id": di_a.id,
                               "cantidad": 4}]}
        p = client.post(f"{FACTURAS}/preview", json=correcto).json()
        check("4 control: la línea de la guía A puede emitir y cita el folio 52111",
              p["puede_emitir"] is True and _ref52(p) == ["52111"],
              (p["problemas"], p.get("referencias")))

        # CINTURÓN del REINTENTO: la factura ya persistida se arma de nuevo, así que el
        # cruce tiene que frenar también ahí (el SII rechazó la 1ª emisión → reintentable).
        r = client.post(f"{FACTURAS}/emitir", json=correcto)
        check("4 reintento: la 1ª emisión queda rechazada por el SII (reintentable)",
              r.status_code == 200 and r.json()["estado"] == "fallido"
              and r.json()["puede_reintentar"] is True, r.text)
        fac_id = r.json()["factura_id"]
        db.rollback()
        db.query(ContFacturaClienteItem).filter(
            ContFacturaClienteItem.factura_id == fac_id,
            ContFacturaClienteItem.despacho_item_id == di_a.id
        ).update({ContFacturaClienteItem.despacho_item_id: di_b.id},
                 synchronize_session=False)
        db.commit()
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("4 reintento con una línea de OTRA guía: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("4 el 409 del reintento nombra el motivo real (otra guía)",
              "OTRA guía" in r.json().get("detail", ""), r.text)

        # CINTURÓN del tipo de DTE: una fila 33 con despacho_id JAMÁS puede prestar su
        # folio como referencia 52 (el buscador de la guía filtra por tipo_dte 52).
        db.rollback()
        db.query(WasabilDte).filter(WasabilDte.despacho_id == desp_b.id).delete(
            synchronize_session=False)
        db.commit()
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_FACTURA, despacho_id=desp_b.id,
                          uuid="uuid-33-intrusa", status_id=STATUS_EMITIDO, folio="9099"))
        db.commit()
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp_b.id}).json()
        check("4 cinturón: el folio de una fila 33 no se cuela como referencia 52",
              _ref52(p) == [N_GUIA_B], (p["problemas"], p.get("referencias")))
        _limpiar(db)

        # ═══ 5 · EL MISMO CALLEJÓN EN LA FACTURA 33 (sin uuid) ═════════════════════
        # Ahí el daño es otro: la factura local se queda sin `numero_factura` PARA SIEMPRE
        # y los adelantos diferidos nunca se aplican (la plata no entra al libro).
        fake.crear_respuesta = {"status_id": STATUS_EMITIDO}   # status 3 pelado
        fake.documento = None
        fake.docs_buscables = []
        fake.responder_ref_folio = None
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="G", estado_despacho="despachado",
                                                guia_manual="800009")
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp.id,
                          uuid="uuid-guia-g", status_id=STATUS_EMITIDO, folio="52999",
                          payload_json='{"documentDate": "2026-07-14"}'))
        db.commit()
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("5 factura 33: emitir 200 (el documento salió) con status 3 pelado",
              r.status_code == 200, r.text)
        fac_id = r.json().get("factura_id")
        f0 = [f for f in _facturas_de(db, oc.id) if f.id == fac_id][0]
        check("5 factura 33: reproduce el callejón (sin folio local, sin uuid)",
              not (f0.numero_factura or "").strip() and r.json().get("uuid") is None,
              (f0.numero_factura, r.json()))
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("5 factura 33: sin poder verificar → 502 y NADA sale al SII",
              r.status_code == 502 and len(fake.creados) == creados_antes + 1, r.text)
        # El listado responde pero el documento no aparece: tampoco habilita re-emitir
        fake.buscar_falla = None
        fake.docs_buscables = []
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("5 factura 33: documento no encontrado → 409, JAMÁS una segunda factura",
              r.status_code == 409 and "no se re-emite" in r.json()["detail"].lower()
              and len(fake.creados) == creados_antes + 1, r.text)
        check("5 factura 33: el 409 habla de la FACTURA, no del despacho",
              "factura" in r.json()["detail"].lower()
              and "despacho" not in r.json()["detail"].lower(), r.text)
        # Wasabil ya asignó el folio → el sondeo lo rescata por FACT-<id>, sin uuid
        fake.responder_ref_folio = "F7001"
        r = client.get(f"{FACTURAS}/{fac_id}/estado")
        check("5 factura 33: el SONDEO rescata el folio por FACT-<id>",
              r.status_code == 200 and r.json().get("folio") == "F7001", r.text)
        check("5 factura 33: el rescate NO re-emite",
              len(fake.creados) == creados_antes + 1, len(fake.creados))
        f0 = [f for f in _facturas_de(db, oc.id) if f.id == fac_id][0]
        check("5 factura 33: el folio rescatado se escribe en numero_factura",
              (f0.numero_factura or "") == "F7001", f0.numero_factura)
        fake.responder_ref_folio = None
        _limpiar(db)

    finally:
        cont._precios_de_cotizacion = orig_precios_cont
        wr._precios = orig_precios_router
        fake.crear_respuesta = {"status_id": STATUS_EMITIDO}
        fake.documento = None
        fake.docs_buscables = []
        fake.buscar_falla = None
        _limpiar(db)
        db.close()
        _verificar_limpieza()

    print()
    if _fails:
        print(f"FALLARON {len(_fails)}: {_fails}")
    assert not _fails, f"{len(_fails)} fallos: {_fails}"
    print("=== CALLEJÓN SIN UUID / RESCATE / AMBIGUA / GUÍA EQUIVOCADA: TODO OK ===")


def test_wasabil_callejon_sin_uuid_y_guia_correcta(): run()


if __name__ == "__main__":
    run()
