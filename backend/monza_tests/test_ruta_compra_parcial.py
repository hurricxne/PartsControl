"""RUTA COMPLETA de una línea PARTIDA en la COMPRA — MonzaParts, de punta a punta.

LA PREGUNTA DEL DUEÑO (commit f45876d): la compra parcial parte una línea de 3 en
1 'comprado' + 2 'por_comprar'. ¿Las hermanas llegan BIEN hasta el final del viaje?

Esta suite recorre el pipeline Monza REAL por API con esa familia partida:

    Venta 'vendida' (1 línea × 3 u × $100.000)
      → COMPRA PARCIAL: 1 u al proveedor A (/comprar con cantidades)
      → el remanente (2 u) al proveedor B (/comprar, body LEGADO — el clon debe ser
        comprable por la vía histórica)
      → preparar las DOS hermanas (ruta internacional)
      → Logística: pantalla agrupada /preparados (cada hermana con SU grupo por OC)
      → cada hermana viaja en SU embarque (AWB distinto) → en_transito
      → Bodega: recepción de cada embarque, 'completo' con la QTY DE LA HERMANA
        (la cura del reclamo fantasma: 1 sobre 1 y 2 sobre 2, cero reclamos)
      → despacho de cada hermana → cerrar → guía FIRMADA (gate 2026-08-06)
      → FACTURAR cada guía: SU cantidad × SU precio congelado
      → Σ facturado == total de la venta; por_facturar físico == 0 por construcción.

EN CADA ESTACIÓN se re-verifica el INVARIANTE DE PLATA de la familia:
    Σ cantidad == 3 · Σ subtotal == $300.000 · cabecera BYTE-idéntica ·
    precio unitario IDÉNTICO en todas las hermanas (jamás prorrateado)
y CERO CRUCES entre las OCs de A y B (cada OC lista solo SU hermana, cada factura
solo SU guía, el retiro en oficina no le roba cupo al canal guía).

ARNÉS (molde de la casa: test_viaje_de_la_plata.py + test_por_facturar_fisico.py):
verificaciones con CONEXIÓN NUEVA y SQL crudo (el snapshot de la sesión miente),
datos MARCADOS con limpieza total en finally, la foto de la firma se sube por el
endpoint multipart REAL y el archivo se borra del disco al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_ruta_compra_parcial.py -q
       o:   ./venv/bin/python monza_tests/test_ruta_compra_parcial.py
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
    MonzaEmbarque, MonzaEmbarqueItem, MonzaRecepcion, MonzaRecepcionItem,
    MonzaDespacho, MonzaDespachoItem, MonzaReclamo, MonzaLog, MonzaNotificacion,
)
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContFacturaClienteItem,
)
from monza_router_abastecimiento import router as abast_router  # noqa: E402
from monza_router_logistica import router as logistica_router  # noqa: E402
from monza_router_bodega import router as bodega_router  # noqa: E402
from monza_router_despachos import router as despachos_router  # noqa: E402
from monza_contabilidad.router import router as contab_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

# monza_cotizaciones.numero es String(20): MARK corto a propósito.
MARK = "tst-mrcp"
EMAIL = f"{MARK}@test.invalid"
PRECIO = 100_000.0
NETO_VENTA, IVA_VENTA, BRUTO_VENTA = 300_000.0, 57_000.0, 357_000.0

app = FastAPI()
app.include_router(abast_router)
app.include_router(logistica_router)
app.include_router(bodega_router)
app.include_router(despachos_router)
app.include_router(contab_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK   | " if cond else "FAIL | ") + name
          + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


def _hoy_chile_str() -> str:
    """Hoy en Chile (la firma rechaza fechas futuras contra hoy_chile())."""
    from monza_wasabil_dte.service import hoy_chile
    return hoy_chile().isoformat()


# ─── Lecturas de VERIFICACIÓN: SIEMPRE con conexión nueva y SQL crudo ─────────

def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _lineas(cot_id):
    return [
        {"id": r[0], "cantidad": r[1], "subtotal": float(r[2] or 0),
         "precio": float(r[3] or 0), "estado": r[4], "ocp": r[5]}
        for r in _sql(
            "SELECT id,cantidad,subtotal_clp,precio_unitario_clp,estado_linea,"
            "oc_proveedor_id FROM monza_cotizacion_items "
            "WHERE cotizacion_id=:c ORDER BY id", c=cot_id)
    ]


def _linea(item_id):
    r = _sql("SELECT estado_linea,cantidad,oc_proveedor_id "
             "FROM monza_cotizacion_items WHERE id=:i", i=item_id)
    return (r[0][0], r[0][1], r[0][2]) if r else (None, None, None)


def _familia_ok(paso, cot_id):
    """EL INVARIANTE DE PLATA en cada estación: Σ cantidad == 3, Σ subtotal ==
    $300.000, cabecera intacta y precio unitario idéntico en todas las hermanas."""
    hs = _lineas(cot_id)
    cab = _sql("SELECT total_neto,iva_monto,total_bruto FROM monza_cotizaciones "
               "WHERE id=:i", i=cot_id)[0]
    cab = tuple(float(x or 0) for x in cab)
    ok = (
        sum(h["cantidad"] or 0 for h in hs) == 3
        and sum(h["subtotal"] for h in hs) == NETO_VENTA      # exacto: es plata
        and cab == (NETO_VENTA, IVA_VENTA, BRUTO_VENTA)
        and all(h["precio"] == PRECIO for h in hs)
    )
    check(f"{paso} ★ familia: Σcant==3, Σsub==300.000, cabecera intacta, "
          "precio idéntico", ok, (hs, cab))
    return hs


def _reclamos_familia(item_ids):
    if not item_ids:
        return 0
    marks = ",".join(str(int(i)) for i in item_ids)
    return _sql(f"SELECT COUNT(*) FROM monza_reclamos WHERE item_id IN ({marks})")[0][0]


def _resumen(cot_id):
    r = client.get(f"/api/monza/contabilidad/ventas/{cot_id}")
    assert r.status_code == 200, r.text
    return r.json()["resumen"]


# ─── Siembra marcada ──────────────────────────────────────────────────────────

def _crear_venta(db):
    """Venta 'vendida' con UNA línea de 3 u ('por_comprar'), foto de precios completa."""
    cli = MonzaCliente(nombre=f"{MARK} SpA", rut="11.111.111-1")
    db.add(cli); db.flush()
    cot = MonzaCotizacion(
        numero=f"{MARK}-C1", cliente_id=cli.id, estado="vendida",
        total_neto=NETO_VENTA, iva_monto=IVA_VENTA, total_bruto=BRUTO_VENTA,
        iva_pct=19, oc_cliente=f"OC-{MARK}",
    )
    db.add(cot); db.flush()
    it = MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion=f"{MARK} FILTRO ACEITE",
        numero_parte="P-RCP-1", marca="MONZA", procedencia="Alemania",
        calidad="original", cantidad=3, estado_linea="por_comprar",
        costo=99.5, moneda="EUR", peso_kg=2.75, tc_aplicado=1050.25,
        tarifa_aerea=7.5, markup_pct=0.28,
        precio_unitario_clp=PRECIO, subtotal_clp=3 * PRECIO,
        plazo_entrega="15 dias",
    )
    db.add(it); db.commit()
    db.refresh(cot); db.refresh(it)
    return cot.id, it.id


# ─── Limpieza TOTAL en orden FK-seguro (idempotente) ──────────────────────────

def _limpiar():
    db = SessionLocal()
    try:
        db.rollback()
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
        item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                    .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
        fac_ids = [r[0] for r in db.query(MonzaContFacturaCliente.id)
                   .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids or [0])).all()]
        dsps = db.query(MonzaDespacho).filter(
            MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()
        dsp_ids = [d.id for d in dsps]
        # La foto de la firma se sube al disco por el endpoint real: se borra acá
        # para dejar el disco como estaba (el gate lee el flag en BD, no el archivo).
        from monza_router_despachos import _DOCS_DIR
        for d in dsps:
            if d.guia_firmada_archivo:
                ruta = os.path.join(_DOCS_DIR, d.guia_firmada_archivo)
                if os.path.isfile(ruta):
                    os.remove(ruta)
        emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
                   .filter(MonzaEmbarque.numero.like(f"{MARK}%")).all()]
        # También los embarques alcanzables por ítems del MARK (numero autogenerado EMB-).
        emb_ids += [r[0] for r in db.query(MonzaEmbarqueItem.embarque_id)
                    .filter(MonzaEmbarqueItem.item_id.in_(item_ids or [0])).all()]
        emb_ids = sorted(set(emb_ids))
        rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
                   .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
        ocp_ids = [r[0] for r in db.query(MonzaOcProveedor.id).filter(
            MonzaOcProveedor.proveedor_nombre.like(f"{MARK}%")).all()]

        db.query(MonzaContFacturaClienteItem).filter(
            MonzaContFacturaClienteItem.factura_id.in_(fac_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id.in_(fac_ids or [0])).delete(synchronize_session=S)
        for ent, ids in (("cotizacion", cot_ids), ("despacho", dsp_ids),
                         ("recepcion", rec_ids), ("embarque", emb_ids),
                         ("oc_proveedor", ocp_ids)):
            db.query(MonzaNotificacion).filter(
                MonzaNotificacion.entidad == ent,
                MonzaNotificacion.entidad_id.in_(ids or [0])).delete(synchronize_session=S)
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
        db.query(MonzaCliente).filter(
            MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


# ═════════════════════════════════════ RUN ══════════════════════════════════════

def test_ruta_compra_parcial():
    _limpiar()
    db = SessionLocal()
    try:
        cot_id, it_id = _crear_venta(db)

        print("\n───────── 1 · COMPRA PARCIAL: 1 de 3 al PROVEEDOR A ─────────")
        r = client.post("/api/monza/abastecimiento/comprar", json={
            "item_ids": [it_id], "proveedor_nombre": f"{MARK} PROV A",
            "moneda": "EUR", "tipo_origen": "internacional",
            "numero_oc": f"{MARK}-OCA",
            "cantidades": [{"item_id": it_id, "cantidad": 1}],
        })
        check("1 comprar 1 de 3 → 200", r.status_code == 200, r.text)
        j = r.json() if r.status_code == 200 else {}
        ocp_a = j.get("ocp_id")
        rem = (j.get("remanentes") or [{}])[0]
        rem_id = rem.get("remanente_item_id")
        check("1 contrato: partidos=1, comprado 1 de 3, remanente 2",
              j.get("partidos") == 1 and rem.get("comprado") == 1
              and rem.get("pendiente") == 2 and rem.get("original") == 3
              and isinstance(rem_id, int), j)
        check("1 hermana A quedó 'comprado' ×1 con SU OC",
              _linea(it_id) == ("comprado", 1, ocp_a), _linea(it_id))
        check("1 remanente quedó 'por_comprar' ×2 y SIN OC (vuelve al panel)",
              _linea(rem_id) == ("por_comprar", 2, None), _linea(rem_id))
        _familia_ok("1", cot_id)
        ids_panel = {x["id"] for x in client.get(
            "/api/monza/abastecimiento/por-comprar").json()}
        check("1 el panel /por-comprar ofrece el remanente y NO la hermana comprada",
              rem_id in ids_panel and it_id not in ids_panel, ids_panel)

        print("\n───────── 2 · EL REMANENTE (2 u) AL PROVEEDOR B — body LEGADO ─────────")
        r = client.post("/api/monza/abastecimiento/comprar", json={
            "item_ids": [rem_id], "proveedor_nombre": f"{MARK} PROV B",
            "moneda": "EUR", "tipo_origen": "internacional",
            "numero_oc": f"{MARK}-OCB",
        })
        check("2 comprar el remanente entero (vía legada) → 200", r.status_code == 200, r.text)
        ocp_b = r.json().get("ocp_id") if r.status_code == 200 else None
        check("2 hermana B quedó 'comprado' ×2 con SU OC (distinta de la de A)",
              _linea(rem_id) == ("comprado", 2, ocp_b) and ocp_b != ocp_a,
              (_linea(rem_id), ocp_a, ocp_b))
        items_a = {x["id"] for x in client.get(
            f"/api/monza/abastecimiento/ocs/{ocp_a}/items").json()}
        items_b = {x["id"] for x in client.get(
            f"/api/monza/abastecimiento/ocs/{ocp_b}/items").json()}
        check("2 ★ CERO CRUCES: la OC de A lista SOLO a la hermana A; la de B, SOLO a B",
              items_a == {it_id} and items_b == {rem_id}, (items_a, items_b))
        _familia_ok("2", cot_id)

        print("\n───────── 3 · PREPARAR LAS DOS HERMANAS (ruta internacional) ─────────")
        r = client.post("/api/monza/abastecimiento/preparar",
                        json={"item_ids": [it_id, rem_id]})
        check("3 preparar → 200 con las 2 hermanas", r.status_code == 200
              and r.json().get("preparados") == 2, r.text)
        check("3 ambas 'preparado' (cada una conserva SU OC)",
              _linea(it_id) == ("preparado", 1, ocp_a)
              and _linea(rem_id) == ("preparado", 2, ocp_b),
              (_linea(it_id), _linea(rem_id)))
        _familia_ok("3", cot_id)

        print("\n───────── 4 · LOGÍSTICA: PANTALLA AGRUPADA /preparados ─────────")
        prep = client.get("/api/monza/logistica/preparados").json()
        fila_a = next((x for x in prep if x["id"] == it_id), None)
        fila_b = next((x for x in prep if x["id"] == rem_id), None)
        check("4 las DOS hermanas aparecen en /preparados", fila_a and fila_b,
              [x.get("id") for x in prep])
        check("4 hermana A viene con SU grupo ocp (OC A, proveedor A)",
              fila_a and (fila_a.get("ocp") or {}).get("id") == ocp_a
              and (fila_a.get("ocp") or {}).get("numero_oc") == f"{MARK}-OCA",
              fila_a and fila_a.get("ocp"))
        check("4 hermana B viene con SU grupo ocp (OC B) — sin cruzarse con A",
              fila_b and (fila_b.get("ocp") or {}).get("id") == ocp_b
              and (fila_b.get("ocp") or {}).get("numero_oc") == f"{MARK}-OCB",
              fila_b and fila_b.get("ocp"))
        comp_a = (fila_a.get("ocp") or {}).get("completitud") if fila_a else None
        comp_b = (fila_b.get("ocp") or {}).get("completitud") if fila_b else None
        check("4 completitud por OC cuenta SOLO sus líneas (1 y 1, ambas 'preparado')",
              comp_a == {"total": 1, "por_estado": {"preparado": 1}}
              and comp_b == {"total": 1, "por_estado": {"preparado": 1}},
              (comp_a, comp_b))
        _familia_ok("4", cot_id)

        print("\n───────── 5 · CADA HERMANA EN SU EMBARQUE (AWB distinto) ─────────")
        r = client.post("/api/monza/logistica/embarques", json={
            "item_ids": [it_id], "awb": f"{MARK}-AWB-A"})
        check("5 embarque A creado (línea completa, sin partir)", r.status_code == 200
              and r.json().get("partidos") == 0, r.text)
        emb_a = r.json().get("id") if r.status_code == 200 else None
        r = client.post("/api/monza/logistica/embarques", json={
            "item_ids": [rem_id], "awb": f"{MARK}-AWB-B"})
        check("5 embarque B creado", r.status_code == 200
              and r.json().get("partidos") == 0, r.text)
        emb_b = r.json().get("id") if r.status_code == 200 else None
        check("5 ambas hermanas 'embarcado'",
              _linea(it_id)[0] == "embarcado" and _linea(rem_id)[0] == "embarcado",
              (_linea(it_id), _linea(rem_id)))
        for e in (emb_a, emb_b):
            client.patch(f"/api/monza/logistica/embarques/{e}",
                         json={"estado": "en_transito"})
        est = _sql("SELECT id,estado FROM monza_embarques WHERE id IN (:a,:b)",
                   a=emb_a, b=emb_b)
        check("5 embarques en tránsito", all(x[1] == "en_transito" for x in est), est)
        _familia_ok("5", cot_id)

        print("\n───────── 6 · BODEGA: RECEPCIÓN COMPLETA DE CADA HERMANA ─────────")
        rec_a = client.post(f"/api/monza/bodega/embarques/{emb_a}/recibir").json()["recepcion_id"]
        r = client.patch(f"/api/monza/bodega/recepciones/{rec_a}/items/{it_id}",
                         json={"estado_recepcion": "completo", "qty_recibida": 1})
        check("6 marcar hermana A 'completo' ×1 (la qty de la HERMANA, no la venta)",
              r.status_code == 200, r.text)
        r = client.post(f"/api/monza/bodega/recepciones/{rec_a}/cerrar",
                        json={"forzar": False})
        check("6 cierre recepción A: 1 a bodega, CERO reclamos (sin faltante fantasma "
              "por las 2 u que viajan aparte)",
              r.status_code == 200 and r.json().get("en_bodega") == 1
              and r.json().get("reclamos") == 0, r.text)
        check("6 hermana A 'en_bodega'; hermana B sigue 'embarcado' (sin cruce)",
              _linea(it_id)[0] == "en_bodega" and _linea(rem_id)[0] == "embarcado",
              (_linea(it_id), _linea(rem_id)))
        rec_b = client.post(f"/api/monza/bodega/embarques/{emb_b}/recibir").json()["recepcion_id"]
        client.patch(f"/api/monza/bodega/recepciones/{rec_b}/items/{rem_id}",
                     json={"estado_recepcion": "completo", "qty_recibida": 2})
        r = client.post(f"/api/monza/bodega/recepciones/{rec_b}/cerrar",
                        json={"forzar": False})
        check("6 cierre recepción B: 1 línea a bodega, cero reclamos",
              r.status_code == 200 and r.json().get("en_bodega") == 1
              and r.json().get("reclamos") == 0, r.text)
        check("6 ambas hermanas 'en_bodega' y CERO reclamos en toda la familia",
              _linea(it_id)[0] == "en_bodega" and _linea(rem_id)[0] == "en_bodega"
              and _reclamos_familia([it_id, rem_id]) == 0,
              (_linea(it_id), _linea(rem_id), _reclamos_familia([it_id, rem_id])))
        _familia_ok("6", cot_id)

        print("\n───────── 7 · DESPACHO DE CADA HERMANA (tope físico por línea) ─────────")
        r = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": cot_id, "items": [{"item_id": it_id, "qty": 1}]})
        check("7 despacho A (×1) creado en preparación", r.status_code == 200, r.text)
        d_a = r.json().get("id") if r.status_code == 200 else None
        r = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": cot_id, "items": [{"item_id": rem_id, "qty": 2}]})
        check("7 despacho B (×2) creado", r.status_code == 200, r.text)
        d_b = r.json().get("id") if r.status_code == 200 else None
        r = client.post("/api/monza/despachos/crear", json={
            "cotizacion_id": cot_id, "items": [{"item_id": it_id, "qty": 1}]})
        check("7 el cupo de la hermana A quedó reservado: un 2º despacho ×1 → 400",
              r.status_code == 400, r.text)
        r = client.post(f"/api/monza/despachos/entidades/{d_a}/cerrar")
        check("7 cerrar despacho A → hermana A 'despachado', B sigue 'en_bodega', "
              "la VENTA aún no voltea",
              r.status_code == 200 and _linea(it_id)[0] == "despachado"
              and _linea(rem_id)[0] == "en_bodega"
              and _sql("SELECT estado FROM monza_cotizaciones WHERE id=:i",
                       i=cot_id)[0][0] == "vendida",
              (r.text, _linea(it_id), _linea(rem_id)))
        r = client.post(f"/api/monza/despachos/entidades/{d_b}/cerrar")
        check("7 cerrar despacho B → familia completa 'despachado' y la VENTA voltea",
              r.status_code == 200 and _linea(rem_id)[0] == "despachado"
              and _sql("SELECT estado FROM monza_cotizaciones WHERE id=:i",
                       i=cot_id)[0][0] == "despachado",
              (r.text, _linea(rem_id)))
        _familia_ok("7", cot_id)

        print("\n───────── 8 · GUÍA FIRMADA: EL GATE Y LA FIRMA REAL ─────────")
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_id, "despacho_id": d_a,
            "numero_factura": f"{MARK}-F0", "plazo_dias": 3650})
        check("8 facturar sin firma → 400 'no está FIRMADA' (gate 2026-08-06)",
              r.status_code == 400 and "FIRMADA" in r.text, r.text)
        hoy = _hoy_chile_str()
        r = client.post(f"/api/monza/despachos/entidades/{d_a}/firmar",
                        files={"file": ("guia-a.jpg", b"foto-a", "image/jpeg")},
                        data={"fecha_firma": hoy, "numero_guia": f"G-{MARK}-A"})
        check("8 firmar guía A (multipart real, con su N° de guía)", r.status_code == 200, r.text)
        r = client.post(f"/api/monza/despachos/entidades/{d_b}/firmar",
                        files={"file": ("guia-b.jpg", b"foto-b", "image/jpeg")},
                        data={"fecha_firma": hoy, "numero_guia": f"G-{MARK}-B"})
        check("8 firmar guía B", r.status_code == 200, r.text)
        fact_dsp = {x["id"]: x for x in client.get(
            f"/api/monza/contabilidad/ventas/{cot_id}/despachos-facturables").json()}
        check("8 despachos-facturables: A facturable 1.0 y B facturable 2.0, "
              "ambos con SU guía firmada",
              fact_dsp.get(d_a, {}).get("facturable") == 1.0
              and fact_dsp.get(d_a, {}).get("guia_firmada") is True
              and fact_dsp.get(d_a, {}).get("numero_guia") == f"G-{MARK}-A"
              and fact_dsp.get(d_b, {}).get("facturable") == 2.0
              and fact_dsp.get(d_b, {}).get("guia_firmada") is True
              and fact_dsp.get(d_b, {}).get("numero_guia") == f"G-{MARK}-B",
              fact_dsp)
        _familia_ok("8", cot_id)

        print("\n───────── 9 · FACTURAR CADA GUÍA: SU CANTIDAD × SU PRECIO ─────────")
        res = _resumen(cot_id)
        check("9 por_facturar FÍSICO antes de facturar == total venta (357.000)",
              res["por_facturar_clp"] == BRUTO_VENTA
              and res["mercaderia_pendiente_clp"] == BRUTO_VENTA, res)
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_id, "sin_guia": True,
            "numero_factura": f"{MARK}-FR", "plazo_dias": 3650})
        check("9 ★ NETEO POR CANAL: el retiro en oficina NO puede robarle el cupo a "
              "las guías (409, todo está comprometido en despachos)",
              r.status_code == 409 and "guías" in r.text, r.text)
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_id, "despacho_id": d_a,
            "numero_factura": f"{MARK}-F1", "plazo_dias": 3650})
        check("9 factura de la guía A emitida", r.status_code == 200, r.text)
        f1 = r.json() if r.status_code == 200 else {}
        lin1 = f1.get("items") or []
        check("9 F1 = 1 línea, ×1, del ÍTEM HERMANA A, a $100.000 → bruto 119.000",
              f1.get("monto_bruto") == 119_000.0 and len(lin1) == 1
              and lin1[0].get("item_cotizacion_id") == it_id
              and float(lin1[0].get("cantidad") or 0) == 1.0, f1)
        check("9 F1 referencia SOLO la guía A", f1.get("despacho_id") == d_a
              and f1.get("numero_guia") == f"G-{MARK}-A", f1)
        res = _resumen(cot_id)
        check("9 por_facturar tras F1 == 238.000 (las 2 u de B × precio congelado)",
              res["por_facturar_clp"] == 238_000.0, res)
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_id, "despacho_id": d_b,
            "numero_factura": f"{MARK}-F2", "plazo_dias": 3650})
        check("9 factura de la guía B emitida", r.status_code == 200, r.text)
        f2 = r.json() if r.status_code == 200 else {}
        lin2 = f2.get("items") or []
        check("9 F2 = 1 línea, ×2, del ÍTEM HERMANA B → bruto 238.000, guía B",
              f2.get("monto_bruto") == 238_000.0 and len(lin2) == 1
              and lin2[0].get("item_cotizacion_id") == rem_id
              and float(lin2[0].get("cantidad") or 0) == 2.0
              and f2.get("despacho_id") == d_b
              and f2.get("numero_guia") == f"G-{MARK}-B", f2)
        check("9 ★ Σ FACTURADO == TOTAL DE LA VENTA (119.000 + 238.000 = 357.000)",
              (f1.get("monto_bruto") or 0) + (f2.get("monto_bruto") or 0) == BRUTO_VENTA,
              (f1.get("monto_bruto"), f2.get("monto_bruto")))

        print("\n───────── 10 · CIERRE: NADA SOBRA, NADA FALTA ─────────")
        res = _resumen(cot_id)
        check("10 por_facturar == 0 y mercadería pendiente == 0 POR CONSTRUCCIÓN",
              res["por_facturar_clp"] == 0 and res["mercaderia_pendiente_clp"] == 0, res)
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_id, "despacho_id": d_a,
            "numero_factura": f"{MARK}-F3", "plazo_dias": 3650})
        check("10 re-facturar la guía A → 409 (ya facturada por completo)",
              r.status_code == 409, r.text)
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_id, "sin_guia": True,
            "numero_factura": f"{MARK}-F4", "plazo_dias": 3650})
        check("10 retiro en oficina al final → 409 (la venta ya está facturada)",
              r.status_code == 409, r.text)
        _familia_ok("10", cot_id)
        check("10 la familia terminó en 2 hermanas exactas (1 y 2), sin clones extra",
              sorted((h["cantidad"], h["estado"]) for h in _lineas(cot_id))
              == [(1, "despachado"), (2, "despachado")], _lineas(cot_id))

        print()
        assert not _fails, f"{len(_fails)} fallos: {_fails}"
    finally:
        db.close()
        _limpiar()


if __name__ == "__main__":
    test_ruta_compra_parcial()
    print("\nTODO OK" if not _fails else f"\n{len(_fails)} FALLOS")
