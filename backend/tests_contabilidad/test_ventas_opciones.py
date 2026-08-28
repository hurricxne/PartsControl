"""Tests de GET /ventas/opciones (listado LIVIANO) + helpers batch de guías facturables.

Protege el contrato del endpoint nuevo y la PARIDAD del refactor que lo sostiene:
`despachos_facturables` y el faltante declarado de `listar_ventas` ahora delegan en los
helpers batch (_guias_facturables_por_ocs / _faltante_declarado_por_ocs), así que estos
checks fijan que la semántica es EXACTAMENTE la de antes del refactor.

Lo no negociable que se prueba acá:
  · la ruta /ventas/opciones se registra ANTES que /ventas/{oc_id} (si no → 422);
  · CERO motor de precios (bomba con poder discriminante: el control SÍ explota);
  · el coalesce firmada efectiva es `is not None`, JAMÁS `or` (qty_firmada == 0 es
    legítimo): check 6-bis (n == 0 sobre el fixture Z) + assert puro de la sección 0;
  · el GEMELO de ese coalesce en la otra pantalla: GET /ventas/{oc} publica
    qty_firmada == 0 TAL CUAL, porque ahí `null` significa «firma completa» y
    publicarlo como null mentiría al revés (sección 6-ter);
  · el adaptador `_SesionDeUnaFilaDte` le entrega al predicado del EMISOR la entidad
    ORM COMPLETA, no una proyección de las 4 columnas que hoy mira (sección
    10-quinquies): sin esta sonda la alarma era DIFERIDA — gate verde y 500 en
    producción el día que el emisor leyera una columna más;
  · plazo_dias == 0 (CONTADO) genera fecha_vencimiento = fecha de emisión, jamás null
    con semáforo 'sin_fecha' (sección 15-bis);
  · la cascada de fecha de la guía (electrónica → bloqueada → papel → None; jamás
    fecha_despacho): checks de la sección 10 + asserts puros de la sección 0;
  · fuente='bloqueada' en TODOS los estados con los que la emisión de la factura 33
    rechaza la referencia 52 — y la EQUIVALENCIA se afirma contra la función del
    emisor (`wasabil_dte.router._guia_no_referenciable`), no contra una copia;
  · el selector de guías (`/ventas/{oc}/despachos-facturables`) publica la MISMA
    fecha/fuente que /ventas/opciones y ordenada (sin fecha primero, luego ASC);
  · payload_json JSON-válido-pero-no-objeto ('null') degrada a None POR FILA, sin
    tumbar el endpoint entero;
  · TOL_QTY (unidades) como umbral, no TOL (pesos);
  · presupuesto de queries (≤ 7 con ~20 OCs marcadas) y, en GET /ventas, el IN de
    guías firmadas pagado UNA sola vez (bajó en 1: los dos derivadores comparten
    las filas pre-obtenidas).

ESTA SUITE NO ESCRIBE NI UN BYTE EN EL FUENTE (regla nueva). Antes mutaba
routers/contabilidad.py en disco para probar que las sondas caían al romper el arreglo;
si la corrida moría en esa ventana (kill -9, y con uvicorn arriba la suite se cuelga),
el árbol quedaba con la REGRESIÓN escrita y la corrida siguiente no la reparaba: el sha
de referencia se tomaba del archivo ya mutado. Las mutaciones además no compraban nada:
los checks planos que ya estaban tienen el mismo poder discriminante (6-bis afirma n==0
sobre el MISMO fixture con el que la mutación afirmaba n==1; el check de F1 en la
sección 10 cae si se invierte el orden de la cascada). Verificar «quito el arreglo y el
test cae» se hace UNA vez A MANO al escribir la sonda, no en cada corrida.
La sección 0 corre ANTES de sembrar nada y es, además, el detector de un fuente que
haya quedado corrupto por una corrida vieja: su FAIL sale como PRIMERA línea de la
salida. Lo que se promete es la POSICIÓN, no un corte: `check()` NO aborta —apila en
`_fails` y sigue, a propósito, para que UNA corrida reporte TODOS los fallos (la base
MySQL es compartida y una corrida es cara)—, así que la suite igual llega hasta el
final. El texto decía «falla ruidoso en el primer check» y eso no era cierto: se
corrigió en vez de hacerla abortar, porque abortar convertiría un helper que usan ~60
checks en un caso especial y cambiaría el contrato de toda la suite por una sección.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_ventas_opciones.py -q
(también:   ./venv/bin/python tests_contabilidad/test_ventas_opciones.py)
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, or_, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContAdelanto,
)
from tesoreria.models import ConciliacionIngreso  # noqa: E402
# Import del modelo DTE ANTES del create_all: la tabla wasabil_dte no se autocrea
# si el modelo no está cargado (trampa conocida de los deploys del módulo).
from wasabil_dte.models import WasabilDte, STATUS_EMITIDO  # noqa: E402
import routers.contabilidad as cont  # noqa: E402

MARK = "TVOPCX"  # SIN guiones bajos: en LIKE el _ es comodín (regla de la casa)
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(cont.router, prefix="/api")


# Auth REALISTA (molde de la casa): además de devolver el usuario hace una lectura en la
# MISMA sesión del request, igual que auth.get_current_user en producción — abre el read
# view de REPEATABLE READ antes de cualquier otra sentencia, la condición real de los
# endpoints. También cuenta como 1 SELECT en el presupuesto de queries (check 16).
def _current_user_realista(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _current_user_realista
client = TestClient(app)

PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0),
                   "total_venta_clp": cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0))}
            for i in items}
    neto = sum(cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0)) for i in items)
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


def _bomba(*a, **k):
    raise RuntimeError("BOMBA: el motor de precios fue llamado")


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear_venta(db, *, precio=10000.0, cantidad=10, qty_desp=None, qty_firmada=None,
                 con_despacho=True, estado="despachado", guia_firmada=1,
                 fecha_guia=None, sufijo="A"):
    """Venta de 1 ítem. Con despacho (estado/firma parametrizables) por qty_desp
    (default: la cantidad completa); qty_firmada None = NULL (firma completa)."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} HEPI",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro", cantidad=cantidad,
                        estado_item="despachado" if con_despacho else "en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}",
                   fecha_oc="2026-07-01", cond_pago="30 días")
    db.add(oc); db.flush()
    desp = None
    if con_despacho:
        desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                        estado=estado, guia_firmada=guia_firmada,
                        numero_guia=f"G-{oc.id}", fecha_guia=fecha_guia)
        db.add(desp); db.flush()
        db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id,
                            qty_despachada=qty_desp if qty_desp is not None else cantidad,
                            qty_firmada=qty_firmada))
    db.commit()
    PRECIOS[it.id] = precio
    return cot, oc, desp, it


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    conds = [OcCliente.numero_oc.like(f"{MARK}%")]
    if cot_ids:
        conds.append(OcCliente.cotizacion_id.in_(cot_ids))
    ocs = db.query(OcCliente).filter(or_(*conds)).all()
    oc_ids = [oc.id for oc in ocs]
    if oc_ids:
        adel_ids = [a.id for a in db.query(ContAdelanto)
                    .filter(ContAdelanto.oc_cliente_id.in_(oc_ids)).all()]
        fac_ids = [f.id for f in db.query(ContFacturaCliente)
                   .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
        if adel_ids:
            db.query(ConciliacionIngreso).filter(
                ConciliacionIngreso.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(ContCobranza).filter(
                ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFacturaClienteItem).filter(
                ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
        if adel_ids:
            db.query(ContAdelanto).filter(
                ContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(ContFacturaCliente).filter(
                ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
        if desp_ids:
            # las filas DTE referencian al despacho (FK): fuera primero
            db.query(WasabilDte).filter(
                WasabilDte.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
    if cot_ids:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.commit()


def _opciones():
    r = client.get("/api/contabilidad/ventas/opciones")
    assert r.status_code == 200, r.text
    return r.json()


def _fila_op(data, oc_id):
    return next((v for v in data["opciones"] if v["oc_cliente_id"] == oc_id), None)


def _n_op(data, oc_id):
    """n de guías facturables de la fila, o None si la OC no aparece (sin reventar
    el run entero por un KeyError: un FAIL informativo vale más que un traceback)."""
    f = _fila_op(data, oc_id)
    return None if f is None else f.get("guias_facturables_n")


def _guia1(data, oc_id):
    """Primera guía anidada de la fila (o {} si no hay: FAIL legible, no IndexError)."""
    f = _fila_op(data, oc_id) or {}
    gs = f.get("guias_facturables") or []
    return gs[0] if gs else {}


def _selector(oc_id):
    r = client.get(f"/api/contabilidad/ventas/{oc_id}/despachos-facturables")
    assert r.status_code == 200, r.text
    return r.json()


def _fila_listado(oc_id):
    r = client.get("/api/contabilidad/ventas")
    assert r.status_code == 200, r.text
    return next((v for v in r.json() if v["oc_cliente_id"] == oc_id), None)


ROW_KEYS = {"oc_cliente_id", "numero_oc", "numero_cotizacion", "cliente", "rut_cliente",
            "fecha_venta", "fecha_oc", "cond_pago", "guias_facturables_n",
            "guias_facturables"}
GUIA_KEYS = {"numero_guia", "numero_despacho", "fecha", "fuente"}
# Contrato del selector: las 8 claves históricas + las 2 ADITIVAS (fecha, fuente).
# `fecha_guia` (columna de la guía en PAPEL) se conserva porque es el contrato viejo,
# pero la que la pantalla debe pintar es `fecha`: mostrar fecha_guia hacía que TODA
# guía ELECTRÓNICA saliera «(sin fecha ⚠)» mientras el chip de la misma pantalla
# mostraba la fecha correcta.
SELECTOR_KEYS = {"id", "numero_despacho", "numero_guia", "numero_expedicion",
                 "fecha_guia", "guia_firmada_archivo", "items_count", "facturable",
                 "fecha", "fuente"}


def run():
    db = SessionLocal()
    cont._precios_de_cotizacion = _fake_precios
    try:
        _limpiar(db)

        # ═══ 0 · Reglas PURAS (y detector de fuente corrupto por una corrida vieja) ═══
        # Van ANTES de sembrar nada: si un árbol quedó con la regresión escrita (la
        # suite vieja mutaba routers/contabilidad.py en disco), el FAIL sale en la
        # PRIMERA línea de la salida y no enterrado después de 20 fixtures. OJO: eso es
        # POSICIÓN, no corte — `check()` no aborta (apila en `_fails` y sigue, para que
        # una corrida reporte todos los fallos), así que la suite continúa igual. Son
        # también la documentación ejecutable de las dos reglas que aquellas mutaciones
        # querían proteger.
        check("0 coalesce: qty_firmada == 0 NO cae a la despachada (el 0 es LEGÍTIMO: "
              "el cliente firmó declarando que no recibió nada)",
              cont._firmada_efectiva_val(0, 5) == 0.0,
              cont._firmada_efectiva_val(0, 5))
        check("0 coalesce: qty_firmada NULL sí cae a la despachada",
              cont._firmada_efectiva_val(None, 5) == 5.0,
              cont._firmada_efectiva_val(None, 5))
        check("0 cascada: con 52 emitida manda su documentDate sobre la fecha de papel",
              cont._fecha_fuente_guia(True, "2026-07-20", "2026-07-01")
              == ("2026-07-20", "electronica"),
              cont._fecha_fuente_guia(True, "2026-07-20", "2026-07-01"))
        check("0 cascada: bloqueada manda sobre papel (la fecha de papel viaja como "
              "aviso, no como guía facturable limpia)",
              cont._fecha_fuente_guia(False, None, "2026-07-01", bloqueada=True)
              == ("2026-07-01", "bloqueada"),
              cont._fecha_fuente_guia(False, None, "2026-07-01", bloqueada=True))
        check("0 cascada: sin electrónica ni papel → (None, None), JAMÁS fecha_despacho",
              cont._fecha_fuente_guia(False, None, None) == (None, None),
              cont._fecha_fuente_guia(False, None, None))

        # ═══ 1 · La ruta existe y NO cae en /ventas/{oc_id} (orden de registro) ═══
        r = client.get("/api/contabilidad/ventas/opciones")
        check("1 GET /ventas/opciones responde 200 (no 422: registrada antes de {oc_id})",
              r.status_code == 200 and r.status_code != 422, f"status={r.status_code}")

        # ═══ 4 · Guía firmada completa sin facturar → n == 1 ═══
        cot_a, oc_a, desp_a, it_a = _crear_venta(db, sufijo="A")
        data = _opciones()
        fila_a = _fila_op(data, oc_a.id)
        check("4 guía firmada completa sin facturar: n == 1 y 1 guía anidada",
              fila_a is not None and fila_a["guias_facturables_n"] == 1
              and len(fila_a["guias_facturables"]) == 1, fila_a)

        # ═══ 2 · Sonda anti-motor CON poder discriminante ═══
        cont._precios_de_cotizacion = _bomba
        r = client.get("/api/contabilidad/ventas/opciones")
        check("2 /opciones con bomba en el motor de precios: 200 (jamás lo llama)",
              r.status_code == 200, f"status={r.status_code}")
        # CONTROL: la bomba de verdad explota donde el motor SÍ se usa — sin esto la
        # sonda no probaría nada (podría estar mal monkeypatcheada).
        exploto = False
        try:
            client.get("/api/contabilidad/ventas")
        except RuntimeError:
            exploto = True
        check("2 CONTROL: GET /ventas con la misma bomba explota (la sonda discrimina)",
              exploto, "el control no explotó: bomba sin poder")
        cont._precios_de_cotizacion = _fake_precios

        # ═══ 3 · Contrato exacto ═══
        data = _opciones()
        fila_a = _fila_op(data, oc_a.id)
        h1 = cont._hoy_chile().isoformat()
        check("3 top-level: claves exactas {hoy, opciones} y hoy == fecha Chile",
              set(data.keys()) == {"hoy", "opciones"}
              and data["hoy"] in (h1, cont._hoy_chile().isoformat()), data.get("hoy"))
        check("3 fila: exactamente los 10 campos pactados",
              fila_a is not None and set(fila_a.keys()) == ROW_KEYS,
              sorted(fila_a.keys()) if fila_a else None)
        check("3 guía anidada: exactamente {numero_guia, numero_despacho, fecha, fuente}",
              fila_a is not None and set(_guia1(data, oc_a.id).keys()) == GUIA_KEYS,
              fila_a["guias_facturables"] if fila_a else None)
        check("3 identidad de la fila (numero_oc / cotización / cliente / cond_pago)",
              fila_a is not None and fila_a["numero_oc"] == oc_a.numero_oc
              and fila_a["numero_cotizacion"] == cot_a.numero
              and fila_a["cliente"] == f"{MARK} HEPI" and fila_a["cond_pago"] == "30 días"
              and fila_a["fecha_oc"] == "2026-07-01", fila_a)

        # ═══ 5 · Firma parcial (despachada 3 / firmada 2) ═══
        cot_p1, oc_p1, desp_p1, it_p1 = _crear_venta(db, cantidad=10, qty_desp=3,
                                                     qty_firmada=2, sufijo="P1")
        cot_p2, oc_p2, desp_p2, it_p2 = _crear_venta(db, cantidad=10, qty_desp=3,
                                                     qty_firmada=2, sufijo="P2")
        sel_p2 = _selector(oc_p2.id)
        data = _opciones()
        check("5 parcial sin facturar: n == 1 y selector con facturable == 2 (lo firmado)",
              _n_op(data, oc_p2.id) == 1
              and len(sel_p2) == 1 and abs(sel_p2[0]["facturable"] - 2.0) < 1e-9, sel_p2)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc_p1.id, "despacho_id": desp_p1.id,
                              "numero_factura": f"{MARK}-FP1"})
        check("5 emitir factura de lo firmado (2) 200", r.status_code == 200, r.text)
        data = _opciones()
        check("5 parcial con las 2 firmadas ya facturadas: n == 0 (la guía desaparece)",
              _n_op(data, oc_p1.id) == 0
              and (_fila_op(data, oc_p1.id) or {}).get("guias_facturables") == [],
              _fila_op(data, oc_p1.id))

        # ═══ 6 · qty_firmada NULL → cuenta por la despachada (coalesce) ═══
        cot_n, oc_n, desp_n, it_n = _crear_venta(db, cantidad=10, qty_desp=4,
                                                 qty_firmada=None, sufijo="N")
        data = _opciones()
        sel_n = _selector(oc_n.id)
        check("6 qty_firmada NULL: n == 1 y facturable == 4 (la despachada)",
              _n_op(data, oc_n.id) == 1
              and abs(sel_n[0]["facturable"] - 4.0) < 1e-9, sel_n)

        # ═══ 6-bis · qty_firmada == 0 NO aporta saldo (cliente no recibió NADA) ═══
        cot_z, oc_z, desp_z, it_z = _crear_venta(db, cantidad=5, qty_desp=5,
                                                 qty_firmada=0, sufijo="Z")
        data = _opciones()
        # PODER DISCRIMINANTE de este check (verificado a mano al escribirlo, y por eso
        # ya no se muta el fuente en cada corrida): con `or` en vez de `is not None`,
        # el 0 es falsy, coalesce a la despachada (5), facturable = 5 > TOL_QTY y esta
        # MISMA aserción pasa a ver n == 1. El check 6 de arriba (qty_firmada NULL →
        # n == 1) cierra el A/B: los dos juntos fijan que sólo el NULL coalesce.
        check("6-bis qty_firmada == 0: n == 0 (no se ofrece facturar lo no recibido)",
              _n_op(data, oc_z.id) == 0,
              _fila_op(data, oc_z.id))

        # ═══ 6-ter · GET /ventas/{oc} publica qty_firmada == 0 TAL CUAL (no null) ═══
        # GEMELO del bug que 6-bis pinza en /ventas/opciones, pero en la OTRA pantalla y
        # mintiendo al REVÉS: en el detalle de venta `null` SIGNIFICA «firma completa»
        # (lo declara el comentario de routers/contabilidad.py, línea de "qty_firmada"),
        # así que publicar el 0 como null pinta como ENTREGADA COMPLETA justo la línea
        # que el cliente firmó declarando que NO recibió NADA de ella.
        # PODER DISCRIMINANTE (razonado sobre la mutación, no ejecutado sobre el fuente):
        # si alguien cambia en contabilidad.py
        #     "qty_firmada": (_f(di.qty_firmada) if di.qty_firmada is not None else None)
        # por  "qty_firmada": (_f(di.qty_firmada) or None),
        # entonces _f(0) == 0.0 es FALSY y la línea FZ-0 pasa a publicar null → CAE el
        # primer check de esta sección. La línea FZ-N (qty_firmada NULL) es el control
        # del A/B: fija que el null legítimo tiene que SEGUIR siendo null, así que el
        # agujero tampoco se puede "tapar" emitiendo siempre un número.
        # ESTADO ALCANZABLE, no de laboratorio: la firma parcial solo rechaza que la SUMA
        # firmada de la guía sea 0 (routers/tests/test_firma_parcial.py), así que una guía
        # con una línea en 0 y otra en 2 es perfectamente legítima — es este fixture.
        cot_fz = Cotizacion(numero=f"{MARK}-COT-FZ", cliente=f"{MARK} HEPI",
                            rut_cliente="78.279.030-7")
        db.add(cot_fz); db.flush()
        it_fz = {}
        for n, parte in ((1, "FZ-0"), (2, "FZ-2"), (3, "FZ-N")):
            it = ItemCotizacion(cotizacion_id=cot_fz.id, item_num=n, numero_parte=parte,
                                descripcion=parte, cantidad=2, estado_item="despachado")
            db.add(it); db.flush()
            it_fz[parte] = it
            PRECIOS[it.id] = 1000.0
        oc_fz = OcCliente(cotizacion_id=cot_fz.id, numero_oc=f"{MARK}-OC-FZ",
                          fecha_oc="2026-07-01")
        db.add(oc_fz); db.flush()
        desp_fz = Despacho(numero_despacho=f"{MARK}-DSP-FZ", oc_cliente_id=oc_fz.id,
                           estado="despachado", guia_firmada=1, numero_guia="G-FZ")
        db.add(desp_fz); db.flush()
        # 0 = firmó declarando que NO recibió nada · 2 = completa · NULL = sin declaración
        for parte, qf in (("FZ-0", 0), ("FZ-2", 2), ("FZ-N", None)):
            db.add(DespachoItem(despacho_id=desp_fz.id,
                                item_cotizacion_id=it_fz[parte].id,
                                qty_despachada=2, qty_firmada=qf))
        db.commit()
        r_det = client.get(f"/api/contabilidad/ventas/{oc_fz.id}")
        check("6-ter GET /ventas/{oc} con firma parcial mixta responde 200",
              r_det.status_code == 200, r_det.text)
        det = r_det.json() if r_det.status_code == 200 else {"items": []}
        g_fz = {i["numero_parte"]: (i["guias"] or [{}])[0] for i in det["items"]}
        check("6-ter la línea firmada en 0 se publica como 0.0, NO como null (null "
              "significa «firma completa»: publicarlo diría que el cliente recibió TODO "
              "justo la línea que declaró no haber recibido)",
              g_fz.get("FZ-0", {}).get("qty_firmada") == 0.0,
              {k: v.get("qty_firmada") for k, v in g_fz.items()})
        check("6-ter CONTROL del A/B: la línea sin declaración (NULL) SIGUE publicando "
              "null y la firmada completa publica 2.0 (el arreglo no puede ser «emitir "
              "siempre un número»)",
              g_fz.get("FZ-N", {}).get("qty_firmada") is None
              and g_fz.get("FZ-2", {}).get("qty_firmada") == 2.0,
              {k: v.get("qty_firmada") for k, v in g_fz.items()})

        # ═══ 7 · Estados del despacho que NO son facturables ═══
        _, oc_c1, _, _ = _crear_venta(db, guia_firmada=0, sufijo="C1")        # cerrado sin firmar
        _, oc_c2, _, _ = _crear_venta(db, estado="en_preparacion", sufijo="C2")
        _, oc_c3, _, _ = _crear_venta(db, estado="anulado", sufijo="C3")
        data = _opciones()
        check("7 cerrado sin firmar / en_preparacion / anulado: n == 0 en los tres",
              all(_n_op(data, oc.id) == 0 for oc in (oc_c1, oc_c2, oc_c3)),
              [_n_op(data, oc.id) for oc in (oc_c1, oc_c2, oc_c3)])

        # ═══ 8 · Umbral TOL_QTY (unidades), jamás TOL (pesos) ═══
        _, oc_t1, _, _ = _crear_venta(db, cantidad=1, qty_desp=0.4, sufijo="T1")
        _, oc_t2, _, _ = _crear_venta(db, cantidad=1, qty_desp=0.0005, sufijo="T2")
        data = _opciones()
        check("8 saldo 0.4 unidades: n == 1 (con TOL pesos=0.5 habría desaparecido)",
              _n_op(data, oc_t1.id) == 1,
              _fila_op(data, oc_t1.id))
        check("8 saldo 0.0005 (≤ TOL_QTY): n == 0 (polvo numérico, no una guía)",
              _n_op(data, oc_t2.id) == 0,
              _fila_op(data, oc_t2.id))

        # ═══ 9 · Paridad con el selector viejo, dataset completo ═══
        # M: guía multi-ítem a medio facturar
        cot_m = Cotizacion(numero=f"{MARK}-COT-M", cliente=f"{MARK} HEPI",
                           rut_cliente="78.279.030-7")
        db.add(cot_m); db.flush()
        it_m1 = ItemCotizacion(cotizacion_id=cot_m.id, item_num=1, numero_parte="M-1",
                               descripcion="A", cantidad=2, estado_item="despachado")
        it_m2 = ItemCotizacion(cotizacion_id=cot_m.id, item_num=2, numero_parte="M-2",
                               descripcion="B", cantidad=3, estado_item="despachado")
        db.add_all([it_m1, it_m2]); db.flush()
        oc_m = OcCliente(cotizacion_id=cot_m.id, numero_oc=f"{MARK}-OC-M",
                         fecha_oc="2026-07-01")
        db.add(oc_m); db.flush()
        desp_m = Despacho(numero_despacho=f"{MARK}-DSP-M", oc_cliente_id=oc_m.id,
                          estado="despachado", guia_firmada=1, numero_guia="G-M")
        db.add(desp_m); db.flush()
        di_m1 = DespachoItem(despacho_id=desp_m.id, item_cotizacion_id=it_m1.id,
                             qty_despachada=2)
        di_m2 = DespachoItem(despacho_id=desp_m.id, item_cotizacion_id=it_m2.id,
                             qty_despachada=3)
        db.add_all([di_m1, di_m2]); db.flush()
        PRECIOS[it_m1.id] = 1000.0; PRECIOS[it_m2.id] = 1000.0
        f_m = ContFacturaCliente(oc_cliente_id=oc_m.id, cotizacion_id=cot_m.id,
                                 empresa="mineria", numero_factura=f"{MARK}-FM1",
                                 monto_neto=2000, iva=380, monto_bruto=2380, saldo=2380)
        db.add(f_m); db.flush()
        db.add(ContFacturaClienteItem(factura_id=f_m.id, item_cotizacion_id=it_m1.id,
                                      despacho_item_id=di_m1.id, cantidad=2,
                                      total_neto=2000))
        db.commit()
        # FN: factura con línea de DESCUENTO negativa SIN despacho_item_id (no resta)
        cot_fn, oc_fn, desp_fn, it_fn = _crear_venta(db, cantidad=4, sufijo="FN")
        f_fn = ContFacturaCliente(oc_cliente_id=oc_fn.id, cotizacion_id=cot_fn.id,
                                  empresa="mineria", numero_factura=f"{MARK}-FFN",
                                  monto_neto=-5000, iva=-950, monto_bruto=-5950, saldo=0)
        db.add(f_fn); db.flush()
        db.add(ContFacturaClienteItem(factura_id=f_fn.id, item_cotizacion_id=None,
                                      despacho_item_id=None, cantidad=-1,
                                      total_neto=-5000))
        db.commit()
        # FM: factura MANUAL por items (sin guía: despacho_item_id NULL). FALSO POSITIVO
        # ACEPTADO y documentado: la guía SIGUE listada como facturable porque el saldo
        # por guía solo descuenta líneas ancladas a un despacho_item — misma semántica
        # que el selector actual (paridad primero); el preview de emisión es quien
        # bloquea la doble facturación real.
        cot_fm, oc_fm, desp_fm, it_fm = _crear_venta(db, cantidad=3, sufijo="FM")
        f_fm = ContFacturaCliente(oc_cliente_id=oc_fm.id, cotizacion_id=cot_fm.id,
                                  empresa="mineria", numero_factura=f"{MARK}-FFM",
                                  monto_neto=30000, iva=5700, monto_bruto=35700,
                                  saldo=35700)
        db.add(f_fm); db.flush()
        db.add(ContFacturaClienteItem(factura_id=f_fm.id, item_cotizacion_id=it_fm.id,
                                      despacho_item_id=None, cantidad=3,
                                      total_neto=30000))
        db.commit()
        data = _opciones()
        paridad_ocs = [oc_a, oc_p1, oc_p2, oc_n, oc_z, oc_c1, oc_c2, oc_c3,
                       oc_t1, oc_t2, oc_m, oc_fn, oc_fm]
        malos = []
        for oc in paridad_ocs:
            n_op = _n_op(data, oc.id)
            n_sel = len(_selector(oc.id))
            if n_op != n_sel:
                malos.append((oc.numero_oc, n_op, n_sel))
        check("9 paridad: n de /opciones == len(despachos-facturables) en TODO el set",
              not malos, malos)
        sel_m = _selector(oc_m.id)
        check("9 multi-ítem a medio facturar: 1 guía, items_count 2, facturable 3",
              len(sel_m) == 1 and sel_m[0]["items_count"] == 2
              and abs(sel_m[0]["facturable"] - 3.0) < 1e-9, sel_m)
        check("9 contrato del selector: las 8 claves históricas INTACTAS + las 2 "
              "aditivas (fecha, fuente); sin DTE ni papel, fuente None",
              sel_m and set(sel_m[0].keys()) == SELECTOR_KEYS
              and sel_m[0]["id"] == desp_m.id
              and sel_m[0]["numero_despacho"] == f"{MARK}-DSP-M"
              and sel_m[0]["numero_guia"] == "G-M"
              and sel_m[0]["numero_expedicion"] is None
              and sel_m[0]["fecha_guia"] is None
              and sel_m[0]["guia_firmada_archivo"] is None
              and sel_m[0]["fecha"] is None and sel_m[0]["fuente"] is None,
              sel_m)
        check("9 línea negativa sin despacho_item_id NO resta (la guía FN sigue entera)",
              _n_op(data, oc_fn.id) == 1
              and abs(_selector(oc_fn.id)[0]["facturable"] - 4.0) < 1e-9,
              _selector(oc_fn.id))
        check("9 factura manual por items sin guía: falso positivo aceptado (guía listada)",
              _n_op(data, oc_fm.id) == 1,
              _fila_op(data, oc_fm.id))

        # ═══ 10 · Cascada de fecha de la guía ═══
        # F1: DTE 52 EMITIDO REAL (status 3 + folio) Y fecha papel → gana documentDate
        _, oc_f1, desp_f1, _ = _crear_venta(db, fecha_guia=date(2026, 7, 1), sufijo="F1")
        db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp_f1.id,
                          status_id=STATUS_EMITIDO, folio="777001",
                          payload_json=json.dumps({"documentDate": "2026-07-20"})))
        # F2: status 3 SIN folio (estado contradictorio conocido) → NO es electrónica
        # NI papel: la emisión de la factura rechaza ese estado con 409
        # (_guia_no_referenciable) → fuente='bloqueada', con la fecha papel de aviso.
        _, oc_f2, desp_f2, _ = _crear_venta(db, fecha_guia=date(2026, 7, 2), sufijo="F2")
        db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp_f2.id,
                          status_id=STATUS_EMITIDO, folio=None,
                          payload_json=json.dumps({"documentDate": "2026-07-21"})))
        # F3: ni DTE ni fecha papel, pero con fecha_despacho puesta (el señuelo que
        # JAMÁS debe usarse: es el reloj del server, no la emisión de la guía)
        _, oc_f3, desp_f3, _ = _crear_venta(db, sufijo="F3")
        db.query(Despacho).filter(Despacho.id == desp_f3.id).update(
            {"fecha_despacho": datetime(2026, 8, 25, 23, 30)})
        # F4: contradictorio SIN fecha papel → 'bloqueada' con fecha None (no hay
        # nada informativo que mostrar, pero la fuente igual avisa el bloqueo)
        _, oc_f4, desp_f4, _ = _crear_venta(db, sufijo="F4")
        db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp_f4.id,
                          status_id=STATUS_EMITIDO, folio=None, payload_json=None))
        # F5: DTE emitido REAL con payload_json='null' — JSON válido que NO es
        # objeto: sin el guard isinstance, el .get moría con AttributeError FUERA
        # del except y tumbaba el endpoint entero con 500 por una fila sucia.
        _, oc_f5, desp_f5, _ = _crear_venta(db, sufijo="F5")
        db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp_f5.id,
                          status_id=STATUS_EMITIDO, folio="777005",
                          payload_json="null"))
        db.commit()
        data = _opciones()
        g1 = _guia1(data, oc_f1.id)
        g2 = _guia1(data, oc_f2.id)
        g3 = _guia1(data, oc_f3.id)
        check("10 DTE emitido + papel: gana documentDate ('2026-07-20', electronica)",
              g1.get("fecha") == "2026-07-20" and g1.get("fuente") == "electronica", g1)
        check("10 status 3 SIN folio con papel: fuente 'bloqueada' y fecha papel "
              "informativa ('2026-07-02') — ya no se disfraza de papel facturable",
              g2.get("fecha") == "2026-07-02" and g2.get("fuente") == "bloqueada", g2)
        check("10 la guía bloqueada SIGUE contando en n == 1 (paridad con el "
              "selector, que también la lista: el aviso es honestidad, no filtro)",
              _n_op(data, oc_f2.id) == 1, _fila_op(data, oc_f2.id))
        check("10 sin DTE ni papel: fecha None/None (JAMÁS fecha_despacho del server)",
              g3 != {} and g3.get("fecha") is None and g3.get("fuente") is None, g3)
        g4 = _guia1(data, oc_f4.id)
        check("10 contradictorio SIN papel: fecha None y fuente 'bloqueada'",
              g4 != {} and g4.get("fecha") is None and g4.get("fuente") == "bloqueada",
              g4)
        g5 = _guia1(data, oc_f5.id)
        check("10 payload_json='null' (JSON válido no-objeto): el endpoint responde "
              "200 y la fila degrada a fecha None/'electronica' (guard por fila)",
              g5 != {} and g5.get("fecha") is None and g5.get("fuente") == "electronica",
              g5)
        # PODER DISCRIMINANTE del check de F1 (verificado a mano; ya no se muta el
        # fuente): F1 tiene DTE emitido REAL **y** fecha de papel, así que si alguien
        # invierte el orden de la cascada (papel primero) esta misma aserción ve
        # 'papel'/'2026-07-01' y cae. Es el único fixture con las dos fechas a la vez:
        # por eso vive acá y no en la sección 0.

        # ═══ 10-bis · TODOS los estados con los que la emisión rechaza la referencia
        # 52 salen 'bloqueada' — y la equivalencia se afirma contra la FUNCIÓN DEL
        # EMISOR, no contra una copia ═══
        # El bug que esto cierra: /ventas/opciones re-derivaba el criterio con
        # `status == EMITIDO and folio vacío` y reproducía UNO de los cuatro estados
        # que bloquean; los otros tres salían pintados como guía en papel facturable
        # (chip verde «lista para facturar») y el operador chocaba con el 409 recién
        # al emitir, con el formulario lleno. Todos estos fixtures llevan fecha de
        # papel a propósito: con el criterio viejo salían 'papel' + fecha limpia.
        from wasabil_dte.router import _guia_no_referenciable as _motivo_guia  # noqa: E402
        ahora = datetime.utcnow()  # el claim usa UTC naive (datetime.utcnow), como el emisor
        casos_dte = []  # (rótulo, oc, desp, espera_bloqueada)

        def _sembrar(sufijo, rotulo, espera_bloqueada, **cols):
            _, oc_b, desp_b, _ = _crear_venta(db, fecha_guia=date(2026, 7, 10),
                                              sufijo=sufijo)
            db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp_b.id,
                              **cols))
            casos_dte.append((rotulo, oc_b, desp_b, espera_bloqueada))
            return oc_b, desp_b

        _sembrar("B1", "procesando (status 2 con uuid)", True,
                 status_id=2, uuid="uuid-B1")
        _sembrar("B2", "borrador (status 6 con uuid)", True,
                 status_id=6, uuid="uuid-B2")
        _sembrar("B3", "status NULL con uuid", True, status_id=None, uuid="uuid-B3")
        _sembrar("B4", "claim de emisión VIGENTE (en vuelo recién)", True,
                 status_id=6, uuid=None, en_vuelo_desde=ahora)
        _sembrar("B5", "AMBIGUA (uuid NULL + en_vuelo vencido = timeout)", True,
                 status_id=4, uuid=None, en_vuelo_desde=ahora - timedelta(hours=2))
        oc_b6, _desp_b6 = _sembrar(
            "B6", "rechazo CONFIRMADO del SII (status 4 con uuid)", False,
            status_id=4, uuid="uuid-B6")
        db.commit()
        data = _opciones()
        malos_bloq = [(rot, _guia1(data, oc.id).get("fuente"))
                      for rot, oc, _d, esp in casos_dte
                      if (_guia1(data, oc.id).get("fuente") == "bloqueada") != esp]
        check("10-bis los 5 estados que la emisión RECHAZA salen 'bloqueada' (antes "
              "sólo salía el contradictorio; los otros 4 se ofrecían como papel)",
              not malos_bloq, malos_bloq)
        # EQUIVALENCIA con el emisor, sobre TODOS los fixtures con fila DTE: la sonda
        # prueba que las dos pantallas cuelgan del MISMO criterio, no cada mitad por
        # separado (si se probaran por separado, podrían volver a divergir en silencio).
        todos_dte = ([(rot, oc, d) for rot, oc, d, _e in casos_dte]
                     + [("F1 emitida real", oc_f1, desp_f1),
                        ("F2 emitida sin folio", oc_f2, desp_f2),
                        ("F4 emitida sin folio sin papel", oc_f4, desp_f4),
                        ("F5 emitida real payload sucio", oc_f5, desp_f5)])
        desalineados = []
        for rot, oc, desp in todos_dte:
            bloq_api = _guia1(data, oc.id).get("fuente") == "bloqueada"
            bloq_emisor = _motivo_guia(db, desp.id) is not None
            if bloq_api != bloq_emisor:
                desalineados.append((rot, bloq_api, bloq_emisor))
        check("10-bis EQUIVALENCIA: fuente=='bloqueada' ⇔ _guia_no_referenciable "
              "bloquea (misma función del emisor, no una copia del criterio)",
              not desalineados, desalineados)
        # Decisión DELIBERADA (no es un olvido): el rechazo CONFIRMADO no se pinta
        # 'bloqueada'. El emisor tampoco lo bloquea por columnas — su umbral extra
        # (_problema_52_de_papel) CONSULTA a Wasabil, y este endpoint es a propósito
        # sin red. Marcar todo rechazo confirmado encendería el aviso también donde la
        # guía de papel sí se factura legítimamente, y un aviso que grita lobo entrena
        # al operador a ignorarlo. Quien bloquea de verdad sigue siendo la emisión.
        g_b6 = _guia1(data, oc_b6.id)
        check("10-bis rechazo confirmado (status 4 con uuid): sigue siendo 'papel' con "
              "su fecha — decisión deliberada, el guard con red vive en la emisión",
              g_b6.get("fuente") == "papel" and g_b6.get("fecha") == "2026-07-10", g_b6)
        check("10-bis las guías bloqueadas SIGUEN contando en n (aviso, no filtro): "
              "paridad con el selector, que también las lista",
              all(_n_op(data, oc.id) == 1 for _r, oc, _d, _e in casos_dte),
              [(r, _n_op(data, oc.id)) for r, oc, _d, _e in casos_dte])

        # ═══ 10-ter · El SELECTOR publica la MISMA fecha/fuente que /opciones ═══
        # H4: la fila del selector pintaba fecha_guia (columna de la guía en PAPEL, que
        # la emisión al SII nunca escribe), así que TODA guía electrónica salía
        # «(sin fecha ⚠)» mientras el chip de la misma pantalla, alimentado por
        # /opciones, mostraba la fecha correcta. Fixture con poder discriminante:
        # DTE 52 emitido REAL y fecha_guia NULL — con el contrato viejo la fila no
        # tenía NINGUNA fecha que mostrar.
        _, oc_se, desp_se, _ = _crear_venta(db, sufijo="SE")   # sin fecha_guia (papel)
        db.add(WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp_se.id,
                          status_id=STATUS_EMITIDO, folio="777900",
                          payload_json=json.dumps({"documentDate": "2026-08-05"})))
        db.commit()
        sel_se = _selector(oc_se.id)
        check("10-ter selector con 52 emitida y SIN fecha de papel: fecha == "
              "documentDate y fuente 'electronica' (fecha_guia sigue None: el campo "
              "histórico no cambia, pero ya no es el que la pantalla debe pintar)",
              len(sel_se) == 1 and sel_se[0]["fecha"] == "2026-08-05"
              and sel_se[0]["fuente"] == "electronica"
              and sel_se[0]["fecha_guia"] is None, sel_se)
        data = _opciones()
        discrepan = []
        for _rot, oc, _d in todos_dte + [("SE", oc_se, desp_se)]:
            g_op = _guia1(data, oc.id)
            g_sel = (_selector(oc.id) or [{}])[0]
            if (g_op.get("fecha"), g_op.get("fuente")) != (g_sel.get("fecha"),
                                                           g_sel.get("fuente")):
                discrepan.append((oc.numero_oc, g_op.get("fecha"), g_op.get("fuente"),
                                  g_sel.get("fecha"), g_sel.get("fuente")))
        check("10-ter chip (/opciones) y fila (selector) dicen lo MISMO de cada guía "
              "— mismo helper, imposible que se contradigan a 40 píxeles",
              not discrepan, discrepan)

        # ═══ 10-quater · Orden del selector: sin fecha PRIMERO, luego fecha ASC ═══
        # Con 3 guías pendientes el operador necesita orden estable para decidir cuál
        # facturar primero (reloj tributario del art. 55); y la que no se puede
        # facturar al SII por falta de fecha encabeza, en vez de perderse al final.
        cot_or = Cotizacion(numero=f"{MARK}-COT-OR", cliente=f"{MARK} HEPI",
                            rut_cliente="78.279.030-7")
        db.add(cot_or); db.flush()
        it_or = ItemCotizacion(cotizacion_id=cot_or.id, item_num=1, numero_parte="OR-1",
                               descripcion="A", cantidad=9, estado_item="despachado")
        db.add(it_or); db.flush()
        oc_or = OcCliente(cotizacion_id=cot_or.id, numero_oc=f"{MARK}-OC-OR",
                          fecha_oc="2026-07-01")
        db.add(oc_or); db.flush()
        PRECIOS[it_or.id] = 1000.0
        for etiq, fg in (("SINF", None), ("TARDE", date(2026, 7, 25)),
                         ("TEMPRANO", date(2026, 7, 3))):
            d = Despacho(numero_despacho=f"{MARK}-DSP-OR-{etiq}", oc_cliente_id=oc_or.id,
                         estado="despachado", guia_firmada=1,
                         numero_guia=f"G-OR-{etiq}", fecha_guia=fg)
            db.add(d); db.flush()
            db.add(DespachoItem(despacho_id=d.id, item_cotizacion_id=it_or.id,
                                qty_despachada=3))
        db.commit()
        sel_or = _selector(oc_or.id)
        check("10-quater selector ordenado: sin fecha primero, después ASC",
              [g["fecha"] for g in sel_or] == [None, "2026-07-03", "2026-07-25"],
              [(g["numero_despacho"], g["fecha"]) for g in sel_or])

        # ═══ 10-quinquies · CONTRATO del adaptador `_SesionDeUnaFilaDte` ═══
        # Lo que `_fecha_fuente_por_despachos` le entrega al predicado del EMISOR
        # (`wasabil_dte.router._guia_no_referenciable`) tiene que ser la fila ORM
        # COMPLETA, no una proyección de las 4 columnas que ese criterio mira HOY
        # (status_id · folio · uuid · en_vuelo_desde). El docstring del adaptador lo
        # promete («le sirve la fila COMPLETA … si mañana el predicado mira una columna
        # más, sigue decidiendo bien») y hasta acá NADA lo probaba: la alarma era
        # DIFERIDA — el gate pasaba verde y el 500 aparecía en PRODUCCIÓN recién el día
        # que el emisor leyera una 5ª columna. Esta sonda afirma el contrato AHORA.
        # PODER DISCRIMINANTE (razonado sobre la mutación): si alguien cambia el batch
        # de `_fecha_fuente_por_despachos` de `db.query(WasabilDte)` por
        #     db.query(WasabilDte.despacho_id, WasabilDte.status_id,
        #              WasabilDte.folio, WasabilDte.uuid)
        # lo que llega al predicado es un Row: `isinstance(fila, WasabilDte)` da False y
        # los atributos que el criterio no usa NO existen → CAE el primer check. El
        # espía DELEGA en la función real, así que no altera ningún veredicto (los
        # checks de 10-bis siguen midiendo lo mismo) y no agrega queries: el adaptador
        # es memoria pura, por eso vive fuera del presupuesto del check 16.
        import wasabil_dte.router as _wdte_router  # noqa: E402
        _orig_no_ref = _wdte_router._guia_no_referenciable
        _filas_al_predicado = []

        def _espia_no_referenciable(_db_ses, _despacho_id):
            # Se pide la fila EXACTAMENTE como la pide el emisor real, vía el adaptador.
            _filas_al_predicado.append(
                _db_ses.query(WasabilDte)
                .filter(WasabilDte.despacho_id == _despacho_id).first())
            return _orig_no_ref(_db_ses, _despacho_id)

        # `_fecha_fuente_por_despachos` importa el predicado DENTRO de la función (ciclo
        # de imports), así que resuelve el atributo del módulo en cada request: pisar el
        # atributo acá es suficiente para interceptarlo.
        _wdte_router._guia_no_referenciable = _espia_no_referenciable
        try:
            _opciones()
        finally:
            _wdte_router._guia_no_referenciable = _orig_no_ref
        # Atributos que el criterio de HOY no lee: son justo los que una proyección
        # dejaría fuera — y los que un predicado futuro podría empezar a mirar.
        NO_USADAS = ("id", "empresa", "factura_id", "pdf_url", "xml_url", "error",
                     "respuesta_json", "usuario_id", "created_at")
        con_fila = [f for f in _filas_al_predicado if f is not None]
        incompletas = [f for f in con_fila
                       if not isinstance(f, WasabilDte)
                       or not all(hasattr(f, a) for a in NO_USADAS)]
        check(f"10-quinquies el adaptador entrega la ENTIDAD ORM COMPLETA al predicado "
              f"del emisor —no una proyección de columnas—: expone hasta los atributos "
              f"que el criterio de hoy NO usa ({len(con_fila)} filas vistas)",
              # el piso de filas no es decoración: sin él, `not incompletas` pasaría
              # VACUAMENTE si el batch dejara de traer filas (hoy son 11: F1/F2/F4/F5,
              # B1..B6 y SE). 8 deja margen para que un fixture cambie sin falso rojo.
              len(con_fila) >= 8 and not incompletas,
              (len(con_fila), [type(f).__name__ for f in incompletas[:3]]))
        # Y con VALORES reales, no sólo atributos presentes: la fila que llega es la de
        # ESE despacho y trae columnas (empresa, tipo_dte) que ninguna de las 4 del
        # criterio incluye. Un tuple anónimo no pasa ninguna de las dos cosas.
        fila_f1 = next((f for f in con_fila
                        if isinstance(f, WasabilDte) and f.despacho_id == desp_f1.id),
                       None)
        check("10-quinquies la fila entregada es la de ESE despacho y trae sus columnas "
              "completas (empresa/tipo_dte/folio), no un tuple anónimo",
              fila_f1 is not None and fila_f1.empresa == "mineria"
              and fila_f1.tipo_dte == 52 and fila_f1.folio == "777001", fila_f1)

        # ═══ 11 · OC sin cotización / cotización sin ítems: fuera del listado ═══
        oc_sincot = OcCliente(cotizacion_id=None, numero_oc=f"{MARK}-OC-SINCOT",
                              fecha_oc="2026-07-01")
        cot_si = Cotizacion(numero=f"{MARK}-COT-SINITEMS", cliente=f"{MARK} VACIA",
                            rut_cliente="78.279.030-7")
        db.add_all([oc_sincot, cot_si]); db.flush()
        oc_si = OcCliente(cotizacion_id=cot_si.id, numero_oc=f"{MARK}-OC-SINITEMS",
                          fecha_oc="2026-07-01")
        db.add(oc_si); db.commit()
        data = _opciones()
        check("11 OC sin cotización y OC sin ítems: ninguna aparece (paridad /ventas)",
              _fila_op(data, oc_sincot.id) is None and _fila_op(data, oc_si.id) is None,
              [v["numero_oc"] for v in data["opciones"] if MARK in (v["numero_oc"] or "")])

        # ═══ 12 · Orden: fecha_venta DESC con los None AL FINAL ═══
        _, oc_o1, _, _ = _crear_venta(db, con_despacho=False, sufijo="O1")
        cot_o2, oc_o2, _, _ = _crear_venta(db, con_despacho=False, sufijo="O2")
        db.query(Cotizacion).filter(Cotizacion.id == cot_o2.id).update(
            {"created_at": None})
        db.commit()
        data = _opciones()
        fechas = [v["fecha_venta"] for v in data["opciones"]]
        con_fecha = [f for f in fechas if f is not None]
        primer_none = next((i for i, f in enumerate(fechas) if f is None), len(fechas))
        check("12 todos los None van AL FINAL (no replica el sort de listar_ventas)",
              all(f is None for f in fechas[primer_none:])
              and _fila_op(data, oc_o2.id) is not None
              and fechas.index(None) > [v["oc_cliente_id"] for v in data["opciones"]].index(oc_o1.id)
              if None in fechas else False,
              fechas[max(0, primer_none - 2):primer_none + 2])
        check("12 el tramo con fecha viene DESC",
              all(con_fecha[i] >= con_fecha[i + 1] for i in range(len(con_fecha) - 1)),
              con_fecha[:5])

        # ═══ 13 · Candado de empresa: automotriz → 403 ═══
        CURRENT["empresa"] = "automotriz"
        r = client.get("/api/contabilidad/ventas/opciones")
        check("13 empresa automotriz: 403 (candado mineria heredado del router)",
              r.status_code == 403, f"status={r.status_code}")
        CURRENT["empresa"] = "mineria"

        # ═══ 14 · listar_ventas: campos históricos + coherencia con /opciones ═══
        fila_p2 = _fila_listado(oc_p2.id)
        historicos = {"oc_cliente_id", "numero_oc", "numero_cotizacion", "cliente",
                      "rut_cliente", "fecha_oc", "fecha_venta", "cond_pago",
                      "total_items", "total_neto_clp", "iva_clp", "total_con_iva_clp",
                      "por_facturar_clp", "faltante_declarado", "adelantos"}
        check("14 listar_ventas conserva los campos históricos + gana guias_facturables_n",
              fila_p2 is not None and historicos.issubset(set(fila_p2.keys()))
              and "guias_facturables_n" in fila_p2,
              sorted(set(historicos) - set(fila_p2.keys())) if fila_p2 else None)
        check("14 faltante_declarado idéntico al pre-refactor con firma parcial (3−2=1)",
              fila_p2 is not None and fila_p2["faltante_declarado"] == 1.0, fila_p2)
        data = _opciones()
        desal = []
        for oc in (oc_a, oc_p1, oc_p2, oc_z, oc_m, oc_f1):
            fl = _fila_listado(oc.id)
            fo = _fila_op(data, oc.id)
            if not fl or not fo or fl["guias_facturables_n"] != fo["guias_facturables_n"]:
                desal.append((oc.numero_oc, fl and fl.get("guias_facturables_n"),
                              fo and fo.get("guias_facturables_n")))
        check("14 guias_facturables_n del listado coincide con /opciones", not desal, desal)

        # ═══ 15 · Facturar la guía → n baja en la misma corrida ═══
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc_a.id, "despacho_id": desp_a.id,
                              "numero_factura": f"{MARK}-FA"})
        check("15 emitir la factura de la guía A 200", r.status_code == 200, r.text)
        data = _opciones()
        check("15 tras facturar: la OC A pasa de n==1 a n==0 en la misma corrida",
              _n_op(data, oc_a.id) == 0
              and (_fila_op(data, oc_a.id) or {}).get("guias_facturables") == [],
              _fila_op(data, oc_a.id))

        # ═══ 15-bis · plazo_dias == 0 (CONTADO) genera vencimiento, no «sin fecha» ═══
        # El 0 llega de verdad desde la pantalla: el schema lo admite EXPLÍCITAMENTE
        # (FacturaCreate.plazo_dias = Field(None, ge=0, le=3650)) — no es un borde
        # inalcanzable, es la venta al contado.
        # PODER DISCRIMINANTE (razonado sobre la mutación): si alguien cambia en
        # `_persistir_factura` el
        #     if payload.plazo_dias is not None   →   if payload.plazo_dias
        # el 0 es FALSY, `fecha_venc` queda None y la factura al contado nace SIN
        # vencimiento: `_semaforo` la clasifica 'sin_fecha' (no vence nunca, nunca cae en
        # la cobranza vencida) → CAEN los dos checks de abajo. El fixture emite 10 días
        # ATRÁS a propósito: con vencimiento la factura sale 'vencida'; sin él, 'sin_fecha'
        # — dos valores distintos, no un empate por casualidad. La fecha es relativa al
        # hoy real (−10 días corridos, sin días hábiles de por medio: no explota el finde).
        cot_pz, oc_pz, desp_pz, it_pz = _crear_venta(db, cantidad=2, sufijo="PZ")
        f_emi = (cont._hoy_chile() - timedelta(days=10)).isoformat()
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc_pz.id, "despacho_id": desp_pz.id,
                              "numero_factura": f"{MARK}-FPZ", "plazo_dias": 0,
                              "fecha_emision": f_emi})
        check("15-bis emitir factura al CONTADO (plazo_dias == 0) 200",
              r.status_code == 200, r.text)
        fpz = r.json() if r.status_code == 200 else {}
        check("15-bis plazo 0: fecha_vencimiento == fecha_emision (NO null) y el 0 queda "
              "persistido — el contado vence el mismo día, no «nunca»",
              fpz.get("fecha_vencimiento") == f_emi and fpz.get("plazo_dias") == 0,
              {k: fpz.get(k) for k in ("plazo_dias", "fecha_emision",
                                       "fecha_vencimiento")})
        check("15-bis plazo 0: el semáforo dice 'vencida' (tiene fecha y ya pasó), "
              "JAMÁS 'sin_fecha' — que es como saldría la factura sin vencimiento",
              fpz.get("semaforo") == "vencida", fpz.get("semaforo"))

        # ═══ 16 · Presupuesto de queries: ≤ 7 con ~20 OCs marcadas ═══
        for k in range(1, 7):
            _crear_venta(db, cantidad=2, sufijo=f"L{k:02d}")
        db.rollback()
        n_mark = db.query(OcCliente).filter(OcCliente.numero_oc.like(f"{MARK}%")).count()
        check(f"16 dataset: ≥ 20 OCs marcadas en la base ({n_mark})", n_mark >= 20, n_mark)
        stmts = []

        def _cap(conn, cursor, statement, parameters, context, executemany):
            stmts.append(statement)

        event.listen(engine, "before_cursor_execute", _cap)
        try:
            r = client.get("/api/contabilidad/ventas/opciones")
        finally:
            event.remove(engine, "before_cursor_execute", _cap)
        n_sel = sum(1 for s in stmts if s.strip().upper().startswith("SELECT"))
        check(f"16 /opciones ≤ 7 queries con ~20 OCs (medido: {n_sel})",
              r.status_code == 200 and 0 < n_sel <= 7,
              [s[:70] for s in stmts])

        # ═══ 16-bis · listar_ventas paga el IN de guías firmadas UNA sola vez ═══
        # El presupuesto de GET /ventas BAJÓ en 1: faltante declarado y guías
        # facturables disparaban CADA UNO su copia del mismo IN
        # (_lineas_guias_firmadas_por_ocs); ahora listar_ventas obtiene las filas
        # una vez y las pasa a ambos derivadores. Se pinza contando las SELECT que
        # tocan guia_firmada — en /ventas SOLO esa query referencia la columna —,
        # así el check cae a 2 si alguien reintroduce la doble llamada, sin la
        # fragilidad de un presupuesto total. (/opciones no cambia: allí el
        # derivador corre una sola vez desde siempre — check 16 intacto.)
        stmts_v = []

        def _cap_v(conn, cursor, statement, parameters, context, executemany):
            stmts_v.append(statement)

        event.listen(engine, "before_cursor_execute", _cap_v)
        try:
            r = client.get("/api/contabilidad/ventas")
        finally:
            event.remove(engine, "before_cursor_execute", _cap_v)
        n_guias_q = sum(1 for s in stmts_v
                        if s.strip().upper().startswith("SELECT") and "guia_firmada" in s)
        check(f"16-bis GET /ventas: la query de guías firmadas corre UNA vez, no dos "
              f"(medido: {n_guias_q})",
              r.status_code == 200 and n_guias_q == 1,
              [s[:70] for s in stmts_v if "guia_firmada" in s])

    finally:
        # Nada que restaurar en disco: esta suite YA NO escribe en el fuente (ver el
        # docstring). Lo único con estado global es el monkeypatch del motor de precios,
        # que se repone SIEMPRE — si la corrida muere aquí, el proceso se va con él.
        cont._precios_de_cotizacion = _orig_precios
        _limpiar(db)
        db.close()

    print()
    if _fails:
        print(f"FALLARON {len(_fails)}: {_fails}")
        sys.exit(1)
    print("TODOS LOS CHECKS OK")


def test_ventas_opciones():
    run()
    assert not _fails, _fails


if __name__ == "__main__":
    run()
