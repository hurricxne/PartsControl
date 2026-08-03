"""Arnés COMPARTIDO de las suites de FACTURA electrónica (DTE 33) de MonzaParts.

NO se llama `test_*.py` a propósito: pytest no debe recolectarlo como suite. Es el
andamiaje que las cuatro suites `test_factura_*.py` reutilizan para no repetir 150
líneas de montaje/limpieza cuatro veces.

Tres reglas de la casa que este archivo hace cumplir:

  1. JAMÁS se llama al API real de Wasabil. `FakeWasabil` pisa por monkeypatch el
     módulo `monza_wasabil_dte.client` — la superficie del client MONZA, NUNCA la de
     Grupo AM (`wasabil_dte.client`). Ese aislamiento es la razón de ser del client
     propio de cada marca y está verificado en `test_factura_aislamiento_fakes.py`.
     Emitir al SII es IRREVERSIBLE: un test que llame al API real emite documentos
     tributarios de verdad.
  2. Cada suite instancia SU PROPIO `FakeWasabil` y usa SU PROPIO MARK. Al empezar
     `run()` re-instala el fake (si pytest importó otras suites, la última instalación
     a nivel de módulo gana: sin esto la batería completa queda flaky).
  3. Todo lo que se crea se limpia, y la limpieza se VERIFICA con una sesión NUEVA.
     Nunca se toca un dato real: los datos de prueba llevan el prefijo MARK.

Orden de borrado (importa): las filas `monza_wasabil_dte` van PRIMERO. Su FK a
`monza_cont_factura_cliente` es RESTRICT (sin ON DELETE, igual que en Grupo AM), así
que borrar la factura antes que su DTE revienta con IntegrityError 1451.
"""
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace
from typing import Optional

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
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContFacturaClienteItem, MonzaContCobranza,
    MonzaContFactoring, MonzaContAdelanto,
)
from monza_contabilidad.router import router as contab_router  # noqa: E402
from monza_wasabil_dte import client as monza_client  # noqa: E402
from monza_wasabil_dte.router import router as wasabil_router  # noqa: E402
from monza_wasabil_dte.models import MonzaWasabilDte  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura monza_wasabil_dte

# RUT VÁLIDO módulo 11: `_validar_receptor_factura` lo exige para tipo 'factura'
# (Fase 3). Un RUT con DV malo bloquea la factura antes de llegar al SII.
RUT_CLIENTE = "77.111.222-6"
RUT_SIN_FICHA = "76.543.210-3"   # válido, pero inexistente en el Wasabil simulado
RUT_INVALIDO = "77.111.222-3"    # DV incorrecto a propósito

# Fecha de EMISIÓN de la guía EN PAPEL de la venta patrón. Se eligió DISTINTA de la fecha
# de cierre del despacho (2026-06-20) a propósito: es la que la factura cita en su
# referencia 52, y si alguna suite las confunde el valor delata cuál está usando.
FECHA_GUIA_PAPEL = date(2026, 6, 18)

# Precios/cantidades de la venta patrón (los usan todas las suites para poder
# escribir los montos esperados a mano en los asserts, sin recalcularlos).
PRECIO_1, QTY_1, QTY_DESP_1 = 15000, 10, 4
PRECIO_2, QTY_2, QTY_DESP_2 = 2500, 20, 20
NETO_DESPACHO = QTY_DESP_1 * PRECIO_1 + QTY_DESP_2 * PRECIO_2   # 110.000
NETO_VENTA = QTY_1 * PRECIO_1 + QTY_2 * PRECIO_2                # 200.000


class Checker:
    """Colector de checks con salida legible (mismo formato que las suites de la Fase 5:
    una línea OK/FAIL por verificación, y al final el listado de las que fallaron)."""

    def __init__(self):
        self.fails: list = []

    def __call__(self, name, cond, extra=""):
        print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
        if not cond:
            self.fails.append(name)
        return bool(cond)

    def finish(self):
        if self.fails:
            raise AssertionError(f"{len(self.fails)} checks fallaron: {self.fails}")
        print("\n=== TODO OK ===")


class FakeWasabil:
    """API de Wasabil SIMULADO para el client MONZA, configurable por escenario.

    `creados` registra cada payload recibido: los asserts anti doble emisión se hacen
    por DELTA de `len(creados)`, nunca por valor absoluto (los escenarios previos de la
    misma suite ya emitieron)."""

    def __init__(self, mark: str, rut: str = RUT_CLIENTE):
        self.configurado = True
        # Ficha en el FORMATO REAL del API (giros[]/addresses[] anidados): así el test
        # pasa por client._normalizar_cliente REAL y protege la normalización.
        self.cliente = {"id": 160065, "rut": rut,
                        "name": f"{mark} AUTOMOTORES SPA",
                        "giros": [{"name": "VENTA DE REPUESTOS AUTOMOTRICES", "default": True}],
                        "addresses": [{"address": "AV. LAS CONDES 10000",
                                       "comuna": "Las Condes", "city": "Santiago",
                                       "default": True}]}
        self.crear_falla: Optional[Exception] = None  # Exception a lanzar en crear_documento
        self.status_respuesta = 2      # status_id que devuelve crear_documento
        self.estado_final = 3          # status_id que devuelve estado_documento
        self.folio_emitido = "9001"    # folio del SII de un documento propio
        self.display_error = None
        self.docs_buscables: list = []  # para buscar_documentos (reintento sin uuid)
        self.busqueda_completa = True   # False = lista truncada por paginación
        self.creados: list = []
        self.antes_de_crear = None      # hook (para los tests de concurrencia)

    def install(self):
        """Pisa SOLO `monza_wasabil_dte.client`. El client de Grupo AM
        (`wasabil_dte.client`) queda intacto — ver test_factura_aislamiento_fakes."""
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
        if self.antes_de_crear:
            self.antes_de_crear(payload)
        if self.crear_falla:
            raise self.crear_falla
        self.creados.append(payload)
        return {"uuid": f"uuid-f{len(self.creados)}", "status_id": self.status_respuesta}

    def _estado(self, uuid):
        return {"uuid": uuid, "status_id": self.estado_final,
                "display_error": self.display_error}

    def _obtener(self, uuid):
        # El folio depende del documento consultado: la adopción de un documento
        # perdido trae SU folio real, no uno genérico.
        folio = "8888" if uuid == "uuid-perdido" else self.folio_emitido
        return {"uuid": uuid, "status_id": self.estado_final, "folio": folio,
                "display_error": self.display_error,
                "document_pdf_url": f"https://api.wasabil.com/pdf/{folio}",
                "document_xml_url": f"https://api.wasabil.com/xml/{folio}"}


def montar_app(current: dict) -> TestClient:
    """App efímera con los DOS routers que intervienen en la Fase 6 (sin tocar main.py):
    Contabilidad Monza (trae su propio prefijo) y Wasabil DTE Monza (prefix=/api, como
    main.py). `current` es un dict mutable: cambiarle 'empresa' prueba el candado."""
    app = FastAPI()
    app.include_router(contab_router)                  # /api/monza/contabilidad/...
    app.include_router(wasabil_router, prefix="/api")  # /api/monza/wasabil/...
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=current["id"], empresa=current["empresa"], email="test-mwfac@monza.cl")
    return TestClient(app)


_seq = {"n": 0}


def crear_venta(db, mark: str, *, rut: str = RUT_CLIENTE, numero_oc: str = "OC-4501",
                oc_fecha: Optional[date] = date(2026, 6, 10), iva_pct: float = 19.0,
                estado_despacho: str = "despachado",
                numero_guia_manual: Optional[str] = None,
                pct_adelanto: int = 0, precio1: int = PRECIO_1, precio2: int = PRECIO_2,
                con_despacho: bool = True):
    """Venta patrón: cliente + cotización vendida + 2 ítems + 1 despacho PARCIAL
    (4/10 del ítem 1, 20/20 del ítem 2).

    El despacho nace en estado 'despachado' porque es el ÚNICO facturable: en el flujo
    real la guía 52 se emite con el despacho en preparación y el cierre lo pasa a
    'despachado'. Las suites que necesitan el DTE de la guía lo fabrican con `dte_guia`.

    `total_bruto` se congela como en una venta real (neto de TODOS los ítems + IVA):
    es el techo del tope Σ brutos de `_construir_factura`."""
    _seq["n"] += 1
    n = _seq["n"]
    numero_cot = f"{mark}-COT-{n}"
    # MonzaCotizacion.numero es String(20): un MARK largo revienta con "Data too long"
    # a mitad de la suite, y el error de MySQL no dice cuál es el prefijo culpable.
    assert len(numero_cot) <= 20, (
        f"MARK demasiado largo: '{numero_cot}' ({len(numero_cot)} chars) no cabe en "
        "monza_cotizaciones.numero (String(20)) — acorta el MARK de la suite")
    neto_venta = QTY_1 * precio1 + QTY_2 * precio2
    iva_venta = round(neto_venta * (iva_pct or 19.0) / 100.0)

    cliente = MonzaCliente(nombre=f"{mark} CLIENTE {n}", rut=(rut or None))
    db.add(cliente); db.flush()
    cot = MonzaCotizacion(numero=numero_cot, cliente_id=cliente.id, estado="vendida",
                          oc_cliente=numero_oc, oc_fecha=oc_fecha, iva_pct=iva_pct,
                          pct_adelanto=pct_adelanto,
                          total_neto=neto_venta, iva_monto=iva_venta,
                          total_bruto=neto_venta + iva_venta)
    db.add(cot); db.flush()
    it1 = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Filtro de aceite motor",
                              numero_parte="A2761800009", cantidad=QTY_1,
                              precio_unitario_clp=precio1, estado_linea="despachado")
    it2 = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Sello de polvo",
                              numero_parte="A0009884399", cantidad=QTY_2,
                              precio_unitario_clp=precio2, estado_linea="despachado")
    db.add_all([it1, it2]); db.flush()
    desp = None
    if con_despacho:
        desp = MonzaDespacho(numero=f"{mark}-DSP-{n}", cotizacion_id=cot.id,
                             cliente_nombre=cliente.nombre, estado=estado_despacho,
                             destinatario="Juan Pérez", numero_guia=numero_guia_manual,
                             # Una guía en PAPEL siempre tiene fecha de emisión, y la
                             # referencia 52 la exige (bloquea si falta). Se pone SÓLO
                             # cuando hay N° manual, para que el caso "sin guía" siga
                             # siendo el caso sin guía. Es una fecha DISTINTA del cierre
                             # a propósito: así ninguna suite pasa por confundirlas.
                             fecha_guia=(FECHA_GUIA_PAPEL if numero_guia_manual else None),
                             fecha_despacho=datetime(2026, 6, 20, 10, 0, 0),
                             direccion_entrega="Bodega central, Roger de Flor 2996")
        db.add(desp); db.flush()
        db.add_all([
            MonzaDespachoItem(despacho_id=desp.id, item_id=it1.id, qty_despachada=QTY_DESP_1),
            MonzaDespachoItem(despacho_id=desp.id, item_id=it2.id, qty_despachada=QTY_DESP_2),
        ])
    db.commit()
    return cot, desp, it1, it2


def despacho_extra(db, mark: str, cot, items_qty: dict, *, estado: str = "despachado",
                   numero_guia: Optional[str] = None) -> MonzaDespacho:
    """Segundo despacho 'despachado' de la MISMA venta (items_qty = {item_id: qty}).

    Lo necesita el candado de INTENCIÓN por cotización: con un solo despacho, la
    segunda emisión ya la frena el tope "el despacho ya fue facturado por completo" y
    el candado nunca llega a ejercitarse."""
    _seq["n"] += 1
    desp = MonzaDespacho(numero=f"{mark}-DSP-{_seq['n']}", cotizacion_id=cot.id,
                         cliente_nombre=cot.cliente.nombre if cot.cliente else None,
                         estado=estado, destinatario="Juan Pérez", numero_guia=numero_guia,
                         fecha_guia=(FECHA_GUIA_PAPEL if numero_guia else None),
                         fecha_despacho=datetime(2026, 6, 22, 10, 0, 0),
                         direccion_entrega="Bodega central, Roger de Flor 2996")
    db.add(desp); db.flush()
    for item_id, qty in items_qty.items():
        db.add(MonzaDespachoItem(despacho_id=desp.id, item_id=item_id, qty_despachada=qty))
    db.commit()
    return desp


def dte_guia(db, desp, **kw) -> MonzaWasabilDte:
    """Fila DTE de GUÍA (52) fabricada a mano: permite montar los estados que en la
    realidad solo se alcanzan con timing (guía en proceso, guía emitida con folio
    mientras `despacho.numero_guia` conserva el N° tecleado viejo)."""
    base = dict(empresa="automotriz", tipo_dte=52, despacho_id=desp.id,
                uuid=None, status_id=None, en_vuelo_desde=None, folio=None, error=None)
    base.update(kw)
    fila = MonzaWasabilDte(**base)
    db.add(fila)
    db.commit()
    return fila


def dte_de_factura(db, factura_id: int) -> Optional[MonzaWasabilDte]:
    return (db.query(MonzaWasabilDte)
            .filter(MonzaWasabilDte.factura_id == factura_id,
                    MonzaWasabilDte.tipo_dte == 33)
            .first())


def facturas_de(db, cot_id: int) -> list:
    return (db.query(MonzaContFacturaCliente)
            .filter(MonzaContFacturaCliente.cotizacion_id == cot_id)
            .order_by(MonzaContFacturaCliente.id.asc()).all())


def cobranzas_de(db, factura_id: int) -> list:
    return (db.query(MonzaContCobranza)
            .filter(MonzaContCobranza.factura_id == factura_id).all())


def limpiar(db, mark: str):
    """Borra TODO lo del MARK. El orden es el contrato: DTE → factoring/cobranzas →
    líneas → factura → adelanto → despacho → cotización → cliente."""
    db.rollback()  # cierra cualquier snapshot/lock abierto por el test

    # Facturas por el SNAPSHOT numero_cotizacion: sobreviven aunque la cotización se
    # haya borrado (la FK es SET NULL) — así una corrida anterior muerta a medias no
    # deja facturas huérfanas invisibles para el barrido por cotización.
    facturas = (db.query(MonzaContFacturaCliente)
                .filter(MonzaContFacturaCliente.numero_cotizacion.like(f"{mark}%")).all())
    for f in facturas:
        db.query(MonzaWasabilDte).filter(MonzaWasabilDte.factura_id == f.id).delete(
            synchronize_session=False)
        db.query(MonzaContFactoring).filter(MonzaContFactoring.factura_id == f.id).delete(
            synchronize_session=False)
        db.query(MonzaContCobranza).filter(MonzaContCobranza.factura_id == f.id).delete(
            synchronize_session=False)
        db.query(MonzaContFacturaClienteItem).filter(
            MonzaContFacturaClienteItem.factura_id == f.id).delete(synchronize_session=False)
        db.delete(f)
    db.flush()

    for cot in db.query(MonzaCotizacion).filter(MonzaCotizacion.numero.like(f"{mark}%")).all():
        for d in db.query(MonzaDespacho).filter(MonzaDespacho.cotizacion_id == cot.id).all():
            db.query(MonzaWasabilDte).filter(MonzaWasabilDte.despacho_id == d.id).delete(
                synchronize_session=False)
            db.query(MonzaDespachoItem).filter(
                MonzaDespachoItem.despacho_id == d.id).delete(synchronize_session=False)
            db.delete(d)
        db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id == cot.id).delete(synchronize_session=False)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id == cot.id).delete(synchronize_session=False)
        db.delete(cot)
    # flush ANTES del borrado masivo de clientes: los db.delete(cot) de arriba son ORM
    # (pendientes) y el bulk DELETE se ejecuta al tiro — sin flush, la FK cliente_id de
    # las cotizaciones aún vivas revienta con 1451.
    db.flush()
    db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{mark}%")).delete(
        synchronize_session=False)
    db.query(MonzaLog).filter(MonzaLog.entidad_ref.like(f"{mark}%")).delete(
        synchronize_session=False)
    db.commit()


def verificar_limpieza(mark: str):
    """Sesión NUEVA (regla de la casa): confirma que no quedó NINGUNA fila del MARK.
    Una sesión reutilizada podría estar sirviendo su propio snapshot."""
    db2 = SessionLocal()
    try:
        restos = (
            db2.query(MonzaCotizacion).filter(MonzaCotizacion.numero.like(f"{mark}%")).count()
            + db2.query(MonzaDespacho).filter(MonzaDespacho.numero.like(f"{mark}%")).count()
            + db2.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{mark}%")).count()
            + db2.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.numero_cotizacion.like(f"{mark}%")).count()
            + db2.query(MonzaLog).filter(MonzaLog.entidad_ref.like(f"{mark}%")).count()
        )
        assert restos == 0, f"quedaron {restos} filas {mark} tras la limpieza"
    finally:
        db2.close()
    print("Cleanup OK (verificado con sesión nueva)")
