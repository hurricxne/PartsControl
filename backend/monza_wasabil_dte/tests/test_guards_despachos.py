"""Guards INVERSOS de la Fase 5 en monza_router_despachos.py (Wasabil SIMULADO).

La guía 52 se emite con el despacho aún en preparación: estas pruebas verifican
que las operaciones del router de despachos que dejarían huérfano un documento
tributario legal (anular el despacho) o pisarían su folio (PUT de cabecera con
numero_guia) se rechazan mientras la guía esté VIVA (emitida | claim en vuelo |
uuid en proceso/borrador) — y que un DTE FALLIDO no bloquea nada.

También cubre el cierre del círculo: una emisión completa vía el router Wasabil
Monza (simulado) copia el folio del SII a MonzaDespacho.numero_guia.

Fakes por monkeypatch de monza_wasabil_dte.client (superficie independiente del
client GA). Datos con prefijo MARK propio + limpieza total verificada con sesión
nueva. JAMÁS se llama al API real.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_guards_despachos.py -q
(también:   ./venv/bin/python monza_wasabil_dte/tests/test_guards_despachos.py)
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
    MonzaDespachoItem, MonzaLog,
)
from monza_router_despachos import router as despachos_router  # noqa: E402
from monza_wasabil_dte import client as monza_client  # noqa: E402
from monza_wasabil_dte.router import router as wasabil_router  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    MonzaWasabilDte, STATUS_EMITIDO, STATUS_FALLIDO, STATUS_PROCESANDO,
    STATUS_PENDIENTE, CLAIM_TTL_SEGUNDOS,
)

Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura monza_wasabil_dte

MARK = "__TEST_MWGRD__"
RUT_CLIENTE = "77.111.222-3"
CURRENT = {"empresa": "automotriz", "id": None}

app = FastAPI()
# El router de despachos Monza ya trae su prefijo /api/monza/despachos completo;
# el router Wasabil se monta con prefix=/api (igual que main.py).
app.include_router(despachos_router)
app.include_router(wasabil_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"], email="test-mwgrd@monza.cl")
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ─── Wasabil SIMULADO mínimo (solo lo que necesita la emisión feliz) ────────────
class FakeWasabil:
    def __init__(self):
        self.cliente = {"id": 160065, "rut": RUT_CLIENTE,
                        "name": f"{MARK} AUTOMOTORES SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS", "default": True}],
                        "addresses": [{"address": "AV. LAS CONDES 10000",
                                       "comuna": "Las Condes", "city": "Santiago",
                                       "default": True}]}
        self.creados: list = []

    def install(self):
        monza_client.esta_configurado = lambda: True
        monza_client.buscar_cliente_por_rut = lambda rut: (
            monza_client._normalizar_cliente(self.cliente) if self.cliente and
            monza_client._normalizar_rut(rut) == monza_client._normalizar_rut(self.cliente["rut"])
            else None)
        monza_client.crear_documento = self._crear
        monza_client.estado_documento = lambda uuid: {"uuid": uuid, "status_id": 3,
                                                      "display_error": None}
        monza_client.obtener_documento = lambda uuid: {
            "uuid": uuid, "status_id": 3, "folio": "777", "display_error": None,
            "document_pdf_url": "https://api.wasabil.com/pdf/777",
            "document_xml_url": "https://api.wasabil.com/xml/777"}
        monza_client.buscar_documentos = lambda search: ([], True)

    def _crear(self, payload):
        self.creados.append(payload)
        return {"uuid": f"uuid-g{len(self.creados)}", "status_id": 2}


fake = FakeWasabil()
fake.install()


# ─── Datos de prueba (MARK propio) ──────────────────────────────────────────────
def _crear_datos(db, estado_despacho="en_preparacion", numero_guia=None):
    cliente = MonzaCliente(nombre=f"{MARK} CLIENTE", rut=RUT_CLIENTE)
    db.add(cliente); db.flush()
    cot = MonzaCotizacion(numero=f"{MARK}-COT", cliente_id=cliente.id, estado="vendida",
                          oc_cliente="OC-4501", oc_fecha=date(2026, 6, 10), iva_pct=19.0)
    db.add(cot); db.flush()
    it = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Filtro de aceite motor",
                             numero_parte="A2761800009", cantidad=10,
                             precio_unitario_clp=15000, estado_linea="en_bodega")
    db.add(it); db.flush()
    desp = MonzaDespacho(numero=f"{MARK}-DSP-{cot.id}", cotizacion_id=cot.id,
                         cliente_nombre=cliente.nombre, estado=estado_despacho,
                         destinatario="Juan Pérez", numero_guia=numero_guia,
                         direccion_entrega="Av. Las Condes 10000")
    db.add(desp); db.flush()
    db.add(MonzaDespachoItem(despacho_id=desp.id, item_id=it.id, qty_despachada=4))
    db.commit()
    return cot, desp, it


def _dte(db, desp, **kw):
    """Inserta la fila DTE directamente (estados fabricados para los guards)."""
    base = dict(empresa="automotriz", tipo_dte=52, despacho_id=desp.id,
                uuid=None, status_id=None, en_vuelo_desde=None, folio=None, error=None)
    base.update(kw)
    fila = MonzaWasabilDte(**base)
    db.add(fila)
    db.commit()
    return fila


def _limpiar(db):
    db.rollback()
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
    db2 = SessionLocal()  # sesión NUEVA (regla de la casa)
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
    fake.install()   # re-instalar: si pytest importó otras suites, la última ganó
    _limpiar(db)     # por si un run anterior murió a medias
    try:
        CURRENT["empresa"] = "automotriz"

        # ── 1. Anular con guía EMITIDA → 409 con el folio en el mensaje ──
        _cot, desp, _it = _crear_datos(db)
        _dte(db, desp, uuid="uuid-emitida", status_id=STATUS_EMITIDO, folio="777")
        r = client.delete(f"/api/monza/despachos/entidades/{desp.id}")
        check("anular con guía emitida → 409", r.status_code == 409, r.text)
        check("anular con guía emitida → el 409 trae el folio y manda a Wasabil",
              "777" in r.json()["detail"] and "Wasabil" in r.json()["detail"], r.text)
        db.rollback()
        check("anular bloqueado: el despacho sigue en preparación",
              db.get(MonzaDespacho, desp.id).estado == "en_preparacion")
        _limpiar(db)

        # ── 2. Anular con CLAIM VIGENTE (emisión en vuelo, sin uuid) → 409 en curso ──
        _cot, desp, _it = _crear_datos(db)
        _dte(db, desp, en_vuelo_desde=datetime.utcnow())
        r = client.delete(f"/api/monza/despachos/entidades/{desp.id}")
        check("anular con claim vigente → 409 'en curso'", r.status_code == 409
              and "en curso" in r.json()["detail"], r.text)
        _limpiar(db)

        # ── 3. Anular con uuid EN PROCESO (status 2) → 409 en curso ──
        _cot, desp, _it = _crear_datos(db)
        _dte(db, desp, uuid="uuid-proc", status_id=STATUS_PROCESANDO)
        r = client.delete(f"/api/monza/despachos/entidades/{desp.id}")
        check("anular con uuid en proceso → 409", r.status_code == 409, r.text)
        _limpiar(db)

        # ── 4. Anular con uuid BORRADOR en Wasabil (status 6) → 409 ──
        _cot, desp, _it = _crear_datos(db)
        _dte(db, desp, uuid="uuid-borr", status_id=STATUS_PENDIENTE)
        r = client.delete(f"/api/monza/despachos/entidades/{desp.id}")
        check("anular con borrador en Wasabil → 409", r.status_code == 409, r.text)
        _limpiar(db)

        # ── 5. Anular con DTE FALLIDO (sin folio, claim vencido) → PERMITIDO ──
        _cot, desp, _it = _crear_datos(db)
        _dte(db, desp, uuid="uuid-fail", status_id=STATUS_FALLIDO,
             error="RUT receptor no autorizado",
             en_vuelo_desde=datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60))
        r = client.delete(f"/api/monza/despachos/entidades/{desp.id}")
        check("anular con DTE fallido → permitido", r.status_code == 200, r.text)
        db.rollback()
        check("anular con DTE fallido: quedó anulado",
              db.get(MonzaDespacho, desp.id).estado == "anulado")
        _limpiar(db)

        # ── 6. Anular sin DTE alguno → permitido (control) ──
        _cot, desp, _it = _crear_datos(db)
        r = client.delete(f"/api/monza/despachos/entidades/{desp.id}")
        check("anular sin DTE → permitido", r.status_code == 200, r.text)
        _limpiar(db)

        # ── 7. PUT cabecera pisando numero_guia con guía viva → 409 ──
        _cot, desp, _it = _crear_datos(db, numero_guia="777")
        _dte(db, desp, uuid="uuid-emitida", status_id=STATUS_EMITIDO, folio="777")
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"numero_guia": "G-MANUAL-PISADA"})
        check("PUT pisa folio con guía emitida → 409", r.status_code == 409
              and "777" in r.json()["detail"], r.text)
        db.rollback()
        check("PUT pisa folio: numero_guia intacto",
              db.get(MonzaDespacho, desp.id).numero_guia == "777")

        # ── 8. PUT con el MISMO valor → no-op OK (idempotente) ──
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"numero_guia": "777"})
        check("PUT mismo numero_guia → no-op OK", r.status_code == 200, r.text)

        # ── 9. PUT de otros campos (transportista) con guía viva → OK ──
        # La cabecera Monza es editable incluso cerrada (flujo guía-primero): el
        # guard protege SOLO el folio, no debe bloquear el resto de la edición.
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"transportista": "Chilexpress",
                             "numero_expedicion": "EXP-123"})
        check("PUT transportista con guía viva → OK", r.status_code == 200, r.text)
        db.rollback()
        d = db.get(MonzaDespacho, desp.id)
        check("PUT transportista persistido y folio intacto",
              d.transportista == "Chilexpress" and d.numero_guia == "777",
              (d.transportista, d.numero_guia))
        _limpiar(db)

        # ── 10. PUT pisando folio con emisión EN CURSO (claim, sin folio) → 409 ──
        _cot, desp, _it = _crear_datos(db, numero_guia="G-MANUAL-1")
        _dte(db, desp, en_vuelo_desde=datetime.utcnow())
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"numero_guia": "OTRA"})
        check("PUT pisa folio con emisión en curso → 409", r.status_code == 409
              and "en curso" in r.json()["detail"], r.text)
        _limpiar(db)

        # ── 11. PUT pisando numero_guia con DTE FALLIDO → permitido ──
        _cot, desp, _it = _crear_datos(db, numero_guia="G-MANUAL-1")
        _dte(db, desp, status_id=STATUS_FALLIDO, error="rechazada",
             en_vuelo_desde=None)
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"numero_guia": "G-MANUAL-2"})
        check("PUT numero_guia con DTE fallido → permitido", r.status_code == 200, r.text)
        db.rollback()
        check("PUT con DTE fallido: numero_guia actualizado",
              db.get(MonzaDespacho, desp.id).numero_guia == "G-MANUAL-2")
        _limpiar(db)

        # ── 12. Círculo completo: emisión real (simulada) copia folio → numero_guia
        #        y desde ahí anular/PUT quedan bloqueados ──
        _cot, desp, _it = _crear_datos(db, numero_guia="G-MANUAL-VIEJA")
        r = client.post(f"/api/monza/wasabil/despachos/{desp.id}/emitir")
        check("emisión simulada 200", r.status_code == 200, r.text)
        r = client.get(f"/api/monza/wasabil/despachos/{desp.id}/estado")
        check("estado emitido folio 777", r.json().get("folio") == "777", r.text)
        db.rollback()
        check("emitido copia folio → MonzaDespacho.numero_guia",
              db.get(MonzaDespacho, desp.id).numero_guia == "777")
        r = client.delete(f"/api/monza/despachos/entidades/{desp.id}")
        check("tras emitir: anular → 409", r.status_code == 409, r.text)
        r = client.put(f"/api/monza/despachos/entidades/{desp.id}",
                       json={"numero_guia": "G-PISADA"})
        check("tras emitir: PUT pisa folio → 409", r.status_code == 409, r.text)
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        _verificar_limpieza()
        print("Cleanup OK (verificado con sesión nueva)")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_monza_wasabil_guards_despachos():
    run()


if __name__ == "__main__":
    run()
