"""Test de integración del módulo Wasabil DTE de MonzaParts contra la DB local
(Wasabil SIMULADO — JAMÁS el API real: emitir al SII es IRREVERSIBLE).

Monta el router Monza en una app efímera (sin tocar main.py), sobreescribe la
auth para simular usuarios de distintas empresas y SIMULA el API de Wasabil por
monkeypatch de monza_wasabil_dte.client — superficie INDEPENDIENTE del client GA
(wasabil_dte.client): los fakes de esta suite no contaminan a las suites GA ni
al revés cuando pytest corre toda la batería junta. Ejerce el flujo completo:
preview → emitir → estado → folio en MonzaDespacho.numero_guia → anti doble
emisión → reintentos → IVA por venta → precio congelado → candado de empresa.
LIMPIA todo lo que creó al terminar (verificado con sesión NUEVA).

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_integration.py -q
(también:   ./venv/bin/python monza_wasabil_dte/tests/test_integration.py)
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
import models.models  # noqa: E402,F401  (FK users.id resolubles en create_all)
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaDespacho,
    MonzaDespachoItem, MonzaConfig, MonzaLog,
)
from monza_contabilidad.service import iva_rate_de  # noqa: E402
from monza_wasabil_dte import client as monza_client  # noqa: E402
from monza_wasabil_dte.router import router  # noqa: E402
from monza_wasabil_dte.models import MonzaWasabilDte  # noqa: E402
from monza_wasabil_dte.service import cuadratura  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura monza_wasabil_dte

MARK = "__TEST_MWDTE__"
RUT_CLIENTE = "77.111.222-3"
CURRENT = {"empresa": "automotriz", "id": None}

app = FastAPI()
app.include_router(router, prefix="/api")   # → /api/monza/wasabil/... (como main.py)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"], email="test-mwdte@monza.cl")
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ─── Wasabil SIMULADO (monkeypatch sobre monza_wasabil_dte.client) ──────────────
class FakeWasabil:
    """Simula el API de Wasabil para el client MONZA: configurable por escenario
    (fallos ambiguos/no ambiguos, estados, cliente inexistente, búsqueda truncada).
    Registra en `creados` cada payload recibido — los asserts anti doble emisión
    van por DELTA de len(creados)."""

    def __init__(self):
        self.configurado = True
        # Ficha en el FORMATO REAL del API (giros[]/addresses[] anidados): así el
        # test pasa por client._normalizar_cliente REAL y protege la normalización
        # (el API no devuelve campos planos — regresión GA 2026-07-17).
        self.cliente = {"id": 160065, "rut": RUT_CLIENTE,
                        "name": f"{MARK} AUTOMOTORES SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS AUTOMOTRICES", "default": True}],
                        "addresses": [{"address": "AV. LAS CONDES 10000",
                                       "comuna": "Las Condes", "city": "Santiago",
                                       "default": True}]}
        self.crear_falla = None          # Exception a lanzar en crear_documento
        self.status_respuesta = 2        # status_id que devuelve crear_documento
        self.estado_final = 3            # status_id que devuelve estado_documento
        self.display_error = None
        self.docs_buscables: list = []   # para buscar_documentos (reintento sin uuid)
        self.busqueda_completa = True    # False = lista truncada por paginación
        self.creados: list = []          # payloads recibidos (auditoría del test)

    def install(self):
        # Se pisa el módulo CLIENT MONZA (monza_wasabil_dte.client), nunca el GA:
        # cada marca tiene su superficie de monkeypatch — es la razón de ser del
        # client propio (decisión de arquitectura F5).
        monza_client.esta_configurado = lambda: self.configurado
        monza_client.buscar_cliente_por_rut = lambda rut: (
            monza_client._normalizar_cliente(self.cliente) if self.cliente and
            monza_client._normalizar_rut(rut) == monza_client._normalizar_rut(self.cliente["rut"])
            else None)
        monza_client.crear_documento = self._crear
        monza_client.estado_documento = self._estado
        monza_client.obtener_documento = self._obtener
        monza_client.buscar_documentos = lambda search: (
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


# ─── Datos de prueba en la DB local (prefijo MARK, se limpian al final) ─────────
def _crear_datos(db, rut=RUT_CLIENTE, numero_oc="OC-4501", oc_fecha=date(2026, 6, 10),
                 estado_despacho="en_preparacion", iva_pct=19.0, con_numero=True,
                 numero_guia_manual=None, precio1=15000, precio2=2500):
    cliente = MonzaCliente(nombre=f"{MARK} CLIENTE", rut=(rut or None))
    db.add(cliente); db.flush()
    cot = MonzaCotizacion(numero=f"{MARK}-COT", cliente_id=cliente.id, estado="vendida",
                          oc_cliente=numero_oc, oc_fecha=oc_fecha, iva_pct=iva_pct)
    db.add(cot); db.flush()
    it1 = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Filtro de aceite motor",
                              numero_parte="A2761800009", cantidad=10,
                              precio_unitario_clp=precio1, estado_linea="en_bodega")
    it2 = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Sello de polvo",
                              numero_parte="A0009884399", cantidad=20,
                              precio_unitario_clp=precio2, estado_linea="en_bodega")
    db.add_all([it1, it2]); db.flush()
    desp = MonzaDespacho(numero=(f"{MARK}-DSP-{cot.id}" if con_numero else None),
                         cotizacion_id=cot.id, cliente_nombre=cliente.nombre,
                         estado=estado_despacho, destinatario="Juan Pérez",
                         direccion_entrega="Bodega central, Roger de Flor 2996",
                         numero_guia=numero_guia_manual)
    db.add(desp); db.flush()
    # Despacho PARCIAL: 4 de 10 del ítem 1, 20 de 20 del ítem 2
    db.add_all([
        MonzaDespachoItem(despacho_id=desp.id, item_id=it1.id, qty_despachada=4),
        MonzaDespachoItem(despacho_id=desp.id, item_id=it2.id, qty_despachada=20),
    ])
    db.commit()
    return cot, desp, it1, it2


def _limpiar(db):
    db.rollback()  # cierra cualquier snapshot/lock del test antes de barrer
    for cot in db.query(MonzaCotizacion).filter(MonzaCotizacion.numero.like(f"{MARK}%")).all():
        for d in db.query(MonzaDespacho).filter(MonzaDespacho.cotizacion_id == cot.id).all():
            db.query(MonzaWasabilDte).filter(MonzaWasabilDte.despacho_id == d.id).delete()
            db.query(MonzaDespachoItem).filter(MonzaDespachoItem.despacho_id == d.id).delete()
            db.delete(d)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id == cot.id).delete()
        db.delete(cot)
    # flush ANTES del borrado masivo de clientes: los db.delete(cot) de arriba son
    # ORM (pendientes) y el bulk DELETE se ejecuta al tiro — sin flush, la FK
    # cliente_id de las cotizaciones aún vivas revienta con 1451.
    db.flush()
    db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(
        synchronize_session=False)
    db.query(MonzaLog).filter(MonzaLog.entidad_ref.like(f"{MARK}%")).delete(
        synchronize_session=False)
    db.commit()


def _verificar_limpieza():
    """Sesión NUEVA (regla de la casa): confirma que no quedó NINGUNA fila MARK."""
    db2 = SessionLocal()
    try:
        restos = (
            db2.query(MonzaCotizacion).filter(MonzaCotizacion.numero.like(f"{MARK}%")).count()
            + db2.query(MonzaDespacho).filter(MonzaDespacho.numero.like(f"{MARK}%")).count()
            + db2.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count()
            + db2.query(MonzaLog).filter(MonzaLog.entidad_ref.like(f"{MARK}%")).count()
        )
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db2.close()


def run():
    db = SessionLocal()
    # Re-instalar NUESTRO fake al empezar: si pytest importó otras suites que
    # instalan fakes a nivel de módulo, la última instalación gana (anti-flaky).
    fake.install()
    _limpiar(db)  # por si un run anterior murió a medias
    try:
        CURRENT["empresa"] = "automotriz"

        # ── Preview feliz (despacho parcial: 4/10 + 20/20, IVA 19% congelado) ──
        cot, desp, it1, _it2 = _crear_datos(db, numero_guia_manual="G-MANUAL-9")
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview")
        check("preview 200", r.status_code == 200, r.text)
        p = r.json()
        check("preview puede_emitir", p["puede_emitir"] is True, p["problemas"])
        check("preview receptor desde Wasabil (ficha anidada normalizada)",
              p["receptor"]["fuente"] == "wasabil"
              and p["receptor"]["comuna"] == "Las Condes", p["receptor"])
        check("preview 2 lineas parciales precio congelado", len(p["lineas"]) == 2
              and p["lineas"][0]["quantity"] == 4
              and p["lineas"][0]["price"] == 15000, p["lineas"])
        neto = 4 * 15000 + 20 * 2500
        check("preview cuadratura IVA 19 por venta",
              p["totales"] == {"neto": neto, "iva": round(neto * 0.19),
                               "total": neto + round(neto * 0.19), "iva_rate": 0.19},
              p["totales"])
        check("preview ref 801 con fecha de columna Date", p["referencias"][0]["tipo"] == "801"
              and p["referencias"][0]["folio"] == "OC-4501"
              and p["referencias"][0]["fecha"] == "2026-06-10", p["referencias"])
        check("preview no emite (issue False y nada creado)",
              p["documento"]["issue"] is False and len(fake.creados) == 0, p["documento"])
        check("preview documento sin reason en la 801 (formato v3)",
              "reason" not in p["documento"]["references"][0], p["documento"]["references"])
        check("preview invoiceReference = SOLO el N° despacho (formato v2)",
              p["documento"]["invoiceReference"] == desp.numero, p["documento"])
        check("preview avisa que pisará el N° manual",
              any("REEMPLAZAR" in a for a in p["advertencias"]), p["advertencias"])
        check("preview trae tipos de traslado (venta default + interno)",
              p["tipo_traslado"] == 1
              and any(t["codigo"] == 1 and "venta" in t["label"].lower() for t in p["tipos_traslado"])
              and any(t["codigo"] == 5 and "interno" in t["label"].lower() for t in p["tipos_traslado"]),
              p.get("tipos_traslado"))

        # ── Emitir (procesando) → estado (emitido) → folio pisa numero_guia ──
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        check("emitir 200", r.status_code == 200, r.text)
        e = r.json()
        check("emitir uuid persistido", e["uuid"] == "uuid-1", e)
        check("emitir issue=true al confirmar", fake.creados[0]["issue"] is True)
        check("emitir payload REST snake_case",
              fake.creados[0]["sii_document_type_code"] == 52
              and fake.creados[0]["dispatch_guide"] == {"dispatch_type_code": 1}
              and fake.creados[0]["client_id"] == 160065, fake.creados[0])
        check("emitir ref 801 snake_case con fecha y sin reason",
              fake.creados[0]["references"][0] == {"document_type": "801",
                                                   "folio": "OC-4501",
                                                   "date": "2026-06-10"},
              fake.creados[0]["references"])
        di1 = db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id == desp.id,
            MonzaDespachoItem.item_id == it1.id).first()
        check("emitir externalId = id del despacho-item (enlace 1:1 con la factura)",
              fake.creados[0]["details"][0]["externalId"] == str(di1.id),
              fake.creados[0]["details"][0])
        r = client.get(f"/api/monza/wasabil/despachos/{desp.id}/estado")
        s = r.json()
        check("estado emitido con folio", s["estado"] == "emitido" and s["folio"] == "777", s)
        check("estado pdf/xml", "pdf/777" in s["pdf_url"], s)
        # rollback: cierra la transacción del test (MySQL REPEATABLE READ congela
        # el snapshot; sin esto no se ven los commits de la sesión del router)
        db.rollback()
        check("folio pisa el N° manual en despacho.numero_guia",
              db.get(MonzaDespacho, desp.id).numero_guia == "777")
        fila = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.despacho_id == desp.id).first()
        check("montos congelados en la fila DTE",
              fila is not None and float(fila.monto_neto) == neto
              and float(fila.iva) == round(neto * 0.19), fila and (fila.monto_neto, fila.iva))

        # ── Anti doble emisión sobre emitida ──
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        check("re-emitir 409", r.status_code == 409, r.text)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("reintentar emitida 409", r.status_code == 409, r.text)
        r = client.get(f"/api/monza/wasabil/despachos/estado-batch?ids={desp.id}")
        check("estado-batch", str(desp.id) in r.json()
              and r.json()[str(desp.id)]["folio"] == "777", r.json())
        _limpiar(db)

        # ── IVA POR VENTA: iva_pct congelado ≠ 19 gobierna la cuadratura ──
        cot, desp, it1, _it2 = _crear_datos(db, iva_pct=10.0)
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("IVA por venta 10% en la cuadratura",
              p["totales"] == {"neto": neto, "iva": round(neto * 0.10),
                               "total": neto + round(neto * 0.10), "iva_rate": 0.10},
              p["totales"])
        _limpiar(db)

        # ── IVA fallback: venta sin iva_pct → MonzaConfig / 0.19 (iva_rate_de) ──
        cot, desp, _i1, _i2 = _crear_datos(db, iva_pct=None)
        cfg = db.query(MonzaConfig).order_by(MonzaConfig.id.asc()).first()
        rate_esperada = iva_rate_de(SimpleNamespace(iva_pct=None), cfg)
        _n_esp, iva_esp, _t_esp = cuadratura(neto, rate_esperada)  # half-up del service
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("IVA fallback a config/0.19 (misma resolución que la factura)",
              abs(p["totales"]["iva_rate"] - rate_esperada) < 1e-9
              and p["totales"]["iva"] == iva_esp, p["totales"])
        _limpiar(db)

        # ── PRECIO CONGELADO: cambiar los insumos de recálculo NO altera la guía ──
        cot, desp, it1, _it2 = _crear_datos(db)
        # Se disparan los insumos del recálculo vivo (costo/markup/TC): si alguien
        # recalculara en vez de usar precio_unitario_clp, la línea cambiaría.
        # (No se toca MonzaConfig: es una fila real compartida de la BD local.)
        it1 = db.get(MonzaCotizacionItem, it1.id)
        it1.costo = 999999; it1.markup_pct = 3.5; it1.tc_aplicado = 5000
        db.commit()
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("precio congelado: la guía usa precio_unitario_clp, no un recálculo",
              p["lineas"][0]["price"] == 15000 and p["totales"]["neto"] == neto,
              (p["lineas"][0], p["totales"]))
        _limpiar(db)

        # ── Validaciones bloqueantes: RUT / N° OC / fecha OC / precio 0 ──
        cot, desp, _i1, _i2 = _crear_datos(db, rut="", numero_oc="", oc_fecha=None)
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("sin RUT / N° OC / fecha OC bloquea con 3 problemas",
              p["puede_emitir"] is False
              and any("RUT" in x for x in p["problemas"])
              and any("N° de OC" in x for x in p["problemas"])
              and any("FECHA" in x for x in p["problemas"]), p["problemas"])
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        check("emitir bloqueado 409", r.status_code == 409, r.text)
        check("emitir bloqueado no llegó a Wasabil", len(fake.creados) == 1, fake.creados)
        _limpiar(db)

        cot, desp, _i1, _i2 = _crear_datos(db, precio1=0)
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("línea sin precio congelado bloquea",
              p["puede_emitir"] is False
              and any("sin precio" in x for x in p["problemas"]), p["problemas"])
        _limpiar(db)

        # N° de OC > 18 caracteres → el SII lo rechaza; se bloquea en el preview
        cot, desp, _i1, _i2 = _crear_datos(db, numero_oc="OC-DEMASIADO-LARGA-123456")
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("N° OC > 18 chars bloquea con mensaje",
              p["puede_emitir"] is False
              and any("18" in x and "OC" in x for x in p["problemas"]), p["problemas"])
        _limpiar(db)

        # Despacho sin N° interno (columna nullable) → sin ancla, se bloquea
        cot, desp, _i1, _i2 = _crear_datos(db, con_numero=False)
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("despacho sin N° interno bloquea (sin ancla anti doble emisión)",
              p["puede_emitir"] is False
              and any("N° interno" in x for x in p["problemas"]), p["problemas"])
        _limpiar(db)

        # ── Despacho legado 'despachado' (default histórico) NO es emitible ──
        cot, desp, _i1, _i2 = _crear_datos(db, estado_despacho="despachado")
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("despacho legado/cerrado bloquea", p["puede_emitir"] is False
              and any("EN PREPARACIÓN" in x for x in p["problemas"]), p["problemas"])
        _limpiar(db)

        # ── Tipo de traslado elegible: emitir como traslado interno (5) ──
        cot, desp, _i1, _i2 = _crear_datos(db)
        fake.creados.clear()
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir?tipo_traslado=5")
        check("emitir traslado interno 200", r.status_code == 200, r.text)
        check("emitir usa dispatch_type_code 5",
              fake.creados[0]["dispatch_guide"] == {"dispatch_type_code": 5}, fake.creados[0])
        db.rollback()
        _limpiar(db)
        # Tipo fuera de la tabla del SII → 400 (ni preview ni emisión lo aceptan)
        cot, desp, _i1, _i2 = _crear_datos(db)
        check("preview tipo_traslado inválido → 400",
              client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview?tipo_traslado=99").status_code == 400)
        check("emitir tipo_traslado inválido → 400",
              client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir?tipo_traslado=99").status_code == 400)
        _limpiar(db)

        # ── Cliente no existe en Wasabil → bloquea ──
        cot, desp, _i1, _i2 = _crear_datos(db, rut="76.999.999-9")
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("cliente inexistente en Wasabil bloquea",
              any("no existe en Wasabil" in x for x in p["problemas"]), p["problemas"])
        _limpiar(db)

        # ── Ficha Wasabil sin giro → solo ADVERTENCIA (asimetría guías vs facturas) ──
        giros_orig = fake.cliente["giros"]
        fake.cliente["giros"] = []
        cot, desp, _i1, _i2 = _crear_datos(db)
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("ficha sin giro: advertencia y puede_emitir True",
              p["puede_emitir"] is True
              and any("giro" in a for a in p["advertencias"]),
              (p["problemas"], p["advertencias"]))
        fake.cliente["giros"] = giros_orig
        _limpiar(db)

        # ── Wasabil no configurado: preview sí (fuente cotizacion), emitir no ──
        fake.configurado = False
        cot, desp, _i1, _i2 = _crear_datos(db)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview")
        p = r.json()
        check("sin token preview funciona", r.status_code == 200
              and any("no está configurado" in x for x in p["problemas"]), p["problemas"])
        check("sin token receptor local", p["receptor"]["fuente"] == "cotizacion")
        check("sin token emitir bloqueado 409",
              client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir").status_code == 409)
        r = client.get("/api/monza/wasabil/config")
        check("config refleja token (sin exponer el valor)",
              r.json() == {"configurado": False})
        fake.configurado = True
        _limpiar(db)

        # ── Sondeo degrada ELEGANTE ante respuesta inesperada de Wasabil ──
        cot, desp, _i1, _i2 = _crear_datos(db)
        client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")   # queda "procesando"
        _orig_estado = monza_client.estado_documento
        monza_client.estado_documento = lambda u: (_ for _ in ()).throw(
            monza_client.WasabilError("respuesta inesperada al consultar el estado",
                                      ambiguo=True))
        r = client.get(f"/api/monza/wasabil/despachos/{desp.id}/estado")
        check("estado con respuesta inesperada degrada (error_consulta, no 500)",
              r.status_code == 200 and r.json().get("error_consulta")
              and r.json()["estado"] != "emitido", r.text)
        db.rollback()
        fila = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.despacho_id == desp.id).first()
        fila.en_vuelo_desde = None   # claim vencido: habilita intentar el reintento
        db.commit()
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("reintentar sin poder verificar el estado → 502 (no re-crea, no 500)",
              r.status_code == 502, r.text)
        monza_client.estado_documento = _orig_estado
        _limpiar(db)

        # ── Falla AMBIGUA al crear (timeout: el doc pudo crearse) → claim bloquea ──
        fake.crear_falla = monza_client.WasabilError("timeout simulado", ambiguo=True)
        cot, desp, _i1, _i2 = _crear_datos(db)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        check("falla ambigua 502", r.status_code == 502, r.text)
        db.rollback()  # cerrar snapshot para ver los commits del router
        fila = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.despacho_id == desp.id).first()
        check("ancla con error, sin uuid y claim puesto", fila is not None
              and fila.uuid is None and "timeout" in (fila.error or "")
              and fila.en_vuelo_desde is not None, fila and fila.error)
        # Mientras el claim está fresco NADIE puede reintentar (anti doble emisión)
        fake.crear_falla = None
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("claim fresco bloquea reintento 409", r.status_code == 409
              and "EN CURSO" in r.json()["detail"], r.text)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        check("claim fresco bloquea emitir 409", r.status_code == 409, r.text)
        # El claim expira (se envejece a mano con un datetime deliberado) → el doc
        # SÍ existía en Wasabil: el reintento lo ADOPTA por match EXACTO en vez de
        # re-crear (formato v2: invoice_reference == N° de despacho, sin sufijos)
        fila.en_vuelo_desde = datetime.utcnow() - timedelta(seconds=600)
        db.commit()
        fake.docs_buscables = [{"uuid": "uuid-perdido", "status_id": 3, "folio": "888",
                                "invoice_reference": desp.numero,
                                "document_pdf_url": "https://api.wasabil.com/pdf/888"}]
        creados_antes = len(fake.creados)   # por DELTA: escenarios previos ya crearon
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("reintento adopta doc existente por match EXACTO (NO re-crea)",
              r.status_code == 200 and r.json()["folio"] == "888"
              and len(fake.creados) == creados_antes, r.text)
        db.rollback()
        check("folio adoptado pisa despacho.numero_guia",
              db.get(MonzaDespacho, desp.id).numero_guia == "888")
        fake.docs_buscables = []
        _limpiar(db)

        # ── Match EXACTO PURO: un doc con formato v1 GA ("OC ... · DSP") NO se
        #    adopta (Monza no tiene legados v1) → se re-crea tras confirmar búsqueda ──
        fake.crear_falla = monza_client.WasabilError("conexión rechazada", ambiguo=False)
        cot, desp, _i1, _i2 = _crear_datos(db)
        client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")   # falla; claim libre
        fake.crear_falla = None
        fake.docs_buscables = [{"uuid": "uuid-ajeno", "status_id": 3, "folio": "999",
                                "invoice_reference": f"OC {cot.oc_cliente} · {desp.numero}"}]
        creados_antes = len(fake.creados)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("referencia estilo v1 NO matchea: se re-crea (delta 1)",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1
              and r.json()["folio"] != "999", r.text)
        fake.docs_buscables = []
        _limpiar(db)

        # ── Falla NO ambigua (conexión rechazada: seguro no se creó) → claim libre ──
        fake.crear_falla = monza_client.WasabilError("conexión rechazada", ambiguo=False)
        cot, desp, _i1, _i2 = _crear_datos(db)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        check("falla no ambigua 502", r.status_code == 502, r.text)
        db.rollback()
        fila = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.despacho_id == desp.id).first()
        check("claim liberado (reintento inmediato posible)",
              fila is not None and fila.en_vuelo_desde is None, fila)
        fake.crear_falla = None
        creados_antes = len(fake.creados)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("reintento inmediato re-crea", r.status_code == 200
              and len(fake.creados) == creados_antes + 1, r.text)
        _limpiar(db)

        # ── Reintento NUNCA re-crea a ciegas si no puede verificar en Wasabil ──
        fake.crear_falla = monza_client.WasabilError("conexión rechazada", ambiguo=False)
        cot, desp, _i1, _i2 = _crear_datos(db)
        client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        fake.crear_falla = None
        creados_antes = len(fake.creados)
        _orig_buscar = monza_client.buscar_documentos
        monza_client.buscar_documentos = lambda s: (_ for _ in ()).throw(
            monza_client.WasabilError("red caída"))
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("verificación caída → 502 sin re-crear", r.status_code == 502
              and len(fake.creados) == creados_antes, r.text)
        monza_client.buscar_documentos = _orig_buscar
        fake.install()   # restaura el buscar_documentos del fake (no el real)
        _limpiar(db)

        # ── Búsqueda paginada INCOMPLETA → tampoco se re-crea ("no lo encontré"
        #    en una lista truncada NO prueba que el documento no exista) ──
        fake.crear_falla = monza_client.WasabilError("conexión rechazada", ambiguo=False)
        cot, desp, _i1, _i2 = _crear_datos(db)
        client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        fake.crear_falla = None
        creados_antes = len(fake.creados)
        fake.docs_buscables = []
        fake.busqueda_completa = False
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("búsqueda incompleta → 502 sin re-crear", r.status_code == 502
              and "incompleta" in r.json()["detail"]
              and len(fake.creados) == creados_antes, r.text)
        fake.busqueda_completa = True
        _limpiar(db)

        # ── Borrador en Wasabil (uuid + status 6) bloquea todo ──
        cot, desp, _i1, _i2 = _crear_datos(db)
        db.add(MonzaWasabilDte(empresa="automotriz", tipo_dte=52, despacho_id=desp.id,
                               uuid="uuid-borrador", status_id=6))
        db.commit()
        p = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview").json()
        check("borrador en Wasabil bloquea", p["puede_emitir"] is False
              and any("BORRADOR" in x for x in p["problemas"]), p["problemas"])
        _limpiar(db)

        # ── Fallido del SII → reintento re-crea ──
        fake.docs_buscables = []
        cot, desp, _i1, _i2 = _crear_datos(db)
        client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")  # Procesando (2)
        fake.estado_final = 4  # ...y el SII lo rechaza
        fake.display_error = "RUT del receptor no autorizado"
        r = client.get(f"/api/monza/wasabil/despachos/{desp.id}/estado")
        check("fallido con motivo del SII", r.json()["estado"] == "fallido"
              and "no autorizado" in (r.json()["error"] or ""), r.json())
        fake.display_error = None
        creados_antes = len(fake.creados)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar")
        check("reintento de fallido re-crea", r.status_code == 200
              and len(fake.creados) == creados_antes + 1, r.text)
        fake.estado_final = 3
        _limpiar(db)

        # ── El reintento puede CORREGIR el tipo de traslado (emitió 1, retry 5) ──
        fake.docs_buscables = []
        fake.crear_falla = monza_client.WasabilError("conexión rechazada", ambiguo=False)
        cot, desp, _i1, _i2 = _crear_datos(db)
        client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")   # falla; claim libre
        fake.crear_falla = None
        creados_antes = len(fake.creados)
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/reintentar?tipo_traslado=5")
        check("reintento con tipo corregido 200", r.status_code == 200, r.text)
        check("reintento re-crea con dispatch_type_code 5",
              len(fake.creados) == creados_antes + 1
              and fake.creados[-1]["dispatch_guide"] == {"dispatch_type_code": 5},
              fake.creados[-1] if fake.creados else None)
        _limpiar(db)

        # ── Candado de empresa: usuario minería queda fuera del módulo Monza ──
        cot, desp, _i1, _i2 = _crear_datos(db)
        CURRENT["empresa"] = "mineria"
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/preview")
        check("candado: mineria 403", r.status_code == 403, r.text)
        check("candado: config también 403",
              client.get("/api/monza/wasabil/config").status_code == 403)
        CURRENT["empresa"] = "automotriz"
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        _verificar_limpieza()
        print("Cleanup OK (verificado con sesión nueva)")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_monza_wasabil_dte_integration():
    run()


if __name__ == "__main__":
    run()
