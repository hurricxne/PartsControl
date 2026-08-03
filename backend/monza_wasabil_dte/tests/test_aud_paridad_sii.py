"""Re-auditoría MEDIO-5 (paridad SII MonzaParts) — los cuatro arreglos que el commit de
paridad NO portó, con el escenario REAL adverso y no el cómodo.

Contexto: el informe de re-auditoría midió, llamando a las funciones reales de MonzaParts,
que en esta marca el callejón «emitida SIN folio y SIN uuid» NO bloqueaba la factura 33
(S8a), que la guía AMBIGUA tampoco bloqueaba (S8b) y que no existía el chequeo de líneas
de otra guía (S8c). La severidad estaba contenida solo porque MonzaParts todavía no hizo
su primera emisión real: una bomba con la espoleta puesta.

REGLA RECTORA de esta suite (y del código que prueba): ante un documento tributario
IRREVERSIBLE, cuando el estado remoto es ambiguo o contradictorio hay que BLOQUEAR Y PEDIR
INTERVENCIÓN HUMANA. Nunca «recuperar con astucia», nunca fusionar el estado remoto de
forma que DEGRADE una emisión ya confirmada, nunca seguir adelante esperando que salga
bien. Un 409 que obliga a un humano a mirar es infinitamente más barato que una nota de
crédito.

POR QUÉ EL FAKE DE ESTA SUITE ES DISTINTO (esto es lo que las suites anteriores no podían
ver): el fake compartido responde a CUALQUIER referencia con UN ÚNICO documento emitido, y
`obtener_documento` devuelve siempre el mismo folio. Pero el estado NORMAL después de un
reintento son DOS documentos con la MISMA referencia (el rechazado viejo + el nuevo), y
cada uno tiene su propio folio y su propio status. `FakeAdverso` modela eso: registro por
uuid, lista de búsqueda con varios documentos, POST que puede volver SIN uuid, y respuestas
que CONTRADICEN lo ya confirmado.

Cero introspección de texto: todo por HTTP contra los routers reales o llamando a la
función real con filas reales. Wasabil SIMULADO por monkeypatch del client MONZA — jamás el
API real: `issue` nunca sale del proceso y no se toca ningún documento del dueño.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_aud_paridad_sii.py -q
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import SessionLocal  # noqa: E402
from monza_models import MonzaDespacho, MonzaDespachoItem  # noqa: E402
from monza_contabilidad.models import MonzaContFacturaClienteItem  # noqa: E402
from monza_wasabil_dte import client as monza_client  # noqa: E402
from monza_wasabil_dte import router as mwr  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    CLAIM_TTL_SEGUNDOS, MonzaWasabilDte, STATUS_EMITIDO, STATUS_FALLIDO,
    STATUS_PROCESANDO,
)
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    Checker, FakeWasabil, crear_venta, despacho_extra, dte_de_factura, dte_guia,
    facturas_de, limpiar, montar_app, verificar_limpieza,
)

# MARK corto A PROPÓSITO: MonzaCotizacion.numero es String(20) y el número de prueba es
# f"{MARK}-COT-{n}".
MARK = "__MWPAR__"
CURRENT = {"empresa": "automotriz", "id": None}

GUIAS = "/api/monza/wasabil/despachos"
FACTURAS = "/api/monza/wasabil/facturas"

client = montar_app(CURRENT)
check = Checker()


class FakeAdverso(FakeWasabil):
    """Wasabil simulado que sabe reproducir los estados REALES adversos.

    Diferencias con el fake compartido, todas necesarias para ver los daños:
      · `docs_por_uuid`: cada documento tiene SU folio y SU status, así que adoptar el
        documento equivocado se nota (el fake común devuelve siempre el mismo folio y
        cualquier elección parece correcta).
      · `sin_uuid_en_post`: el POST responde `{"status_id": 3}` PELADO. Es la variante del
        callejón que no tiene a quién consultar por id y obliga al rescate por referencia.
      · `docs_buscables` con VARIOS documentos de la misma `invoice_reference` — el estado
        normal tras un reintento.
      · `estado_por_uuid`: `estado_documento` puede CONTRADECIR lo ya confirmado (Wasabil
        respondiendo 4 sobre un documento que el POST dio por emitido).
    """

    def __init__(self, mark: str):
        super().__init__(mark)
        self.docs_por_uuid: dict = {}
        self.estado_por_uuid: dict = {}
        self.sin_uuid_en_post = False

    def install(self):
        super().install()
        monza_client.obtener_documento = self._obtener
        monza_client.estado_documento = self._estado
        monza_client.crear_documento = self._crear

    def registrar(self, uuid, *, status_id, folio=None, referencia=None):
        """Documento existente en Wasabil (el que devuelve obtener_documento)."""
        doc = {"uuid": uuid, "status_id": status_id, "folio": folio,
               "invoice_reference": referencia,
               "document_pdf_url": f"https://api.wasabil.com/pdf/{folio or 'x'}",
               "document_xml_url": f"https://api.wasabil.com/xml/{folio or 'x'}"}
        self.docs_por_uuid[uuid] = doc
        return doc

    def buscable(self, uuid, *, status_id, folio=None, referencia):
        """Lo mismo, pero además aparece en la LISTA de búsqueda por referencia."""
        doc = self.registrar(uuid, status_id=status_id, folio=folio, referencia=referencia)
        # La lista de Wasabil trae la ficha resumida: el folio del documento COMPLETO no
        # viene garantizado en el ítem de la lista.
        self.docs_buscables.append({"uuid": uuid, "status_id": status_id,
                                    "invoice_reference": referencia})
        return doc

    def _crear(self, payload):
        if self.antes_de_crear:
            self.antes_de_crear(payload)
        if self.crear_falla:
            raise self.crear_falla
        self.creados.append(payload)
        if self.sin_uuid_en_post:
            return {"status_id": self.status_respuesta}
        return {"uuid": f"uuid-f{len(self.creados)}", "status_id": self.status_respuesta}

    def _obtener(self, uuid):
        if uuid in self.docs_por_uuid:
            return dict(self.docs_por_uuid[uuid])
        return {"uuid": uuid, "status_id": self.estado_final, "folio": self.folio_emitido}

    def _estado(self, uuid):
        if uuid in self.estado_por_uuid:
            return dict(self.estado_por_uuid[uuid])
        return {"uuid": uuid, "status_id": self.estado_final,
                "display_error": self.display_error}

    def reset(self):
        self.docs_por_uuid = {}
        self.estado_por_uuid = {}
        self.docs_buscables = []
        self.sin_uuid_en_post = False
        self.busqueda_completa = True
        self.status_respuesta = 2
        self.estado_final = STATUS_EMITIDO
        self.folio_emitido = "9001"
        self.crear_falla = None


fake = FakeAdverso(MARK)
fake.install()


def _dte_de(db, desp_id):
    """Fila DTE 52 del despacho, releída fresca (el test escribe por HTTP)."""
    db.rollback()  # sesión propia del test: descarta su snapshot viejo
    return (db.query(MonzaWasabilDte)
            .filter(MonzaWasabilDte.despacho_id == desp_id,
                    MonzaWasabilDte.tipo_dte == 52).first())


def _items_de(db, desp_id):
    return (db.query(MonzaDespachoItem)
            .filter(MonzaDespachoItem.despacho_id == desp_id)
            .order_by(MonzaDespachoItem.id.asc()).all())


def _ref52(preview) -> list:
    return [x["folio"] for x in preview.get("referencias", []) if x["tipo"] == "52"]


def _vencido():
    """`en_vuelo_desde` de un claim YA EXPIRADO (el POST salió y nadie confirmó)."""
    return datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)


def _cerrar(db, desp):
    """El despacho pasa a 'despachado' (único estado facturable), como el cierre real."""
    d = db.get(MonzaDespacho, desp.id)
    d.estado = "despachado"
    db.commit()


def run():
    db = SessionLocal()
    fake.install()   # anti-flaky: la última instalación a nivel de módulo gana
    fake.reset()
    limpiar(db, MARK)
    try:
        CURRENT["empresa"] = "automotriz"

        # ═══ A · S8a — el callejón «EMITIDA sin folio y SIN uuid» BLOQUEA la 33 ══════
        # Estado: Wasabil respondió `{"status_id": 3}` pelado. El documento EXISTE ante el
        # SII, su folio no llegó y no hay uuid con el que preguntar. El guard viejo
        # (`bool(uuid) or claim_vigente`) devolvía False y la 33 salía citando el N°
        # TECLEADO A MANO — un folio 52 que el SII no reconoce, irreversible.
        fake.reset()
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="8001")
        dte_guia(db, desp, status_id=STATUS_EMITIDO, folio=None, uuid=None,
                 en_vuelo_desde=None)
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        check("A1 emitida sin folio y sin uuid: la 33 NO puede emitir",
              p["puede_emitir"] is False and any("EN PROCESO" in x for x in p["problemas"]),
              p["problemas"])
        check("A2 y NO arma una referencia 52 con el N° tecleado a mano",
              _ref52(p) == [], p.get("referencias"))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id})
        check("A3 emitir la 33: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados, r.text)
        db.rollback()
        check("A4 no quedó factura local zombi", len(facturas_de(db, cot.id)) == 0,
              facturas_de(db, cot.id))
        limpiar(db, MARK)

        # ═══ B · S8b — la guía AMBIGUA bloquea, y se evalúa ANTES del corte por FALLIDO ═
        # Estado: `en_vuelo_desde` puesto (el POST salió), uuid NULL (nadie confirmó) y
        # status 4. Con el orden viejo (…→ FALLIDO → en_vuelo) el `status == 4` disparaba
        # primero y la guía ambigua NO bloqueaba — y ese es justo el estado que dejan los
        # caminos de rescate. El documento PUDO nacer con folio real.
        fake.reset()
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="8055")
        dte_guia(db, desp, status_id=STATUS_FALLIDO, uuid=None, folio=None,
                 en_vuelo_desde=_vencido())
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        check("B1 guía AMBIGUA: la 33 NO puede emitir",
              p["puede_emitir"] is False
              and any("no se pudo confirmar" in x.lower() for x in p["problemas"]),
              p["problemas"])
        check("B2 y NO cita el N° tecleado a mano", _ref52(p) == [], p.get("referencias"))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id})
        check("B3 emitir la 33 con guía ambigua: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados, r.text)
        limpiar(db, MARK)

        # ─── B4 · CORREGIDO en la ronda de re-refutación (MEDIO-5) ──────────────────
        # ESTE CHECK FIJABA EL INVARIANTE EQUIVOCADO. Decía «CONTROL anti sobre-bloqueo:
        # rechazo CONFIRMADO (status 4 CON uuid) NO bloquea» y exigía `_ref52 == ["9345"]`,
        # o sea: exigía como CORRECTO que la 33 saliera al SII citando el N° de guía
        # TECLEADO A MANO. Por qué está mal:
        #   · El rechazo es de ESE documento (el del uuid). No prueba que Wasabil no
        #     conserve OTRO EMITIDO con la MISMA referencia — que es el estado normal tras
        #     un reintento y justo el que el CRÍTICO-2 explota.
        #   · En el flujo guía-primero de Monza, el `numero_guia` que sobrevive a un intento
        #     electrónico es el VIEJO tecleado a mano: el que la emisión iba a pisar.
        # El "sobre-bloqueo" que este control temía NO existe: el despacho igual se puede
        # facturar, resolviendo primero la guía (Reintentar, que ahora cruza el cinturón por
        # referencia) o registrando el folio de la guía de papel. Y los DOS controles anti
        # sobre-bloqueo que sí valen siguen verdes: B5 (nunca llegó a Wasabil) y E4 (guía en
        # papel de un despacho SIN intento electrónico).
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9345")
        dte_guia(db, desp, status_id=STATUS_FALLIDO, uuid="uuid-rechazo-confirmado",
                 folio=None, en_vuelo_desde=None, error="rechazada por el SII")
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        check("B4 rechazo CON uuid (el documento EXISTE en Wasabil): la 33 NO puede emitir",
              p["puede_emitir"] is False
              and any("ya NO se acepta como referencia 52" in x for x in p["problemas"]),
              p["problemas"])
        check("B4-bis y NO cita el N° tecleado a mano ('9345')",
              _ref52(p) == [], p.get("referencias"))
        # Hermano PELIGROSO del control (lo que el invariante viejo permitía de verdad):
        # emitir la 33 en ese estado. Antes salía al SII con FolioRef 9345.
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id})
        check("B4-ter emitir la 33 en ese estado: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados, r.text)
        db.rollback()
        check("B4-quater y no quedó factura local zombi",
              len(facturas_de(db, cot.id)) == 0, facturas_de(db, cot.id))
        limpiar(db, MARK)

        # CONTROL anti sobre-bloqueo 2: el intento que nunca llegó a Wasabil (sin uuid y
        # con el claim ya liberado) tampoco bloquea: no hay guía electrónica.
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9346")
        dte_guia(db, desp, status_id=STATUS_FALLIDO, uuid=None, folio=None,
                 en_vuelo_desde=None, error="no llegó a Wasabil")
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        check("B5 CONTROL: intento que nunca llegó a Wasabil NO bloquea",
              p["puede_emitir"] is True and _ref52(p) == ["9346"],
              (p["problemas"], p.get("referencias")))
        limpiar(db, MARK)

        # ═══ C · S8c — la 52 no puede amparar líneas de OTRA guía ═══════════════════
        # C-a) CONTRADICCIÓN entre la guía ELEGIDA y la que sale de las líneas. En Monza el
        #      snapshot de guía se DERIVA de las líneas (monza_contabilidad: snap_desp_id),
        #      así que elegir la guía A y mandar líneas de la B emitía una 33 citando la B
        #      EN SILENCIO: el operador aprobó otra cosa en el preview.
        fake.reset()
        cot, despA, i1, _i2 = crear_venta(db, MARK, numero_guia_manual="7001")
        despB = despacho_extra(db, MARK, cot, {i1.id: 6}, numero_guia="7002")
        di_a = [x for x in _items_de(db, despA.id) if x.item_id == i1.id][0]
        di_b = _items_de(db, despB.id)[0]
        creados = len(fake.creados)
        p = client.post(f"{FACTURAS}/preview", json={
            "cotizacion_id": cot.id, "despacho_id": despA.id,
            "items": [{"item_cotizacion_id": i1.id, "despacho_item_id": di_b.id,
                       "cantidad": 6}]}).json()
        check("C1 elegir la guía A y mandar líneas de la B: BLOQUEA",
              p["puede_emitir"] is False
              and any("NO es la que sale de las líneas" in x for x in p["problemas"]),
              p["problemas"])
        r = client.post(f"{FACTURAS}/emitir", json={
            "cotizacion_id": cot.id, "despacho_id": despA.id,
            "items": [{"item_cotizacion_id": i1.id, "despacho_item_id": di_b.id,
                       "cantidad": 6}]})
        check("C2 emitir así: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados, r.text)

        # C-b) El agujero que deja el guard «solo los declarados»: `despacho_item_id` es
        #      OPCIONAL, así que basta OMITIRLO para vaciarlo. El tope por ÍTEM suma TODOS
        #      los despachos de la venta (4 en A + 6 en B = 10), así que 6 unidades que
        #      salieron en B se facturan citando la 52 de A.
        p = client.post(f"{FACTURAS}/preview", json={
            "cotizacion_id": cot.id, "despacho_id": despA.id,
            "items": [{"item_cotizacion_id": i1.id, "despacho_item_id": di_a.id,
                       "cantidad": 4},
                      {"item_cotizacion_id": i1.id, "cantidad": 6}]}).json()
        check("C3 línea de mercadería sin declarar su guía: BLOQUEA",
              p["puede_emitir"] is False
              and any("no declaran de qué guía salieron" in x for x in p["problemas"]),
              p["problemas"])
        r = client.post(f"{FACTURAS}/emitir", json={
            "cotizacion_id": cot.id, "despacho_id": despA.id,
            "items": [{"item_cotizacion_id": i1.id, "despacho_item_id": di_a.id,
                       "cantidad": 4},
                      {"item_cotizacion_id": i1.id, "cantidad": 6}]})
        check("C4 emitir así: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados, r.text)
        db.rollback()
        check("C5 y no quedó factura local zombi", len(facturas_de(db, cot.id)) == 0,
              facturas_de(db, cot.id))

        # C-c) CONTROL anti sobre-bloqueo: la vía normal (todas las líneas declaradas y de
        #      la guía elegida) sigue emitiendo, y cita el folio de SU guía.
        fake.status_respuesta = STATUS_EMITIDO
        fake.folio_emitido = "52100"
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": despB.id}).json()
        check("C6 CONTROL: facturar la guía B completa SÍ puede emitir y cita su 52",
              p["puede_emitir"] is True and _ref52(p) == ["7002"],
              (p["problemas"], p.get("referencias")))

        # C-d) CINTURÓN DEL REINTENTO sobre la factura ya congelada: una factura creada
        #      antes de este guard (o por una vía que no lo aplicó) no puede RE-emitirse
        #      citando una guía que no la ampara. Se fabrica el estado adverso mutando la
        #      línea persistida, que es lo que dejaría ese legado.
        r = client.post(f"{FACTURAS}/emitir",
                        json={"cotizacion_id": cot.id, "despacho_id": despB.id})
        check("C7 la factura de la guía B se emite (montaje del cinturón)",
              r.status_code == 200, r.text)
        factura_id = r.json()["factura_id"]
        db.rollback()
        linea = (db.query(MonzaContFacturaClienteItem)
                 .filter(MonzaContFacturaClienteItem.factura_id == factura_id,
                         MonzaContFacturaClienteItem.item_cotizacion_id.isnot(None))
                 .first())
        linea.despacho_item_id = di_a.id          # ← la línea pasa a ser de la guía A
        dte_f = dte_de_factura(db, factura_id)
        dte_f.status_id = STATUS_FALLIDO          # el SII la rechazó: hay que reintentar
        dte_f.folio = None
        dte_f.uuid = None
        dte_f.en_vuelo_desde = None
        db.commit()
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/{factura_id}/reintentar")
        check("C8 reintentar con una línea de OTRA guía: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados
              and "OTRA guía" in r.text, r.text)
        limpiar(db, MARK)

        # ═══ D1 · el rescate por referencia PREFIERE el emitido (y no degrada) ═══════
        # Estado REAL después de un reintento: DOS documentos con la MISMA referencia — el
        # rechazado viejo (Wasabil reusa la referencia) y el nuevo emitido. Quedarse con
        # «el primero de la lista» adopta el VIEJO: la fila queda status 4 sin folio y el
        # documento que el SII SÍ aceptó pierde su folio para siempre.
        fake.reset()
        fake.status_respuesta = STATUS_EMITIDO
        fake.sin_uuid_en_post = True     # el POST vuelve `{"status_id": 3}` pelado
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion",
                                          numero_guia_manual="8101")
        db.refresh(desp)
        ref = desp.numero
        fake.buscable("uuid-viejo-rechazado", status_id=STATUS_FALLIDO, referencia=ref)
        fake.buscable("uuid-nuevo-emitido", status_id=STATUS_EMITIDO, folio="52999",
                      referencia=ref)
        # Ruido: otro documento emitido de OTRA referencia (el match tiene que ser exacto)
        fake.buscable("uuid-de-otro-despacho", status_id=STATUS_EMITIDO, folio="52111",
                      referencia=ref + "9")
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("D1a emitir 200 aunque el POST volvió sin uuid", r.status_code == 200, r.text)
        check("D1b el rescate por referencia trae el folio del EMITIDO",
              r.json().get("folio") == "52999", r.text)
        d = _dte_de(db, desp.id)
        check("D1c la fila NO queda degradada a fallida",
              d.status_id == STATUS_EMITIDO and d.folio == "52999",
              (d.status_id, d.folio))
        check("D1d y adopta el uuid del documento correcto",
              d.uuid == "uuid-nuevo-emitido", d.uuid)
        check("D1e el folio real pisa el N° tecleado en el despacho",
              db.get(MonzaDespacho, desp.id).numero_guia == "52999",
              db.get(MonzaDespacho, desp.id).numero_guia)
        limpiar(db, MARK)

        # ═══ D2 · DOS documentos EMITIDOS con la misma referencia → ABORTA y BLOQUEA ══
        # Ya hay una doble emisión real ante el SII. Elegir uno sería inventar una verdad:
        # se bloquea y un humano mira. Lo que NO puede pasar es que la 33 salga citando el
        # folio que ganó el sorteo.
        fake.reset()
        fake.status_respuesta = STATUS_EMITIDO
        fake.sin_uuid_en_post = True
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion",
                                          numero_guia_manual="8102")
        db.refresh(desp)
        ref = desp.numero
        fake.buscable("uuid-emitido-1", status_id=STATUS_EMITIDO, folio="53001",
                      referencia=ref)
        fake.buscable("uuid-emitido-2", status_id=STATUS_EMITIDO, folio="53002",
                      referencia=ref)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        d = _dte_de(db, desp.id)
        check("D2a con DOS emitidos el rescate ABORTA: la fila queda sin folio",
              r.status_code == 200 and (d.folio or "") == "", (r.text, d.folio))
        check("D2b y no se inventó un uuid ganador",
              d.uuid in (None, ""), d.uuid)
        _cerrar(db, desp)
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        check("D2c la 33 queda BLOQUEADA (no cita ningún folio)",
              p["puede_emitir"] is False and _ref52(p) == [], (p["problemas"], p))
        creados = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("D2d reintentar la guía: 409 con el remedio humano y CERO re-emisión",
              r.status_code == 409 and len(fake.creados) == creados
              and "NO se re-emite" in r.text, (r.status_code, r.text))
        limpiar(db, MARK)

        # ═══ D3 · PISO del status 3: un emitido confirmado no baja a fallido ═════════
        # Camino real: la fila queda «emitida sin folio», el sondeo re-consulta y Wasabil
        # responde 4 (transitorio, o el documento equivocado). Sin piso, la fila se marca
        # FALLIDA, el botón Reintentar se habilita y sale una SEGUNDA guía REAL al SII.
        fake.reset()
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion",
                                          numero_guia_manual="8103")
        dte_guia(db, desp, status_id=STATUS_EMITIDO, folio=None, uuid="uuid-emitido-d3",
                 en_vuelo_desde=None)
        # El documento COMPLETO existe y sigue sin folio asignado (el SII no lo devolvió
        # todavía), pero el ESTADO contradice: responde 4. Las dos respuestas son de
        # Wasabil y se contradicen: hay que quedarse con lo confirmado y avisar.
        fake.registrar("uuid-emitido-d3", status_id=STATUS_EMITIDO, folio=None)
        fake.estado_por_uuid["uuid-emitido-d3"] = {
            "uuid": "uuid-emitido-d3", "status_id": STATUS_FALLIDO,
            "display_error": "rechazado (respuesta contradictoria)"}
        creados = len(fake.creados)
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        d = _dte_de(db, desp.id)
        check("D3a el sondeo NO degrada el status 3 confirmado",
              d.status_id == STATUS_EMITIDO, (r.text, d.status_id))
        check("D3b y deja constancia del CONFLICTO para que un humano lo mire",
              (d.error or "").startswith("CONFLICTO"), d.error)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("D3c reintentar sigue BLOQUEADO: cero documentos nuevos al SII",
              len(fake.creados) == creados and r.status_code == 409
              and "NO se re-emite" in r.text,
              (r.status_code, r.text, len(fake.creados)))
        limpiar(db, MARK)

        # ═══ D4 · la fusión no degrada el uuid del POST ══════════════════════════════
        # `{**data, **completo}` con un documento completo que responde `uuid: None` BORRA
        # el uuid que sí trajo el POST: el rescate CREA el callejón que existe para evitar
        # (sin uuid el sondeo no puede curar solo).
        fake.reset()
        fake.status_respuesta = STATUS_EMITIDO
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion",
                                          numero_guia_manual="8104")
        # El POST devuelve uuid-f<N> (predecible: el fake numera por orden de creación) y
        # el documento COMPLETO de ese uuid responde `uuid: None, folio: None` — la forma
        # exacta que borraba el uuid con el merge plano. `docs_buscables` vacío a
        # propósito: si el rescate por referencia curara, no se vería la degradación.
        uuid_post = f"uuid-f{len(fake.creados) + 1}"
        fake.docs_por_uuid[uuid_post] = {"uuid": None, "status_id": STATUS_EMITIDO,
                                         "folio": None}
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        d = _dte_de(db, desp.id)
        check("D4a el uuid del POST SOBREVIVE al rescate", d.uuid == uuid_post,
              (r.text, d.uuid, uuid_post))
        check("D4a-bis y la fila queda emitida SIN folio (no se inventó ninguno)",
              d.status_id == STATUS_EMITIDO and (d.folio or "") == "",
              (d.status_id, d.folio))
        # Con el uuid vivo, el sondeo se cura solo cuando Wasabil asigna el folio.
        fake.docs_por_uuid[uuid_post] = {"uuid": uuid_post, "status_id": STATUS_EMITIDO,
                                         "folio": "52777"}
        fake.estado_por_uuid[uuid_post] = {"uuid": uuid_post, "status_id": STATUS_EMITIDO}
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        check("D4b y por eso el sondeo rescata el folio después",
              r.json().get("folio") == "52777", r.text)
        limpiar(db, MARK)

        # ═══ D5 · el sondeo SIN uuid también se cura por referencia (solo lectura) ════
        fake.reset()
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion",
                                          numero_guia_manual="8105")
        db.refresh(desp)
        dte_guia(db, desp, status_id=STATUS_EMITIDO, folio=None, uuid=None,
                 en_vuelo_desde=None)
        fake.buscable("uuid-encontrado-d5", status_id=STATUS_EMITIDO, folio="52555",
                      referencia=desp.numero)
        creados = len(fake.creados)
        r = client.get(f"{GUIAS}/{desp.id}/estado")
        check("D5a el sondeo sin uuid rescata el folio por la referencia",
              r.json().get("folio") == "52555", r.text)
        check("D5b y NO emite nada (rescate de solo lectura)",
              len(fake.creados) == creados, len(fake.creados))
        limpiar(db, MARK)

        # ═══ E · el folio de la 52 tiene que ser NUMÉRICO ════════════════════════════
        # La referencia 52 apunta a un documento tributario y su folio es un correlativo
        # del SII (también en la guía en PAPEL, cuyo folio autoriza el SII). El N° manual
        # lo TECLEA el operador: se reprodujo un 'G-TECLEADO-A-MANO' viajando como
        # FolioRef. El SII rechaza el documento y el rechazo llega con el folio propio YA
        # CONSUMIDO. Mismo criterio que la referencia 33 del anticipo.
        fake.reset()
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="GUIA-A-MANO-9")
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        check("E1 un N° de guía manual NO numérico BLOQUEA la 33",
              p["puede_emitir"] is False
              and any("no es un número correlativo del SII" in x for x in p["problemas"]),
              p["problemas"])
        check("E2 y no viaja como referencia 52", _ref52(p) == [], p.get("referencias"))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id})
        check("E3 emitir así: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados, r.text)
        limpiar(db, MARK)

        # CONTROL: el N° de guía en papel numérico sigue siendo referencia legítima.
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9347")
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        check("E4 CONTROL: guía en papel con folio numérico SÍ puede emitir",
              p["puede_emitir"] is True and _ref52(p) == ["9347"],
              (p["problemas"], p.get("referencias")))
        limpiar(db, MARK)

        # ═══ F · el criterio es UNO: la función real que consume Contabilidad ════════
        # `monza_contabilidad._guia_sii_en_proceso` llama a `_guia_electronica_en_proceso`,
        # que ahora es un bool sobre `_guia_no_referenciable` (misma verdad, un solo
        # mensaje). Se ejercita la función REAL con filas reales.
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9348")
        casos = [
            ("sin fila DTE", None, False),
            ("emitida CON folio", dict(status_id=STATUS_EMITIDO, folio="52001",
                                       uuid="u1"), False),
            ("emitida SIN folio y SIN uuid", dict(status_id=STATUS_EMITIDO, folio=None,
                                                  uuid=None), True),
            ("emitida SIN folio CON uuid", dict(status_id=STATUS_EMITIDO, folio=None,
                                                uuid="u2"), True),
            ("procesando con uuid", dict(status_id=STATUS_PROCESANDO, uuid="u3"), True),
            ("AMBIGUA (en vuelo vencido, sin uuid)",
             dict(status_id=STATUS_FALLIDO, uuid=None, en_vuelo_desde=_vencido()), True),
            ("claim VIGENTE", dict(status_id=None, uuid=None,
                                   en_vuelo_desde=datetime.utcnow()), True),
            # CORREGIDO (MEDIO-5): este caso esperaba False — «el rechazo con uuid no
            # bloquea» — y con eso fijaba como correcto que la 33 citara el N° tecleado a
            # mano. El documento EXISTE en Wasabil: el rechazo de ESE documento no prueba
            # que no haya otro EMITIDO con la misma referencia. Ahora bloquea.
            ("rechazo CONFIRMADO con uuid (el documento existe en Wasabil)",
             dict(status_id=STATUS_FALLIDO, uuid="u4"), True),
            # …y el control que sí vale sigue en False: sin uuid nunca nació documento.
            ("nunca llegó a Wasabil", dict(status_id=STATUS_FALLIDO, uuid=None,
                                           en_vuelo_desde=None), False),
        ]
        for nombre, kw, esperado in casos:
            db.query(MonzaWasabilDte).filter(
                MonzaWasabilDte.despacho_id == desp.id).delete(synchronize_session=False)
            db.commit()
            if kw:
                dte_guia(db, desp, **kw)
            db.rollback()
            obtenido = mwr._guia_electronica_en_proceso(db, desp.id)
            check(f"F · {nombre} → bloquea={esperado}", obtenido is esperado,
                  (nombre, obtenido))
        limpiar(db, MARK)

    finally:
        fake.reset()
        limpiar(db, MARK)
        db.close()
        verificar_limpieza(MARK)

    check.finish()


def test_monza_wasabil_paridad_sii():
    run()


if __name__ == "__main__":
    run()
