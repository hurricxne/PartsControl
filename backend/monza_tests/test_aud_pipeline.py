"""Regresión de los hallazgos del reparador R6 (Abastecimiento + Bodega + Nacional)
de la AUDITORÍA INTEGRAL Fases 1-6 de MonzaParts.

Cubre, uno por sección, los arreglos que validó el verificador adversarial:

  #9  KPI «En tránsito» de Abastecimiento: el pipeline NUNCA escribe
      estado_linea='en_transito' (la mercadería volando está en 'preparado' y
      'embarcado'), así que la tarjeta daba SIEMPRE 0. Ahora suma los dos estados
      reales — misma semántica que _STATE_BUCKETS de monza_router_despachos.
  #10 «En bodega» mostraba la cantidad VENDIDA, no la RECIBIDA: con vendido 10 /
      recibido 4 el bodeguero leía 10 en bodega + 6 reclamadas = 16 unidades sobre
      una línea de 10, contradiciendo el tope físico de Despachos. El listado ahora
      trae qty_recibida y qty_disponible (cupo real, ya descontados los borradores).
  #11 La entrega NACIONAL no avisaba «venta lista para despacho» (la vía embarque
      sí): con compras nacionales (F8) la venta quedaba lista y nadie se enteraba.
  #13 Bodega no tenía require_empresa: un usuario de minería leía y mutaba datos de
      MonzaParts. (Abastecimiento queda a propósito SIN candado: diferido por el
      dueño; se documenta en el propio router, no se testea aquí.)

Prueba de integración contra la BD real: TODO lo que crea va marcado y se borra en
un finally, en orden FK-seguro; nunca toca datos reales. Verificaciones de estado
con SESIÓN NUEVA (la sesión del cliente puede mentir bajo REPEATABLE READ) y con
delta para los KPIs (la BD tiene datos propios que no se pueden asumir en 0).

Corre con:  ./venv/bin/python -m pytest monza_tests/test_aud_pipeline.py -q
       o:   ./venv/bin/python monza_tests/test_aud_pipeline.py
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, Base, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
    MonzaEmbarque, MonzaEmbarqueItem, MonzaRecepcion, MonzaRecepcionItem,
    MonzaDespacho, MonzaDespachoItem, MonzaReclamo, MonzaLog, MonzaNotificacion,
)
from monza_recepcion_nacional.models import (  # noqa: E402
    MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
)
from monza_router_abastecimiento import router as abastecimiento_router  # noqa: E402
from monza_router_bodega import router as bodega_router  # noqa: E402
from monza_router_despachos import router as despachos_router  # noqa: E402
from monza_recepcion_nacional.router import router as mrn_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

# monza_cotizaciones.numero es String(20): el MARK vive en el CLIENTE (ancla de
# limpieza, patrón del repo) y el número usa un prefijo corto + sufijo aleatorio.
MARK = "__TEST_AUD_R6__"
EMAIL = f"{MARK}@test.invalid"
# Mutable: la sección 4 cambia la empresa en caliente para ejercer el candado.
CURRENT = {"empresa": "automotriz", "id": None}

app = FastAPI()
app.include_router(abastecimiento_router)
app.include_router(bodega_router)
app.include_router(despachos_router)
app.include_router(mrn_router)


# Auth REALISTA (lección G13): además de devolver el usuario hace una lectura en la
# MISMA sesión del request, igual que auth.get_current_user en producción — así el
# read view de MySQL nace ANTES de cualquier with_for_update(), como en la vida real.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], email=EMAIL, empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Fixtures marcadas ─────────────────────────────────────────────────────────
def _venta(db, cantidades=(10,), estado_linea="embarcado", ocp=None):
    """Cliente + cotización VENDIDA + N ítems (opcionalmente asignados a una OC)."""
    suf = uuid.uuid4().hex[:6].upper()
    cli = MonzaCliente(nombre=MARK)
    db.add(cli); db.flush()
    cot = MonzaCotizacion(numero=f"CT-AR6-{suf}", cliente_id=cli.id, estado="vendida")
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion=f"{MARK} Parte {n}",
            numero_parte=f"P-AR6-{n}", cantidad=cant, estado_linea=estado_linea,
            oc_proveedor_id=(ocp.id if ocp else None),
        )
        db.add(it); items.append(it)
    db.commit()
    for obj in [cot] + items:
        db.refresh(obj)
    return cot, items


def _oc_nacional(db):
    suf = uuid.uuid4().hex[:6].upper()
    ocp = MonzaOcProveedor(numero=f"{MARK}-OCP-{suf}", proveedor_nombre=f"{MARK} PROV",
                           moneda="CLP", tipo_origen="nacional")
    db.add(ocp); db.commit(); db.refresh(ocp)
    return ocp


def _embarque(db, items):
    suf = uuid.uuid4().hex[:6].upper()
    emb = MonzaEmbarque(numero=f"{MARK}-E-{suf}", estado="en_transito")
    db.add(emb); db.flush()
    for it in items:
        db.add(MonzaEmbarqueItem(embarque_id=emb.id, item_id=it.id))
    db.commit(); db.refresh(emb)
    return emb


def _fila_en_bodega(item_id):
    r = client.get("/api/monza/bodega/en-bodega")
    assert r.status_code == 200, r.text
    return next((x for x in r.json() if x["id"] == item_id), None)


def _notifs_listas(cot_id):
    """Notificaciones «lista para despacho» de una venta, con SESIÓN NUEVA."""
    db = SessionLocal()
    try:
        return [n.titulo for n in db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id == cot_id,
            MonzaNotificacion.titulo.like("%lista para despacho%")).all()]
    finally:
        db.close()


def _estado_linea(item_id):
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacionItem.estado_linea).filter(
            MonzaCotizacionItem.id == item_id).scalar()
    finally:
        db.close()


def _vendido_del_repuesto(cot_id, numero_parte):
    """Σ cantidad de TODAS las líneas hermanas de ese repuesto en la venta.

    Desde la Fase 9b (envíos parciales) «lo vendido» de un repuesto ya NO es la
    `cantidad` de una fila: una línea de 10 puede partirse en 6 (que viajan) + 4 (que
    esperan el próximo AWB), y son dos filas hermanas. Lo vendido es la SUMA — ver el
    check reinterpretado de la sección 2. Sesión nueva, como el resto de las lecturas
    de verificación."""
    db = SessionLocal()
    try:
        return sum(c or 0 for (c,) in db.query(MonzaCotizacionItem.cantidad).filter(
            MonzaCotizacionItem.cotizacion_id == cot_id,
            MonzaCotizacionItem.numero_parte == numero_parte).all())
    finally:
        db.close()


# ── Limpieza total (orden FK-seguro) ──────────────────────────────────────────
def _limpiar(db):
    db.rollback()
    S = "fetch"
    cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
               .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
               .filter(MonzaCliente.nombre == MARK).all()]
    item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
    dsp_ids = [r[0] for r in db.query(MonzaDespacho.id)
               .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
    emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
               .filter(MonzaEmbarque.numero.like(f"{MARK}%")).all()]
    rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
               .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
    ocp_ids = [r[0] for r in db.query(MonzaOcProveedor.id)
               .filter(MonzaOcProveedor.numero.like(f"{MARK}%")).all()]
    rn_ids = [r[0] for r in db.query(MonzaRecepcionNacionalItem.recepcion_id)
              .filter(MonzaRecepcionNacionalItem.item_cotizacion_id.in_(item_ids or [0])).all()]
    rn_ids += [r[0] for r in db.query(MonzaRecepcionNacional.id)
               .filter(MonzaRecepcionNacional.oc_proveedor_id.in_(ocp_ids or [0])).all()]
    rn_ids = list(set(rn_ids))

    if rn_ids:
        db.query(MonzaRecepcionNacionalItem).filter(
            MonzaRecepcionNacionalItem.recepcion_id.in_(rn_ids)).delete(synchronize_session=S)
        db.query(MonzaRecepcionNacional).filter(
            MonzaRecepcionNacional.id.in_(rn_ids)).delete(synchronize_session=S)
    # Notifs acotadas por entidad (nunca un producto cruzado con ids ajenos)
    db.query(MonzaNotificacion).filter(
        MonzaNotificacion.entidad == "cotizacion",
        MonzaNotificacion.entidad_id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaNotificacion).filter(
        MonzaNotificacion.entidad == "recepcion",
        MonzaNotificacion.entidad_id.in_(rec_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaNotificacion).filter(
        MonzaNotificacion.entidad == "despacho",
        MonzaNotificacion.entidad_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
    db.query(MonzaReclamo).filter(
        MonzaReclamo.item_id.in_(item_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaDespachoItem).filter(
        MonzaDespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaDespacho).filter(
        MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaRecepcionItem).filter(
        MonzaRecepcionItem.recepcion_id.in_(rec_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaRecepcion).filter(
        MonzaRecepcion.id.in_(rec_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaEmbarqueItem).filter(
        MonzaEmbarqueItem.embarque_id.in_(emb_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaEmbarque).filter(
        MonzaEmbarque.id.in_(emb_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacionItem).filter(
        MonzaCotizacionItem.id.in_(item_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacion).filter(
        MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaOcProveedor).filter(
        MonzaOcProveedor.id.in_(ocp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
    db.commit()


# ── Escenarios ────────────────────────────────────────────────────────────────
def run():
    db = SessionLocal()
    try:
        _limpiar(db)

        # ── 1 · Hallazgo #9: el KPI «En tránsito» deja de ser un cero perpetuo ──
        # Delta, no valor absoluto: la BD trae sus propias filas vivas.
        antes = client.get("/api/monza/abastecimiento/kpis")
        check("1: /abastecimiento/kpis responde 200", antes.status_code == 200, antes.text)
        k0 = antes.json()
        _venta(db, cantidades=(3,), estado_linea="preparado")
        _venta(db, cantidades=(7,), estado_linea="embarcado")
        k1 = client.get("/api/monza/abastecimiento/kpis").json()
        # ANTES del arreglo este delta era 0 con mercadería en el aire (el pipeline
        # nunca escribe 'en_transito'); ahora 'preparado' + 'embarcado' sí suman.
        check("1: 1 'preparado' + 1 'embarcado' suman 2 a en_transito",
              k1["en_transito"] - k0["en_transito"] == 2,
              f"{k0['en_transito']} -> {k1['en_transito']}")
        check("1: no se contaminan los demás contadores",
              k1["comprado"] == k0["comprado"] and k1["en_bodega"] == k0["en_bodega"]
              and k1["despachado"] == k0["despachado"] and k1["reclamo"] == k0["reclamo"],
              f"{k0} -> {k1}")
        _limpiar(db)

        # ── 2 · Hallazgo #10: «En bodega» muestra lo RECIBIDO, no lo vendido ────
        # Escenario exacto de la auditoría: vendido 10, llegan 4 (estado 'faltante'),
        # reclamo automático por las 6 que faltan.
        cot, (A, B) = _venta(db, cantidades=(10, 5), estado_linea="embarcado")
        emb = _embarque(db, [A, B])
        r = client.post(f"/api/monza/bodega/embarques/{emb.id}/recibir")
        check("2: abrir recepción de embarque", r.status_code == 200, r.text)
        rid = r.json()["recepcion_id"]
        client.patch(f"/api/monza/bodega/recepciones/{rid}/items/{A.id}",
                     json={"estado_recepcion": "faltante", "qty_recibida": 4})
        client.patch(f"/api/monza/bodega/recepciones/{rid}/items/{B.id}",
                     json={"estado_recepcion": "completo", "qty_recibida": 5})
        r = client.post(f"/api/monza/bodega/recepciones/{rid}/cerrar", json={})
        check("2: cerrar recepción", r.status_code == 200, r.text)
        check("2: la llegada parcial deja el ítem A en bodega (cierre particionado)",
              _estado_linea(A.id) == "en_bodega", _estado_linea(A.id))

        fa = _fila_en_bodega(A.id)
        check("2: el ítem A aparece en /bodega/en-bodega", fa is not None)
        if fa:
            # El bug: 'cantidad' (10) era el ÚNICO número y contradecía a Despachos.
            #
            # REINTERPRETADO en la Fase 9b (envíos parciales). El check decía
            # "cantidad sigue siendo lo VENDIDO (10, sin cambio de contrato)", y esa
            # frase ya no describe una invariante GLOBAL: la partición de líneas
            # (preparar-parcial / embarque con cantidades) deja 6 en la fila original y
            # crea una hermana con 4, así que `cantidad` es lo vendido DE ESA LÍNEA y
            # lo vendido del REPUESTO en la venta es Σ hermanas. No se borra el check
            # porque lo que de verdad protege sigue vigente y sigue siendo valioso:
            # el listado de Bodega publica la cifra de la LÍNEA sin reemplazarla por lo
            # recibido (4) — eso era el hallazgo #10 — y esta línea, que nunca se
            # partió, tiene su 10 intacto. Se le agrega la forma que SÍ sobrevive a la
            # partición: Σ hermanas == lo vendido, la misma identidad que asserta
            # monza_tests/test_preparar_parcial_monza.py sobre líneas ya partidas.
            check("2: cantidad = lo vendido DE ESA LÍNEA (10; el listado no la "
                  "reemplaza por lo recibido)", fa["cantidad"] == 10, fa)
            check("2: Σ hermanas del repuesto == lo vendido (10) — la forma de "
                  "«lo vendido» que sobrevive a la partición de líneas",
                  _vendido_del_repuesto(cot.id, "P-AR6-1") == 10,
                  _vendido_del_repuesto(cot.id, "P-AR6-1"))
            check("2: qty_recibida = 4 (lo que realmente llegó)",
                  float(fa.get("qty_recibida") or 0) == 4.0, fa)
            check("2: qty_disponible = 4 (coherente con el tope físico de Despachos)",
                  float(fa.get("qty_disponible") or 0) == 4.0, fa)
        fb = _fila_en_bodega(B.id)
        if fb:
            check("2: ítem completo → recibida 5 y disponible 5",
                  float(fb.get("qty_recibida") or 0) == 5.0
                  and float(fb.get("qty_disponible") or 0) == 5.0, fb)

        # Contraste con Despachos: la misma cifra por las dos puertas.
        rl = client.get("/api/monza/despachos/listos").json()
        venta = next((v for v in rl if v["id"] == cot.id), None)
        disp_dsp = next((i["qty_disponible"] for i in venta["items"] if i["id"] == A.id),
                        None) if venta else None
        check("2: /despachos/listos ofrece el MISMO cupo que Bodega (4)",
              disp_dsp == 4, disp_dsp)

        # Un borrador de despacho reserva mercadería: el cupo visible debe bajar a 0
        # sin que la línea salga de bodega (sigue esperando las 6 que faltan).
        r = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": cot.id, "items": [{"item_id": A.id, "qty": 4}]})
        check("2: crear despacho borrador por las 4 recibidas", r.status_code == 200, r.text)
        fa2 = _fila_en_bodega(A.id)
        check("2: con el borrador abierto, qty_disponible cae a 0",
              fa2 is not None and float(fa2.get("qty_disponible") or 0) == 0.0, fa2)
        check("2: pero qty_recibida sigue siendo 4 (lo físico no cambió)",
              fa2 is not None and float(fa2.get("qty_recibida") or 0) == 4.0, fa2)
        _limpiar(db)

        # ── 2b · Dato LEGADO sin recepción: no se acota ni se inventa un número ──
        cot2, (L,) = _venta(db, cantidades=(6,), estado_linea="en_bodega")
        fl = _fila_en_bodega(L.id)
        check("2b: sin recepción registrada qty_recibida viene null (no 0)",
              fl is not None and fl.get("qty_recibida") is None, fl)
        check("2b: y el disponible es la cantidad vendida (comportamiento histórico)",
              fl is not None and float(fl.get("qty_disponible") or 0) == 6.0, fl)
        _limpiar(db)

        # ── 3 · Hallazgo #11: la entrega NACIONAL avisa «lista para despacho» ────
        ocp = _oc_nacional(db)
        cot3, (N,) = _venta(db, cantidades=(4,), estado_linea="comprado", ocp=ocp)
        check("3: la venta arranca SIN aviso", _notifs_listas(cot3.id) == [])
        r = client.post("/api/monza/recepcion-nacional", json={
            "oc_proveedor_id": ocp.id, "numero_guia_proveedor": "G-AR6-1",
            "fecha": "2026-07-28", "cerrar": True,
            "items": [{"item_cotizacion_id": N.id, "qty_recibida": 4,
                       "estado_recepcion": "completo"}]})
        check("3: registrar entrega nacional cerrando", r.status_code == 200, r.text)
        check("3: el ítem queda en bodega", _estado_linea(N.id) == "en_bodega",
              _estado_linea(N.id))
        avisos = _notifs_listas(cot3.id)
        # ANTES del arreglo esta lista era [] (solo la vía embarque notificaba).
        check("3: llega UNA notificación «lista para despacho» por la vía nacional",
              len(avisos) == 1, avisos)
        _limpiar(db)

        # ── 3b · Camino de DOS pasos: cerrar=False + POST /{id}/cerrar ───────────
        ocp2 = _oc_nacional(db)
        cot4, (N2,) = _venta(db, cantidades=(2,), estado_linea="comprado", ocp=ocp2)
        r = client.post("/api/monza/recepcion-nacional", json={
            "oc_proveedor_id": ocp2.id, "numero_guia_proveedor": "G-AR6-2",
            "fecha": "2026-07-28", "cerrar": False,
            "items": [{"item_cotizacion_id": N2.id, "qty_recibida": 2,
                       "estado_recepcion": "completo"}]})
        check("3b: registrar entrega ABIERTA", r.status_code == 200, r.text)
        rec_id = r.json()["id"]
        check("3b: con la recepción abierta todavía NO hay aviso (nada está en bodega)",
              _notifs_listas(cot4.id) == [], _notifs_listas(cot4.id))
        r = client.post(f"/api/monza/recepcion-nacional/{rec_id}/cerrar")
        check("3b: cerrar la recepción", r.status_code == 200, r.text)
        check("3b: el aviso sale al CERRAR", len(_notifs_listas(cot4.id)) == 1,
              _notifs_listas(cot4.id))
        _limpiar(db)

        # ── 4 · Hallazgo #13: candado de empresa en Bodega ───────────────────────
        cot5, (E,) = _venta(db, cantidades=(3,), estado_linea="embarcado")
        emb5 = _embarque(db, [E])
        CURRENT["empresa"] = "mineria"
        try:
            for ruta in ("/api/monza/bodega/kpis", "/api/monza/bodega/en-bodega",
                         "/api/monza/bodega/embarques", "/api/monza/bodega/reclamos"):
                rr = client.get(ruta)
                check(f"4: GET {ruta} con usuario de minería → 403",
                      rr.status_code == 403, f"{rr.status_code} {rr.text[:120]}")
            # La ESCRITURA debe cortarse en la puerta, no entrar a la lógica de negocio.
            rr = client.post(f"/api/monza/bodega/embarques/{emb5.id}/recibir")
            check("4: POST recibir embarque con minería → 403 (no 404/200)",
                  rr.status_code == 403, f"{rr.status_code} {rr.text[:120]}")
            db.rollback()
            sin_rec = db.query(MonzaRecepcion).filter(
                MonzaRecepcion.embarque_id == emb5.id).count()
            check("4: y NO se creó ninguna recepción", sin_rec == 0, sin_rec)
        finally:
            CURRENT["empresa"] = "automotriz"
        rr = client.get("/api/monza/bodega/kpis")
        check("4: el usuario automotriz sigue entrando (200)", rr.status_code == 200, rr.text)
        _limpiar(db)

    finally:
        CURRENT["empresa"] = "automotriz"
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_aud_pipeline():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
