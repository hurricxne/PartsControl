"""Integración de la FASE B: emisión de FACTURAS electrónicas (DTE 33) vía Wasabil.

Wasabil SIMULADO (mismo fake del test de guías). Cubre las dos vías del negocio:
  · Vía GUÍA: factura de una guía firmada — referencias 801 (OC) + 52 (guía),
    factura local SIN folio hasta que el SII confirma, folio al emitirse.
  · Vía ANTICIPO: factura es_anticipo ligada a adelantos — la aplicación del
    adelanto como cobranza se DIFIERE hasta el Emitido (una factura rechazada
    no mueve plata).
  · Factura final CON descuento de anticipo: línea NEGATIVA + referencia 33.
Bordes: folio manual en el payload (bloquea), receptor incompleto (bloquea),
anticipo sin folio SII (bloquea), fallo ambiguo → claim → adopción por la
referencia interna FACT-<id>, fallo del SII → reintento, doble emisión (tope),
eliminar con DTE emitido (409) / fallido (limpia).

LIMPIA todo al final. Corre con:
    ./venv/bin/python wasabil_dte/tests/test_facturas_integration.py
    ./venv/bin/python -m pytest wasabil_dte/tests/test_facturas_integration.py -q
"""
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContAdelanto,
)
from wasabil_dte import client as wasabil_client  # noqa: E402
from wasabil_dte.models import WasabilDte  # noqa: E402
from wasabil_dte.router import router as wasabil_router  # noqa: E402
import wasabil_dte.router as wasabil_router_mod  # noqa: E402
import routers.contabilidad as cont  # noqa: E402
from routers.contabilidad import router as cont_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_WFAC__"
# Fecha de EMISIÓN de la guía EN PAPEL. DISTINTA de la fecha de cierre del despacho a
# propósito: la referencia 52 de la factura cita ESTA, y si el código volviera a usar
# `fecha_despacho` el valor delataría cuál está leyendo.
FECHA_GUIA_PAPEL = date(2026, 6, 18)

CURRENT = {"empresa": "mineria", "id": None}

app = FastAPI()
app.include_router(wasabil_router, prefix="/api")
app.include_router(cont_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


# ─── Wasabil SIMULADO (mismo fake del test de guías) ────────────────────────────
class FakeWasabil:
    def __init__(self):
        self.configurado = True
        self.cliente = {"id": 158381, "rut": "78.279.030-7",
                        "name": "H-E PARTS INTERNATIONAL CHILE SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS", "default": True}],
                        "addresses": [{"address": "RUTA 26 KM 15 S/N",
                                       "comuna": "Antofagasta", "city": "Antofagasta",
                                       "default": True}]}
        self.crear_falla = None
        self.status_respuesta = 2
        self.estado_final = 3
        self.display_error = None
        self.docs_buscables: list = []
        self.busqueda_completa = True
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
        wasabil_client.buscar_documentos = lambda search: (
            list(self.docs_buscables), self.busqueda_completa)

    def _crear(self, payload):
        if self.crear_falla:
            raise self.crear_falla
        # Validaciones REALES del API (verificadas en borrador el 2026-07-20):
        # price ≥ 0, quantity > 0, paymentMethod obligatorio, referencias ≤ 5.
        for i, d in enumerate(payload.get("details") or []):
            if float(d.get("price") or 0) < 0:
                raise wasabil_client.WasabilError(
                    f"Wasabil respondió 400: details.{i}.price: El campo debe ser "
                    "mayor o igual que 0.", ambiguo=False)
            if float(d.get("quantity") or 0) <= 0:
                raise wasabil_client.WasabilError(
                    f"Wasabil respondió 400: details.{i}.quantity: El campo debe ser "
                    "mayor que 0.", ambiguo=False)
        if payload.get("sii_document_type_code") == 33 and not payload.get("payment_method"):
            raise wasabil_client.WasabilError(
                "Wasabil respondió 400: payment_method: obligatorio", ambiguo=False)
        if len(payload.get("references") or []) > 5:
            raise wasabil_client.WasabilError(
                "Wasabil respondió 400: references: máximo 5", ambiguo=False)
        self.creados.append(payload)
        return {"uuid": f"uuid-f{len(self.creados)}", "status_id": self.status_respuesta}

    def _estado(self, uuid):
        return {"uuid": uuid, "status_id": self.estado_final,
                "display_error": self.display_error}

    def _obtener(self, uuid):
        folio = "888" if uuid == "uuid-perdido" else "777"
        return {"uuid": uuid, "status_id": self.estado_final, "folio": folio,
                "display_error": self.display_error,
                "document_pdf_url": f"https://api.wasabil.com/pdf/{folio}",
                "document_xml_url": f"https://api.wasabil.com/xml/{folio}"}


fake = FakeWasabil()
fake.install()

# Precios deterministas (el pricing tiene sus propios tests): neto 10.000/u
PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0),
                   "total_venta_clp": PRECIOS.get(i.id, 0.0) * float(i.cantidad or 0)}
            for i in items}
    neto = sum(PRECIOS.get(i.id, 0.0) * float(i.cantidad or 0) for i in items)
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


# N° de guía en PAPEL tecleado por el operador: NUMÉRICO, porque el folio de una guía
# manual viene autorizado por el SII y armar_referencias_factura exige folio numérico
# en la referencia 52 (apunta a un documento tributario). Antes decía "G-MANUAL-9".
N_GUIA_MANUAL = "900456"


def _crear_venta(db, *, cantidad=10, precio=10000.0, sufijo="A", con_guia=True,
                 guia_manual=N_GUIA_MANUAL):
    """Venta 1 ítem + despacho despachado y FIRMADO (facturable)."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} HEPI",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro de aceite motor", cantidad=cantidad,
                        estado_item="despachado")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK[:2]}OC-{sufijo}",
                   fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = None
    if con_guia:
        desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                        estado="despachado", guia_firmada=1, numero_guia=guia_manual,
                        # Guía en PAPEL: su fecha de emisión es lo que cita la referencia 52.
                        fecha_guia=(FECHA_GUIA_PAPEL if guia_manual else None))
        db.add(desp); db.flush()
        db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id,
                            qty_despachada=cantidad))
    db.commit()
    PRECIOS[it.id] = precio
    return cot, oc, desp, it


def _limpiar(db):
    db.rollback()
    # Anclas CONSERVADAS de este fake: al eliminar una factura con DTE rechazado,
    # routers/contabilidad.py:_conservar_ancla_dte desliga la fila (factura_id = NULL) en
    # vez de borrarla, para no perder la única llave hacia el documento de Wasabil. Esa
    # fila ya no cuelga de ninguna factura ni despacho, así que la limpieza por padre NO
    # la alcanza y se acumulaba una por corrida. El uuid `uuid-f<N>` lo inventa este fake,
    # así que identifica exactamente sus propias sobras (ningún documento real lo lleva).
    db.query(WasabilDte).filter(WasabilDte.despacho_id.is_(None),
                                WasabilDte.factura_id.is_(None),
                                WasabilDte.uuid.like("uuid-f%")).delete(synchronize_session=False)
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
    if cots:
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.commit()


def run():
    db = SessionLocal()
    # Re-instalar NUESTRO fake al empezar: si pytest importó también el test de
    # guías (que instala el suyo a nivel de módulo), el último import ganó y las
    # funciones del client apuntarían al fake equivocado.
    fake.install()
    cont._precios_de_cotizacion = _fake_precios
    try:
        _limpiar(db)

        # ═══ 1 · VÍA GUÍA: preview → emitir → sondeo → folio en la factura local ═══
        cot, oc, desp, it = _crear_venta(db, sufijo="A")   # neto 100.000
        r = client.post("/api/wasabil/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        p = r.json()
        check("1 preview 200 y puede_emitir", r.status_code == 200 and p["puede_emitir"], r.text)
        tipos_ref = [x["tipo"] for x in p["referencias"]]
        check("1 referencias = OC(801) + guía(52)", tipos_ref == ["801", "52"], p["referencias"])
        check("1 receptor desde la ficha Wasabil (comuna incluida)",
              p["receptor"]["fuente"] == "wasabil" and p["receptor"]["comuna"], p["receptor"])
        check("1 totales 100.000/19.000/119.000",
              p["totales"] == {"neto": 100000.0, "iva": 19000.0, "bruto": 119000.0}, p["totales"])

        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("1 emitir 200 (respuesta procesando)", r.status_code == 200, r.text)
        fac_id = r.json()["factura_id"]
        db.rollback()
        fac = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fac_id).first()
        check("1 factura local creada SIN folio (espera al SII)",
              fac is not None and fac.numero_factura is None, fac and fac.numero_factura)
        payload_enviado = fake.creados[-1]
        check("1 payload: issue=true, tipo 33, invoice_reference=FACT-<id>",
              payload_enviado.get("issue") is True
              and payload_enviado.get("sii_document_type_code") == 33
              and payload_enviado.get("invoice_reference") == f"FACT-{fac_id}", payload_enviado)
        refs_env = payload_enviado.get("references") or []
        check("1 payload: referencias 801 y 52 con folio de la guía",
              [x["document_type"] for x in refs_env] == ["801", "52"]
              and refs_env[1]["folio"] == N_GUIA_MANUAL, refs_env)

        r = client.get(f"/api/wasabil/facturas/{fac_id}/estado")
        check("1 sondeo → emitido con folio 777", r.status_code == 200
              and r.json()["estado"] == "emitido" and r.json()["folio"] == "777", r.text)
        db.rollback()
        fac = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fac_id).first()
        check("1 folio del SII escrito en la factura local", fac.numero_factura == "777", fac.numero_factura)
        r = client.get("/api/contabilidad/facturas")
        fila = next((x for x in r.json()["facturas"] if x["id"] == fac_id), None)
        check("1 listado trae dte_folio/dte_estado embebidos",
              fila and fila.get("dte_folio") == "777" and fila.get("dte_estado") == "emitido", fila)
        _limpiar(db)

        # ═══ 2 · ADELANTO DIFERIDO: la cobranza cae SOLO al quedar emitida ═══
        cot, oc, desp, it = _crear_venta(db, sufijo="B")
        adel = ContAdelanto(oc_cliente_id=oc.id, empresa="mineria",
                            estado="aprobado", monto_esperado=50000, monto=50000,
                            monto_aplicado=0)
        db.add(adel); db.commit()
        fake.status_respuesta = 2   # crear → procesando: emitir NO deja emitida aún
        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        fac_id = r.json()["factura_id"]
        db.rollback()
        n_cobr = db.query(ContCobranza).filter(ContCobranza.factura_id == fac_id).count()
        check("2 con la emisión EN PROCESO: cero cobranzas (adelanto diferido)", n_cobr == 0, n_cobr)
        client.get(f"/api/wasabil/facturas/{fac_id}/estado")   # → emitido (fake)
        db.rollback()
        cobr = db.query(ContCobranza).filter(ContCobranza.factura_id == fac_id).all()
        check("2 al EMITIRSE: el adelanto aprobado cae como cobranza medio='adelanto'",
              len(cobr) == 1 and cobr[0].medio == "adelanto" and float(cobr[0].monto) == 50000,
              [(c.medio, float(c.monto)) for c in cobr])
        _limpiar(db)

        # ═══ 3 · VÍA ANTICIPO electrónica + factura FINAL con descuento y ref 33 ═══
        cot, oc, desp, it = _crear_venta(db, sufijo="C")
        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "es_anticipo": True,
                              "monto_neto_anticipo": 30000})
        check("3 anticipo electrónico emitido 200", r.status_code == 200, r.text)
        ant_id = r.json()["factura_id"]
        client.get(f"/api/wasabil/facturas/{ant_id}/estado")   # folio 777
        db.rollback()
        ant = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == ant_id).first()
        check("3 anticipo con folio del SII y es_anticipo", ant.numero_factura == "777"
              and ant.es_anticipo == 1 and ant.despacho_id is None, (ant.numero_factura, ant.es_anticipo))

        r = client.post("/api/wasabil/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        p = r.json()
        check("3 preview final: referencias 801 + 52 + 33 (anticipo)",
              [x["tipo"] for x in p["referencias"]] == ["801", "52", "33"]
              and p["referencias"][2]["folio"] == "777", p["referencias"])
        check("3 preview final: descuento del anticipo (neto 70.000)",
              p["totales"]["neto"] == 70000.0 and len(p.get("descuentos") or []) == 1, p["totales"])
        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("3 emitir final 200", r.status_code == 200, r.text)
        fin_id = r.json()["factura_id"]
        payload_fin = fake.creados[-1]
        # El descuento del anticipo NO viaja como línea negativa (el API real la
        # rechaza): va como `discount` % sobre las líneas, con neto exacto.
        check("3 payload final: SIN líneas negativas (API real las rechaza)",
              all(d["price"] >= 0 and d["quantity"] > 0 for d in payload_fin["details"]),
              payload_fin["details"])
        con_dcto = [d for d in payload_fin["details"] if d.get("discount")]
        neto_dte = sum(round(d["price"] * d["quantity"], 2) * (1 - d.get("discount", 0) / 100.0)
                       for d in payload_fin["details"])
        check("3 payload final: descuento % aplicado y neto exacto 70.000",
              len(con_dcto) >= 1 and abs(neto_dte - 70000.0) < 0.01,
              (payload_fin["details"], neto_dte))
        check("3 payload final: paymentMethod presente (obligatorio en el 33)",
              payload_fin.get("payment_method") in ("contado", "credito"), payload_fin.get("payment_method"))
        check("3 payload final: referencia 33 al folio del anticipo",
              any(x["document_type"] == "33" and x["folio"] == "777"
                  for x in payload_fin["references"]), payload_fin["references"])
        # v3 (hallazgo folio 137): el motivo de la referencia NO repite lo que Wasabil
        # ya imprime por el tipo y el folio — si no, la OC / la guía salen escritas dos
        # veces en el papel. Se conserva solo donde aporta (el 33 dice que se descuenta).
        por_tipo = {x["document_type"]: x for x in payload_fin["references"]}
        check("3 payload final: la ref a la OC (801) va SIN motivo redundante",
              "reason" not in por_tipo.get("801", {}), por_tipo.get("801"))
        check("3 payload final: la ref a la guía (52) va SIN motivo redundante",
              "reason" not in por_tipo.get("52", {}), por_tipo.get("52"))
        check("3 payload final: la ref al anticipo (33) explica el descuento, sin repetir folio",
              por_tipo.get("33", {}).get("reason") == "Descuento anticipo"
              and "777" not in por_tipo.get("33", {}).get("reason", ""), por_tipo.get("33"))
        check("3 payload final: NINGÚN motivo contiene el folio de su propia referencia",
              all(str(x["folio"]) not in (x.get("reason") or "")
                  for x in payload_fin["references"]), payload_fin["references"])
        _limpiar(db)

        # ═══ 4 · BLOQUEOS del preview ═══
        cot, oc, desp, it = _crear_venta(db, sufijo="D")
        r = client.post("/api/wasabil/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": "123"})
        check("4 folio manual digitado → bloquea (el folio lo asigna el SII)",
              not r.json()["puede_emitir"]
              and any("folio" in x.lower() for x in r.json()["problemas"]), r.json()["problemas"])
        # receptor sin comuna → bloquea (la 33 es más estricta que la 52)
        fake.cliente["addresses"][0]["comuna"] = ""
        r = client.post("/api/wasabil/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("4 ficha sin comuna → bloquea", not r.json()["puede_emitir"], r.json()["problemas"])
        fake.cliente["addresses"][0]["comuna"] = "Antofagasta"
        # cliente inexistente en Wasabil → bloquea
        rut_ok = fake.cliente["rut"]
        fake.cliente = None
        r = client.post("/api/wasabil/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("4 cliente no existe en Wasabil → bloquea", not r.json()["puede_emitir"], r.json()["problemas"])
        fake.install()  # restaurar (install rehace los lambdas sobre el cliente nuevo)
        fake.cliente = {"id": 158381, "rut": rut_ok,
                        "name": "H-E PARTS INTERNATIONAL CHILE SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS", "default": True}],
                        "addresses": [{"address": "RUTA 26 KM 15 S/N",
                                       "comuna": "Antofagasta", "city": "Antofagasta",
                                       "default": True}]}
        # anticipo manual SIN folio → la final no puede referenciarlo (33)
        ant_sin_folio = ContFacturaCliente(
            empresa="mineria", oc_cliente_id=oc.id, cotizacion_id=cot.id,
            es_anticipo=1, monto_neto=10000, iva=1900, monto_bruto=11900, saldo=11900)
        db.add(ant_sin_folio); db.flush()
        db.add(ContFacturaClienteItem(factura_id=ant_sin_folio.id, numero_parte="ANTICIPO",
                                      descripcion="ant", cantidad=1,
                                      precio_unit_neto=10000, total_neto=10000))
        db.commit()
        r = client.post("/api/wasabil/facturas/preview",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("4 anticipo descontable SIN folio SII → bloquea la referencia 33",
              not r.json()["puede_emitir"]
              and any("anticipo" in x.lower() for x in r.json()["problemas"]), r.json()["problemas"])
        _limpiar(db)

        # ═══ 5 · FALLO AMBIGUO → claim → adopción por referencia FACT-<id> ═══
        cot, oc, desp, it = _crear_venta(db, sufijo="E")
        fake.crear_falla = wasabil_client.WasabilError("timeout", ambiguo=True)
        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("5 fallo ambiguo → 502", r.status_code == 502, r.text)
        db.rollback()
        fac = (db.query(ContFacturaCliente)
               .filter(ContFacturaCliente.oc_cliente_id == oc.id).first())
        dte = db.query(WasabilDte).filter(WasabilDte.factura_id == fac.id).first()
        check("5 factura local sin folio + claim puesto (ancla)",
              fac.numero_factura is None and dte is not None
              and dte.en_vuelo_desde is not None and dte.uuid is None, dte)
        fake.crear_falla = None
        r = client.post(f"/api/wasabil/facturas/{fac.id}/reintentar")
        check("5 claim fresco bloquea reintento 409", r.status_code == 409, r.text)
        dte.en_vuelo_desde = datetime.now() - timedelta(seconds=600)
        db.commit()
        fake.docs_buscables = [{"uuid": "uuid-perdido", "status_id": 3, "folio": "888",
                                "invoice_reference": f"FACT-{fac.id}",
                                "document_pdf_url": "https://api.wasabil.com/pdf/888"}]
        creados_antes = len(fake.creados)
        r = client.post(f"/api/wasabil/facturas/{fac.id}/reintentar")
        check("5 reintento ADOPTA el documento perdido (no re-crea)",
              r.status_code == 200 and r.json()["folio"] == "888"
              and len(fake.creados) == creados_antes, r.text)
        db.rollback()
        fac = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fac.id).first()
        check("5 folio adoptado en la factura local", fac.numero_factura == "888", fac.numero_factura)
        fake.docs_buscables = []
        _limpiar(db)

        # ═══ 6 · FALLO del SII → reintento re-crea; eliminar con DTE ═══
        cot, oc, desp, it = _crear_venta(db, sufijo="F")
        fake.status_respuesta = 4
        fake.estado_final = 4
        fake.display_error = "RUT receptor inválido"
        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        fac_id = r.json()["factura_id"]
        check("6 emisión rechazada por el SII → puede_reintentar",
              r.json()["estado"] == "fallido" and r.json()["puede_reintentar"], r.json())
        # eliminar con DTE FALLIDO → permitido (limpia el ancla)
        r = client.delete(f"/api/contabilidad/facturas/{fac_id}")
        check("6 eliminar factura con DTE fallido → 200 y limpia el ancla",
              r.status_code == 200
              and db.query(WasabilDte).filter(WasabilDte.factura_id == fac_id).count() == 0, r.text)
        # REGRESIÓN (auditoría 2026-07-21): eliminar con la respuesta de Wasabil PERDIDA.
        # Un fallo AMBIGUO (timeout/5xx) deja uuid=None pero conserva en_vuelo_desde: el
        # documento PUDO nacer con folio real. Aunque el claim expire, borrar aquí
        # destruiría el ancla FACT-<id> (dejando el documento real inadoptable) y
        # habilitaría un SEGUNDO DTE por la misma mercadería. Debe seguir bloqueado hasta
        # que un reintento verifique contra Wasabil.
        fake.crear_falla = wasabil_client.WasabilError("timeout", ambiguo=True)
        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        fake.crear_falla = None
        db.rollback()  # REPEATABLE READ: ver lo que commiteó el TestClient
        # el emitir respondió 502 (fallo ambiguo), así que la factura sin folio se busca
        fac_amb_id = (db.query(ContFacturaCliente)
                      .filter(ContFacturaCliente.oc_cliente_id == oc.id,
                              ContFacturaCliente.numero_factura.is_(None))
                      .order_by(ContFacturaCliente.id.desc()).first().id)
        dte_amb = db.query(WasabilDte).filter(WasabilDte.factura_id == fac_amb_id).first()
        dte_amb.en_vuelo_desde = datetime.now() - timedelta(seconds=600)  # claim expirado
        db.commit()
        r = client.delete(f"/api/contabilidad/facturas/{fac_amb_id}")
        check("6 eliminar tras fallo AMBIGUO con claim expirado → 409 (el doc pudo emitirse)",
              r.status_code == 409
              and db.query(WasabilDte).filter(WasabilDte.factura_id == fac_amb_id).count() == 1,
              r.text)
        # y tras un reintento que CONFIRMA que no existe en Wasabil, sí se puede eliminar
        fake.docs_buscables = []
        fake.crear_falla = wasabil_client.WasabilError("400 payload inválido", ambiguo=False)
        client.post(f"/api/wasabil/facturas/{fac_amb_id}/reintentar")
        fake.crear_falla = None
        db.rollback()
        r = client.delete(f"/api/contabilidad/facturas/{fac_amb_id}")
        check("6 tras reintento con fallo CONFIRMADO → eliminar 200 (ancla limpia)",
              r.status_code == 200
              and db.query(WasabilDte).filter(WasabilDte.factura_id == fac_amb_id).count() == 0,
              r.text)

        # nueva emisión OK → eliminar con DTE EMITIDO → 409
        fake.status_respuesta = 2
        fake.estado_final = 3
        fake.display_error = None
        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        fac_id = r.json()["factura_id"]
        client.get(f"/api/wasabil/facturas/{fac_id}/estado")
        r = client.delete(f"/api/contabilidad/facturas/{fac_id}")
        check("6 eliminar factura con DTE EMITIDO → 409 (anular en Wasabil primero)",
              r.status_code == 409 and "folio" in r.json()["detail"].lower(), r.text)
        # doble emisión de la misma guía → tope de lo facturable
        r = client.post("/api/wasabil/facturas/emitir",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id})
        check("6 segunda emisión de la MISMA guía → 409 (nada por facturar)",
              r.status_code == 409, r.text)
        _limpiar(db)

    finally:
        cont._precios_de_cotizacion = _orig_precios
        fake.status_respuesta = 2
        fake.estado_final = 3
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    print()
    if _fails:
        print(f"FALLARON {len(_fails)}: {_fails}")
        sys.exit(1)
    print("=== FASE B INTEGRACIÓN: TODO OK ===")


def test_wasabil_facturas_integracion():
    run()
    assert not _fails, _fails


if __name__ == "__main__":
    run()
