"""Test de integración del módulo Wasabil DTE contra la DB local (Wasabil SIMULADO).

Monta el router en una app efímera (sin tocar main.py), sobreescribe la auth para
simular usuarios de distintas empresas, SIMULA el API de Wasabil (sin red) y
ejerce el flujo completo: preview → emitir → estado → folio en despacho.numero_guia
→ anti doble emisión → reintento de fallido → candado de empresa. LIMPIA todo lo
que creó al terminar (deja la DB intacta).

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_integration.py -q
(también:   ./venv/bin/python wasabil_dte/tests/test_integration.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem  # noqa: E402
from wasabil_dte import client as wasabil_client  # noqa: E402
from wasabil_dte import router as wasabil_router_mod  # noqa: E402
from wasabil_dte.router import router  # noqa: E402
from wasabil_dte.models import WasabilDte  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura wasabil_dte

MARK = "__TEST_WDTE__"
CURRENT = {"empresa": "mineria", "id": None}

app = FastAPI()
app.include_router(router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ─── Wasabil SIMULADO (se instala con monkeypatch sobre el módulo client) ────────
class FakeWasabil:
    """Simula el API: configurable por test (fallos, estados, cliente inexistente)."""

    def __init__(self):
        self.configurado = True
        # Ficha en el FORMATO REAL del API (giros[]/addresses[] anidados): así el
        # test ejercita la normalización de client._normalizar_cliente y protege
        # contra la regresión del 2026-07-17 (el API no devuelve campos planos).
        self.cliente = {"id": 158381, "rut": "78.279.030-7",
                        "name": "H-E PARTS INTERNATIONAL CHILE SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS", "default": True}],
                        "addresses": [{"address": "RUTA 26 KM 15 S/N",
                                       "comuna": "Antofagasta", "city": "Antofagasta",
                                       "default": True}]}
        self.crear_falla = None          # Exception a lanzar en crear_documento
        self.status_respuesta = 2        # status_id que devuelve crear_documento
        self.estado_final = 3            # status_id que devuelve estado_documento
        self.display_error = None
        self.docs_buscables: list = []   # para buscar_documentos (reintento sin uuid)
        self.busqueda_completa = True    # False = lista truncada por paginación
        self.creados: list = []          # payloads recibidos (auditoría del test)

    def install(self):
        wasabil_client.esta_configurado = lambda: self.configurado
        # Normaliza igual que la función real: así el router recibe la ficha aplanada
        # (giro/dirección/comuna) y el test cubre _normalizar_cliente.
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
        self.creados.append(payload)
        return {"uuid": f"uuid-{len(self.creados)}", "status_id": self.status_respuesta}

    def _estado(self, uuid):
        return {"uuid": uuid, "status_id": self.estado_final,
                "display_error": self.display_error}

    def _obtener(self, uuid):
        # El folio depende del documento consultado (la adopción de un doc perdido
        # trae SU folio real, no uno genérico)
        folio = "888" if uuid == "uuid-perdido" else "777"
        return {"uuid": uuid, "status_id": self.estado_final, "folio": folio,
                "display_error": self.display_error,
                "document_pdf_url": f"https://api.wasabil.com/pdf/{folio}",
                "document_xml_url": f"https://api.wasabil.com/xml/{folio}"}


fake = FakeWasabil()
fake.install()
# Precios deterministas (el pricing engine tiene sus propios tests)
PRECIOS = {}
wasabil_router_mod._precios = lambda db, cot: PRECIOS


# ─── Datos de prueba en la DB local ─────────────────────────────────────────────
def _crear_datos(db, rut="78.279.030-7", fecha_oc="2026-06-10", numero_oc=None,
                 estado_despacho="en_preparacion"):
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} CLIENTE", rut_cliente=rut)
    db.add(cot); db.flush()
    it1 = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                         descripcion="Filtro de aceite motor", cantidad=10,
                         estado_item="en_bodega")
    it2 = ItemCotizacion(cotizacion_id=cot.id, item_num=2, numero_parte="6I-2503",
                         descripcion="Sello de polvo", cantidad=20, estado_item="en_bodega")
    db.add_all([it1, it2]); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=numero_oc or f"{MARK}-OC",
                   fecha_oc=fecha_oc)
    db.add(oc); db.flush()
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    estado=estado_despacho, contacto_destinatario="Juan Pérez",
                    direccion_entrega="Faena Salar, Antofagasta")
    db.add(desp); db.flush()
    # Despacho PARCIAL: 4 de 10 del ítem 1, 20 de 20 del ítem 2
    db.add_all([
        DespachoItem(despacho_id=desp.id, item_cotizacion_id=it1.id, qty_despachada=4),
        DespachoItem(despacho_id=desp.id, item_cotizacion_id=it2.id, qty_despachada=20),
    ])
    db.commit()
    PRECIOS.clear()
    PRECIOS.update({it1.id: {"precio_venta_clp": 15000},
                    it2.id: {"precio_venta_clp": 2500}})
    return cot, oc, desp


def _limpiar(db):
    for cot in db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all():
        oc = db.query(OcCliente).filter(OcCliente.cotizacion_id == cot.id).first()
        if oc:
            for d in db.query(Despacho).filter(Despacho.oc_cliente_id == oc.id).all():
                db.query(WasabilDte).filter(WasabilDte.despacho_id == d.id).delete()
                db.query(DespachoItem).filter(DespachoItem.despacho_id == d.id).delete()
                db.delete(d)
            db.delete(oc)
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot.id).delete()
        db.delete(cot)
    db.commit()


def run():
    db = SessionLocal()
    # Re-instalar NUESTRO fake al empezar: si pytest importó también el test de
    # facturas (que instala el suyo a nivel de módulo), el último import ganó.
    fake.install()
    _limpiar(db)  # por si un run anterior murió a medias
    try:
        CURRENT["empresa"] = "mineria"

        # ── Preview feliz (despacho parcial: 4/10 + 20/20) ──
        _cot, oc, desp = _crear_datos(db)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/preview")
        check("preview 200", r.status_code == 200, r.text)
        p = r.json()
        check("preview puede_emitir", p["puede_emitir"] is True, p["problemas"])
        check("preview receptor desde Wasabil", p["receptor"]["fuente"] == "wasabil"
              and p["receptor"]["comuna"] == "Antofagasta", p["receptor"])
        check("preview 2 lineas parciales", len(p["lineas"]) == 2
              and p["lineas"][0]["quantity"] == 4, p["lineas"])
        neto = 4 * 15000 + 20 * 2500
        check("preview cuadratura", p["totales"] == {"neto": neto, "iva": round(neto * 0.19),
                                                     "total": neto + round(neto * 0.19)}, p["totales"])
        check("preview ref 801 con fecha", p["referencias"][0]["tipo"] == "801"
              and p["referencias"][0]["fecha"] == "2026-06-10", p["referencias"])
        check("preview no emite", p["documento"]["issue"] is False, p["documento"])
        check("preview trae tipos de traslado (venta default + interno)",
              p["tipo_traslado"] == 1
              and any(t["codigo"] == 1 and "venta" in t["label"].lower() for t in p["tipos_traslado"])
              and any(t["codigo"] == 5 and "interno" in t["label"].lower() for t in p["tipos_traslado"]),
              p.get("tipos_traslado"))

        # ── Emitir (procesando) → estado (emitido) → folio en el despacho ──
        r = client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        check("emitir 200", r.status_code == 200, r.text)
        e = r.json()
        check("emitir uuid persistido", e["uuid"] == "uuid-1", e)
        check("emitir issue=true al confirmar", fake.creados[0]["issue"] is True)
        check("emitir payload REST snake_case",
              fake.creados[0]["sii_document_type_code"] == 52
              and fake.creados[0]["dispatch_guide"] == {"dispatch_type_code": 1}
              and fake.creados[0]["client_id"] == 158381, fake.creados[0])
        r = client.get(f"/api/wasabil/despachos/{desp.id}/estado")
        s = r.json()
        check("estado emitido con folio", s["estado"] == "emitido" and s["folio"] == "777", s)
        check("estado pdf/xml", "pdf/777" in s["pdf_url"], s)
        # rollback: cierra la transacción del test (MySQL REPEATABLE READ congela el
        # snapshot; sin esto no se ven los commits hechos por la sesión del router)
        db.rollback()
        check("folio grabado en despacho.numero_guia",
              db.get(Despacho, desp.id).numero_guia == "777")

        # ── Anti doble emisión ──
        r = client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        check("re-emitir 409", r.status_code == 409, r.text)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        check("reintentar emitida 409", r.status_code == 409, r.text)
        r = client.get(f"/api/wasabil/despachos/estado-batch?ids={desp.id}")
        check("estado-batch", str(desp.id) in r.json()
              and r.json()[str(desp.id)]["folio"] == "777", r.json())
        _limpiar(db)

        # ── Validaciones bloqueantes ──
        _cot, oc, desp = _crear_datos(db, rut="", fecha_oc="por confirmar")
        r = client.post(f"/api/wasabil/despachos/{desp.id}/preview")
        p = r.json()
        check("sin rut ni fecha bloquea", p["puede_emitir"] is False
              and any("RUT" in x for x in p["problemas"])
              and any("fecha de la OC" in x for x in p["problemas"]), p["problemas"])
        r = client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        check("emitir bloqueado 409", r.status_code == 409, r.text)
        _limpiar(db)

        # N° de OC > 18 caracteres → el SII lo rechaza; se bloquea en el preview con
        # mensaje claro (en vez de fallar recién al emitir). Confirmado contra el API
        # real 2026-07-17: 'references.0.folio debe tener máximo 18 caracteres'.
        _cot, oc, desp = _crear_datos(db, numero_oc="OC-DEMASIADO-LARGA-123456")
        p = client.post(f"/api/wasabil/despachos/{desp.id}/preview").json()
        check("N° OC > 18 chars bloquea con mensaje",
              p["puede_emitir"] is False
              and any("18" in x and "OC" in x for x in p["problemas"]), p["problemas"])
        _limpiar(db)

        # ── Tipo de traslado elegible: emitir como traslado interno (5) ──
        _cot, oc, desp = _crear_datos(db)
        fake.creados.clear()
        r = client.post(f"/api/wasabil/despachos/{desp.id}/emitir?tipo_traslado=5")
        check("emitir traslado interno 200", r.status_code == 200, r.text)
        check("emitir usa dispatch_type_code 5",
              fake.creados[0]["dispatch_guide"] == {"dispatch_type_code": 5}, fake.creados[0])
        db.rollback()
        # Tipo fuera de la tabla del SII → 400 (ni preview ni emisión lo aceptan)
        _limpiar(db)
        _cot, oc, desp = _crear_datos(db)
        check("preview tipo_traslado inválido → 400",
              client.post(f"/api/wasabil/despachos/{desp.id}/preview?tipo_traslado=99").status_code == 400)
        check("emitir tipo_traslado inválido → 400",
              client.post(f"/api/wasabil/despachos/{desp.id}/emitir?tipo_traslado=99").status_code == 400)
        _limpiar(db)

        # ── Sondeo degrada ELEGANTE ante respuesta inesperada de Wasabil ──
        # (2xx con data:null → client lanza WasabilError AMBIGUO; el GET /estado
        # responde error_consulta y el reintento aborta seguro — nunca un 500)
        _cot, oc, desp = _crear_datos(db)
        client.post(f"/api/wasabil/despachos/{desp.id}/emitir")   # queda "procesando"
        _orig_estado = wasabil_client.estado_documento
        wasabil_client.estado_documento = lambda u: (_ for _ in ()).throw(
            wasabil_client.WasabilError("respuesta inesperada al consultar el estado",
                                        ambiguo=True))
        r = client.get(f"/api/wasabil/despachos/{desp.id}/estado")
        check("estado con respuesta inesperada degrada (error_consulta, no 500)",
              r.status_code == 200 and r.json().get("error_consulta")
              and r.json()["estado"] != "emitido", r.text)
        db.rollback()
        fila = db.query(WasabilDte).filter(WasabilDte.despacho_id == desp.id).first()
        fila.en_vuelo_desde = None   # claim vencido: habilita intentar el reintento
        db.commit()
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        check("reintentar sin poder verificar el estado → 502 (no re-crea, no 500)",
              r.status_code == 502, r.text)
        wasabil_client.estado_documento = _orig_estado
        _limpiar(db)

        # ── El reintento puede CORREGIR el tipo de traslado (emitió con 1, retry 5) ──
        fake.docs_buscables = []
        fake.crear_falla = wasabil_client.WasabilError("conexión rechazada", ambiguo=False)
        _cot, oc, desp = _crear_datos(db)
        client.post(f"/api/wasabil/despachos/{desp.id}/emitir")   # falla; claim libre
        fake.crear_falla = None
        creados_antes = len(fake.creados)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar?tipo_traslado=5")
        check("reintento con tipo corregido 200", r.status_code == 200, r.text)
        check("reintento re-crea con dispatch_type_code 5",
              len(fake.creados) == creados_antes + 1
              and fake.creados[-1]["dispatch_guide"] == {"dispatch_type_code": 5},
              fake.creados[-1] if fake.creados else None)
        _limpiar(db)

        _cot, oc, desp = _crear_datos(db, estado_despacho="despachado")
        r = client.post(f"/api/wasabil/despachos/{desp.id}/preview")
        check("despacho cerrado bloquea", r.json()["puede_emitir"] is False,
              r.json()["problemas"])
        _limpiar(db)

        # ── Cliente no existe en Wasabil ──
        _cot, oc, desp = _crear_datos(db, rut="76.999.999-9")
        r = client.post(f"/api/wasabil/despachos/{desp.id}/preview")
        check("cliente inexistente bloquea",
              any("no existe en Wasabil" in x for x in r.json()["problemas"]),
              r.json()["problemas"])
        _limpiar(db)

        # ── Wasabil no configurado: preview sí, emitir no ──
        fake.configurado = False
        _cot, oc, desp = _crear_datos(db)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/preview")
        p = r.json()
        check("sin token preview funciona", r.status_code == 200
              and any("no está configurado" in x for x in p["problemas"]), p["problemas"])
        check("sin token receptor local", p["receptor"]["fuente"] == "cotizacion")
        r = client.get("/api/wasabil/config")
        check("config refleja token", r.json() == {"configurado": False})
        fake.configurado = True
        _limpiar(db)

        # ── Falla AMBIGUA al crear (timeout: el doc pudo crearse) → claim bloquea ──
        fake.crear_falla = wasabil_client.WasabilError("timeout simulado", ambiguo=True)
        _cot, oc, desp = _crear_datos(db)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        check("falla ambigua 502", r.status_code == 502, r.text)
        db.rollback()  # cerrar snapshot para ver los commits del router
        fila = db.query(WasabilDte).filter(WasabilDte.despacho_id == desp.id).first()
        check("ancla con error, sin uuid y claim puesto", fila is not None
              and fila.uuid is None and "timeout" in (fila.error or "")
              and fila.en_vuelo_desde is not None, fila and fila.error)
        # Mientras el claim está fresco NADIE puede reintentar (anti doble emisión)
        fake.crear_falla = None
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        check("claim fresco bloquea reintento 409", r.status_code == 409
              and "EN CURSO" in r.json()["detail"], r.text)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        check("claim fresco bloquea emitir 409", r.status_code == 409, r.text)
        # El claim expira (se envejece a mano) → el documento SÍ se había creado en
        # Wasabil (respuesta perdida): el reintento lo ADOPTA en vez de re-crear
        from datetime import datetime as _dt, timedelta as _tdlt
        fila.en_vuelo_desde = _dt.now() - _tdlt(seconds=600)
        db.commit()
        fake.docs_buscables = [{"uuid": "uuid-perdido", "status_id": 3, "folio": "888",
                                "invoice_reference": f"OC {oc.numero_oc} · {desp.numero_despacho}",
                                "document_pdf_url": "https://api.wasabil.com/pdf/888"}]
        creados_antes = len(fake.creados)   # por DELTA: los escenarios previos ya crearon
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        # NOTA: el doc simulado usa el formato v1 "OC <n> · <N° despacho>" (documentos
        # ya emitidos, p.ej. folio 136 real) → esto prueba la rama de COMPATIBILIDAD
        check("reintento adopta doc existente formato v1 (NO re-crea)", r.status_code == 200
              and r.json()["folio"] == "888" and len(fake.creados) == creados_antes, r.text)
        db.rollback()
        check("folio adoptado en despacho", db.get(Despacho, desp.id).numero_guia == "888")
        _limpiar(db)

        # ── Recuperación con el formato v2 (invoice_reference = SOLO el N° despacho) ──
        fake.crear_falla = wasabil_client.WasabilError("timeout", ambiguo=True)
        _cot, oc, desp = _crear_datos(db)
        client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        db.rollback()
        fila = db.query(WasabilDte).filter(WasabilDte.despacho_id == desp.id).first()
        fila.en_vuelo_desde = _dt.now() - _tdlt(seconds=600)   # claim vencido
        db.commit()
        fake.crear_falla = None
        fake.docs_buscables = [{"uuid": "uuid-perdido", "status_id": 3, "folio": "888",
                                "invoice_reference": desp.numero_despacho,   # formato v2
                                "document_pdf_url": "https://api.wasabil.com/pdf/888"}]
        creados_antes = len(fake.creados)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        check("v2: reintento adopta por referencia == N° despacho (NO re-crea)",
              r.status_code == 200 and r.json()["folio"] == "888"
              and len(fake.creados) == creados_antes, r.text)
        fake.docs_buscables = []
        _limpiar(db)

        # ── Falla NO ambigua (conexión rechazada: seguro no se creó) → claim libre ──
        fake.docs_buscables = []
        fake.crear_falla = wasabil_client.WasabilError("conexión rechazada", ambiguo=False)
        _cot, oc, desp = _crear_datos(db)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        check("falla no ambigua 502", r.status_code == 502, r.text)
        db.rollback()
        fila = db.query(WasabilDte).filter(WasabilDte.despacho_id == desp.id).first()
        check("claim liberado (reintento inmediato posible)",
              fila is not None and fila.en_vuelo_desde is None, fila)
        fake.crear_falla = None
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        check("reintento inmediato re-crea", r.status_code == 200, r.text)
        _limpiar(db)

        # ── Reintento NUNCA re-crea a ciegas si no puede verificar en Wasabil ──
        fake.crear_falla = wasabil_client.WasabilError("conexión rechazada", ambiguo=False)
        _cot, oc, desp = _crear_datos(db)
        client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        fake.crear_falla = None
        creados_antes = len(fake.creados)
        _orig_buscar = wasabil_client.buscar_documentos
        wasabil_client.buscar_documentos = lambda s: (_ for _ in ()).throw(
            wasabil_client.WasabilError("red caída"))
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        check("verificación caída → 502 sin re-crear", r.status_code == 502
              and len(fake.creados) == creados_antes, r.text)
        wasabil_client.buscar_documentos = _orig_buscar
        _limpiar(db)

        # ── Búsqueda paginada INCOMPLETA → tampoco se re-crea ("no lo encontré"
        #    en una lista truncada NO prueba que el documento no exista) ──
        fake.crear_falla = wasabil_client.WasabilError("conexión rechazada", ambiguo=False)
        _cot, oc, desp = _crear_datos(db)
        client.post(f"/api/wasabil/despachos/{desp.id}/emitir")
        fake.crear_falla = None
        creados_antes = len(fake.creados)
        fake.docs_buscables = []
        fake.busqueda_completa = False
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        check("búsqueda incompleta → 502 sin re-crear", r.status_code == 502
              and "incompleta" in r.json()["detail"]
              and len(fake.creados) == creados_antes, r.text)
        fake.busqueda_completa = True
        _limpiar(db)

        # ── Hueco cubierto: borrador en Wasabil (uuid + status 6) bloquea todo ──
        _cot, oc, desp = _crear_datos(db)
        db.add(WasabilDte(tipo_dte=52, despacho_id=desp.id, uuid="uuid-borrador",
                          status_id=6))
        db.commit()
        r = client.post(f"/api/wasabil/despachos/{desp.id}/preview")
        check("borrador en Wasabil bloquea", r.json()["puede_emitir"] is False
              and any("BORRADOR" in x for x in r.json()["problemas"]),
              r.json()["problemas"])
        _limpiar(db)

        # ── Fallido del SII → reintento re-crea ──
        fake.docs_buscables = []
        _cot, oc, desp = _crear_datos(db)
        client.post(f"/api/wasabil/despachos/{desp.id}/emitir")  # queda Procesando (2)
        fake.estado_final = 4  # ...y el SII lo rechaza
        fake.display_error = "RUT del receptor no autorizado"
        r = client.get(f"/api/wasabil/despachos/{desp.id}/estado")
        check("fallido con motivo del SII", r.json()["estado"] == "fallido"
              and "no autorizado" in (r.json()["error"] or ""), r.json())
        # Reintento: Wasabil confirma que sigue fallido → se re-crea el documento
        fake.display_error = None
        creados_antes = len(fake.creados)
        r = client.post(f"/api/wasabil/despachos/{desp.id}/reintentar")
        check("reintento de fallido re-crea", r.status_code == 200
              and len(fake.creados) == creados_antes + 1, r.text)
        fake.estado_final = 3
        _limpiar(db)

        # ── Candado de empresa ──
        _cot, oc, desp = _crear_datos(db)
        CURRENT["empresa"] = "automotriz"
        r = client.post(f"/api/wasabil/despachos/{desp.id}/preview")
        check("candado: automotriz 403", r.status_code == 403, r.text)
        CURRENT["empresa"] = "mineria"
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_wasabil_dte_integration():
    run()


if __name__ == "__main__":
    run()
