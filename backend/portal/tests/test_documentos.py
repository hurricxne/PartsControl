"""DOCUMENTOS del Portal de Clientes — guías y facturas, con la app completa, cuentas
reales y sesiones reales. A Wasabil no se le llama: su descarga se reemplaza por una falsa.

LO QUE ESTA SUITE PROTEGE (portal/documentos.py, portal/descargas.py y las rutas de descarga)
  §A  QUÉ DOCUMENTOS EXISTEN PARA EL CLIENTE: las guías de los despachos que ya salieron
      y las facturas emitidas de Grupo AM. No una guía en preparación, no una factura
      sin folio ni de la otra marca. El folio y la fecha son los del documento real
      (los del SII si es electrónico; los tecleados si es en papel; ninguno mientras la
      emisión está a medio camino). De cada guía: qué viajó, y qué firmó el cliente
      como recibido (nada, si todavía no firma).
  §B  EL ENLACE PÚBLICO DE WASABIL NO SALE NUNCA: ni `pdf_url`, ni `xml_url`, ni el uuid.
  §C  EL PDF LO ENTREGA EL SERVIDOR: como bytes, con la sesión, dejando rastro (con la
      IP) ANTES de entregar y sin tener tomada una conexión de la base mientras habla
      con Wasabil; guarda una copia para no pedirlo dos veces; si Wasabil falla, 502 con
      texto propio y no se reintenta por un minuto.
  §D  CADA CLIENTE BAJA LO SUYO: el documento de otro → 404, sin rastro y sin llamar a
      Wasabil.
  §E  EL PERFIL BODEGA no ve facturas ni baja archivos (traen precios).
  §F  LA GUÍA FIRMADA sale solo si es un PDF o una imagen con el nombre que genera la
      subida del ERP: nada de rutas, .docx ni archivos que no están.
  §G  A QUIÉN SE EMITIÓ (portal/dtes.py): si consta que el DTE fue a otro RUT, no se
      entrega ningún archivo, esté en el estado que esté; y los archivos de un documento
      que el SII ya aceptó solo se entregan si consta que fue al RUT de la venta (sin
      poder leer a quién —p. ej. un folio registrado a mano—, no salen).
      Una factura de anticipo no marca el repuesto como facturado.
  §H  «Facturado» en el tablero = la suma de las facturas que el cliente ve.
  §I  LA BAJADA DESDE WASABIL (descargas._bajar_de_wasabil, con un httpx falso): qué
      pide, qué acepta y cuándo corta.
  §J  VARIAS PETICIONES A LA VEZ: el mismo documento se baja una vez; hay un máximo de
      bajadas simultáneas y la que no cabe no hace fila.
  §K  EL TOPE DE DESCARGAS POR CUENTA corta sin dejar rastro ni llamar a Wasabil.

MUTACIONES verificadas al construir la suite: la lista está en portal/tests/mutaciones/
(`python -m portal.tests.mutaciones documentos` la vuelve a correr).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_documentos.py -q
(también:   ./venv/bin/python portal/tests/test_documentos.py)
"""
import json
import os
import sys
import threading
import time
from datetime import date, datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from sqlalchemy.orm.exc import DetachedInstanceError  # noqa: E402

from config import settings  # noqa: E402
from database import SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.models import Despacho  # noqa: E402
from portal import cupo, descargas  # noqa: E402
from portal.deps import PortalScope  # noqa: E402
from portal.models import PortalDescargaLog  # noqa: E402
from portal.router_ordenes import DOCUMENTO_NO_DISPONIBLE  # noqa: E402
from portal.tests import _datos as D  # noqa: E402
from wasabil_dte.models import STATUS_EMITIDO, STATUS_FALLIDO, STATUS_PROCESANDO  # noqa: E402

MARK = "ZZPORTALDOC"  # SIN guiones bajos: en LIKE el _ es comodín
client = TestClient(app)
_fails: list = []


def _uuid(n: int) -> str:
    """uuid de prueba: empieza como los archivos de las fábricas, para reconocerlo."""
    return f"{D.PREFIJO_ARCHIVO}-0000-4000-8000-{n:012d}"


class WasabilFalso:
    """Reemplaza descargas._bajar_de_wasabil: anota cada llamada, cuántas conexiones de
    la base había tomadas en ese instante, y responde lo que se le pida."""

    def __init__(self):
        self.llamadas: list = []
        self.conexiones_tomadas: list = []
        self.caido = False

    def __call__(self, uuid: str) -> bytes:
        self.llamadas.append(uuid)
        self.conexiones_tomadas.append(engine.pool.checkedout())
        if self.caido:
            raise descargas.DocumentoNoDisponible("Wasabil de prueba: caído")
        return b"%PDF-1.4 documento de prueba " + uuid.encode()


class WasabilLento:
    """Un Wasabil que no responde hasta que se abre `puerta`. Cuenta cuántas bajadas
    tuvo a la vez."""

    def __init__(self, falla: bool = False):
        self.llamadas: list = []
        self.en_curso = self.maximo = 0
        self.puerta = threading.Event()
        self.falla = falla
        self._candado = threading.Lock()

    def __call__(self, uuid: str) -> bytes:
        with self._candado:
            self.llamadas.append(uuid)
            self.en_curso += 1
            self.maximo = max(self.maximo, self.en_curso)
        try:
            self.puerta.wait(10)
            if self.falla:
                raise descargas.DocumentoNoDisponible("Wasabil de prueba: respondió tarde y mal")
            return b"%PDF-1.4 lento " + uuid.encode()
        finally:
            with self._candado:
                self.en_curso -= 1

    def esperar_en_curso(self, cuantas: int) -> bool:
        limite = time.monotonic() + 5
        while time.monotonic() < limite:
            if self.en_curso >= cuantas:
                return True
            time.sleep(0.01)
        return False


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _cab(email: str) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": D.CLAVE})
    assert r.status_code == 200, f"login de {email}: {r.status_code} {r.text[:120]}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _get(ruta: str, cab: dict, **cabeceras):
    return client.get(f"/api/portal{ruta}", headers={**cab, **cabeceras})


def _rastros(usuario_id: int) -> list:
    """[(tipo, referencia_id, ip)] de las descargas de esa cuenta, en orden."""
    db = SessionLocal()
    try:
        return [(r.tipo, r.referencia_id, r.ip) for r in db.query(PortalDescargaLog)
                .filter(PortalDescargaLog.usuario_id == usuario_id).order_by(PortalDescargaLog.id).all()]
    finally:
        db.close()


def _copia(uuid: str) -> str:
    return os.path.join(descargas.DIRECTORIO_PDF, f"{uuid}.pdf")


# ── Siembra ─────────────────────────────────────────────────────────────────────────────

def _sembrar() -> dict:
    db = SessionLocal()
    try:
        fa, fb = D.ficha(db, MARK, "A", 75000011), D.ficha(db, MARK, "B", 75000022)
        pa, pb = D.portal(db, MARK, "a", [fa.id]), D.portal(db, MARK, "b", [fb.id])
        bodega = D.portal(db, MARK, "bod", [fa.id], perfil="bodega")
        # El mismo RUT de la ficha A, escrito de otra forma (sin puntos, con un cero delante).
        rut_a_de_otra_forma = "0" + fa.rut.replace(".", "")
        assert rut_a_de_otra_forma != fa.rut

        _, oc, it = D.venta(db, MARK, "A1", fa, [
            dict(numero_parte="1D-0001", cantidad=4, precio=10000, estado="despachado"),  # en la factura normal
            dict(numero_parte="1D-0002", cantidad=2, precio=20000, estado="despachado"),  # solo en el anticipo
            dict(numero_parte="1D-0003", cantidad=1, precio=30000, estado="despachado"),  # solo en una factura a otro RUT
            dict(numero_parte="1D-0004", cantidad=5, precio=1000, estado="en_bodega"),    # facturado sin haber salido
            dict(numero_parte="1D-0005", cantidad=4, precio=2000, estado="despachado"),   # el cliente firmó 3 de 4
            dict(numero_parte="1D-0006", cantidad=2, precio=3000, estado="despachado"),   # el cliente firmó 0 de 2
            dict(numero_parte="1D-0007", cantidad=30, precio=500, estado="despachado"),   # el resto de las guías
        ])
        i1, i2, i3, i4, i5, i6, i7 = it

        # ── Guías ──
        # En papel y firmada. La guía se tecleó con fecha 4 de marzo, salió a las 02:00 UTC
        # del día 6 (las 23:00 del 5 en Chile) y el operador tecleó que se firmó el día 7.
        papel = D.despacho(db, MARK, "papel", oc, [(i1, 4), (i5, 4, 3), (i6, 2, 0)], numero_guia="5001",
                           fecha=datetime(2026, 3, 6, 2, 0), fecha_guia=date(2026, 3, 4), firmada=True,
                           fecha_firma=date(2026, 3, 7), archivo_firmado=D.archivo_de_guia_firmada(1))
        electronica = D.despacho(db, MARK, "elec", oc, [(i2, 2)], numero_guia="5002")
        D.dte(db, despacho=electronica, uuid=_uuid(2), folio="5002", receptor_rut=fa.rut,
              fecha_documento="2026-04-10")
        a_medias = D.despacho(db, MARK, "medias", oc, [(i7, 1)], numero_guia="MANUAL-9")
        D.dte(db, despacho=a_medias, uuid=_uuid(3), folio=None, status_id=STATUS_PROCESANDO)
        rechazada = D.despacho(db, MARK, "rech", oc, [(i7, 1)], numero_guia="5004")
        D.dte(db, despacho=rechazada, uuid=_uuid(4), folio=None, status_id=STATUS_FALLIDO)
        en_preparacion = D.despacho(db, MARK, "prep", oc, [(i4, 5)], numero_guia="5005",
                                    estado="en_preparacion")
        D.dte(db, despacho=en_preparacion, uuid=_uuid(5), folio="5005", receptor_rut=fa.rut)
        caida = D.despacho(db, MARK, "caida", oc, [(i7, 1)], numero_guia="5013")
        D.dte(db, despacho=caida, uuid=_uuid(23), folio="5013", receptor_rut=fa.rut)
        uuid_malo = D.despacho(db, MARK, "uuid", oc, [(i7, 1)], numero_guia="5008")
        D.dte(db, despacho=uuid_malo, uuid="../../../etc/passwd", folio="5008", receptor_rut=fa.rut)

        # Guías electrónicas emitidas: ¿a quién? (§G)
        a_otro_rut = D.despacho(db, MARK, "otro", oc, [(i3, 1)], numero_guia="5006", firmada=True,
                                archivo_firmado=D.archivo_de_guia_firmada(6))
        D.dte(db, despacho=a_otro_rut, uuid=_uuid(6), folio="5006", receptor_rut=fb.rut)
        # Así deja la respuesta el registro manual de un folio (wasabil_dte/router.py::
        # _registrar_folio_a_mano): el folio y quién lo registró, sin el receptor.
        registro_manual = json.dumps({
            "status_id": STATUS_EMITIDO, "folio": "570", "uuid": _uuid(60),
            "_registro_manual_de_folio": {"folio": "570", "usuario_id": 1, "origen": "declarado por el operador"}})
        # (qué dice la respuesta, estado del DTE, folio) → nunca se ofrecen archivos.
        sin_archivos = {}
        for n, (nombre, respuesta, estado_dte, folio) in enumerate((
            ("aceptado por el SII y el folio registrado A MANO (la respuesta no trae el receptor)",
             registro_manual, STATUS_EMITIDO, "570"),
            ("aceptado por el SII con la respuesta cortada antes del receptor", '{"receiver_rut": "75.', STATUS_EMITIDO, "571"),
            ("aceptado por el SII con un receptor que no es un RUT válido",
             '{"receiver_rut": "75.000.011-9"}', STATUS_EMITIDO, "572"),
            ("aceptado por el SII con una respuesta que no es un objeto", '["75.000.011-8"]', STATUS_EMITIDO, "573"),
            ("aceptado por el SII, todavía SIN folio y sin receptor legible", "{}", STATUS_EMITIDO, None),
            ("aceptado por el SII, todavía sin folio, y consta que fue a OTRO RUT",
             json.dumps({"receiver_rut": fb.rut}), STATUS_EMITIDO, None),
            ("todavía en proceso, y consta que fue a OTRO RUT", json.dumps({"receiver_rut": fb.rut}), STATUS_PROCESANDO, None),
            ("rechazado por el SII, y consta que iba a OTRO RUT", json.dumps({"receiver_rut": fb.rut}), STATUS_FALLIDO, None),
        )):
            d = D.despacho(db, MARK, f"sinarch{n}", oc, [(i7, 1)], numero_guia=f"57{n}", firmada=True,
                           archivo_firmado=D.archivo_de_guia_firmada(60 + n))
            D.dte(db, despacho=d, uuid=_uuid(60 + n), folio=folio, status_id=estado_dte, respuesta_cruda=respuesta)
            sin_archivos[nombre] = d.id
        # En cambio: emisión en curso o rechazada SIN evidencia de otro receptor → la guía
        # va en papel y su copia firmada sí se entrega.
        en_papel_por_rechazo = D.despacho(db, MARK, "rechfirm", oc, [(i7, 1)], numero_guia="5790", firmada=True,
                                          archivo_firmado=D.archivo_de_guia_firmada(79))
        D.dte(db, despacho=en_papel_por_rechazo, uuid=_uuid(79), folio=None, status_id=STATUS_FALLIDO,
              respuesta_cruda='{"error": "rechazado"}')
        # La respuesta quedó recortada (JSON inválido) pero el receptor alcanza a leerse.
        truncada = D.despacho(db, MARK, "trunc", oc, [(i7, 1)], numero_guia="5011")
        D.dte(db, despacho=truncada, uuid=_uuid(21), folio="5011",
              respuesta_cruda='{"uuid": "x", "receiver_rut": "%s", "details": [{"name": "aaa' % fa.rut)
        otra_forma = D.despacho(db, MARK, "forma", oc, [(i7, 1)], numero_guia="5012")
        D.dte(db, despacho=otra_forma, uuid=_uuid(22), folio="5012", receptor_rut=rut_a_de_otra_forma)

        # Guías firmadas: el archivo (§F)
        D.archivo_de_guia_firmada(20, "docx", b"PK no es una guia")
        firmadas_malas = {
            "una ruta fuera de la carpeta": "../../.env",
            "un .docx": f"{D.PREFIJO_ARCHIVO}{20:024x}.docx",
            "un archivo que no está": f"{D.PREFIJO_ARCHIVO}{21:024x}.pdf",
            "un nombre que no es de la subida": "guia final.pdf",
        }
        malas = {
            nombre: D.despacho(db, MARK, f"mala{n}", oc, [(i7, 1)], numero_guia=f"60{n}", firmada=True,
                               archivo_firmado=archivo).id
            for n, (nombre, archivo) in enumerate(firmadas_malas.items())
        }
        sin_firma = D.despacho(db, MARK, "sinfirma", oc, [(i7, 1)], numero_guia="6100", firmada=False,
                               archivo_firmado=D.archivo_de_guia_firmada(22))
        en_jpg = D.despacho(db, MARK, "jpg", oc, [(i7, 1)], numero_guia="6201", firmada=True,
                            archivo_firmado=D.archivo_de_guia_firmada(40, "jpg", D.imagen_jpeg()))
        en_png = D.despacho(db, MARK, "png", oc, [(i7, 1)], numero_guia="GD 62/02", firmada=True,
                            archivo_firmado=D.archivo_de_guia_firmada(41, "png", b"\x89PNG\r\n\x1a\n de prueba"))

        # ── Facturas ──
        normal = D.factura(db, MARK, "N", oc, [(i1, 4, 10000)], fecha=date(2026, 4, 12))
        D.dte(db, factura=normal, uuid=_uuid(11), folio=normal.numero_factura, receptor_rut=fa.rut)
        en_papel = D.factura(db, MARK, "P", oc, [(i4, 5, 1000)])
        sin_folio = D.factura(db, MARK, "S", oc, [(i4, 5, 1000)], folio=None)
        D.dte(db, factura=sin_folio, uuid=_uuid(13), folio=None, status_id=STATUS_PROCESANDO)
        otra_marca = D.factura(db, MARK, "M", oc, [(i4, 5, 1000)], empresa="automotriz")
        D.dte(db, factura=otra_marca, uuid=_uuid(14), folio=otra_marca.numero_factura, receptor_rut=fa.rut)
        de_otro_rut = D.factura(db, MARK, "O", oc, [(i3, 1, 30000)])
        D.dte(db, factura=de_otro_rut, uuid=_uuid(15), folio=de_otro_rut.numero_factura, receptor_rut=fb.rut)
        anticipo = D.factura(db, MARK, "ANT", oc, [(i2, 2, 20000)], es_anticipo=True)
        # Emitidas, pero no se puede leer a quién (respuesta ilegible; folio registrado a
        # mano): la factura existe para el cliente; su archivo no se ofrece.
        f_ilegible = D.factura(db, MARK, "I", oc, [(i7, 1, 500)])
        D.dte(db, factura=f_ilegible, uuid=_uuid(16), folio=f_ilegible.numero_factura,
              respuesta_cruda="esto no es JSON")
        f_a_mano = D.factura(db, MARK, "MANO", oc, [(i7, 1, 500)])
        D.dte(db, factura=f_a_mano, uuid=_uuid(17), folio=f_a_mano.numero_factura, respuesta_cruda=registro_manual)

        _, ocb, itb = D.venta(db, MARK, "B1", fb, [dict(numero_parte="2D-0001", estado="despachado")])
        guia_b = D.despacho(db, MARK, "b", ocb, [(itb[0], 1)], numero_guia="7001", firmada=True,
                            archivo_firmado=D.archivo_de_guia_firmada(30))
        D.dte(db, despacho=guia_b, uuid=_uuid(31), folio="7001", receptor_rut=fb.rut)
        factura_b = D.factura(db, MARK, "B", ocb, [(itb[0], 1, 1000)])
        D.dte(db, factura=factura_b, uuid=_uuid(32), folio=factura_b.numero_factura, receptor_rut=fb.rut)
        db.commit()
        return {
            "oc": oc.id, "ocb": ocb.id, "items": [i.id for i in it],
            "papel": papel.id, "electronica": electronica.id, "a_medias": a_medias.id,
            "rechazada": rechazada.id, "en_preparacion": en_preparacion.id, "a_otro_rut": a_otro_rut.id,
            "sin_archivos": sin_archivos, "en_papel_por_rechazo": en_papel_por_rechazo.id,
            "truncada": truncada.id, "otra_forma": otra_forma.id,
            "caida": caida.id, "uuid_malo": uuid_malo.id, "malas": malas, "sin_firma": sin_firma.id,
            "en_jpg": en_jpg.id, "en_png": en_png.id,
            "normal": normal.id, "en_papel": en_papel.id, "sin_folio": sin_folio.id,
            "otra_marca": otra_marca.id, "de_otro_rut": de_otro_rut.id, "anticipo": anticipo.id,
            "f_ilegible": f_ilegible.id, "f_a_mano": f_a_mano.id, "folio_normal": normal.numero_factura,
            "guia_b": guia_b.id, "factura_b": factura_b.id,
            "email_a": pa.email, "email_b": pb.email, "email_bod": bodega.email,
            "uid_a": pa.id, "uid_b": pb.id, "uid_bod": bodega.id,
        }
    finally:
        db.close()


# ── §A ──────────────────────────────────────────────────────────────────────────────────

def _paso_a_que_documentos_existen(cab_a, ids):
    orden = _get(f"/ordenes/{ids['oc']}", cab_a).json()
    guias = {g["id"]: g for g in orden["guias"]}
    check("§A la guía de un despacho en preparación NO aparece (aunque ya tenga folio del SII)",
          ids["en_preparacion"] not in guias and ids["papel"] in guias, sorted(guias))
    papel = guias[ids["papel"]]
    check("§A guía en papel: el folio y la FECHA que tecleó el operador (el 4, no el día en que salió); "
          "sin PDF electrónico, con la firmada",
          papel["folio"] == "5001" and papel["fecha"] == "2026-03-04" and not papel["pdf"] and papel["firmada"], papel)
    check("§A …salió el día 5 en Chile (las 02:00 UTC del 6) y se recibió el día que tecleó el operador (el 7, no el 6)",
          papel["despachada_el"] == "2026-03-05" and papel["fecha_recepcion"] == "2026-03-07",
          (papel["despachada_el"], papel["fecha_recepcion"]))
    recibidas = {i["numero_parte"]: (i["cantidad"], i["cantidad_recibida"]) for i in papel["items"]}
    check("§A …con lo que viajó y lo que se firmó (sin dato = todo; 3 de 4; 0 de 2 es CERO, no «todo»)",
          recibidas == {"1D-0001": (4, 4), "1D-0005": (4, 3), "1D-0006": (2, 0)}, recibidas)
    check("§A …y como firmó menos de lo que viajó, la recepción es «con faltante»",
          papel["recepcion"] == "con_faltante", papel["recepcion"])
    check("§A una guía firmada completa es «conforme»",
          guias[ids["en_jpg"]]["recepcion"] == "conforme"
          and guias[ids["en_jpg"]]["items"][0]["cantidad_recibida"] == 1, guias[ids["en_jpg"]])
    elec = guias[ids["electronica"]]
    check("§A guía electrónica: folio del SII y fecha tributaria del documento, con PDF",
          elec["folio"] == "5002" and elec["fecha"] == "2026-04-10" and elec["pdf"] and not elec["firmada"], elec)
    check("§A …sin firmar todavía: recepción «pendiente», sin fecha, y de lo recibido no se dice nada (null, no 0)",
          elec["recepcion"] == "pendiente" and elec["fecha_recepcion"] is None
          and [i["cantidad_recibida"] for i in elec["items"]] == [None], elec)
    check("§A emisión a medio camino: sin folio (el número tecleado ya no vale) y sin PDF",
          guias[ids["a_medias"]]["folio"] == "" and not guias[ids["a_medias"]]["pdf"], guias[ids["a_medias"]])
    check("§A emisión rechazada por el SII: vale el folio en papel, sin PDF",
          guias[ids["rechazada"]]["folio"] == "5004" and not guias[ids["rechazada"]]["pdf"], guias[ids["rechazada"]])

    facturas = {f["id"]: f for f in orden["facturas"]}
    check("§A facturas: la normal, la emitida fuera del sistema, el anticipo y las dos sin receptor legible; "
          "NO la sin folio, la de otra marca ni la emitida a otro RUT",
          set(facturas) == {ids["normal"], ids["en_papel"], ids["anticipo"], ids["f_ilegible"], ids["f_a_mano"]},
          sorted(facturas))
    normal = facturas[ids["normal"]]
    check("§A la factura trae folio, fecha, neto, IVA y total; y nada de pagos ni saldos",
          normal["folio"] == ids["folio_normal"] and normal["fecha"] == "2026-04-12"
          and (normal["neto_clp"], normal["iva_clp"], normal["total_clp"]) == (40000, 7600, 47600)
          and normal["pdf"] and not normal["es_anticipo"]
          and not ({"saldo", "estado_pago", "monto_pagado", "condicion_pago"} & set(normal)), normal)
    check("§A la emitida fuera del sistema no ofrece PDF; el anticipo va marcado",
          not facturas[ids["en_papel"]]["pdf"] and facturas[ids["anticipo"]]["es_anticipo"])


# ── §B ──────────────────────────────────────────────────────────────────────────────────

def _paso_b_el_enlace_publico_no_sale(cab_a, ids):
    texto = "".join(_get(ruta, cab_a).text for ruta in (
        f"/ordenes/{ids['oc']}", "/ordenes", "/resumen",
        f"/ordenes/{ids['oc']}/items/{ids['items'][0]}", f"/ordenes/{ids['oc']}/items/{ids['items'][3]}"))
    for nombre, valor in (("el enlace público de Wasabil", "publico.invalid"), ("un uuid de DTE", f"{D.PREFIJO_ARCHIVO}-0000"),
                          ("el campo pdf_url", "pdf_url"), ("el campo xml_url", "xml_url"),
                          ("el nombre en disco de la guía firmada", D.PREFIJO_ARCHIVO)):
        check(f"§B {nombre} no aparece en ninguna respuesta", valor not in texto, valor)


# ── §C ──────────────────────────────────────────────────────────────────────────────────

def _paso_c_el_pdf_lo_entrega_el_servidor(cab_a, ids, wasabil):
    cupo.reset()
    r = _get(f"/guias/{ids['electronica']}/pdf", cab_a)
    check("§C PDF de la guía: 200, es un PDF y llega como bytes",
          r.status_code == 200 and r.headers["content-type"] == "application/pdf"
          and r.content.startswith(b"%PDF"), f"{r.status_code} {r.headers.get('content-type')}")
    check("§C …con nombre de archivo propio y sin caché compartido",
          'filename="guia-5002.pdf"' in r.headers.get("content-disposition", "")
          and r.headers.get("cache-control") == "private, no-store"
          and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))
    check("§C …sin el uuid ni el enlace público en ninguna cabecera",
          D.PREFIJO_ARCHIVO not in str(r.headers) and "publico.invalid" not in str(r.headers))
    check("§C mientras se habla con Wasabil no hay ninguna conexión de la base tomada",
          wasabil.conexiones_tomadas == [0], wasabil.conexiones_tomadas)
    check("§C la copia quedó en la carpeta del portal, sin temporales a medio escribir",
          os.listdir(descargas.DIRECTORIO_PDF) == [f"{_uuid(2)}.pdf"], os.listdir(descargas.DIRECTORIO_PDF))
    _get(f"/guias/{ids['electronica']}/pdf", cab_a, **{"X-Forwarded-For": "203.0.113.9, 198.51.100.7"})
    check("§C la segunda descarga sale de la copia local: a Wasabil se le pidió UNA vez",
          wasabil.llamadas.count(_uuid(2)) == 1, wasabil.llamadas)
    check("§C cada descarga deja su rastro con la IP (detrás del proxy: el último salto, que escribe nginx)",
          _rastros(ids["uid_a"]) == [("guia_pdf", ids["electronica"], "testclient"),
                                     ("guia_pdf", ids["electronica"], "198.51.100.7")], _rastros(ids["uid_a"]))
    r = _get(f"/guias/{ids['electronica']}/pdf", cab_a, **{"X-Forwarded-For": "9" * 200})
    check("§C una IP desmesurada en la cabecera no rompe la descarga: se guarda recortada",
          r.status_code == 200 and _rastros(ids["uid_a"])[-1][2] == "9" * 64, (r.status_code, _rastros(ids["uid_a"])[-1]))

    r = _get(f"/facturas/{ids['normal']}/pdf", cab_a)
    check("§C PDF de la factura: 200, PDF, con su folio en el nombre",
          r.status_code == 200 and r.content.startswith(b"%PDF")
          and f'filename="factura-{ids["folio_normal"]}.pdf"' in r.headers.get("content-disposition", ""),
          f"{r.status_code} {r.headers.get('content-disposition')}")

    # Una copia local que no es un PDF (quedó de un fallo antiguo) no se sirve: se vuelve a pedir.
    with open(_copia(_uuid(21)), "wb") as basura:
        basura.write(b"<html>error</html>")
    r = _get(f"/guias/{ids['truncada']}/pdf", cab_a)
    check("§C una copia local que no es un PDF no se entrega: se baja de nuevo y se reemplaza",
          r.status_code == 200 and r.content.startswith(b"%PDF") and wasabil.llamadas.count(_uuid(21)) == 1,
          f"{r.status_code} {r.content[:20]!r}")

    wasabil.caido = True
    r = _get(f"/guias/{ids['caida']}/pdf", cab_a)
    check("§C Wasabil caído → 502 con un texto para el cliente (no el error técnico)",
          r.status_code == 502 and r.json().get("detail") == DOCUMENTO_NO_DISPONIBLE and "Wasabil" not in r.text,
          f"{r.status_code} {r.text[:120]}")
    check("§C …y el intento quedó registrado igual",
          _rastros(ids["uid_a"])[-1][:2] == ("guia_pdf", ids["caida"]), _rastros(ids["uid_a"])[-1])
    check("§C …y no quedó una copia local a medias", not os.path.exists(_copia(_uuid(23))))
    r = _get(f"/guias/{ids['electronica']}/pdf", cab_a)
    check("§C con Wasabil caído, lo que ya se bajó una vez sigue disponible", r.status_code == 200)
    antes = len(wasabil.llamadas)
    r = _get(f"/guias/{ids['caida']}/pdf", cab_a)
    check("§C un segundo clic dentro del minuto no vuelve a esperar a Wasabil: 502 sin llamarlo",
          r.status_code == 502 and len(wasabil.llamadas) == antes, (r.status_code, wasabil.llamadas[antes:]))
    wasabil.caido = False
    r = _get(f"/guias/{ids['caida']}/pdf", cab_a)
    check("§C …ni aunque Wasabil ya haya vuelto (el fallo se recuerda un minuto)",
          r.status_code == 502 and len(wasabil.llamadas) == antes, (r.status_code, wasabil.llamadas[antes:]))
    descargas.reset()
    r = _get(f"/guias/{ids['caida']}/pdf", cab_a)
    check("§C pasado ese tiempo se vuelve a pedir, y sale",
          r.status_code == 200 and wasabil.llamadas[antes:] == [_uuid(23)], (r.status_code, wasabil.llamadas[antes:]))

    antes = len(wasabil.llamadas)
    r = _get(f"/guias/{ids['uuid_malo']}/pdf", cab_a)
    check("§C un uuid con forma de ruta no se usa para pedir ni para escribir nada → 502",
          r.status_code == 502 and len(wasabil.llamadas) == antes, f"{r.status_code} {wasabil.llamadas[antes:]}")

    for nombre, ruta in (
        ("guía en papel (no hay PDF electrónico)", f"/guias/{ids['papel']}/pdf"),
        ("guía con emisión a medias", f"/guias/{ids['a_medias']}/pdf"),
        ("guía con emisión rechazada", f"/guias/{ids['rechazada']}/pdf"),
        ("guía de un despacho en preparación", f"/guias/{ids['en_preparacion']}/pdf"),
        ("factura emitida fuera del sistema", f"/facturas/{ids['en_papel']}/pdf"),
        ("factura sin folio", f"/facturas/{ids['sin_folio']}/pdf"),
        ("factura de la otra marca", f"/facturas/{ids['otra_marca']}/pdf"),
        ("guía inexistente", "/guias/999999999/pdf"), ("factura inexistente", "/facturas/999999999/pdf"),
    ):
        antes_rastro, antes_llamadas = len(_rastros(ids["uid_a"])), len(wasabil.llamadas)
        r = _get(ruta, cab_a)
        check(f"§C {nombre} → 404, sin rastro y sin llamar a Wasabil",
              r.status_code == 404 and len(_rastros(ids["uid_a"])) == antes_rastro
              and len(wasabil.llamadas) == antes_llamadas, f"{r.status_code} {r.text[:80]}")
    check("§C en ninguna llamada a Wasabil había una conexión de la base tomada",
          set(wasabil.conexiones_tomadas) == {0}, wasabil.conexiones_tomadas)


def _paso_c_la_sesion_queda_cerrada(ids):
    """descargas.anotar_descarga deja el rastro y CIERRA la sesión: lo que venga después
    (la bajada desde Wasabil) no puede tener tomada una conexión de la base."""
    cupo.reset()
    db = SessionLocal()
    try:
        despacho = db.query(Despacho).filter(Despacho.id == ids["papel"]).one()
        scope = PortalScope(usuario_id=ids["uid_a"], cliente_ids=frozenset({0}), perfil="procurement")
        antes = len(_rastros(ids["uid_a"]))
        descargas.anotar_descarga(db, scope, "guia_firmada", ids["papel"], "192.0.2.1")
        try:
            _ = despacho.numero_guia
            volvio_a_leer = True
        except DetachedInstanceError:
            volvio_a_leer = False
        check("§C después de anotar la descarga la sesión queda cerrada: leer un objeto de la base FALLA "
              "(en vez de volver a tomar una conexión en silencio)",
              not volvio_a_leer and engine.pool.checkedout() == 0, (volvio_a_leer, engine.pool.checkedout()))
        check("§C …y el rastro quedó escrito", _rastros(ids["uid_a"])[antes:] == [("guia_firmada", ids["papel"], "192.0.2.1")],
              _rastros(ids["uid_a"])[antes:])
    finally:
        db.close()


def _paso_c_sin_rastro_no_hay_descarga(cab_a, ids, wasabil):
    """Si la bitácora no se puede escribir, el archivo no se entrega."""
    cupo.reset()

    def bitacora_rota(**_):
        raise RuntimeError("bitácora de prueba: rota")

    tolerante = TestClient(app, raise_server_exceptions=False)
    antes, original = len(wasabil.llamadas), descargas.PortalDescargaLog
    descargas.PortalDescargaLog = bitacora_rota
    try:
        pdf = tolerante.get(f"/api/portal/guias/{ids['otra_forma']}/pdf", headers=cab_a)
        firmada = tolerante.get(f"/api/portal/guias/{ids['papel']}/firmada", headers=cab_a)
        factura = tolerante.get(f"/api/portal/facturas/{ids['normal']}/pdf", headers=cab_a)
    finally:
        descargas.PortalDescargaLog = original
    check("§C si el rastro no se puede escribir, ninguna de las tres descargas entrega el archivo",
          [r.status_code for r in (pdf, firmada, factura)] == [500, 500, 500]
          and not any(r.content.startswith(b"%PDF") for r in (pdf, firmada, factura)),
          [(r.status_code, r.content[:12]) for r in (pdf, firmada, factura)])
    check("§C …y a Wasabil ni se le alcanza a pedir", len(wasabil.llamadas) == antes, wasabil.llamadas[antes:])
    check("§C …con la bitácora de vuelta, la descarga sale",
          _get(f"/guias/{ids['otra_forma']}/pdf", cab_a).status_code == 200)


# ── §D ──────────────────────────────────────────────────────────────────────────────────

def _paso_d_cada_cliente_baja_lo_suyo(cab_a, cab_b, ids, wasabil):
    cupo.reset()
    for nombre, ruta in (("el PDF de la guía", f"/guias/{ids['guia_b']}/pdf"),
                         ("la guía firmada", f"/guias/{ids['guia_b']}/firmada"),
                         ("el PDF de la factura", f"/facturas/{ids['factura_b']}/pdf")):
        antes_rastro, antes_llamadas = len(_rastros(ids["uid_a"])), len(wasabil.llamadas)
        r = _get(ruta, cab_a)
        check(f"§D A pide {nombre} de B → 404, sin rastro y sin llamar a Wasabil",
              r.status_code == 404 and len(_rastros(ids["uid_a"])) == antes_rastro
              and len(wasabil.llamadas) == antes_llamadas, f"{r.status_code} {r.text[:80]}")
        check(f"§D …y B sí baja {nombre}", _get(ruta, cab_b).status_code == 200)
    check("§D el rastro de B registra sus tres descargas, con el tipo de cada una",
          [r[:2] for r in _rastros(ids["uid_b"])] == [("guia_pdf", ids["guia_b"]), ("guia_firmada", ids["guia_b"]),
                                                      ("factura_pdf", ids["factura_b"])], _rastros(ids["uid_b"]))


# ── §E ──────────────────────────────────────────────────────────────────────────────────

def _paso_e_perfil_bodega(cab_bod, ids, wasabil):
    cupo.reset()
    orden = _get(f"/ordenes/{ids['oc']}", cab_bod).json()
    check("§E bodega ve las guías (qué viajó y qué se recibió) pero ninguna ofrece archivos",
          len(orden["guias"]) >= 5 and not any(g["pdf"] or g["firmada"] for g in orden["guias"])
          and all(g["items"] for g in orden["guias"]), [(g["id"], g["pdf"], g["firmada"]) for g in orden["guias"]])
    check("§E bodega no ve facturas", orden["facturas"] == [], orden["facturas"])
    item = _get(f"/ordenes/{ids['oc']}/items/{ids['items'][0]}", cab_bod).json()
    check("§E …ni en el seguimiento del ítem, donde el hito «facturado» va sin folio",
          item["facturas"] == [] and item["item"]["estado"]["clave"] == "facturado"
          and next(h for h in item["hitos"] if h["clave"] == "facturado")["detalle"] is None, item["hitos"])
    antes = len(wasabil.llamadas)
    for nombre, ruta in (("PDF de guía", f"/guias/{ids['electronica']}/pdf"),
                         ("guía firmada", f"/guias/{ids['papel']}/firmada"),
                         ("PDF de factura", f"/facturas/{ids['normal']}/pdf")):
        check(f"§E bodega no baja {nombre} → 404", _get(ruta, cab_bod).status_code == 404)
    check("§E …sin rastro y sin llamar a Wasabil",
          _rastros(ids["uid_bod"]) == [] and len(wasabil.llamadas) == antes)


# ── §F ──────────────────────────────────────────────────────────────────────────────────

def _paso_f_guia_firmada(cab_a, ids):
    cupo.reset()
    r = _get(f"/guias/{ids['papel']}/firmada", cab_a)
    check("§F la guía firmada (PDF) se baja con su tipo y un nombre limpio",
          r.status_code == 200 and r.headers["content-type"] == "application/pdf"
          and 'filename="guia-firmada-5001.pdf"' in r.headers.get("content-disposition", "")
          and r.headers.get("cache-control") == "private, no-store"
          and r.headers.get("x-content-type-options") == "nosniff",
          f"{r.status_code} {dict(r.headers)}")
    check("§F …y deja rastro", _rastros(ids["uid_a"])[-1][:2] == ("guia_firmada", ids["papel"]))
    r = _get(f"/guias/{ids['en_jpg']}/firmada", cab_a)
    check("§F una guía firmada fotografiada (JPG) sale como imagen",
          r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
          and 'filename="guia-firmada-6201.jpg"' in r.headers.get("content-disposition", ""),
          f"{r.status_code} {dict(r.headers)}")
    r = _get(f"/guias/{ids['en_png']}/firmada", cab_a)
    check("§F …y en PNG también; el folio tecleado «GD 62/02» no ensucia el nombre del archivo",
          r.status_code == 200 and r.headers["content-type"] == "image/png"
          and 'filename="guia-firmada-GD6202.png"' in r.headers.get("content-disposition", ""),
          f"{r.status_code} {dict(r.headers)}")
    guias = {g["id"]: g for g in _get(f"/ordenes/{ids['oc']}", cab_a).json()["guias"]}
    for nombre, despacho_id in ids["malas"].items():
        r = _get(f"/guias/{despacho_id}/firmada", cab_a)
        check(f"§F guía firmada que apunta a {nombre} → no se ofrece y la descarga da 404",
              r.status_code == 404 and not guias[despacho_id]["firmada"],
              f"{r.status_code} {r.headers.get('content-type')} {r.content[:40]!r}")
    check("§F un archivo adjunto en un despacho NO marcado como firmado tampoco se entrega",
          _get(f"/guias/{ids['sin_firma']}/firmada", cab_a).status_code == 404
          and not guias[ids["sin_firma"]]["firmada"])


# ── §G ──────────────────────────────────────────────────────────────────────────────────

def _paso_g_a_quien_se_emitio(cab_a, ids, wasabil):
    cupo.reset()
    orden = _get(f"/ordenes/{ids['oc']}", cab_a).json()
    guias = {g["id"]: g for g in orden["guias"]}
    otro = guias[ids["a_otro_rut"]]
    check("§G una guía emitida a otro RUT se lista (el despacho ocurrió) pero no ofrece archivos",
          otro["folio"] == "5006" and not otro["pdf"] and not otro["firmada"], otro)
    antes = len(wasabil.llamadas)
    check("§G …y ni su PDF ni su copia firmada se entregan",
          _get(f"/guias/{ids['a_otro_rut']}/pdf", cab_a).status_code == 404
          and _get(f"/guias/{ids['a_otro_rut']}/firmada", cab_a).status_code == 404
          and len(wasabil.llamadas) == antes)
    check("§G el PDF de una factura emitida a otro RUT → 404",
          _get(f"/facturas/{ids['de_otro_rut']}/pdf", cab_a).status_code == 404
          and len(wasabil.llamadas) == antes)

    for nombre, despacho_id in ids["sin_archivos"].items():
        guia = guias[despacho_id]
        check(f"§G guía con el DTE {nombre}: se lista, pero no ofrece ni entrega el PDF ni la copia firmada",
              not guia["pdf"] and not guia["firmada"]
              and _get(f"/guias/{despacho_id}/pdf", cab_a).status_code == 404
              and _get(f"/guias/{despacho_id}/firmada", cab_a).status_code == 404
              and len(wasabil.llamadas) == antes, (guia, wasabil.llamadas[antes:]))
    check("§G en cambio, un DTE rechazado SIN evidencia de otro receptor no veta nada: la guía va en papel y su firmada sale",
          guias[ids["en_papel_por_rechazo"]]["firmada"] and guias[ids["en_papel_por_rechazo"]]["folio"] == "5790"
          and _get(f"/guias/{ids['en_papel_por_rechazo']}/firmada", cab_a).status_code == 200,
          guias[ids["en_papel_por_rechazo"]])
    for nombre, factura_id in (("el receptor ilegible", ids["f_ilegible"]),
                               ("el folio registrado a mano", ids["f_a_mano"])):
        factura = next(f for f in orden["facturas"] if f["id"] == factura_id)
        check(f"§G factura con {nombre}: existe para el cliente, pero su PDF no se ofrece ni se baja",
              not factura["pdf"] and _get(f"/facturas/{factura_id}/pdf", cab_a).status_code == 404
              and len(wasabil.llamadas) == antes, factura)

    check("§G si la respuesta quedó recortada pero el receptor se alcanza a leer y es el de la venta, hay PDF",
          guias[ids["truncada"]]["pdf"] and _get(f"/guias/{ids['truncada']}/pdf", cab_a).status_code == 200)
    check("§G el mismo RUT escrito de otra forma (sin puntos, con un cero delante) es el mismo RUT",
          guias[ids["otra_forma"]]["pdf"] and _get(f"/guias/{ids['otra_forma']}/pdf", cab_a).status_code == 200)

    items = {l["numero_parte"]: l for l in orden["items"]}
    check("§G ítem despachado y en una factura visible → «Despachado y facturado»",
          items["1D-0001"]["estado"]["clave"] == "facturado", items["1D-0001"]["estado"])
    check("§G ítem que solo figura en una factura de ANTICIPO → sigue «Despachado»",
          items["1D-0002"]["estado"]["clave"] == "despachado", items["1D-0002"]["estado"])
    check("§G ítem que solo figura en una factura emitida a otro RUT → sigue «Despachado»",
          items["1D-0003"]["estado"]["clave"] == "despachado", items["1D-0003"]["estado"])
    check("§G ítem facturado que todavía no sale → sigue «En nuestra bodega»",
          items["1D-0004"]["estado"]["clave"] == "en_bodega", items["1D-0004"]["estado"])
    check("§G ítem del que el cliente firmó 3 de 4 → 3 despachadas y 1 «no recibido en la entrega»",
          {p["estado"]["clave"]: p["cantidad"] for p in items["1D-0005"]["partes"]} == {"despachado": 3, "no_recibido": 1},
          items["1D-0005"]["partes"])
    check("§G ítem del que el cliente firmó CERO de 2 → nada entregado (un 0 firmado no es «firmó todo»)",
          items["1D-0006"]["estado"]["clave"] == "no_recibido" and items["1D-0006"]["cantidad_entregada"] == 0
          and not items["1D-0006"]["entregado"], items["1D-0006"])
    seguimiento = _get(f"/ordenes/{ids['oc']}/items/{ids['items'][0]}", cab_a).json()
    hito = next(h for h in seguimiento["hitos"] if h["clave"] == "facturado")
    check("§G el hito «facturado» nombra la factura que el cliente ve, con su fecha",
          hito["detalle"] == f"Factura {ids['folio_normal']}" and hito["fecha"] == "2026-04-12", hito)
    check("§G el seguimiento del ítem trae solo SUS documentos",
          {f["id"] for f in seguimiento["facturas"]} == {ids["normal"]}
          and {g["id"] for g in seguimiento["guias"]} == {ids["papel"]},
          (seguimiento["facturas"], [g["id"] for g in seguimiento["guias"]]))
    hito_despacho = next(h for h in seguimiento["hitos"] if h["clave"] == "despachado")
    check("§G el hito «despachado» nombra esa guía y lleva el día en que salió",
          hito_despacho["detalle"] == "Guía 5001" and hito_despacho["fecha"] == "2026-03-05", hito_despacho)


# ── §H ──────────────────────────────────────────────────────────────────────────────────

def _paso_h_facturado_del_tablero(cab_a, ids):
    visibles = _get(f"/ordenes/{ids['oc']}", cab_a).json()["facturas"]
    tablero = _get("/resumen", cab_a).json()
    check("§H «facturado» del tablero = la suma neta de las facturas que el cliente ve",
          tablero["montos"]["facturado_neto_clp"] == sum(f["neto_clp"] for f in visibles) == 86000,
          (tablero["montos"], [f["neto_clp"] for f in visibles]))


# ── §I ──────────────────────────────────────────────────────────────────────────────────

class _RespuestaFalsa:
    def __init__(self, status_code: int, trozos: list):
        self.status_code, self.trozos, self.entregados = status_code, trozos, 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_bytes(self):
        for trozo in self.trozos:
            self.entregados += 1
            yield trozo


def _paso_i_la_bajada_desde_wasabil():
    """descargas._bajar_de_wasabil de verdad, con un httpx falso."""
    pedidos: list = []

    def cliente_que_responde(respuesta):
        class Cliente:
            def __init__(self, **opciones):
                pedidos.append({"opciones": opciones})

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def stream(self, metodo, url, headers=None):
                pedidos[-1].update(metodo=metodo, url=url, headers=headers)
                if isinstance(respuesta, Exception):
                    raise respuesta
                return respuesta
        return Cliente

    def intentar(respuesta, token="token-de-prueba"):
        del pedidos[:]
        original_cliente, original_token = httpx.Client, settings.WASABIL_API_TOKEN
        httpx.Client, settings.WASABIL_API_TOKEN = cliente_que_responde(respuesta), token
        try:
            return descargas._bajar_de_wasabil(_uuid(99))
        except descargas.DocumentoNoDisponible:
            return None
        finally:
            httpx.Client, settings.WASABIL_API_TOKEN = original_cliente, original_token

    contenido = intentar(_RespuestaFalsa(200, [b"%PDF-1.7 ", b"bueno"]))
    check("§I pide GET {base}/documents/{uuid}/pdf con el token de la empresa",
          contenido == b"%PDF-1.7 bueno" and pedidos and pedidos[0].get("metodo") == "GET"
          and pedidos[0]["url"] == f"{settings.WASABIL_API_BASE.rstrip('/')}/documents/{_uuid(99)}/pdf"
          and pedidos[0]["headers"] == {"Authorization": "Bearer token-de-prueba"}, pedidos)
    check("§I …sin seguir redirecciones y con tiempo máximo de espera",
          pedidos[0]["opciones"].get("follow_redirects") is False
          and pedidos[0]["opciones"].get("timeout") is descargas.ESPERA_WASABIL
          and descargas.ESPERA_WASABIL.read is not None and descargas.ESPERA_WASABIL.connect is not None,
          pedidos[0]["opciones"])
    for nombre, respuesta in (
        ("un 401", _RespuestaFalsa(401, [b'{"error":"unauthorized"}'])),
        ("una redirección", _RespuestaFalsa(302, [b""])),
        ("un 206 (contenido parcial) aunque el cuerpo parezca un PDF", _RespuestaFalsa(206, [b"%PDF-1.7 a medias"])),
        ("un 200 que no es un PDF (HTML)", _RespuestaFalsa(200, [b"<html>login</html>"])),
        ("un 200 vacío", _RespuestaFalsa(200, [])),
        ("un error de red", httpx.ConnectError("sin red")),
        ("un timeout", httpx.ReadTimeout("lento")),
    ):
        check(f"§I rechaza {nombre}", intentar(respuesta) is None)

    enorme = _RespuestaFalsa(200, [b"%PDF" + b"0" * 396] + [b"0" * 400] * 9)
    tope = descargas.PESO_MAXIMO_PDF
    descargas.PESO_MAXIMO_PDF = 1000
    try:
        resultado = intentar(enorme)
    finally:
        descargas.PESO_MAXIMO_PDF = tope
    check("§I un PDF que pasa el peso máximo se corta al pasarlo: no se termina de leer",
          resultado is None and enorme.entregados == 3, (resultado, enorme.entregados))
    check("§I …y el peso máximo real deja pasar una guía normal",
          descargas.PESO_MAXIMO_PDF >= 5 * 1024 * 1024, descargas.PESO_MAXIMO_PDF)

    goteo = _RespuestaFalsa(200, [b"%PDF", b"0", b"0"])
    tope = descargas.SEGUNDOS_MAXIMOS_POR_BAJADA
    descargas.SEGUNDOS_MAXIMOS_POR_BAJADA = -1  # el plazo total ya venció cuando llega el primer trozo
    try:
        resultado = intentar(goteo)
    finally:
        descargas.SEGUNDOS_MAXIMOS_POR_BAJADA = tope
    check("§I una respuesta que gotea más allá del plazo total se corta",
          resultado is None and goteo.entregados == 1, (resultado, goteo.entregados))
    check("§I sin token configurado no se pide nada",
          intentar(_RespuestaFalsa(200, [b"%PDF"]), token="") is None and not pedidos, pedidos)


# ── §J ──────────────────────────────────────────────────────────────────────────────────

def _uuids_en_casilleros_distintos(cuantos: int) -> list:
    """uuid de prueba que no comparten candado (ver descargas._candado_de)."""
    elegidos, casilleros, n = [], set(), 500
    while len(elegidos) < cuantos:
        casillero = id(descargas._candado_de(_uuid(n)))
        if casillero not in casilleros:
            casilleros.add(casillero)
            elegidos.append(_uuid(n))
        n += 1
    return elegidos


def _uuids_en_el_mismo_casillero(cuantos: int) -> list:
    """uuid de prueba distintos que comparten candado (ver descargas._candado_de)."""
    por_casillero: dict = {}
    for n in range(600, 5000):
        juntos = por_casillero.setdefault(id(descargas._candado_de(_uuid(n))), [])
        juntos.append(_uuid(n))
        if len(juntos) == cuantos:
            return juntos
    raise AssertionError("no se encontraron suficientes uuid en el mismo casillero")


def _en_hilos(uuids: list) -> tuple:
    """Lanza descargas.pdf_del_dte(uuid) en un hilo por uuid. Devuelve (hilos, resultados)."""
    resultados: dict = {}

    def bajar(n, uuid):
        try:
            resultados[n] = descargas.pdf_del_dte(uuid)
        except descargas.DocumentoNoDisponible as error:
            resultados[n] = error

    hilos = [threading.Thread(target=bajar, args=(n, uuid)) for n, uuid in enumerate(uuids)]
    for hilo in hilos:
        hilo.start()
    return hilos, resultados


def _paso_j_varias_peticiones_a_la_vez():
    original = descargas._bajar_de_wasabil
    espera = descargas.SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA
    try:
        # El mismo documento pedido cinco veces a la vez.
        lento = descargas._bajar_de_wasabil = WasabilLento()
        hilos, resultados = _en_hilos([_uuid(400)] * 5)
        llego = lento.esperar_en_curso(1)
        time.sleep(0.2)  # las otras cuatro quedan esperando a la primera
        lento.puerta.set()
        for hilo in hilos:
            hilo.join(10)
        check("§J el mismo documento pedido 5 veces a la vez: a Wasabil se le pide UNA vez y las 5 lo reciben",
              llego and lento.llamadas == [_uuid(400)] and list(resultados.values()) == [_copia(_uuid(400))] * 5,
              (lento.llamadas, resultados))

        # Wasabil no responde: las que esperan a la misma bajada no esperan para siempre.
        descargas.SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA = 0.2
        lento = descargas._bajar_de_wasabil = WasabilLento()
        inicio = time.monotonic()
        hilos, resultados = _en_hilos([_uuid(401)] * 10)
        limite = time.monotonic() + 5
        while len(resultados) < 9 and time.monotonic() < limite:
            time.sleep(0.01)
        demora = time.monotonic() - inicio
        desistieron = [r for r in resultados.values() if isinstance(r, descargas.DocumentoNoDisponible)]
        check("§J el mismo documento pedido 10 veces con Wasabil lento: UNA llamada, y las otras 9 desisten "
              "en un par de segundos (no se quedan ocupando hilos del servidor)",
              len(desistieron) == 9 and demora < 3 and lento.llamadas == [_uuid(401)],
              (len(resultados), round(demora, 2), lento.llamadas))
        lento.puerta.set()
        for hilo in hilos:
            hilo.join(10)
        check("§J …y la que estaba bajando termina bien de todos modos",
              sorted(str(r) for r in resultados.values() if not isinstance(r, Exception)) == [_copia(_uuid(401))],
              resultados)
        descargas.SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA = espera

        # Lo que se puede responder sin esperar a nadie no espera, aunque otro documento
        # que comparte su candado esté bajando: el que YA tiene copia local, y el que
        # falló hace poco.
        lento = descargas._bajar_de_wasabil = WasabilLento()
        ya_bajado, bajando, ya_fallado = _uuids_en_el_mismo_casillero(3)
        with open(_copia(ya_bajado), "wb") as copia:
            copia.write(b"%PDF-1.4 copia que ya estaba")
        descargas._recordar_fallo(ya_fallado)
        hilos, resultados = _en_hilos([bajando])
        llego = lento.esperar_en_curso(1)
        inicio = time.monotonic()
        ruta = descargas.pdf_del_dte(ya_bajado)
        demora = time.monotonic() - inicio
        check("§J un documento con copia local se entrega al instante mientras otro del mismo candado está bajando",
              llego and ruta == _copia(ya_bajado) and demora < 0.5 and lento.llamadas == [bajando],
              (round(demora, 2), lento.llamadas))
        inicio, error = time.monotonic(), None
        try:
            descargas.pdf_del_dte(ya_fallado)
        except descargas.DocumentoNoDisponible as e:
            error = e
        demora = time.monotonic() - inicio
        check("§J …y uno que falló hace poco recibe su «no disponible» al instante, sin esperar ese candado",
              error is not None and demora < 0.5 and lento.llamadas == [bajando], (error, round(demora, 2)))
        lento.puerta.set()
        hilos[0].join(10)

        # Dos peticiones del mismo documento y Wasabil FALLA: la que esperaba no lo vuelve a pedir.
        descargas.reset()
        descargas.SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA = 8
        lento = descargas._bajar_de_wasabil = WasabilLento(falla=True)
        hilos, resultados = _en_hilos([_uuid(403)] * 2)
        llego = lento.esperar_en_curso(1)
        time.sleep(0.2)  # la segunda queda esperando a la primera
        lento.puerta.set()
        for hilo in hilos:
            hilo.join(10)
        check("§J si la bajada falla, la petición que la esperaba recibe «no disponible» sin volver a pedirle a Wasabil",
              llego and lento.llamadas == [_uuid(403)] and len(resultados) == 2
              and all(isinstance(r, descargas.DocumentoNoDisponible) for r in resultados.values()),
              (lento.llamadas, resultados))
        descargas.SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA = espera

        # Más documentos distintos que el máximo de bajadas simultáneas.
        lento = descargas._bajar_de_wasabil = WasabilLento()
        distintos = _uuids_en_casilleros_distintos(descargas.BAJADAS_SIMULTANEAS + 1)
        hilos, resultados = _en_hilos(distintos[:-1])
        llenas = lento.esperar_en_curso(descargas.BAJADAS_SIMULTANEAS)
        inicio, error = time.monotonic(), None
        try:
            descargas.pdf_del_dte(distintos[-1])
        except descargas.DocumentoNoDisponible as e:
            error = e
        demora = time.monotonic() - inicio
        check("§J con el máximo de bajadas en curso, una más recibe el aviso de inmediato y no llega a Wasabil",
              llenas and error is not None and demora < 1 and distintos[-1] not in lento.llamadas
              and lento.maximo == descargas.BAJADAS_SIMULTANEAS, (error, round(demora, 2), lento.maximo))
        lento.puerta.set()
        for hilo in hilos:
            hilo.join(10)
        check("§J …las que estaban en curso terminan bien",
              sorted(resultados.values()) == sorted(_copia(u) for u in distintos[:-1]), resultados)
        ruta = descargas.pdf_del_dte(distintos[-1])
        check("§J …y la que no cupo sale al reintentar (no quedó anotada como fallo)",
              ruta == _copia(distintos[-1]) and lento.llamadas.count(distintos[-1]) == 1, lento.llamadas)

        # La copia no se pudo guardar (disco lleno, carpeta sin permisos).
        normal = descargas._bajar_de_wasabil = WasabilFalso()
        carpeta = descargas.DIRECTORIO_PDF
        descargas.DIRECTORIO_PDF = _copia(_uuid(400))  # un ARCHIVO donde debería haber una carpeta
        try:
            errores = []
            for _ in range(2):
                try:
                    descargas.pdf_del_dte(_uuid(402))
                except descargas.DocumentoNoDisponible as e:
                    errores.append(e)
        finally:
            descargas.DIRECTORIO_PDF = carpeta
        check("§J si la copia no se puede guardar, es «no disponible» (no un error 500) y tampoco se reintenta en bucle",
              len(errores) == 2 and normal.llamadas == [_uuid(402)], (errores, normal.llamadas))
    finally:
        descargas._bajar_de_wasabil = original
        descargas.SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA = espera
        descargas.reset()


# ── §K ──────────────────────────────────────────────────────────────────────────────────

def _paso_k_tope_de_descargas(cab_a, cab_b, ids, wasabil):
    cupo.reset()
    tope = cupo.MAXIMOS[cupo.DESCARGAS]
    cupo.MAXIMOS[cupo.DESCARGAS] = 2
    try:
        ajenas = [_get(f"/guias/{ids['guia_b']}/pdf", cab_a).status_code for _ in range(3)]
        propias = [_get(f"/guias/{ids['electronica']}/pdf", cab_a).status_code for _ in range(2)]
        check("§K una descarga que da 404 no gasta el cupo: después de tres, las dos propias salen",
              ajenas == [404] * 3 and propias == [200] * 2, (ajenas, propias))
        antes_rastro, antes_llamadas = len(_rastros(ids["uid_a"])), len(wasabil.llamadas)
        r = _get(f"/guias/{ids['caida']}/pdf", cab_a)
        check("§K pasado el tope de descargas → 429, sin rastro y sin llamar a Wasabil",
              r.status_code == 429 and len(_rastros(ids["uid_a"])) == antes_rastro
              and len(wasabil.llamadas) == antes_llamadas, f"{r.status_code} {r.text[:80]}")
        check("§K …también para la guía firmada, que no pasa por Wasabil",
              _get(f"/guias/{ids['papel']}/firmada", cab_a).status_code == 429)
        check("§K …y la otra cuenta baja lo suyo sin enterarse",
              _get(f"/guias/{ids['guia_b']}/pdf", cab_b).status_code == 200)
    finally:
        cupo.MAXIMOS[cupo.DESCARGAS] = tope
        cupo.reset()


def run():
    del _fails[:]
    D.limpiar(MARK)
    wasabil, original = WasabilFalso(), descargas._bajar_de_wasabil
    try:
        with D.carpetas_de_prueba():
            descargas._bajar_de_wasabil = wasabil
            ids = _sembrar()
            cab_a, cab_b, cab_bod = _cab(ids["email_a"]), _cab(ids["email_b"]), _cab(ids["email_bod"])
            _paso_a_que_documentos_existen(cab_a, ids)
            _paso_b_el_enlace_publico_no_sale(cab_a, ids)
            _paso_c_el_pdf_lo_entrega_el_servidor(cab_a, ids, wasabil)
            _paso_c_la_sesion_queda_cerrada(ids)
            _paso_c_sin_rastro_no_hay_descarga(cab_a, ids, wasabil)
            _paso_d_cada_cliente_baja_lo_suyo(cab_a, cab_b, ids, wasabil)
            _paso_e_perfil_bodega(cab_bod, ids, wasabil)
            _paso_f_guia_firmada(cab_a, ids)
            _paso_g_a_quien_se_emitio(cab_a, ids, wasabil)
            _paso_h_facturado_del_tablero(cab_a, ids)
            _paso_k_tope_de_descargas(cab_a, cab_b, ids, wasabil)
            descargas._bajar_de_wasabil = original
            _paso_i_la_bajada_desde_wasabil()
            _paso_j_varias_peticiones_a_la_vez()
        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: documentos del portal")
        assert not _fails, _fails
    finally:
        descargas._bajar_de_wasabil = original
        D.limpiar(MARK)


def test_documentos_portal():
    run()


if __name__ == "__main__":
    run()
