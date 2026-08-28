"""«Listo para despachar» a nivel de LISTADO (Grupo AM / MachParts) — suite de contrato.

LO QUE ESTA SUITE PROTEGE (helper compartido _disponibles_por_item + panel nuevo):
  S1  GET /despachos/listo-para-despachar responde 200 con la app montada — JAMÁS
      422: pinza el ORDEN de registro de rutas (declarado después de
      GET /{despacho_id}, el conversor a int capturaría el literal y el panel
      moriría con 422 permanente).
  S2  CENTINELA DE PERTENENCIA: un ítem en_bodega SIN registro de recepción
      (flujo antiguo) tiene disponible == cantidad vendida en LAS TRES vistas —
      detalle de OC, listado (unidades_despachables) y panel. _tope_fisico decide
      por `item.id in recibidos`; cambiarlo por `.get(id, 0)` colapsa el
      histórico a 0 y caen las tres sondas.
  S3  PARIDAD DE LOS 6 CONSUMIDORES: con la misma semilla (recepción parcial +
      despacho abierto), qty_disponible del detalle == panel == listado ==
      BUSCADOR DE BODEGA == tope REAL del guard de creación (POST disponible+1 →
      400 «excede»; POST exacto → 200). Si una mutación del helper hace caer solo
      una vista, quedó una copia. El buscador de Bodega entró acá porque su suite
      propia (tests_contabilidad/test_buscador_bodega_despachos_ga) afirma el
      disponible sobre una línea SIN despachos encima: cubre el tope físico pero
      nunca la rama del descuento.
  S4  Cupo comido por despacho ABIERTO (en_preparacion): la OC sigue en la
      pestaña 'listas' pero con items_despachables == 0 y unidades == 0, y está
      AUSENTE del panel; al ANULAR el despacho reaparece con su cupo entero.
  S5  Recepción PARCIAL (recibido < vendido): disponible topeado por lo recibido
      también en el listado y en el panel (no solo en el detalle, que ya lo
      protegía test_bodega_despachos_flujo).
  S6  El ORDEN del panel es el MISMO de la pestaña 'listas' (urgencia asc, sin
      fecha al final, id DESC de desempate).
  S7  En en_curso/historial los campos nuevos NO viajan (allí no se calculan:
      serían queries muertas).
  C   /counts: campos ADITIVOS ocs_con_disponible / unidades_despachables
      coherentes con el panel (mismo universo, misma fórmula) y los campos
      viejos (líneas en_bodega SIN capar) conservados para sus consumidores.
  E1  Residuo flotante de la resta del cupo (disponible ínfimo ≤ _TOL_QTY) NO es
      cupo visible: fuera del panel, insignia 0 en 'listas' y counts sin contarlo
      (_es_despachable, la misma tolerancia 0.001 del guard de crear).
  E2  …y tampoco en el DETALLE (la pantalla de picking) ni en el guard: el
      residuo se sirve como 0 —la tolerancia vive DENTRO de la fórmula— y un
      payload de 7e-9 se rechaza con mensaje propio en vez de crear un despacho
      ZOMBI que la guía SII no puede emitir.
  M   motivo_sin_cupo: cuando unidades_despachables == 0 la card dice POR QUÉ,
      derivado en el backend — 'en_preparacion' (despacho abierto: el único caso
      que autoriza «ciérralos o anúlalos»), 'sin_stock' (llegó menos de lo
      vendido: reclamo al proveedor, nada que anular) o 'despachado' (se lo
      comieron despachos ya CERRADOS, que NO se anulan). Con cupo > 0 va None y
      en en_curso/historial no viaja.
  T   El tope de la vía SII gratuito viaja en el detalle de OC
      (max_lineas_sii_gratuito, leído de wasabil_dte.service): el picking puede
      avisar ANTES de crear el despacho, que es cuando dividir todavía es gratis.

MUTACIONES verificadas al construir la suite (mutar → rojo → restaurar → verde,
sha256 idéntico; evidencia en el reporte del encargo):
  M1 la pertenencia de _tope_fisico (`item.id in recibidos`) vuelta `.get(id, 0)`
     → caen las TRES vistas de S2 (detalle, listado, panel).
  M2 _disponibles_por_item sin el descuento de qty_already → caen JUNTOS detalle,
     panel y guard de creación (S3): la fórmula tiene una sola casa.

Datos MARCADOS con TLISTO (sin guiones bajos: en LIKE el _ es comodín) y limpieza
FK-safe verificada con SESIÓN NUEVA. Requiere la BD local (igual que las demás
suites GA).

Corre con:  ./venv/bin/python -m pytest routers/tests/test_listo_para_despachar.py -q
(también:   ./venv/bin/python routers/tests/test_listo_para_despachar.py)
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    Embarque, EmbarqueItem, RecepcionEmbarque, RecepcionEmbarqueItem,
    Notificacion, User,
)
# PIN del módulo raíz `notificaciones` (mismo patrón que test_bulto_despacho.py):
# con routers/ en sys.path, el ROUTER homónimo routers/notificaciones.py puede sombrear
# al módulo raíz que trae crear_notificacion. Importarlo acá, con backend/ al frente
# del path, deja la resolución correcta cacheada en sys.modules.
import notificaciones as _notif_raiz  # noqa: E402
assert hasattr(_notif_raiz, "crear_notificacion"), (
    f"el módulo notificaciones resolvió a {_notif_raiz.__file__} (sombra del router)")

from routers.despachos import router as despachos_router  # noqa: E402
# 6º consumidor de la fórmula única del cupo: el buscador de picking de Bodega
# (routers/bodega.py:buscar_items delega en _disponibles_por_item). Se monta acá
# para que la sonda de paridad S3 lo incluya — sin él, reponer la copia inline
# sin el descuento por despachos no ponía NADA en rojo en ninguna suite.
from routers.bodega import router as bodega_router  # noqa: E402
# Para la pinza real del «hoy» del panel (s1c): el MISMO helper que usa el
# endpoint — comparar contra otro reloj sería probar dos relojes, no el contrato.
from wasabil_dte.service import hoy_chile, MAX_LINEAS_SII_GRATUITO  # noqa: E402

MARK = "TLISTO"  # SIN guiones bajos: en LIKE el _ es comodín
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(despachos_router, prefix="/api")
# El candado require_empresa("mineria") de bodega pasa con el override de auth
# (sirve empresa="mineria"), igual que el del router de despachos.
app.include_router(bodega_router, prefix="/api")


# Auth REALISTA (patrón test_bulto_despacho.py): una lectura en la MISMA sesión
# del request, igual que en producción.
def _current_user_realista(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _current_user_realista
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Fábricas de datos MARCADOS ────────────────────────────────────────────────────

def _venta(db, sufijo, qty=10.0, fecha_entrega=None):
    """Cotización + OC + 1 ítem EN BODEGA. Sin recepción por defecto: el tope
    físico no acota ítems sin registro (centinela de pertenencia)."""
    cot = Cotizacion(numero=f"{MARK}-{sufijo}", cliente=f"{MARK} Cliente",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1,
                        numero_parte=f"{MARK}-P{sufijo}",
                        descripcion=f"{MARK} pieza {sufijo}", cantidad=qty,
                        estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}",
                   fecha_oc="2026-08-01", fecha_entrega=fecha_entrega)
    db.add(oc); db.flush()
    db.commit()
    for obj in (cot, it, oc):
        db.refresh(obj)
    return SimpleNamespace(cot_id=cot.id, item_id=it.id, oc_id=oc.id, qty=qty)


def _recepcion_cerrada(db, item_id, qty, sufijo, estado_recepcion="completo"):
    """Recepción de embarque CERRADA y utilizable directo en BD: el ciclo completo
    de bodega tiene su propia suite (test_bodega_despachos_flujo); aquí solo
    interesa que _qty_recibida_utilizable vea el tope.

    `estado_recepcion='faltante'`: llegaron `qty` unidades buenas y el resto queda
    en reclamo al proveedor. La línea NO se voltea a 'despachado' aunque se
    despache todo lo recibido — es el escenario del motivo 'sin_stock'."""
    emb = Embarque(numero=f"{MARK}-EMB-{sufijo}", estado="recibido")
    db.add(emb); db.flush()
    ei = EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=item_id)
    db.add(ei); db.flush()
    rec = RecepcionEmbarque(embarque_id=emb.id, estado="cerrada")
    db.add(rec); db.flush()
    db.add(RecepcionEmbarqueItem(recepcion_id=rec.id, embarque_item_id=ei.id,
                                 qty_recibida=qty, estado_recepcion=estado_recepcion))
    db.commit()


def _crear_despacho(oc_id, item_id, qty):
    """POST real de creación (observaciones marcadas: la fila queda rastreable
    por sí sola para la verificación de limpieza)."""
    return client.post("/api/despachos/", json={
        "oc_cliente_id": oc_id, "observaciones": f"{MARK} obs",
        "items": [{"item_cotizacion_id": item_id, "qty_despachada": qty}],
    })


# ── Lectores de las tres vistas ──────────────────────────────────────────────────

def _detalle_item(oc_id, item_id):
    r = client.get(f"/api/despachos/oc-clientes/{oc_id}")
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["items"] if x["id"] == item_id)


def _cards_listas():
    """Cards de la pestaña 'listas' filtradas al MARK (lista ordenada + índice)."""
    r = client.get("/api/despachos/oc-clientes",
                   params={"tab": "listas", "q": MARK, "page_size": 200})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    return items, {c["id"]: c for c in items}


def _panel():
    r = client.get("/api/despachos/listo-para-despachar")
    assert r.status_code == 200, r.text
    return r.json()


def _grupo_panel(oc_id):
    return next((g for g in _panel()["grupos"] if g["oc_cliente_id"] == oc_id), None)


def _bodega_item(item_id):
    """Fila del BUSCADOR DE PICKING de Bodega (6º consumidor de la fórmula)."""
    r = client.get("/api/bodega/items", params={"q": MARK, "page_size": 200})
    assert r.status_code == 200, r.text
    return next((x for x in r.json()["items"]
                 if x["item_cotizacion_id"] == item_id), None)


# ── Limpieza ─────────────────────────────────────────────────────────────────────

def _limpiar():
    db = SessionLocal()
    try:
        db.rollback()
        cot_ids = [c.id for c in db.query(Cotizacion)
                   .filter(Cotizacion.numero.like(f"{MARK}%")).all()]
        item_ids = [i.id for i in db.query(ItemCotizacion)
                    .filter(ItemCotizacion.cotizacion_id.in_(cot_ids or [0])).all()]
        oc_ids = [o.id for o in db.query(OcCliente)
                  .filter(OcCliente.cotizacion_id.in_(cot_ids or [0])).all()]
        if oc_ids:
            desp_ids = [d.id for d in db.query(Despacho)
                        .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
            if desp_ids:
                db.query(Notificacion).filter(
                    Notificacion.entidad_tipo == "despacho",
                    Notificacion.entidad_id.in_(desp_ids)).delete(synchronize_session=False)
                db.query(DespachoItem).filter(
                    DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
                db.query(Despacho).filter(
                    Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        # Embarques/recepciones del MARK (venta B y rezagos si un fallo dejó el
        # escenario a medias): hijos → padres, FK-safe.
        emb_ids = [e.id for e in db.query(Embarque)
                   .filter(Embarque.numero.like(f"{MARK}%")).all()]
        if emb_ids or item_ids:
            ei_q = db.query(EmbarqueItem)
            if emb_ids and item_ids:
                ei_q = ei_q.filter((EmbarqueItem.embarque_id.in_(emb_ids))
                                   | (EmbarqueItem.item_cotizacion_id.in_(item_ids)))
            elif emb_ids:
                ei_q = ei_q.filter(EmbarqueItem.embarque_id.in_(emb_ids))
            else:
                ei_q = ei_q.filter(EmbarqueItem.item_cotizacion_id.in_(item_ids))
            ei_ids = [e.id for e in ei_q.all()]
            if ei_ids:
                db.query(RecepcionEmbarqueItem).filter(
                    RecepcionEmbarqueItem.embarque_item_id.in_(ei_ids)).delete(synchronize_session=False)
            if emb_ids:
                rec_ids = [r.id for r in db.query(RecepcionEmbarque)
                           .filter(RecepcionEmbarque.embarque_id.in_(emb_ids)).all()]
                if rec_ids:
                    db.query(RecepcionEmbarqueItem).filter(
                        RecepcionEmbarqueItem.recepcion_id.in_(rec_ids)).delete(synchronize_session=False)
                    db.query(RecepcionEmbarque).filter(
                        RecepcionEmbarque.id.in_(rec_ids)).delete(synchronize_session=False)
            if ei_ids:
                db.query(EmbarqueItem).filter(
                    EmbarqueItem.id.in_(ei_ids)).delete(synchronize_session=False)
            if emb_ids:
                db.query(Embarque).filter(Embarque.id.in_(emb_ids)).delete(synchronize_session=False)
        if oc_ids:
            db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
        if cot_ids:
            db.query(ItemCotizacion).filter(
                ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.email.like(f"{MARK}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (
            db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
            + db.query(OcCliente).filter(OcCliente.numero_oc.like(f"{MARK}%")).count()
            # Los despachos llevan observaciones marcadas: contables por sí solos
            # aunque su numero_despacho sea el correlativo DSP genérico.
            + db.query(Despacho).filter(Despacho.observaciones.like(f"{MARK}%")).count()
            + db.query(Embarque).filter(Embarque.numero.like(f"{MARK}%")).count()
            + db.query(User).filter(User.email.like(f"{MARK}%")).count()
        )
        assert restos == 0, f"limpieza incompleta: quedan {restos} filas {MARK}"
        print("Limpieza verificada con sesión nueva: 0 restos")
    finally:
        db.close()


# ── Suite ────────────────────────────────────────────────────────────────────────

def run():
    _limpiar()
    db = SessionLocal()
    try:
        CURRENT["empresa"] = "mineria"
        u = User(email=f"{MARK}@test.cl", nombre=f"{MARK} user",
                 hashed_password="x", empresa="mineria")
        db.add(u); db.commit(); db.refresh(u)
        CURRENT["id"] = u.id

        # A: centinela (sin recepción). B: recepción parcial 6/10 y fecha de
        # entrega (para S6 ordena primero en 'listas'). C: cupo comido por
        # despacho abierto.
        VA = _venta(db, "A", qty=10.0)
        VB = _venta(db, "B", qty=10.0, fecha_entrega=date(2026, 9, 1))
        _recepcion_cerrada(db, VB.item_id, 6, "B")
        VC = _venta(db, "C", qty=5.0)
        NUESTRAS = {VA.oc_id, VB.oc_id, VC.oc_id}

        # ── S1 · el panel existe y responde (pinza del orden de registro) ────────
        h_antes = hoy_chile().isoformat()  # ANTES del request (tolerancia de medianoche)
        r = client.get("/api/despachos/listo-para-despachar")
        check("s1a GET /listo-para-despachar → 200 (jamás 422: registrado ANTES "
              "de /{despacho_id})", r.status_code == 200,
              (r.status_code, r.text[:200]))
        body = r.json() if r.status_code == 200 else {}
        check("s1b contrato del sobre: {hoy, grupos[]}",
              isinstance(body.get("hoy"), str) and isinstance(body.get("grupos"), list),
              body if not isinstance(body, dict) else list(body.keys()))
        # s1c (L4): pinza REAL del valor, no solo el tipo — «hoy» es el de CHILE.
        # Tolerancia de medianoche (patrón del check 3 de test_ventas_opciones):
        # se acepta la fecha capturada antes del request o la de después; si la
        # corrida cruza las 00:00, una de las dos coincide sí o sí.
        check("s1c el «hoy» del panel == hoy_chile() (tolerancia de medianoche)",
              body.get("hoy") in (h_antes, hoy_chile().isoformat()), body.get("hoy"))

        # ── S2 · CENTINELA: ítem en_bodega SIN recepción → disponible = cantidad ─
        d = _detalle_item(VA.oc_id, VA.item_id)
        check("s2a ★ detalle: sin recepción registrada, qty_disponible == cantidad (10)",
              d["qty_disponible"] == 10, d)
        _, cards = _cards_listas()
        ca = cards.get(VA.oc_id, {})
        check("s2b ★ listado 'listas': unidades_despachables == 10 e "
              "items_despachables == 1", ca.get("unidades_despachables") == 10
              and ca.get("items_despachables") == 1, ca)
        g = _grupo_panel(VA.oc_id)
        check("s2c ★ panel: grupo presente, qty_disponible == 10 == total_unidades "
              "y cantidad == 10", g is not None and g["total_unidades"] == 10
              and g["items"][0]["qty_disponible"] == 10
              and g["items"][0]["cantidad"] == 10
              and g["items"][0]["numero_parte"] == f"{MARK}-PA", g)

        # ── S5 · recepción PARCIAL (6 de 10): tope por lo recibido en las 3 vistas ─
        d = _detalle_item(VB.oc_id, VB.item_id)
        check("s5a detalle: disponible topeado a lo RECIBIDO (6, no 10)",
              d["qty_disponible"] == 6, d)
        _, cards = _cards_listas()
        check("s5b listado: unidades_despachables == 6",
              cards.get(VB.oc_id, {}).get("unidades_despachables") == 6,
              cards.get(VB.oc_id))
        g = _grupo_panel(VB.oc_id)
        check("s5c panel: qty_disponible == 6 == total_unidades",
              g is not None and g["total_unidades"] == 6
              and g["items"][0]["qty_disponible"] == 6, g)

        # ── S3 · PARIDAD TRIPLE: detalle == panel == tope real del guard ─────────
        r = _crear_despacho(VB.oc_id, VB.item_id, 2)
        check("s3-setup despacho abierto de 2 sobre B → 200", r.status_code == 200,
              r.text[:200])
        # ESPERADO fijado por la SEMILLA (recibido 6 − despacho abierto 2), no
        # leído en vivo: una sonda que sigue lo que la app diga acompañaría a la
        # mutación en vez de delatarla — el guard debe caer POR SÍ SOLO si la
        # fórmula compartida cambia (poder discriminante; lección de la casa
        # «sondas que no prueban nada»).
        ESPERADO = 4
        disp_det = _detalle_item(VB.oc_id, VB.item_id)["qty_disponible"]
        g = _grupo_panel(VB.oc_id)
        disp_panel = g["items"][0]["qty_disponible"] if g else None
        _, cards = _cards_listas()
        disp_card = cards.get(VB.oc_id, {}).get("unidades_despachables")
        # 6º consumidor: el buscador de picking de Bodega. Es la rama que NINGUNA
        # suite probaba — la suya (tests_contabilidad/test_buscador_bodega_...)
        # afirma qty_disponible sobre una línea SIN despachos encima, así que
        # cubre el tope físico pero jamás el DESCUENTO. Con esto, reponer la copia
        # inline sin `− qty_desp` en bodega.py cae acá.
        fila_bod = _bodega_item(VB.item_id)
        disp_bod = fila_bod.get("qty_disponible") if fila_bod else None
        check("s3a ★ misma semilla: detalle == panel == listado == buscador de "
              "Bodega == 4 (recibido 6 − despacho abierto 2)",
              disp_det == disp_panel == disp_card == disp_bod == ESPERADO,
              (disp_det, disp_panel, disp_card, disp_bod))
        check("s3a-bis el buscador de Bodega también reporta el tope físico "
              "(qty_recibida 6) — la fila entera es coherente, no solo el cupo",
              fila_bod is not None and fila_bod.get("qty_recibida") == 6.0
              and fila_bod.get("cantidad") == 10.0, fila_bod)
        r = _crear_despacho(VB.oc_id, VB.item_id, ESPERADO + 1)
        check("s3b ★ POST con disponible+1 (5) → 400 «excede» (el guard usa LA "
              "MISMA fórmula)", r.status_code == 400 and "excede" in r.text,
              (r.status_code, r.text[:200]))
        r = _crear_despacho(VB.oc_id, VB.item_id, ESPERADO)
        check("s3c ★ POST con el disponible EXACTO (4) → 200", r.status_code == 200,
              r.text[:200])
        _, cards = _cards_listas()
        check("s3d agotado el cupo: B sigue en 'listas' (ítem aún en_bodega) con "
              "unidades == 0 y AUSENTE del panel",
              cards.get(VB.oc_id, {}).get("unidades_despachables") == 0
              and cards.get(VB.oc_id, {}).get("items_despachables") == 0
              and _grupo_panel(VB.oc_id) is None, cards.get(VB.oc_id))
        check("s3e el cupo agotado por despachos ABIERTOS se declara como tal "
              "(único motivo que autoriza «ciérralos o anúlalos»)",
              cards.get(VB.oc_id, {}).get("motivo_sin_cupo") == "en_preparacion",
              cards.get(VB.oc_id, {}).get("motivo_sin_cupo"))

        # ── S4 · cupo comido por despacho ABIERTO → invisible en el panel ────────
        r = _crear_despacho(VC.oc_id, VC.item_id, 5)
        check("s4-setup despacho abierto por TODO el cupo de C → 200",
              r.status_code == 200, r.text[:200])
        d_abierto = r.json().get("id")
        _, cards = _cards_listas()
        cc = cards.get(VC.oc_id, {})
        check("s4a C SIGUE en la pestaña 'listas' (el EXISTS por en_bodega no "
              "cambió)", VC.oc_id in cards, list(cards))
        check("s4b …pero con items_despachables == 0 y unidades_despachables == 0 "
              "(la card ya no miente)", cc.get("items_despachables") == 0
              and cc.get("unidades_despachables") == 0, cc)
        check("s4b-bis motivo_sin_cupo == 'en_preparacion' (acá SÍ hay un despacho "
              "abierto que anular)", cc.get("motivo_sin_cupo") == "en_preparacion",
              cc.get("motivo_sin_cupo"))
        check("s4c y AUSENTE del panel", _grupo_panel(VC.oc_id) is None,
              _grupo_panel(VC.oc_id))
        r = client.delete(f"/api/despachos/{d_abierto}")
        check("s4d anular el despacho → 200", r.status_code == 200, r.text[:200])
        g = _grupo_panel(VC.oc_id)
        _, cards = _cards_listas()
        check("s4e ★ anulado: C REAPARECE en el panel con su cupo entero (5) y el "
              "listado lo refleja", g is not None and g["total_unidades"] == 5
              and g["items"][0]["qty_disponible"] == 5
              and cards.get(VC.oc_id, {}).get("unidades_despachables") == 5, (g, cards.get(VC.oc_id)))
        check("s4f con cupo > 0 NO hay motivo que explicar (el campo va en None)",
              cards.get(VC.oc_id, {}).get("motivo_sin_cupo") is None
              and cards.get(VA.oc_id, {}).get("motivo_sin_cupo") is None,
              (cards.get(VC.oc_id, {}).get("motivo_sin_cupo"),
               cards.get(VA.oc_id, {}).get("motivo_sin_cupo")))

        # ── S6 · orden del panel == orden de la pestaña 'listas' ─────────────────
        # Estado final: A disp 10 (sin fecha), C disp 5 (sin fecha), B disp 0
        # (con fecha, ausente). Sin fecha → al final, desempate id DESC → C antes
        # que A en AMBAS vistas.
        panel_ids = [g["oc_cliente_id"] for g in _panel()["grupos"]
                     if g["oc_cliente_id"] in NUESTRAS]
        lista_cards, _ = _cards_listas()
        lista_ids = [c["id"] for c in lista_cards if c["id"] in set(panel_ids)]
        check("s6 orden del panel == orden de 'listas' (urgencia asc, sin fecha "
              "al final, id DESC)", panel_ids == lista_ids and panel_ids == [VC.oc_id, VA.oc_id],
              (panel_ids, lista_ids))

        # ── S7 · en en_curso/historial los campos NO viajan (queries muertas) ────
        r = client.get("/api/despachos/oc-clientes",
                       params={"tab": "en_curso", "q": MARK, "page_size": 200})
        en_curso = r.json()["items"] if r.status_code == 200 else []
        check("s7 tab en_curso: cards del MARK presentes (B tiene despachos "
              "abiertos) y SIN items_despachables/unidades_despachables",
              any(c["id"] == VB.oc_id for c in en_curso)
              and all("items_despachables" not in c and "unidades_despachables" not in c
                      for c in en_curso),
              [(c["id"], sorted(k for k in c if "despacha" in k)) for c in en_curso])

        # ── C · /counts: aditivos coherentes con el panel, viejos conservados ────
        counts = client.get("/api/despachos/counts").json()
        panel = _panel()
        suma_panel = sum(g["total_unidades"] for g in panel["grupos"])
        check("c1 campos viejos conservados (otros consumidores los leen)",
              all(k in counts for k in ("ocs_listas", "items_listos", "items_despachados")),
              sorted(counts))
        check("c2 ★ counts.ocs_con_disponible == nº de grupos del panel "
              "(mismo universo, misma fórmula)",
              counts.get("ocs_con_disponible") == len(panel["grupos"]),
              (counts.get("ocs_con_disponible"), len(panel["grupos"])))
        check("c3 ★ counts.unidades_despachables == Σ total_unidades del panel",
              abs(float(counts.get("unidades_despachables", -1)) - suma_panel) < 1e-6,
              (counts.get("unidades_despachables"), suma_panel))
        check("c4 la diferencia semántica ES visible: items_listos (líneas en "
              "bodega sin capar) cuenta la línea agotada de B; el panel no",
              counts.get("items_listos", 0) >= counts.get("ocs_con_disponible", 0)
              and _grupo_panel(VB.oc_id) is None,
              (counts.get("items_listos"), counts.get("ocs_con_disponible")))

        # ── E1 · residuo flotante del cupo: visible-como-CERO en las 3 vistas ────
        # Disponible ínfimo pero > 0 (aquí 1e-4: qty_despachada es FLOAT de simple
        # precisión en MySQL y un residuo 1e-7 literal se redondearía a 0 en el
        # roundtrip — 1e-4 sobrevive y sigue BAJO la tolerancia 0.001 del guard,
        # que es exactamente lo que _es_despachable debe tratar como cero). El
        # despacho se siembra DIRECTO en BD para fijar el residuo exacto sin
        # depender de la aritmética del server.
        counts_pre = client.get("/api/despachos/counts").json()
        VE = _venta(db, "E", qty=5.0)
        desp_e = Despacho(numero_despacho=f"{MARK}-DSPE", oc_cliente_id=VE.oc_id,
                          estado="en_preparacion", observaciones=f"{MARK} obs")
        db.add(desp_e); db.flush()
        db.add(DespachoItem(despacho_id=desp_e.id, item_cotizacion_id=VE.item_id,
                            qty_despachada=5.0 - 1e-4))
        db.commit()
        counts_post = client.get("/api/despachos/counts").json()
        check("e1a panel: el residuo NO es una línea despachable (grupo ausente)",
              _grupo_panel(VE.oc_id) is None, _grupo_panel(VE.oc_id))
        _, cards = _cards_listas()
        ce = cards.get(VE.oc_id, {})
        check("e1b insignia 'listas': items_despachables == 0 y unidades == 0 "
              "(el residuo no pinta cupo)",
              ce.get("items_despachables") == 0
              and ce.get("unidades_despachables") == 0, ce)
        check("e1c counts: los aditivos quedan IDÉNTICOS a antes de sembrar el "
              "residuo (ni la OC ni sus 'unidades' cuentan)",
              counts_post.get("ocs_con_disponible") == counts_pre.get("ocs_con_disponible")
              and abs(float(counts_post.get("unidades_despachables", -1))
                      - float(counts_pre.get("unidades_despachables", -2))) < 1e-9,
              ((counts_pre.get("ocs_con_disponible"),
                counts_pre.get("unidades_despachables")),
               (counts_post.get("ocs_con_disponible"),
                counts_post.get("unidades_despachables"))))

        # ── E2 · el residuo también es CERO en el DETALLE y en el guard ─────────
        # La fórmula colapsa el residuo en su fuente (_disponibles_por_item), así
        # que la pantalla de picking no puede ofrecer lo que el listado y el panel
        # dan por agotado. Antes el detalle emitía el crudo, el modal imprimía
        # «1.1102230246251565e-16», prellenaba la cantidad y dejaba crear un
        # despacho que la guía SII rechaza («El despacho no tiene cantidades a
        # despachar»): un zombi que hay que anular a mano.
        d = _detalle_item(VE.oc_id, VE.item_id)
        check("e2a ★ detalle: el residuo se sirve como 0, no como 1e-4 "
              "(misma verdad que listado, panel y counts)",
              d["qty_disponible"] == 0, d)
        fila_bod = _bodega_item(VE.item_id)
        check("e2b buscador de Bodega: mismo 0 (6º consumidor de la fórmula)",
              fila_bod is not None and fila_bod.get("qty_disponible") == 0.0, fila_bod)
        # El agujero FUNCIONAL: con disponible 0, `qty > 0 + 0.001` es False para
        # 7e-9 y el guard viejo (`qty <= 0`) dejaba nacer el despacho por API.
        r = _crear_despacho(VE.oc_id, VE.item_id, 7e-9)
        check("e2c ★ POST con cantidad infinitesimal (7e-9) → 400: no nace el "
              "despacho zombi por API", r.status_code == 400
              and "demasiado peque" in r.text, (r.status_code, r.text[:200]))
        check("e2d el rechazo tiene mensaje PROPIO (no «Cantidad inválida»: el "
              "operador no tipeó mal)", "Cantidad inválida" not in r.text,
              r.text[:200])

        # ── M · motivo_sin_cupo: la card dice la VERDAD de por qué no hay cupo ──
        # CONTRATO con el frontend: 'en_preparacion' | 'sin_stock' | 'despachado'
        # | None, presente solo cuando unidades_despachables == 0. El motivo nace
        # acá, donde vive LA fórmula del cupo; la pantalla solo elige el texto.
        #
        # F: recepción 'faltante' 4 de 10 (6 en reclamo al proveedor), se despachan
        # las 4 y se CIERRA el despacho. La línea sigue 'en_bodega' (4 + 0.001 < 10:
        # _cerrar_despacho_tx no la voltea) y la OC sigue en 'listas' durante todas
        # las semanas del reclamo. La insignia culpaba a «despachos abiertos» que NO
        # EXISTEN y mandaba a anular un despacho cerrado (con su guía 52 emitida).
        VF = _venta(db, "F", qty=10.0)
        _recepcion_cerrada(db, VF.item_id, 4, "F", estado_recepcion="faltante")
        r = _crear_despacho(VF.oc_id, VF.item_id, 4)
        check("m0-setup despacho de las 4 recibidas → 200", r.status_code == 200,
              r.text[:200])
        d_f = r.json().get("id")
        r = client.post(f"/api/despachos/{d_f}/cerrar")
        check("m0-setup el despacho se CIERRA → 200", r.status_code == 200, r.text[:200])
        _, cards = _cards_listas()
        cf = cards.get(VF.oc_id, {})
        check("m1 la línea faltante sigue en 'listas' y en_bodega (el reclamo no "
              "la saca)", VF.oc_id in cards
              and _detalle_item(VF.oc_id, VF.item_id)["estado_item"] == "en_bodega",
              cf)
        check("m2 ★ cupo 0 por MERCADERÍA QUE NO LLEGÓ → 'sin_stock' (jamás "
              "'en_preparacion': no hay ningún despacho abierto que anular)",
              cf.get("unidades_despachables") == 0
              and cf.get("motivo_sin_cupo") == "sin_stock", cf)

        # G: llegó TODO (10 de 10) y se lo comieron despachos ya CERRADOS, con la
        # línea aún en_bodega (el estado que deja la reversa defensiva
        # embarque→en_bodega). Se siembra directo en BD porque por el flujo normal
        # el cierre voltearía la línea a 'despachado' y saldría de 'listas'.
        VG = _venta(db, "G", qty=10.0)
        _recepcion_cerrada(db, VG.item_id, 10, "G")
        desp_g = Despacho(numero_despacho=f"{MARK}-DSPG", oc_cliente_id=VG.oc_id,
                          estado="despachado", observaciones=f"{MARK} obs")
        db.add(desp_g); db.flush()
        db.add(DespachoItem(despacho_id=desp_g.id, item_cotizacion_id=VG.item_id,
                            qty_despachada=10.0))
        db.commit()
        _, cards = _cards_listas()
        cg = cards.get(VG.oc_id, {})
        check("m3 ★ cupo 0 por despachos ya CERRADOS → 'despachado' (un cerrado "
              "NO se anula: el texto no puede ofrecerlo)",
              cg.get("unidades_despachables") == 0
              and cg.get("motivo_sin_cupo") == "despachado", cg)
        check("m4 ninguna de las dos aparece en el panel (cupo real 0)",
              _grupo_panel(VF.oc_id) is None and _grupo_panel(VG.oc_id) is None,
              (_grupo_panel(VF.oc_id), _grupo_panel(VG.oc_id)))
        r = client.get("/api/despachos/oc-clientes",
                       params={"tab": "en_curso", "q": MARK, "page_size": 200})
        check("m5 el campo NO viaja en en_curso (allí no se calcula el cupo)",
              all("motivo_sin_cupo" not in c for c in r.json().get("items", [])),
              [c.get("motivo_sin_cupo") for c in r.json().get("items", [])])

        # ── T · tope de la vía SII gratuito, AVISADO EN EL PICKING ──────────────
        # El aviso vivía solo en el preview de emisión, cuando dividir ya cuesta
        # anular la guía (o es imposible en la factura). El detalle de OC es la
        # respuesta que arma el picking: el único momento en que dividir es gratis.
        r = client.get(f"/api/despachos/oc-clientes/{VA.oc_id}")
        check("t1 ★ el detalle de OC expone max_lineas_sii_gratuito == 10 desde "
              "wasabil_dte.service (una sola definición, sin hardcode en el TSX)",
              r.status_code == 200
              and r.json().get("max_lineas_sii_gratuito") == MAX_LINEAS_SII_GRATUITO
              and MAX_LINEAS_SII_GRATUITO == 10,
              (r.status_code, r.json().get("max_lineas_sii_gratuito")))

    finally:
        db.close()
        _limpiar()
        print("Cleanup OK")
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_listo_para_despachar():
    run()


if __name__ == "__main__":
    run()
