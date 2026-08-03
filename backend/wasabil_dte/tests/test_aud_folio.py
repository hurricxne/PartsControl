"""DTE «EMITIDO pero SIN folio» — el callejón sin salida que dejaba la factura 33 real
citando un folio 52 que el SII no reconoce.

Wasabil puede responder al POST /documents con {"uuid": ..., "status_id": 3} y SIN la
clave `folio` (forma que el propio README del cliente declara PENDIENTE de confirmar
contra el API real, client.py). Antes de estos arreglos eso producía dos daños:

  · La guía quedaba 'emitida' con folio NULL y `despacho.numero_guia` conservaba el N°
    TECLEADO a mano. `_guia_electronica_en_proceso` devolvía False (solo miraba
    status == 3), así que la factura 33 salía al SII referenciando ese N° manual — un
    folio 52 inexistente, irreversible salvo nota de crédito. Además contradecía a
    `_rechazar_si_pisa_folio` (routers/despachos.py), que ya exigía folio no vacío.
  · El estado era PERMANENTE: el sondeo cortaba en cuanto status era 3, /reintentar
    responde 409 'ya está emitida' y el N° manual no se podía editar (guard
    anti-pisado). El despacho nunca recibía su N° de guía y la factura local nunca
    recibía su `numero_factura` (con los adelantos diferidos sin aplicar).

Esta suite cubre las tres capas del arreglo (_completar_documento_emitido en la
emisión · guard alineado a «estado 3 Y folio no vacío» · sondeo que re-consulta ese
caso), en las DOS direcciones (guía 52 y factura 33), y protege la direccionalidad que
NO debe cambiar (una guía fallida que nunca llegó a Wasabil sigue sin bloquear; una
guía emitida CON folio sigue sin bloquear).

El invariante de fondo es uno solo —**una factura 33 nunca sale sin su referencia 52
válida**—, así que la suite cierra también los otros dos caminos por los que se colaba:
el modo `items` SIN despacho_id (la referencia se omitía en silencio) y el despacho sin
N° interno, que es el ancla anti doble emisión del reintento.

Wasabil SIMULADO por monkeypatch de wasabil_dte.client — JAMÁS el API real (emitir al
SII es IRREVERSIBLE: los documentos vivos de Grupo AM son las guías 136/137 y la
factura 116, y nada de acá los toca). Datos MARCADOS y limpieza verificada.

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_aud_folio.py -q
(también:   ./venv/bin/python wasabil_dte/tests/test_aud_folio.py)
"""
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from auth import get_current_user  # noqa: E402
from database import SessionLocal, engine, Base  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContAdelanto,
)
import routers.contabilidad as cont  # noqa: E402
from wasabil_dte import client as wasabil_client  # noqa: E402
from wasabil_dte.models import WasabilDte, STATUS_EMITIDO, STATUS_FALLIDO  # noqa: E402
from wasabil_dte.router import router as wasabil_router  # noqa: E402
import wasabil_dte.router as wasabil_router_mod  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_WAUD__"
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

# N° de guía tecleado a mano por el operador — NUMÉRICO, como el folio de una guía en papel
# autorizada por el SII (armar_referencias_factura ahora exige folio numérico en la
# referencia 52: la 52 apunta a un documento tributario). Con los "G-…" de antes, la
# validación de folio bloqueaba sola y los checks del guard perdían poder discriminante.
N_TECLEADO = "990077"

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


# ─── Wasabil SIMULADO (mismo fake de las otras suites + folio configurable) ──────
class FakeWasabil:
    """`status_respuesta` es lo que devuelve crear_documento y `folio_emitido` lo que
    trae el documento COMPLETO: con status 3 + folio None se reproduce exactamente el
    'emitido sin folio' del API."""

    def __init__(self):
        self.configurado = True
        self.cliente = {"id": 158381, "rut": "78.279.030-7",
                        "name": "H-E PARTS INTERNATIONAL CHILE SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS", "default": True}],
                        "addresses": [{"address": "RUTA 26 KM 15 S/N",
                                       "comuna": "Antofagasta", "city": "Antofagasta",
                                       "default": True}]}
        self.status_respuesta = 3        # crear_documento responde EMITIDO...
        self.estado_final = 3
        self.folio_emitido = None       # ...y el folio lo trae (o no) el doc completo
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
        wasabil_client.buscar_documentos = lambda search: ([], True)

    def _crear(self, payload):
        # El POST responde SIN folio: es la forma que dispara todo el hallazgo.
        self.creados.append(payload)
        return {"uuid": f"uuid-aud{len(self.creados)}", "status_id": self.status_respuesta}

    def _estado(self, uuid):
        return {"uuid": uuid, "status_id": self.estado_final}

    def _obtener(self, uuid):
        doc = {"uuid": uuid, "status_id": self.estado_final}
        if self.folio_emitido:
            doc.update({"folio": self.folio_emitido,
                        "document_pdf_url": f"https://api.wasabil.com/pdf/{self.folio_emitido}",
                        "document_xml_url": f"https://api.wasabil.com/xml/{self.folio_emitido}"})
        return doc


# El fake se instala DENTRO de run(), no al importar: pisar el client a nivel de módulo
# le cambiaría las respuestas a cualquier otra suite de la sesión que importe después.
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


def _crear_venta(db, *, sufijo="A", estado_despacho="en_preparacion",
                 guia_manual=N_TECLEADO, cantidad=10, precio=10000.0):
    """Venta 1 ítem + despacho con N° de guía TECLEADO a mano (el que el folio del SII
    debe pisar). `estado_despacho` 'en_preparacion' para emitir la guía; 'despachado'
    (firmado) para facturar."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} HEPI",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro de aceite motor", cantidad=cantidad,
                        estado_item="despachado")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"WAUD-{sufijo}", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    # Guía EN PAPEL: siempre trae fecha de emisión, y la referencia 52 de la
                    # factura la exige (sin ella se bloquea). Se pone SÓLO cuando hay N° manual
                    # y con una fecha DISTINTA del cierre, para que ninguna suite las confunda.
                    estado=estado_despacho, guia_firmada=1, numero_guia=guia_manual,
                    fecha_guia=(FECHA_GUIA_PAPEL if guia_manual else None),
                    contacto_destinatario="Juan Pérez")
    db.add(desp); db.flush()
    db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id,
                        qty_despachada=cantidad))
    db.commit()
    # Forma que espera armar_lineas: {item_cotizacion_id: {"precio_venta_clp": ...}}
    PRECIOS[it.id] = {"precio_venta_clp": precio}
    return cot, oc, desp, it


def _facturas_de(db, oc_id):
    db.rollback()
    return (db.query(ContFacturaCliente)
            .filter(ContFacturaCliente.oc_cliente_id == oc_id)
            .order_by(ContFacturaCliente.id.asc()).all())


def _dte_guia_de(db, desp_id):
    db.rollback()  # sesión propia del test: descarta su snapshot viejo
    return (db.query(WasabilDte)
            .filter(WasabilDte.despacho_id == desp_id, WasabilDte.tipo_dte == 52).first())


def _limpiar(db):
    db.rollback()
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
            Despacho.oc_cliente_id.in_(oc_ids)).all()]
        if desp_ids:
            db.query(WasabilDte).filter(WasabilDte.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(DespachoItem).filter(DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
    for c in cots:
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == c.id).delete(synchronize_session=False)
    if cot_ids:
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.commit()
    PRECIOS.clear()


def _verificar_limpieza():
    """Sesión NUEVA: que no quede ni una fila marcada (deltas a cero)."""
    db2 = SessionLocal()
    try:
        n = db2.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
        n += db2.query(Despacho).filter(Despacho.numero_despacho.like(f"{MARK}%")).count()
        check("limpieza: 0 filas marcadas al final", n == 0, n)
    finally:
        db2.close()


def run():
    db = SessionLocal()
    # Re-instalar NUESTRO fake al empezar: si pytest importó otras suites que instalan
    # el suyo a nivel de módulo, la última instalación gana (anti-flaky).
    fake.install()
    # Los originales se capturan ACÁ, no al importar: otras suites del módulo pisan
    # estas funciones a nivel de MÓDULO (test_integration lo hace con _precios), y
    # restaurar lo que había antes de esa importación las dejaba sin sus precios.
    orig_precios_cont = cont._precios_de_cotizacion
    orig_precios_router = wasabil_router_mod._precios
    cont._precios_de_cotizacion = _fake_precios_cont
    wasabil_router_mod._precios = lambda db_, cot_: PRECIOS
    _limpiar(db)  # por si una corrida anterior murió a medias
    try:
        CURRENT["empresa"] = "mineria"

        # ═══ 1 · GUÍA 52, ruta A — el POST vuelve EMITIDO sin folio y la emisión
        #        misma lo rescata con obtener_documento ═══════════════════════════
        fake.folio_emitido = "52777"          # el documento completo SÍ lo trae
        _cot, _oc, desp, _it = _crear_venta(db, sufijo="A")
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("1 guía: emitir 200 aunque el POST volvió sin folio", r.status_code == 200, r.text)
        check("1 guía: la emisión RESCATA el folio en el acto (obtener_documento)",
              r.json().get("folio") == "52777", r.text)
        check("1 guía: se envió UN solo documento (no hay doble emisión)",
              len(fake.creados) == creados_antes + 1, len(fake.creados))
        db.rollback()
        check("1 guía: el folio pisa el N° tecleado en despacho.numero_guia",
              db.get(Despacho, desp.id).numero_guia == "52777",
              db.get(Despacho, desp.id).numero_guia)
        _limpiar(db)

        # ═══ 2 · GUÍA 52, ruta B — ni el POST ni el documento completo traen folio ══
        fake.folio_emitido = None
        cot, oc, desp, _it = _crear_venta(db, sufijo="B")
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("2 guía sin folio: emitir 200 (el documento SÍ salió)", r.status_code == 200, r.text)
        check("2 guía sin folio: el DTE queda emitido y SIN folio",
              r.json().get("folio") in (None, ""), r.text)
        check("2 guía sin folio: el serializador dice 'emitido' y NO ofrece reintentar",
              r.json().get("estado") == "emitido"
              and r.json().get("puede_reintentar") is False, r.json())
        db.rollback()
        check("2 guía sin folio: el despacho conserva el N° tecleado (aún no hay folio)",
              db.get(Despacho, desp.id).numero_guia == N_TECLEADO,
              db.get(Despacho, desp.id).numero_guia)

        # El despacho se cierra (flujo real: la guía se emite en preparación y el cierre
        # lo pasa a 'despachado', el único estado facturable), así el ÚNICO bloqueo
        # posible de la 33 de más abajo es la guía sin folio.
        d = db.get(Despacho, desp.id)
        d.estado = "despachado"
        db.commit()

        # CINTURÓN: con la guía emitida SIN folio, la factura 33 NO puede salir
        # referenciando el N° tecleado a mano.
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("2 factura 33: la guía emitida SIN folio cuenta como EN PROCESO y BLOQUEA",
              p["puede_emitir"] is False
              and any("EN PROCESO" in x for x in p["problemas"]), p["problemas"])
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("2 factura 33: emitir 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("2 factura 33: tampoco quedó factura local zombi",
              len(_facturas_de(db, oc.id)) == 0, _facturas_de(db, oc.id))

        # DESATASCO: Wasabil ya asignó el folio → el sondeo lo rescata solo, sin
        # re-emitir nada (antes el sondeo frenaba en status 3 y esto era permanente).
        fake.folio_emitido = "52999"
        creados_antes = len(fake.creados)
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        check("2 guía sin folio: el SONDEO re-consulta y rescata el folio",
              r.status_code == 200 and r.json().get("folio") == "52999", r.text)
        check("2 guía sin folio: el sondeo NO re-emite (0 documentos nuevos)",
              len(fake.creados) == creados_antes, len(fake.creados))
        db.rollback()
        check("2 guía sin folio: el folio rescatado llega a despacho.numero_guia",
              db.get(Despacho, desp.id).numero_guia == "52999",
              db.get(Despacho, desp.id).numero_guia)

        # Y recién ahí la factura 33 se destraba, referenciando el FOLIO REAL.
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        ref52 = [x for x in p["referencias"] if x["tipo"] == "52"]
        check("2 factura 33: con el folio rescatado ya puede emitir",
              p["puede_emitir"] is True, p["problemas"])
        check("2 factura 33: referencia 52 con el FOLIO REAL, no el N° tecleado",
              len(ref52) == 1 and ref52[0]["folio"] == "52999", p["referencias"])
        _limpiar(db)

        # ═══ 3 · DIRECCIONALIDAD que NO cambia (control anti sobre-bloqueo) ════════
        # (a) guía EMITIDA CON folio → no bloquea (el caso feliz de siempre)
        cot, oc, desp, _it = _crear_venta(db, sufijo="C", estado_despacho="despachado",
                                          guia_manual="770077")
        db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp.id,
                          uuid="uuid-guia-ok", status_id=STATUS_EMITIDO, folio="777",
                          payload_json='{"documentDate": "2026-06-20"}'))
        db.commit()
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3a control: guía emitida CON folio no bloquea y referencia el folio",
              p["puede_emitir"] is True
              and [x["folio"] for x in p["referencias"] if x["tipo"] == "52"] == ["777"],
              (p["problemas"], p["referencias"]))
        _limpiar(db)

        # (b) guía FALLIDA que NUNCA llegó a Wasabil (sin uuid ni claim) → no bloquea:
        #     ahí no hay guía electrónica y el N° manual es la referencia legítima…
        #     …SIEMPRE QUE sea un folio del SII. CORREGIDO EN ESTA RONDA: este check fijaba
        #     'G-MANUAL-9' como correcto, y así ninguna prueba veía que un N° tecleado a
        #     mano sin forma de folio viajara como FolioRef de un DTE 33 REAL (el SII
        #     rechaza ese documento y el rechazo llega con el folio propio ya consumido).
        cot, oc, desp, _it = _crear_venta(db, sufijo="D", estado_despacho="despachado",
                                          guia_manual="900123")
        db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp.id,
                          status_id=STATUS_FALLIDO, error="rechazada"))
        db.commit()
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3b control: guía FALLIDA sin uuid sigue SIN bloquear (folio manual legítimo)",
              p["puede_emitir"] is True
              and [x["folio"] for x in p["referencias"] if x["tipo"] == "52"] == ["900123"],
              (p["problemas"], p["referencias"]))
        # …y el MISMO caso con el N° tecleado sin forma de folio SÍ bloquea
        db.rollback()
        db.get(Despacho, desp.id).numero_guia = "G-MANUAL-9"
        db.commit()
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("3b bis: un N° de guía NO numérico bloquea la 33 (no viaja como FolioRef)",
              p["puede_emitir"] is False
              and any("no es un folio numérico" in x for x in p["problemas"])
              and [x for x in p["referencias"] if x["tipo"] == "52"] == [],
              (p["problemas"], p["referencias"]))
        _limpiar(db)

        # ═══ 4 · FACTURA 33 — el MISMO hueco: emitida sin folio ════════════════════
        # 4a) el documento completo trae el folio → la emisión lo escribe ya
        fake.folio_emitido = "F5001"
        cot, oc, desp, _it = _crear_venta(db, sufijo="E", estado_despacho="despachado",
                                          guia_manual="800101")
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("4a factura: emitir 200 con el POST sin folio", r.status_code == 200, r.text)
        check("4a factura: la emisión rescata el folio en el acto",
              r.json().get("folio") == "F5001", r.text)
        facturas = _facturas_de(db, oc.id)
        check("4a factura: el folio rescatado se escribe en numero_factura",
              len(facturas) == 1 and (facturas[0].numero_factura or "") == "F5001",
              [(f.id, f.numero_factura) for f in facturas])
        _limpiar(db)

        # 4b) tampoco hay folio al emitir → el SONDEO lo rescata (antes la factura
        #     quedaba sin numero_factura para siempre y el adelanto diferido sin aplicar)
        fake.folio_emitido = None
        cot, oc, desp, _it = _crear_venta(db, sufijo="F", estado_despacho="despachado",
                                          guia_manual="800101")
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("4b factura sin folio: emitir 200 (el documento salió)", r.status_code == 200, r.text)
        factura_id = r.json().get("factura_id")
        check("4b factura sin folio: la respuesta trae factura_id (serializador)",
              factura_id is not None, r.json())
        f0 = [f for f in _facturas_de(db, oc.id) if f.id == factura_id][0]
        check("4b factura sin folio: numero_factura sigue vacío",
              not (f0.numero_factura or "").strip(), f0.numero_factura)
        r = client.post(f"{FACTURAS}/{factura_id}/reintentar")
        check("4b factura sin folio: /reintentar responde 409 hablando de FACTURA, "
              "no de despacho",
              r.status_code == 409 and "factura" in r.json()["detail"].lower()
              and "despacho" not in r.json()["detail"].lower(), r.text)

        fake.folio_emitido = "F5002"          # Wasabil ya asignó el folio
        creados_antes = len(fake.creados)
        r = client.get(f"{FACTURAS}/{factura_id}/estado")
        check("4b factura sin folio: el SONDEO re-consulta y rescata el folio",
              r.status_code == 200 and r.json().get("folio") == "F5002", r.text)
        check("4b factura sin folio: el sondeo NO re-emite (0 documentos nuevos)",
              len(fake.creados) == creados_antes, len(fake.creados))
        f0 = [f for f in _facturas_de(db, oc.id) if f.id == factura_id][0]
        check("4b factura sin folio: el folio rescatado llega a numero_factura",
              (f0.numero_factura or "") == "F5002", f0.numero_factura)
        _limpiar(db)

        # ═══ 5 · MODO `items` SIN despacho_id — la 52 se omitía EN SILENCIO ════════
        # La mercadería SÍ está despachada (_construir_factura lo exige por ítem), pero
        # sin despacho_id no hay guía identificable: antes la factura salía al SII con la
        # sola referencia 801, es decir mercadería trasladada sin el documento que la
        # ampara, y nadie se enteraba (`problemas` venía vacío).
        fake.folio_emitido = "F6001"
        cot, oc, desp, it = _crear_venta(db, sufijo="G", estado_despacho="despachado",
                                         guia_manual="800102")
        di = db.query(DespachoItem).filter(DespachoItem.despacho_id == desp.id).first()
        payload_items = {"oc_cliente_id": oc.id,
                         "items": [{"item_cotizacion_id": it.id, "despacho_item_id": di.id,
                                    "cantidad": 2}]}
        creados_antes = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview", json=payload_items).json()
        check("5 items sin despacho_id: el preview BLOQUEA (no se puede armar la 52)",
              p["puede_emitir"] is False
              and any("tipo 52" in x for x in p["problemas"]), p["problemas"])
        check("5 items sin despacho_id: el preview NO inventa una referencia 52",
              [x["tipo"] for x in p["referencias"]] == ["801"], p["referencias"])
        r = client.post(f"{FACTURAS}/emitir", json=payload_items)
        check("5 items sin despacho_id: emitir 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("5 items sin despacho_id: tampoco quedó factura local zombi",
              len(_facturas_de(db, oc.id)) == 0, _facturas_de(db, oc.id))
        # Control: la MISMA venta, facturada DESDE la guía, sí puede emitir con su 52.
        p = client.post(f"{FACTURAS}/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id}).json()
        check("5 control: desde la guía sí puede emitir y lleva 801 + 52",
              p["puede_emitir"] is True
              and [x["tipo"] for x in p["referencias"]] == ["801", "52"],
              (p["problemas"], p["referencias"]))
        _limpiar(db)

        # ═══ 6 · ANCLA INTERNA: sin N° de despacho no hay dónde reencontrar el doc ══
        # El invoiceReference de la guía es el N° de despacho: sin él, el reintento no
        # puede verificar en Wasabil si el documento ya existe. Y el preview tiene que
        # decirlo, no reventar en 500 al armar el documento.
        cot, oc, desp, _it = _crear_venta(db, sufijo="H")
        d = db.get(Despacho, desp.id)
        d.numero_despacho = ""     # el MARK se pierde: se limpia por la OC/cotización
        db.commit()
        r = client.post(f"{GUIAS}/{desp.id}/preview")
        check("6 despacho sin N° interno: preview 200 (no 500) y BLOQUEA con el motivo",
              r.status_code == 200 and r.json()["puede_emitir"] is False
              and any("ancla" in x for x in r.json()["problemas"]), r.text)
        check("6 despacho sin N° interno: el preview NO arma documento",
              r.json().get("documento") is None, r.json().get("documento"))
        creados_antes = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("6 despacho sin N° interno: emitir 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        # N° de OC sobre el límite del SII: mismo trato (bloquea sin reventar el armado)
        d = db.get(Despacho, desp.id)
        d.numero_despacho = f"{MARK}-DSP-{oc.id}"
        db.get(OcCliente, oc.id).numero_oc = "OC-DEMASIADO-LARGA-123456"
        db.commit()
        r = client.post(f"{GUIAS}/{desp.id}/preview")
        check("6 N° de OC de más de 18 chars: preview 200 (no 500) y bloquea",
              r.status_code == 200 and r.json()["puede_emitir"] is False
              and r.json().get("documento") is None, r.text)
        _limpiar(db)

        # ═══ 7 · EL REINTENTO re-valida la guía DESDE LA FACTURA PERSISTIDA ════════
        # La ventana: la factura se creó con la guía manual (el preview la aprobó) y
        # DESPUÉS alguien emitió la guía electrónica del despacho. El reintento arma el
        # documento desde la factura congelada, así que ES ESE camino el que tiene que
        # frenar — si no, la RE-emisión sale al SII sin referencia 52 (o con el N° viejo).
        fake.status_respuesta = STATUS_FALLIDO      # el SII rechaza: queda reintentable
        fake.estado_final = STATUS_FALLIDO
        cot, oc, desp, _it = _crear_venta(db, sufijo="I", estado_despacho="despachado",
                                          guia_manual="800103")
        r = client.post(f"{FACTURAS}/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("7 factura rechazada por el SII → queda reintentable",
              r.status_code == 200 and r.json()["estado"] == "fallido"
              and r.json()["puede_reintentar"] is True, r.text)
        fac_id = r.json()["factura_id"]
        # Ahora la guía electrónica del despacho entra EN PROCESO (folio en camino)
        db.rollback()
        db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp.id,
                          uuid="uuid-guia-proceso", status_id=2))
        db.commit()
        creados_antes = len(fake.creados)
        r = client.post(f"{FACTURAS}/{fac_id}/reintentar")
        check("7 reintentar con la guía EN PROCESO → 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        check("7 el 409 del reintento nombra el motivo real (la guía, no otra cosa)",
              "EN PROCESO" in r.json().get("detail", ""), r.text)

        # Y el claim de _reclamar_emision_factura habla de la FACTURA, no del despacho
        # (es una ventana de carrera: se ejercita la función directo).
        from fastapi import HTTPException  # noqa: E402  (local: solo para esta sonda)
        from wasabil_dte.router import _reclamar_emision_factura  # noqa: E402
        db.rollback()
        dte_fac = (db.query(WasabilDte)
                   .filter(WasabilDte.factura_id == fac_id,
                           WasabilDte.tipo_dte == 33).first())
        dte_fac.en_vuelo_desde = datetime.utcnow()      # claim FRESCO de otra pestaña
        db.commit()
        db_claim = SessionLocal()
        try:
            _reclamar_emision_factura(db_claim, fac_id, para_reintento=True,
                                      usuario_id=None, empresa="mineria")
            check("7 claim fresco: _reclamar_emision_factura bloquea", False, "no lanzó")
        except HTTPException as e:
            check("7 claim fresco: _reclamar_emision_factura bloquea con 409",
                  e.status_code == 409, e.detail)
            check("7 el 409 del claim habla de la FACTURA, no del despacho",
                  "factura" in str(e.detail).lower()
                  and "despacho" not in str(e.detail).lower(), e.detail)
        finally:
            db_claim.close()
        _limpiar(db)

    finally:
        cont._precios_de_cotizacion = orig_precios_cont
        wasabil_router_mod._precios = orig_precios_router
        fake.status_respuesta = 3
        fake.folio_emitido = None
        _limpiar(db)
        db.close()
        _verificar_limpieza()

    print()
    if _fails:
        print(f"FALLARON {len(_fails)}: {_fails}")
        sys.exit(1)
    print("=== EMITIDO SIN FOLIO: TODO OK ===")


def test_wasabil_dte_emitido_sin_folio(): run()


if __name__ == "__main__":
    run()
