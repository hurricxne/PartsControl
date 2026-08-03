"""Ante un documento tributario IRREVERSIBLE con estado remoto AMBIGUO o CONTRADICTORIO:
BLOQUEAR y pedir intervención humana. Nunca "recuperar con astucia", nunca fusionar el
estado remoto de forma que DEGRADE una emisión ya confirmada, nunca seguir esperando que
salga bien. Grupo AM es la marca que YA emite documentos reales (guías 136/137, factura
116): un 409 que obliga a un humano a mirar es infinitamente más barato que una nota de
crédito.

Esta suite existe porque la ronda anterior tenía un fake DEMASIADO AMABLE: respondía a
cualquier referencia con UN único documento emitido. El estado NORMAL después de un
reintento son DOS documentos con la MISMA referencia interna (`_reclamar_emision` reutiliza
la fila y el ancla), y con dos documentos el rescate se quedaba con el viejo RECHAZADO.

  S1 · RESCATE POR REFERENCIA CON DOS DOCUMENTOS. El rescate elegía "el primero que pase":
       con [rechazado, emitido] se quedaba con el rechazado, `_actualizar_desde_wasabil`
       escribía status 4 sin piso y el documento REAL perdía su folio para siempre. De ahí
       salían (a) la 33 citando el N° de guía TECLEADO a mano y (b) la doble emisión.
       Ahora gana SIEMPRE el EMITIDO (el único estado que trae folio), en cualquier orden.

  S2 · DOS DOCUMENTOS EMITIDOS con la misma referencia → NO se elige ninguno: se aborta,
       se anota qué folios hay y quién tiene que mirar, y nada nuevo sale al SII.

  S3 · DOBLE EMISIÓN DEL REINTENTO. Con el uuid del intento rechazado, `estado_documento`
       confirma "fallido" y se re-emitía aunque Wasabil ya tuviera un documento EMITIDO con
       la misma referencia. Cinturón por REFERENCIA justo antes de lo irreversible. El
       cinturón FALLA CERRADO: cuando el listado no permite concluir (405 del API real,
       error, lista truncada) NO re-emite — responde 409 con el remedio humano. La salida
       es una verificación humana AUDITADA (repetir la referencia exacta), y esa salida no
       existe para el caso en que consta que el documento emitido está ahí.
       El control S3b de la ronda anterior fijaba el bug como correcto ("best effort: con
       el listado caído se re-emite") y además corría con el documento emitido ya quitado
       del registro, así que la combinación dañina —listado caído CON emitido— no se
       probaba nunca. Ahora S3b afirma su precondición y S3b bis prueba ese caso.

  S4 · PISO MONÓTONO DEL ESTADO: una respuesta que contradice una emisión confirmada no
       degrada la fila ni pisa el folio; se anota para que lo resuelva una persona.

  S5 · LÍNEAS SIN `despacho_item_id` (era OPCIONAL): sin él el guard de "líneas de otra
       guía" quedaba vacío y la 52 volvía a citar la guía equivocada — se facturaban 8
       unidades de las que 4 salieron en otra guía. La vía electrónica ahora lo exige.

  S6 · ALINEACIÓN REAL con routers/despachos.py: tabla estado por estado contra las DOS
       funciones reales. Si despachos.py considera la guía VIVA y no hay folio del SII,
       la 33 no puede citar el N° manual.

  S7 · FOLIO DE LA 52 NUMÉRICO: la referencia 52 apunta a un documento tributario y su
       folio es un correlativo del SII, pero salía de `despacho.numero_guia`, que teclea el
       operador ('G-TECLEADO-A-MANO' viajando como FolioRef de un DTE 33 real).

Wasabil SIMULADO por monkeypatch de wasabil_dte.client — JAMÁS el API real: `issue` nunca
sale del proceso, cero llamadas al SII, y los documentos vivos de Grupo AM no se tocan.
Datos MARCADOS, limpieza por MARK **y** por padre, con verificación de deltas al final.

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_ra_sii_bloquear.py -q
(también:   ./venv/bin/python wasabil_dte/tests/test_ra_sii_bloquear.py)
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
import routers.despachos as desp_mod  # noqa: E402
from wasabil_dte import client as wasabil_client  # noqa: E402
from wasabil_dte.models import (  # noqa: E402
    WasabilDte, CLAIM_TTL_SEGUNDOS, STATUS_EMITIDO, STATUS_FALLIDO,
    STATUS_PENDIENTE, STATUS_PROCESANDO,
)
from wasabil_dte.router import router as wasabil_router  # noqa: E402
import wasabil_dte.router as wr  # noqa: E402
from wasabil_dte.service import (  # noqa: E402
    TIPO_DOC_GUIA, TIPO_DOC_FACTURA, armar_referencias_factura,
)

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_RASII__"
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
VENCIDO = timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)

# N° de guía TECLEADO a mano por el operador. Numérico a propósito (el folio de una guía en
# papel es un correlativo autorizado por el SII): así el único freno de los escenarios de
# guard es el GUARD, no la validación de folio de S7.
N_TECLEADO = "990501"

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:400]}"))
    if not cond:
        _fails.append(name)


# ─── Wasabil SIMULADO — ADVERSO: un REGISTRO de documentos, no una respuesta amable ─────
class FakeWasabil:
    """`documentos` es lo que Wasabil TIENE (varios pueden compartir `invoice_reference`,
    que es el estado normal tras un reintento). El ORDEN de la lista importa: es justo lo
    que el rescate viejo usaba para decidir, así que las sondas lo controlan.

      crear_respuesta   → lo que devuelve el POST /documents
      documentos        → registro; `buscar_documentos` busca por SUBSTRING (como el API
                          real), así que también ejercita el match exacto del router
      busqueda_completa → segunda componente de buscar_documentos (lista truncada)
      buscar_falla      → si está, buscar_documentos LANZA (el API real da 405)
      estado_override   → uuid → respuesta de estado_documento (para contradecir al POST)
    """

    def __init__(self):
        self.configurado = True
        self.cliente = {"id": 158381, "rut": "78.279.030-7",
                        "name": "H-E PARTS INTERNATIONAL CHILE SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS", "default": True}],
                        "addresses": [{"address": "RUTA 26 KM 15 S/N",
                                       "comuna": "Antofagasta", "city": "Antofagasta",
                                       "default": True}]}
        self.reset()

    def reset(self):
        self.crear_respuesta = {"status_id": STATUS_EMITIDO}   # ni uuid ni folio
        self.documentos: list = []
        self.busqueda_completa = True
        self.buscar_falla = None
        self.estado_override: dict = {}
        self.creados: list = []

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

    # -- API simulada --
    def _crear(self, payload):
        self.creados.append(payload)
        return dict(self.crear_respuesta)

    def _por_uuid(self, uuid):
        return next((d for d in self.documentos if d.get("uuid") == uuid), None)

    def _estado(self, uuid):
        if uuid in self.estado_override:
            return dict(self.estado_override[uuid])
        doc = self._por_uuid(uuid) or {}
        return {"uuid": uuid, "status_id": doc.get("status_id", STATUS_EMITIDO)}

    def _obtener(self, uuid):
        doc = self._por_uuid(uuid)
        return dict(doc) if doc else {"uuid": uuid}

    def _buscar(self, search):
        if self.buscar_falla:
            raise self.buscar_falla
        hallados = [dict(d) for d in self.documentos
                    if search and search in str(d.get("invoice_reference") or "")]
        return hallados, self.busqueda_completa


fake = FakeWasabil()

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
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"RASII-{sufijo}", fecha_oc="2026-07-01")
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
    db.rollback()
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
    db.rollback()
    d = db.get(Despacho, desp_id)
    d.estado = "despachado"
    db.commit()


def _doc(uuid, status, ref, folio=None):
    """Un documento del registro de Wasabil."""
    d = {"uuid": uuid, "status_id": status, "invoice_reference": ref}
    if folio:
        d["folio"] = folio
        d["document_pdf_url"] = f"https://api.wasabil.com/pdf/{folio}"
    return d


def _limpiar(db):
    """Por MARK **y** por padre: una corrida interrumpida no puede dejar la suite roja
    para siempre."""
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
    db2 = SessionLocal()
    try:
        n = db2.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
        n += db2.query(Despacho).filter(Despacho.numero_despacho.like(f"{MARK}%")).count()
        check("limpieza: 0 filas marcadas al final", n == 0, n)
    finally:
        db2.close()


# ═══ escenarios ════════════════════════════════════════════════════════════════
def _s1_rescate_dos_documentos(db, orden_inverso: bool):
    """El POST responde status 3 PELADO y Wasabil tiene DOS documentos con la misma
    referencia: el rechazado del 1er intento y el emitido con folio real."""
    etq = "S1C (orden inverso)" if orden_inverso else "S1A"
    sufijo = "C1" if orden_inverso else "A1"
    fake.reset()
    fake.crear_respuesta = {"status_id": STATUS_EMITIDO}
    _cot, oc, desp, _it, _di = _crear_venta(db, sufijo=sufijo)
    ref = f"{MARK}-DSP-{sufijo}"
    folio_real = "52777"
    docs = [_doc("u-rechazado", STATUS_FALLIDO, ref),
            _doc("u-emitido", STATUS_EMITIDO, ref, folio_real)]
    fake.documentos = list(reversed(docs)) if orden_inverso else docs

    creados_antes = len(fake.creados)
    r = client.post(f"{GUIAS}/{desp.id}/emitir")
    check(f"{etq} emitir 200 y UN solo documento al SII",
          r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
    fila = _dte_guia(db, desp.id)
    check(f"{etq} la fila queda EMITIDA con el folio REAL (no la degrada el rechazado)",
          fila is not None and fila.status_id == STATUS_EMITIDO and fila.folio == folio_real,
          fila and (fila.status_id, fila.folio, fila.uuid))
    check(f"{etq} el uuid es el del documento EMITIDO, no el del rechazado",
          fila is not None and fila.uuid == "u-emitido", fila and fila.uuid)
    check(f"{etq} el folio real pisa el N° tecleado en despacho.numero_guia",
          db.get(Despacho, desp.id).numero_guia == folio_real,
          db.get(Despacho, desp.id).numero_guia)
    _cerrar(db, desp.id)
    p = client.post(f"{FACTURAS}/preview",
                    json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
    check(f"{etq} la 33 cita el FOLIO REAL y jamás el N° tecleado a mano",
          p["puede_emitir"] is True and _ref52(p) == [folio_real],
          (p["problemas"], p.get("referencias")))
    _limpiar(db)


def run():
    db = SessionLocal()
    fake.install()
    orig_precios_cont = cont._precios_de_cotizacion
    orig_precios_router = wr._precios
    cont._precios_de_cotizacion = _fake_precios_cont
    wr._precios = lambda db_, cot_: PRECIOS
    _limpiar(db)
    try:
        CURRENT["empresa"] = "mineria"

        # ═══ S1 · el rescate elige el EMITIDO, en cualquier orden ══════════════════
        _s1_rescate_dos_documentos(db, orden_inverso=False)
        _s1_rescate_dos_documentos(db, orden_inverso=True)

        # ═══ S2 · DOS EMITIDOS con la misma referencia → abortar, no elegir ════════
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="B1")
        ref = f"{MARK}-DSP-B1"
        fake.documentos = [_doc("u-emit-1", STATUS_EMITIDO, ref, "52777"),
                           _doc("u-emit-2", STATUS_EMITIDO, ref, "52888")]
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("S2 emitir 200 (el documento salió) y UN solo POST",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
        fila = _dte_guia(db, desp.id)
        check("S2 el rescate NO elige folio: la fila queda emitida SIN folio",
              fila is not None and fila.status_id == STATUS_EMITIDO and not fila.folio,
              fila and (fila.status_id, fila.folio))
        check("S2 el error de la fila nombra los DOS folios y pide intervención humana",
              fila is not None and fila.error and "52777" in fila.error
              and "52888" in fila.error and "soporte" in fila.error.lower(),
              fila and fila.error)
        check("S2 nadie pisó el N° tecleado con un folio adivinado",
              db.get(Despacho, desp.id).numero_guia == N_TECLEADO,
              db.get(Despacho, desp.id).numero_guia)
        _cerrar(db, desp.id)
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("S2 la 33 queda BLOQUEADA y sin referencia 52",
              p["puede_emitir"] is False and _ref52(p) == [], (p["problemas"], p.get("referencias")))
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("S2 emitir 33: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("S2 tampoco quedó factura local zombi",
              len(_facturas_de(db, oc.id)) == 0, _facturas_de(db, oc.id))
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        check("S2 el sondeo informa la ambigüedad y no inventa folio",
              r.status_code == 200 and r.json().get("folio") in (None, "")
              and "52888" in str(r.json().get("error_consulta") or r.json().get("error") or ""),
              r.text)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        # 502 (no se pudo CONCLUIR el estado real) o 409: lo que importa es que no re-crea
        # y que el mensaje nombre los dos folios para que un humano los mire.
        check("S2 reintentar: bloquea, nombra los dos folios y 0 documentos nuevos",
              r.status_code in (409, 502) and len(fake.creados) == creados_antes
              and "52777" in r.json().get("detail", "")
              and "52888" in r.json().get("detail", ""), (r.status_code, r.text))
        _limpiar(db)

        # ═══ S3 · el reintento NO puede duplicar un documento ya EMITIDO ═══════════
        # Fila con el uuid del intento RECHAZADO (rechazo confirmado por el SII) y, en
        # Wasabil, OTRO documento EMITIDO con la MISMA referencia: el estado normal
        # después de un reintento. `estado_documento(uuid)` contesta por el rechazado.
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="D1")
        ref = f"{MARK}-DSP-D1"
        fake.documentos = [_doc("u-rech-d", STATUS_FALLIDO, ref),
                           _doc("u-emit-d", STATUS_EMITIDO, ref, "52999")]
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp.id,
                          uuid="u-rech-d", status_id=STATUS_FALLIDO,
                          error="SII: rechazado", en_vuelo_desde=None))
        db.commit()
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("S3 reintentar con una guía ya EMITIDA en Wasabil: 409",
              r.status_code == 409, r.text)
        check("S3 CERO documentos nuevos al SII (era la doble emisión REAL)",
              len(fake.creados) == creados_antes, len(fake.creados))
        check("S3 el 409 nombra el folio que ya existe y pide intervención humana",
              "52999" in r.json().get("detail", "") and "soporte" in r.json()["detail"].lower(),
              r.text)
        fila = _dte_guia(db, desp.id)
        check("S3 la fila NO se repara sola (estado local y remoto se contradicen)",
              fila is not None and fila.status_id == STATUS_FALLIDO, fila and fila.status_id)
        # CONTROL a: sin documento emitido en Wasabil, el reintento legítimo SÍ re-emite
        fake.documentos = [_doc("u-rech-d", STATUS_FALLIDO, ref)]
        fake.crear_respuesta = {"uuid": "u-nuevo-d", "status_id": STATUS_PROCESANDO}
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("S3 control: rechazo confirmado y sin emitido → el reintento re-emite (200)",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
        # ── CONTROL b · CORREGIDO EN ESTA RONDA ──────────────────────────────────
        # El control anterior FIJABA UN BUG COMO CORRECTO: decía "el cinturón es best
        # effort — con el listado caído se re-emite", y encima corría después de haber
        # QUITADO el documento emitido del registro (CONTROL a), así que nunca probaba la
        # combinación que hace daño. Ahora: (1) afirma su precondición, (2) exige el
        # comportamiento FAIL CLOSED, y (3) tiene el check hermano del caso peligroso
        # (listado caído CON documento emitido), que es el que salió al SII dos veces.
        def _fila_rechazada():
            db.rollback()
            f = _dte_guia(db, desp.id)
            f.status_id, f.uuid, f.folio, f.en_vuelo_desde = STATUS_FALLIDO, "u-rech-d", None, None
            f.error = "SII: rechazado"
            db.commit()
            return f

        _fila_rechazada()
        fake.documentos = [_doc("u-rech-d", STATUS_FALLIDO, ref)]
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)
        check("S3b PRECONDICIÓN: el listado NO responde (405 del API real)",
              isinstance(fake.buscar_falla, wasabil_client.WasabilError), fake.buscar_falla)
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("S3b listado caído: el cinturón FALLA CERRADO (409, 0 documentos nuevos)",
              r.status_code == 409 and len(fake.creados) == creados_antes,
              (r.status_code, len(fake.creados) - creados_antes, r.text))
        detalle = r.json().get("detail", "")
        check("S3b el 409 dice QUÉ mirar en Wasabil (la referencia EXACTA)",
              ref in detalle and "wasabil.com" in detalle, detalle)
        check("S3b el 409 dice CÓMO destrabarlo (el parámetro de verificación humana)",
              "verificado_sin_emitido" in detalle, detalle)
        check("S3b el 409 dice qué defensa queda de verdad (ancla local, no el cinturón)",
              "wasabil_dte" in detalle and "claim" in detalle.lower(), detalle)

        # S3b bis · EL CASO PELIGROSO QUE NUNCA SE PROBÓ: listado caído **y** documento
        # EMITIDO en Wasabil. Es el estado exacto con el que salieron dos guías 52 reales.
        _fila_rechazada()
        fake.documentos = [_doc("u-rech-d", STATUS_FALLIDO, ref),
                           _doc("u-emit-d", STATUS_EMITIDO, ref, "52999")]
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("S3b bis listado caído CON un emitido real: 409 y CERO documentos nuevos",
              r.status_code == 409 and len(fake.creados) == creados_antes,
              (r.status_code, len(fake.creados) - creados_antes, r.text))
        # …y tampoco con la declaración humana, si el listado se recupera y lo muestra
        fake.buscar_falla = None
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar",
                        params={"verificado_sin_emitido": ref})
        check("S3b bis la declaración humana NO levanta el bloqueo con evidencia (folio real)",
              r.status_code == 409 and len(fake.creados) == creados_antes
              and "52999" in r.json().get("detail", ""), (r.status_code, r.text))

        # S3b ter · la SALIDA humana auditada: con el listado caído y sin evidencia de
        # emitido, el operador declara la referencia EXACTA y el reintento procede…
        _fila_rechazada()
        fake.documentos = [_doc("u-rech-d", STATUS_FALLIDO, ref)]
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)
        creados_antes = len(fake.creados)
        for mentira in ("true", "1", ref[:-1], ref + "9"):
            r = client.post(f"{GUIAS}/{desp.id}/reintentar",
                            params={"verificado_sin_emitido": mentira})
            check(f"S3b ter declaración inexacta '{mentira}': sigue bloqueado, 0 documentos",
                  r.status_code == 409 and len(fake.creados) == creados_antes,
                  (r.status_code, len(fake.creados) - creados_antes))
        fake.crear_respuesta = {"uuid": "u-nuevo-d", "status_id": STATUS_PROCESANDO}
        r = client.post(f"{GUIAS}/{desp.id}/reintentar",
                        params={"verificado_sin_emitido": ref})
        check("S3b ter con la referencia EXACTA el reintento procede (1 documento)",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
        db.rollback()
        fila = _dte_guia(db, desp.id)
        check("S3b ter queda RASTRO de quién autorizó y sobre qué referencia",
              fila is not None and fila.error and "VERIFICACIÓN HUMANA" in fila.error
              and ref in fila.error, fila and fila.error)

        # S3b quater · el rastro SOBREVIVE a la emisión exitosa (que limpia `error`): si no,
        # la autorización desaparecería justo cuando hay un documento real que auditar.
        _fila_rechazada()
        fake.documentos = [_doc("u-rech-d", STATUS_FALLIDO, ref)]
        fake.crear_respuesta = {"uuid": "u-ok-d", "status_id": STATUS_EMITIDO, "folio": "52321"}
        r = client.post(f"{GUIAS}/{desp.id}/reintentar",
                        params={"verificado_sin_emitido": ref})
        db.rollback()
        fila = _dte_guia(db, desp.id)
        check("S3b quater emitida con folio y el rastro de la autorización SIGUE ahí",
              r.status_code == 200 and fila is not None and fila.folio == "52321"
              and fila.error and "VERIFICACIÓN HUMANA" in fila.error,
              (r.status_code, fila and (fila.folio, fila.error)))
        fake.crear_respuesta = {"uuid": "u-nuevo-d", "status_id": STATUS_PROCESANDO}
        # S3d · LISTA TRUNCADA: encontrar un rechazado NO prueba que no exista un emitido
        # más adelante en la lista. No se concluye nada: se aborta.
        db.rollback()
        fila = _dte_guia(db, desp.id)
        fila.status_id = STATUS_FALLIDO
        fila.uuid = None          # fuerza la verificación POR REFERENCIA
        fila.en_vuelo_desde = None
        db.commit()
        fake.buscar_falla = None
        fake.busqueda_completa = False
        fake.documentos = [_doc("u-rech-d", STATUS_FALLIDO, ref)]
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("S3d lista truncada: 502 sin concluir y 0 documentos nuevos",
              r.status_code == 502 and len(fake.creados) == creados_antes,
              (r.status_code, r.text))
        _limpiar(db)

        # ═══ S4 · PISO MONÓTONO: nada degrada una emisión confirmada ═══════════════
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="E1",
                                                estado_despacho="despachado")
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp.id,
                          uuid="u-emitido-e", status_id=STATUS_EMITIDO, folio=None))
        db.commit()
        # Wasabil ahora CONTRADICE la emisión: responde status 4 por ese mismo uuid
        fake.estado_override = {"u-emitido-e": {"uuid": "u-emitido-e",
                                               "status_id": STATUS_FALLIDO,
                                               "display_error": "rechazado"}}
        creados_antes = len(fake.creados)
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        fila = _dte_guia(db, desp.id)
        check("S4 el sondeo NO degrada el status 3 que el POST ya confirmó",
              fila is not None and fila.status_id == STATUS_EMITIDO,
              fila and (fila.status_id, r.text[:160]))
        check("S4 el error explica la contradicción (para que la mire un humano)",
              fila is not None and fila.error and "NO se degrada" in fila.error,
              fila and fila.error)
        check("S4 sin folio y sin documentos nuevos",
              not fila.folio and len(fake.creados) == creados_antes,
              (fila.folio, len(fake.creados)))
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("S4 y por eso la 33 sigue bloqueada, sin citar el N° tecleado",
              p["puede_emitir"] is False and _ref52(p) == [],
              (p["problemas"], p.get("referencias")))
        # CONTROL: el piso no estorba el enriquecimiento legítimo (3 → 3 con folio)
        fake.estado_override = {"u-emitido-e": {"uuid": "u-emitido-e",
                                               "status_id": STATUS_EMITIDO}}
        fake.documentos = [_doc("u-emitido-e", STATUS_EMITIDO, f"{MARK}-DSP-E1", "52321")]
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        fila = _dte_guia(db, desp.id)
        check("S4 control: el sondeo legítimo SÍ escribe el folio que faltaba",
              fila is not None and fila.folio == "52321", fila and fila.folio)
        # Y un folio DISTINTO del ya registrado no se pisa (función real)
        wr._actualizar_desde_wasabil(db, fila, {"status_id": STATUS_EMITIDO,
                                                "folio": "52654"})
        db.commit()
        fila = _dte_guia(db, desp.id)
        check("S4 un folio DISTINTO no pisa el registrado; se anota la discrepancia",
              fila.folio == "52321" and fila.error and "52654" in fila.error,
              (fila.folio, fila.error))
        # Un status ILEGIBLE tampoco cambia el estado (antes reventaba el volcado en 500)
        wr._actualizar_desde_wasabil(db, fila, {"status_id": "N/A"})
        db.commit()
        fila = _dte_guia(db, desp.id)
        check("S4 un status ilegible no cambia el estado y queda anotado",
              fila.status_id == STATUS_EMITIDO and "ilegible" in (fila.error or ""),
              (fila.status_id, fila.error))
        _limpiar(db)

        # ═══ S5 · líneas que no dicen de qué ítem de la guía salieron ══════════════
        fake.reset()
        fake.crear_respuesta = {"uuid": "u-f33", "status_id": STATUS_FALLIDO}
        # Venta de 8 unidades repartidas en DOS guías firmadas de 4 (así el tope por ÍTEM
        # de toda la OC deja pasar las 8 y el único freno posible es el tope por GUÍA).
        _cot, oc, desp_a, it, di_a = _crear_venta(db, sufijo="F1",
                                                  estado_despacho="despachado",
                                                  guia_manual="800001", cantidad=8)
        db.rollback()
        db.get(DespachoItem, di_a.id).qty_despachada = 4
        db.commit()
        desp_b, di_b = _agregar_despacho(db, oc, it, sufijo="F2", qty=4,
                                         guia_manual="800002")
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp_a.id,
                          uuid="u-guia-a", status_id=STATUS_EMITIDO, folio="52111",
                          payload_json='{"documentDate": "2026-07-10"}'))
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp_b.id,
                          uuid="u-guia-b", status_id=STATUS_EMITIDO, folio="52222",
                          payload_json='{"documentDate": "2026-07-12"}'))
        db.commit()
        # 8 unidades declarando la guía A (que solo trasladó 4) y SIN despacho_item_id
        sin_guia = {"oc_cliente_id": oc.id, "despacho_id": desp_a.id,
                    "items": [{"item_cotizacion_id": it.id, "cantidad": 8}]}
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview", json=sin_guia).json()
        # Se comprueba el RESULTADO, no qué capa lo atajó: en esta misma ronda
        # routers/contabilidad.py:_ligar_lineas_a_su_guia (archivo de otro reparador) liga
        # la línea suelta al ítem de ESTA guía, y con eso el tope por guía —que antes se
        # saltaba— deja 4 disponibles. El guard de este módulo es el cinturón del camino
        # del REINTENTO, donde la línea ya está persistida con despacho_item_id NULL y
        # nadie puede ligarla: eso se prueba más abajo.
        check("S5 preview: 8 unidades (4 salieron en otra guía) NO se pueden facturar aquí",
              p["puede_emitir"] is False and _ref52(p) in ([], ["52111"]),
              (p["problemas"], p.get("referencias")))
        r = client.post(f"{FACTURAS}/emitir", json=sin_guia)
        check("S5 emitir: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("S5 sin factura local zombi", len(_facturas_de(db, oc.id)) == 0,
              _facturas_de(db, oc.id))
        # CONTROL 1: la misma línea DECLARANDO su ítem de despacho sí puede
        correcto = {"oc_cliente_id": oc.id, "despacho_id": desp_a.id,
                    "items": [{"item_cotizacion_id": it.id, "despacho_item_id": di_a.id,
                               "cantidad": 4}]}
        p = client.post(f"{FACTURAS}/preview", json=correcto).json()
        check("S5 control: declarando el ítem de ESTA guía puede emitir y cita su folio",
              p["puede_emitir"] is True and _ref52(p) == ["52111"],
              (p["problemas"], p.get("referencias")))
        # CONTROL 2: el camino REAL del frontend (solo despacho_id, líneas derivadas)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp_a.id}).json()
        check("S5 control: el camino del modal (líneas derivadas de la guía) NO se rompió",
              p["puede_emitir"] is True and _ref52(p) == ["52111"],
              (p["problemas"], p.get("referencias")))
        # CINTURÓN DEL REINTENTO: la factura persistida con la línea sin ítem de despacho
        r = client.post(f"{FACTURAS}/emitir", json=correcto)
        check("S5 la 1ª emisión queda rechazada por el SII (reintentable)",
              r.status_code == 200 and r.json()["estado"] == "fallido", r.text)
        fac_id = r.json()["factura_id"]
        db.rollback()
        db.query(ContFacturaClienteItem).filter(
            ContFacturaClienteItem.factura_id == fac_id,
            ContFacturaClienteItem.despacho_item_id == di_a.id
        ).update({ContFacturaClienteItem.despacho_item_id: None},
                 synchronize_session=False)
        db.commit()
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("S5 reintento con la línea sin ítem de despacho: 409 y 0 documentos nuevos",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("S5 el 409 del reintento nombra el motivo real",
              "despacho_item_id" in r.json().get("detail", ""), r.text)
        # CONTROL 3: con la línea intacta, el reintento pasa el guard y re-emite
        db.rollback()
        db.query(ContFacturaClienteItem).filter(
            ContFacturaClienteItem.factura_id == fac_id,
            ContFacturaClienteItem.item_cotizacion_id == it.id
        ).update({ContFacturaClienteItem.despacho_item_id: di_a.id},
                 synchronize_session=False)
        db.commit()
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("S5 control: con la línea en su guía el reintento SÍ procede",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
        _limpiar(db)

        # ═══ S6 · alineación REAL con routers/despachos.py, estado por estado ══════
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="G1",
                                                estado_despacho="despachado")
        ahora = datetime.utcnow()
        estados = [
            # (etiqueta, status, uuid, en_vuelo, folio)
            ("emitida con folio", STATUS_EMITIDO, "u1", None, "52001"),
            ("emitida SIN folio", STATUS_EMITIDO, None, None, None),
            ("procesando con uuid", STATUS_PROCESANDO, "u2", None, None),
            ("borrador con uuid", STATUS_PENDIENTE, "u3", None, None),
            ("status NULL con uuid", None, "u4", None, None),
            ("claim FRESCO", STATUS_PENDIENTE, None, ahora, None),
            ("ambigua: pendiente sin uuid, claim vencido", STATUS_PENDIENTE, None,
             ahora - VENCIDO, None),
            ("ambigua: status 4 sin uuid, claim vencido", STATUS_FALLIDO, None,
             ahora - VENCIDO, None),
            ("rechazo CONFIRMADO (status 4 con uuid)", STATUS_FALLIDO, "u5", None, None),
            ("fallida sin uuid y sin claim", STATUS_FALLIDO, None, None, None),
        ]
        db2 = SessionLocal()
        try:
            desviaciones = []
            for etq, status, uuid, en_vuelo, folio in estados:
                db.rollback()
                db.query(WasabilDte).filter(WasabilDte.despacho_id == desp.id).delete(
                    synchronize_session=False)
                db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA,
                                  despacho_id=desp.id, status_id=status, uuid=uuid,
                                  en_vuelo_desde=en_vuelo, folio=folio))
                db.commit()
                db2.rollback()   # sesión limpia: sin snapshot viejo
                viva = desp_mod._guia_electronica_activa(db2, desp.id,
                                                         incluir_ambiguo=True) is not None
                motivo = wr._guia_no_referenciable(db2, desp.id)
                if viva and not folio and motivo is None:
                    desviaciones.append((etq, viva, motivo))
            check("S6 INVARIANTE: si despachos.py la ve VIVA y no hay folio, la 33 BLOQUEA",
                  desviaciones == [], desviaciones)

            # Los dos estados puntuales del hallazgo, con su nombre
            def _estado(status, uuid, en_vuelo, folio=None):
                db.rollback()
                db.query(WasabilDte).filter(WasabilDte.despacho_id == desp.id).delete(
                    synchronize_session=False)
                db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA,
                                  despacho_id=desp.id, status_id=status, uuid=uuid,
                                  en_vuelo_desde=en_vuelo, folio=folio))
                db.commit()
                db2.rollback()

            _estado(STATUS_FALLIDO, None, ahora - VENCIDO)
            check("S6a 'status 4 · sin uuid · en vuelo' bloquea (despachos.py: VIVA)",
                  desp_mod._guia_electronica_activa(db2, desp.id, incluir_ambiguo=True) is not None
                  and wr._guia_no_referenciable(db2, desp.id) is not None,
                  wr._guia_no_referenciable(db2, desp.id))
            _estado(STATUS_FALLIDO, "u5", None)
            check("S6b rechazo CONFIRMADO con uuid NO deja preso al despacho",
                  desp_mod._guia_electronica_activa(db2, desp.id, incluir_ambiguo=True) is None
                  and wr._guia_no_referenciable(db2, desp.id) is None,
                  wr._guia_no_referenciable(db2, desp.id))
            p = client.post(f"{FACTURAS}/preview",
                            json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
            check("S6b y la 33 sale con el folio de la guía en papel",
                  p["puede_emitir"] is True and _ref52(p) == [N_TECLEADO],
                  (p["problemas"], p.get("referencias")))
        finally:
            db2.close()
        _limpiar(db)

        # ═══ S7 · el folio de la 52 tiene que ser un folio del SII ═════════════════
        # (a) la función real, sin BD
        for folio_malo in ("G-MANUAL-9", "G-TECLEADO-A-MANO", "0", "12A", "٣٤"):
            refs, probs = armar_referencias_factura(
                numero_oc="OC-1", fecha_oc=date(2026, 7, 1), guia_folio=folio_malo,
                guia_fecha=date(2026, 7, 2))
            check(f"S7a folio de guía '{folio_malo}' → problema y SIN referencia 52",
                  any("no es un folio numérico" in x for x in probs)
                  and [r["documentType"] for r in refs] == ["801"], (refs, probs))
        refs, probs = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 7, 1), guia_folio="800001",
            guia_fecha=date(2026, 7, 2))
        check("S7a control: un folio numérico sí arma la referencia 52",
              not probs and [r["documentType"] for r in refs] == ["801", "52"], (refs, probs))
        # (b) de punta a punta: guía en papel con N° tecleado sin forma de folio
        fake.reset()
        fake.crear_respuesta = {"uuid": "u-33-h", "status_id": STATUS_PROCESANDO}
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="H1",
                                                estado_despacho="despachado",
                                                guia_manual="G-MANUAL-9")
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("S7b preview: el N° tecleado sin forma de folio BLOQUEA la 33",
              p["puede_emitir"] is False and _ref52(p) == []
              and any("Despachos" in x for x in p["problemas"]), p["problemas"])
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("S7b emitir: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("S7b sin factura local zombi", len(_facturas_de(db, oc.id)) == 0,
              _facturas_de(db, oc.id))
        db.rollback()
        db.get(Despacho, desp.id).numero_guia = "900321"
        db.commit()
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("S7b control: corregido el N° en Despachos, la 33 emite citando ese folio",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
        env = fake.creados[-1]
        refs_env = [(x.get("document_type"), x.get("folio"))
                    for x in (env.get("references") or [])]
        check("S7b control: el payload lleva la 52 con el folio corregido",
              ("52", "900321") in refs_env, refs_env)
        _limpiar(db)

        # ═══ S8 · el cinturón de la FACTURA 33 falla cerrado igual que el de la guía ══
        # La mitad de la 33 nunca se había probado y es la peor de duplicar: dos ventas
        # ante el SII. Mismo guion: rechazo confirmado por uuid + listado que no concluye.
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="H1")
        fake.crear_respuesta = {"uuid": "u-g-h1", "status_id": STATUS_EMITIDO,
                                "folio": "52111"}
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("S8 preparación: la guía 52 queda emitida con folio",
              r.status_code == 200 and r.json().get("folio") == "52111", r.text)
        _cerrar(db, desp.id)
        fake.crear_respuesta = {"uuid": "uf-1", "status_id": STATUS_FALLIDO}
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        fac_id = r.json().get("factura_id")
        ref_fac = f"FACT-{fac_id}"
        check("S8 preparación: la 33 queda RECHAZADA con uuid (rechazo confirmado)",
              r.status_code == 200 and r.json().get("estado") == "fallido"
              and fac_id, r.text)

        def _fila_fac_rechazada():
            db.rollback()
            f = (db.query(WasabilDte)
                 .filter(WasabilDte.factura_id == fac_id,
                         WasabilDte.tipo_dte == TIPO_DOC_FACTURA).first())
            f.status_id, f.uuid, f.folio, f.en_vuelo_desde = STATUS_FALLIDO, "uf-1", None, None
            db.commit()
            return f

        # (c) listado caído → FALLA CERRADO
        _fila_fac_rechazada()
        fake.documentos = [_doc("uf-1", STATUS_FALLIDO, ref_fac)]
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("S8 33 con el listado caído: 409 y CERO DTE 33 nuevos al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes,
              (r.status_code, len(fake.creados) - creados_antes, r.text))
        check("S8 el 409 nombra la referencia FACT-<id> y el parámetro de verificación",
              ref_fac in r.json().get("detail", "")
              and "verificado_sin_emitido" in r.json().get("detail", ""), r.text)

        # (b) listado caído CON una 33 ya EMITIDA: el caso que duplicaba una VENTA
        _fila_fac_rechazada()
        fake.documentos = [_doc("uf-1", STATUS_FALLIDO, ref_fac),
                           _doc("uf-emit", STATUS_EMITIDO, ref_fac, "F900999")]
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("S8 33 listado caído CON una emitida real: 409 y 0 documentos nuevos",
              r.status_code == 409 and len(fake.creados) == creados_antes,
              (r.status_code, len(fake.creados) - creados_antes, r.text))
        fake.buscar_falla = None
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar",
                        params={"verificado_sin_emitido": ref_fac})
        check("S8 33 con evidencia del folio real, la declaración humana NO destraba",
              r.status_code == 409 and len(fake.creados) == creados_antes
              and "F900999" in r.json().get("detail", ""), (r.status_code, r.text))

        # (a)/(c)+declaración: el reintento legítimo sigue existiendo
        _fila_fac_rechazada()
        fake.documentos = [_doc("uf-1", STATUS_FALLIDO, ref_fac)]
        creados_antes = len(fake.creados)
        fake.crear_respuesta = {"uuid": "uf-2", "status_id": STATUS_PROCESANDO}
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("S8 control: listado SANO sin emitidos → la 33 se re-emite (1 documento)",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
        _fila_fac_rechazada()
        fake.buscar_falla = wasabil_client.WasabilError("405", ambiguo=True)
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar",
                        params={"verificado_sin_emitido": ref_fac})
        check("S8 con el listado caído y verificación humana EXACTA: re-emite (1 documento)",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1, r.text)
        db.rollback()
        fila = (db.query(WasabilDte).filter(WasabilDte.factura_id == fac_id,
                                            WasabilDte.tipo_dte == TIPO_DOC_FACTURA).first())
        check("S8 y queda el rastro de la autorización en la fila de la factura",
              fila is not None and fila.error and "VERIFICACIÓN HUMANA" in fila.error
              and ref_fac in fila.error, fila and fila.error)
        _limpiar(db)

        # ═══ S9 · SALIDA del callejón "EMITIDA sin folio" (registrar el folio a mano) ══
        # El documento existe ante el SII y su folio no llegó: el reintento responde 409
        # (correcto) y no había NINGUNA acción en el producto para salir — sólo un UPDATE
        # a mano en la base. Estos endpoints son la salida: registran el folio LEÍDO en
        # Wasabil, con confirmación explícita y rastro, y jamás emiten nada.
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="I1")
        fake.crear_respuesta = {"status_id": STATUS_EMITIDO}      # ni uuid ni folio
        fake.buscar_falla = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("S9 preparación: la guía queda EMITIDA sin folio (el callejón)",
              r.status_code == 200 and r.json().get("estado") == "emitido"
              and not r.json().get("folio"), r.text)
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        # 409 (cinturón) o 502 (el rescate por referencia tampoco pudo concluir): lo que
        # importa es que NO re-emite — el documento ya existe ante el SII.
        check("S9 el reintento sigue bloqueado (re-emitir sería una 2ª guía REAL)",
              r.status_code in (409, 502) and len(fake.creados) == creados_antes,
              (r.status_code, r.text))
        # guards del registro manual
        for folio_malo, confirmo, esperado, nombre in (
                ("52654", "52655", 400, "confirmación distinta del folio"),
                ("G-52654", "G-52654", 400, "folio no numérico"),
                ("", "", 400, "folio vacío")):
            r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                            params={"folio": folio_malo, "confirmo_folio": confirmo})
            check(f"S9 rechaza {nombre} ({esperado})",
                  r.status_code == esperado and len(fake.creados) == creados_antes,
                  (r.status_code, r.text[:160]))
        # el registro legítimo: el humano leyó 52654 en app.wasabil.com
        r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                        params={"folio": "52654", "confirmo_folio": "52654"})
        check("S9 registra el folio leído en Wasabil (200) y NO emite nada",
              r.status_code == 200 and r.json().get("folio") == "52654"
              and len(fake.creados) == creados_antes, r.text)
        db.rollback()
        check("S9 el folio llega al despacho, igual que en una emisión normal",
              db.get(Despacho, desp.id).numero_guia == "52654",
              db.get(Despacho, desp.id).numero_guia)
        fila = _dte_guia(db, desp.id)
        check("S9 queda RASTRO auditable de quién lo registró y por qué",
              fila is not None and "_registro_manual_de_folio" in (fila.respuesta_json or "")
              and "52654" in (fila.respuesta_json or ""), fila and fila.respuesta_json)
        # idempotente con el MISMO folio, y jamás pisa uno distinto
        r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                        params={"folio": "52654", "confirmo_folio": "52654"})
        check("S9 repetirlo con el mismo folio es idempotente (200)", r.status_code == 200, r.text)
        r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                        params={"folio": "52999", "confirmo_folio": "52999"})
        check("S9 NUNCA pisa un folio ya registrado (409 nombrando el que hay)",
              r.status_code == 409 and "52654" in r.json().get("detail", ""), r.text)
        # y ahora que hay folio, la 33 puede citarlo
        _cerrar(db, desp.id)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("S9 con el folio registrado la 33 sale citando ESE folio (callejón resuelto)",
              p["puede_emitir"] is True and _ref52(p) == ["52654"],
              (p["problemas"], p.get("referencias")))
        _limpiar(db)

        # S9b · la MÁQUINA manda sobre el humano cuando puede concluir
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="I2")
        ref_i2 = f"{MARK}-DSP-I2"
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp.id,
                          uuid="u-sin-folio", status_id=STATUS_EMITIDO, folio=None))
        db.commit()
        fake.documentos = [_doc("u-sin-folio", STATUS_EMITIDO, ref_i2, "52777")]
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                        params={"folio": "52111", "confirmo_folio": "52111"})
        check("S9b si Wasabil sabe el folio y NO es el tecleado: 409 nombrando los dos",
              r.status_code == 409 and "52777" in r.json().get("detail", "")
              and "52111" in r.json().get("detail", "")
              and len(fake.creados) == creados_antes, r.text)
        r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                        params={"folio": "52777", "confirmo_folio": "52777"})
        check("S9b con el folio que Wasabil confirma, se registra (200)",
              r.status_code == 200 and r.json().get("folio") == "52777", r.text)
        _limpiar(db)

        # S9c · CONTRADICCIÓN: acá figura emitido y Wasabil dice que no hay ninguno
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="I3")
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp.id,
                          uuid=None, status_id=STATUS_EMITIDO, folio=None))
        db.commit()
        fake.documentos = []                     # listado SANO y vacío
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                        params={"folio": "52654", "confirmo_folio": "52654"})
        check("S9c estado contradictorio: NO se registra nada (409) y no se emite",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        fila = _dte_guia(db, desp.id)
        check("S9c la fila sigue SIN folio (nadie inventó uno)",
              fila is not None and not fila.folio, fila and fila.folio)
        # y sobre un documento que NO está emitido tampoco se registra folio a mano
        fila.status_id = STATUS_FALLIDO; fila.uuid = "u-x"; db.commit()
        r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                        params={"folio": "52654", "confirmo_folio": "52654"})
        check("S9c sobre un documento NO emitido: 409 (esto no es una vía de emisión)",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        _limpiar(db)

        # S9e · CARRERA: el sondeo (u otra pestaña) registra el folio MIENTRAS este
        # request está consultando a Wasabil. La consulta va SIN locks (regla de la casa:
        # nunca red dentro de una transacción con locks), así que la escritura re-lee la
        # fila BAJO LOCK y vuelve a validar. Sin eso, el registro manual pisaría —o
        # confundiría— un folio real que ya llegó.
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="I5")
        db.add(WasabilDte(empresa="mineria", tipo_dte=TIPO_DOC_GUIA, despacho_id=desp.id,
                          uuid="u-carrera", status_id=STATUS_EMITIDO, folio=None))
        db.commit()
        fake.buscar_falla = wasabil_client.WasabilError("405", ambiguo=True)

        def _obtener_con_carrera(uuid):
            """Otra sesión GANA la carrera justo mientras consultamos a Wasabil."""
            otra = SessionLocal()
            f = (otra.query(WasabilDte)
                 .filter(WasabilDte.despacho_id == desp.id,
                         WasabilDte.tipo_dte == TIPO_DOC_GUIA).first())
            if f is not None and not f.folio:
                f.folio = "52505"
                otra.commit()
            otra.close()
            return {"uuid": uuid}

        wasabil_client.obtener_documento = _obtener_con_carrera
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/registrar-folio",
                        params={"folio": "52654", "confirmo_folio": "52654"})
        wasabil_client.obtener_documento = fake._obtener
        check("S9e carrera: el folio llegó mientras consultábamos → 409, no se pisa",
              r.status_code == 409 and len(fake.creados) == creados_antes,
              (r.status_code, r.text))
        fila = _dte_guia(db, desp.id)
        check("S9e el folio que ganó la carrera queda INTACTO",
              fila is not None and fila.folio == "52505", fila and fila.folio)
        _limpiar(db)

        # S9d · gemela de FACTURAS: registrar el folio cierra la factura de verdad
        fake.reset()
        _cot, oc, desp, _it, _di = _crear_venta(db, sufijo="I4")
        fake.crear_respuesta = {"uuid": "u-g-i4", "status_id": STATUS_EMITIDO,
                                "folio": "52444"}
        client.post(f"{GUIAS}/{desp.id}/emitir")
        _cerrar(db, desp.id)
        fake.crear_respuesta = {"status_id": STATUS_EMITIDO}       # 33 emitida SIN folio
        fake.buscar_falla = wasabil_client.WasabilError("405", ambiguo=True)
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        fac2_id = r.json().get("factura_id")
        check("S9d preparación: la 33 queda EMITIDA sin folio",
              r.status_code == 200 and fac2_id and not r.json().get("folio"), r.text)
        db.rollback()
        check("S9d y por eso la factura local sigue SIN numero_factura",
              not (db.get(ContFacturaCliente, fac2_id).numero_factura or ""),
              db.get(ContFacturaCliente, fac2_id).numero_factura)
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac2_id}/registrar-folio",
                        params={"folio": "778899", "confirmo_folio": "778899"})
        check("S9d registrar el folio a mano: 200 y NADA sale al SII",
              r.status_code == 200 and r.json().get("folio") == "778899"
              and len(fake.creados) == creados_antes, r.text)
        db.rollback()
        check("S9d el folio queda en la factura local (cierre normal, adelantos incluidos)",
              (db.get(ContFacturaCliente, fac2_id).numero_factura or "") == "778899",
              db.get(ContFacturaCliente, fac2_id).numero_factura)
        _limpiar(db)

    finally:
        cont._precios_de_cotizacion = orig_precios_cont
        wr._precios = orig_precios_router
        fake.reset()
        _limpiar(db)
        db.close()
        _verificar_limpieza()

    print()
    if _fails:
        print(f"FALLARON {len(_fails)}: {_fails}")
    assert not _fails, f"{len(_fails)} fallos: {_fails}"
    print("=== RESCATE / PISO / DOBLE EMISIÓN / LÍNEAS / ALINEACIÓN / FOLIO: TODO OK ===")


def test_ra_sii_bloquear_no_recuperar_con_astucia(): run()


if __name__ == "__main__":
    run()
