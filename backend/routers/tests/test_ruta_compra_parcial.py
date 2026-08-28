"""RUTA COMPLETA de una línea PARTIDA por la compra parcial (Grupo AM / MachParts).

Pregunta del dueño: la asignación PARCIAL a OC de proveedor (commit 1d2a069: una
línea de 3 se parte en 1 comprada al proveedor A + 2 remanentes) ¿tiene problemas
EN EL RESTO DEL VIAJE? Esta suite recorre el pipeline REAL por API con la línea
partida y verifica en CADA estación los invariantes de la familia:

  §1  Venta cerrada (1 línea × 3) → asignación parcial: 1 unidad a la OCP A.
      Hermana comprada (1) + remanente 'cerrado' (2); Σ cantidad 3; Σ total 300;
      cabecera intacta; el remanente sin vínculo.
  §2  El remanente (2) se asigna ENTERO a la OCP B (camino legado, sin clon).
      Bandeja Seguimiento (/items/comprados): la hermana A SOLO bajo la OCP A y
      la B SOLO bajo la OCP B — jamás cruzadas.
  §3  Preparar ambas hermanas → 'preparado'; la bandeja /items/preparados trae a
      cada una con SU oc_proveedor_id.
  §4  UN pre-embarque consolidado con las 2 hermanas + cierre con INVOX por OCP →
      embarque real con EmbarqueItem.oc_proveedor_id correcto por hermana y
      factura_comercial "OCP-A: INV-A, OCP-B: INV-B".
  §5  Embarque → en_transito (PUT embarques-list) → recepción en bodega marcando
      'completo' 1 y 2 → ambas en_bodega con qty_disponible = SU cantidad.
  §6  Despacho por hermana. El tope es el de CADA hermana (despachar 2 de la A → 400,
      la familia no presta unidades). Cerrar ambos → líneas 'despachado', embarque
      'despachado'. Cada despacho contiene SOLO su hermana.
  §7  CRUCE con la firma parcial: guía A (qty 1) firmada COMPLETA; guía B (qty 2)
      firmada PARCIAL 1 de 2 con motivo (faltante declarado 1).
  §8  Facturación: la factura de A trae SU cantidad (1) con SU precio congelado;
      la de B trae lo FIRMADO (1, no 2). Σ facturado == Σ vendido − faltante.
      por_facturar honesto (0 con faltante_declarado 1); el selector se vacía;
      re-derivar o facturar el faltante por ítems explícitos → 409.
  §9  Invariantes finales: la familia sigue sumando 3 unidades y $300 de compra;
      2 hermanas, 2 vínculos (A y B), cero cruces.

Precios de venta DETERMINISTAS: se parchea cont._precios_de_cotizacion (patrón
test_firma_parcial.py) con $100.000/unidad para poder afirmar montos exactos.

Datos MARCADOS (MARK sin guiones bajos: en LIKE el _ es comodín) y limpieza
FK-segura verificada con SESIÓN NUEVA. Requiere la BD local (como las suites vecinas).

Corre con:  ./venv/bin/python -m pytest routers/tests/test_ruta_compra_parcial.py -q
(también:   ./venv/bin/python routers/tests/test_ruta_compra_parcial.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, OcProveedor, OcProveedorItem,
    PreEmbarque, PreEmbarqueItem, Embarque, EmbarqueItem,
    RecepcionEmbarque, RecepcionEmbarqueItem, ReclamoProveedor,
    Despacho, DespachoItem, Notificacion, User,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContFactoring,
)
# PIN del módulo raíz `notificaciones` (mismo patrón que test_asignacion_parcial.py):
# con routers/ en sys.path, el ROUTER homónimo routers/notificaciones.py puede sombrear
# al módulo raíz que trae crear_notificacion (el split, cerrar despacho y firmar lo usan).
import notificaciones as _notif_raiz  # noqa: E402
assert hasattr(_notif_raiz, "crear_notificacion"), (
    f"el módulo notificaciones resolvió a {_notif_raiz.__file__} (sombra del router)")

import routers.contabilidad as cont  # noqa: E402
from routers.compras import router as compras_router  # noqa: E402
from routers.bodega import router as bodega_router  # noqa: E402
from routers.despachos import router as despachos_router  # noqa: E402

MARK = "ZZRUTACOMPARC"  # SIN guiones bajos: en LIKE el _ es comodín
CURRENT = {"empresa": "mineria", "id": None}
PRECIO_VENTA_UNIT = 100000.0  # $/unidad, determinista vía el parche de precios

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(compras_router, prefix="/api")
app.include_router(bodega_router, prefix="/api")
app.include_router(despachos_router, prefix="/api")
app.include_router(cont.router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    """Precio de venta plano $100.000/unidad para TODA hermana de la cotización.
    Clave: mapea por id VIVO (las hermanas nacen con ids nuevos en cada split)."""
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {}
    neto = 0.0
    for i in items:
        tot = cont._total_linea(PRECIO_VENTA_UNIT, float(i.cantidad or 0))
        pmap[i.id] = {"id": i.id, "precio_venta_clp": PRECIO_VENTA_UNIT,
                      "total_venta_clp": tot}
        neto += tot
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Helpers de lectura con SESIÓN NUEVA ──────────────────────────────────────────

def _fresco(item_id):
    db = SessionLocal()
    try:
        return db.query(ItemCotizacion).filter(ItemCotizacion.id == item_id).first()
    finally:
        db.close()


def _familia(cot_id):
    """Todas las hermanas de la línea (sesión nueva) + sus vínculos OCP."""
    db = SessionLocal()
    try:
        items = (db.query(ItemCotizacion)
                 .filter(ItemCotizacion.cotizacion_id == cot_id,
                         ItemCotizacion.numero_parte == f"{MARK}-P1")
                 .order_by(ItemCotizacion.id.asc()).all())
        asigs = (db.query(OcProveedorItem)
                 .filter(OcProveedorItem.item_cotizacion_id.in_(
                     [i.id for i in items] or [0])).all())
        return items, asigs
    finally:
        db.close()


def _check_familia(etiqueta, cot_id, esperadas=None):
    """El invariante de CADA estación: Σ cantidad == 3 y Σ total compra == 300."""
    items, _ = _familia(cot_id)
    suma_qty = sum(float(i.cantidad or 0) for i in items)
    suma_total = sum(float(i.total_cotizacion or 0) for i in items)
    ok = suma_qty == 3 and suma_total == 300
    if esperadas is not None:
        ok = ok and len(items) == esperadas
    check(f"{etiqueta}: Σ cantidad familia == 3 y Σ total compra == $300",
          ok, (suma_qty, suma_total, len(items),
               [(i.id, i.estado_item, i.cantidad) for i in items]))


def _grupo_de(bandeja, ocp_id):
    return next((g for g in bandeja if g.get("oc_proveedor_id") == ocp_id), None)


def _ids_en_grupo(grupo):
    return {d.get("id") for d in (grupo or {}).get("items", [])}


# ── Fábricas de datos MARCADOS ────────────────────────────────────────────────────

def _escenario(db):
    """Venta cerrada: cotización + OC-Cliente + UNA línea de cantidad 3 'cerrado'."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} Cliente",
                     rut_cliente="78.279.030-7", user_id=CURRENT["id"])
    db.add(cot)
    db.flush()
    it = ItemCotizacion(
        cotizacion_id=cot.id, item_num=1, numero_parte=f"{MARK}-P1",
        descripcion=f"{MARK} pieza unica", marca="CAT", cantidad=3.0,
        precio_unit_cotizacion=100.0, total_cotizacion=300.0,
        estado_item="cerrado", peso_unit_lbs=2.0)
    db.add(it)
    db.flush()
    occ = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC",
                    fecha_oc="2026-08-01")
    db.add(occ)
    db.commit()
    for o in (cot, it, occ):
        db.refresh(o)
    return cot, it, occ


def _ocp(db, sufijo):
    o = OcProveedor(numero=f"{MARK}-OCP-{sufijo}", proveedor=f"{MARK} prov {sufijo}")
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _asignar(ocp_id, occ_id, items):
    return client.post(f"/api/compras/oc-proveedor/{ocp_id}/items-parcial",
                       json={"oc_cliente_id": occ_id, "items": items})


# ── Limpieza FK-segura ───────────────────────────────────────────────────────────

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
        # Facturas (cobranzas/factoring/líneas primero)
        if oc_ids:
            fac_ids = [f.id for f in db.query(ContFacturaCliente)
                       .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
            if fac_ids:
                db.query(ContCobranza).filter(
                    ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(ContFactoring).filter(
                    ContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(ContFacturaClienteItem).filter(
                    ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(ContFacturaCliente).filter(
                    ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
            # Despachos (numero autogenerado DSP-…: se ubican por la OC)
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
        if item_ids:
            db.query(ReclamoProveedor).filter(
                ReclamoProveedor.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=False)
            # Embarques y recepciones (números autogenerados: se ubican por los ítems)
            ei_rows = db.query(EmbarqueItem).filter(
                EmbarqueItem.item_cotizacion_id.in_(item_ids)).all()
            emb_ids = list({e.embarque_id for e in ei_rows})
            if ei_rows:
                db.query(RecepcionEmbarqueItem).filter(
                    RecepcionEmbarqueItem.embarque_item_id.in_(
                        [e.id for e in ei_rows])).delete(synchronize_session=False)
                db.query(EmbarqueItem).filter(
                    EmbarqueItem.id.in_([e.id for e in ei_rows])).delete(synchronize_session=False)
            if emb_ids:
                db.query(RecepcionEmbarque).filter(
                    RecepcionEmbarque.embarque_id.in_(emb_ids)).delete(synchronize_session=False)
                db.query(Embarque).filter(
                    Embarque.id.in_(emb_ids)).delete(synchronize_session=False)
            # Pre-embarques (ídem: por los ítems; el Embarque que los referencia ya cayó)
            pei_rows = db.query(PreEmbarqueItem).filter(
                PreEmbarqueItem.item_cotizacion_id.in_(item_ids)).all()
            pre_ids = list({p.pre_embarque_id for p in pei_rows})
            if pei_rows:
                db.query(PreEmbarqueItem).filter(
                    PreEmbarqueItem.id.in_([p.id for p in pei_rows])).delete(synchronize_session=False)
            if pre_ids:
                db.query(PreEmbarque).filter(
                    PreEmbarque.id.in_(pre_ids)).delete(synchronize_session=False)
            db.query(OcProveedorItem).filter(
                OcProveedorItem.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=False)
        if oc_ids:
            db.query(OcProveedorItem).filter(
                OcProveedorItem.oc_cliente_id.in_(oc_ids)).delete(synchronize_session=False)
            db.query(OcCliente).filter(
                OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
        if cot_ids:
            db.query(ItemCotizacion).filter(
                ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(Cotizacion).filter(
                Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        ocp_ids = [o.id for o in db.query(OcProveedor)
                   .filter(OcProveedor.numero.like(f"{MARK}%")).all()]
        if ocp_ids:
            db.query(Notificacion).filter(
                Notificacion.entidad_tipo == "oc_proveedor",
                Notificacion.entidad_id.in_(ocp_ids)).delete(synchronize_session=False)
            db.query(OcProveedor).filter(
                OcProveedor.id.in_(ocp_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.email.like(f"{MARK}%")).delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (
            db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
            + db.query(OcProveedor).filter(OcProveedor.numero.like(f"{MARK}%")).count()
            + db.query(ContFacturaCliente)
              .filter(ContFacturaCliente.numero_factura.like(f"{MARK}%")).count()
            + db.query(User).filter(User.email.like(f"{MARK}%")).count()
        )
        assert restos == 0, f"limpieza incompleta: quedan {restos} filas {MARK}"
        print("Limpieza verificada con sesión nueva: 0 restos")
    finally:
        db.close()


# ═════════════════════════════════════════════════════════════════════════════════

def run():
    cont._precios_de_cotizacion = _fake_precios
    _limpiar()
    docs_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "uploads", "docs"))
    os.makedirs(docs_dir, exist_ok=True)
    archivo = f"{MARK}-guia.pdf"
    with open(os.path.join(docs_dir, archivo), "wb") as fh:
        fh.write(b"%PDF-1.4 dummy")
    db = SessionLocal()
    try:
        CURRENT["empresa"] = "mineria"
        user = User(email=f"{MARK}@test.invalid", nombre=MARK,
                    hashed_password="x", is_active=1, empresa="mineria")
        db.add(user)
        db.commit()
        db.refresh(user)
        CURRENT["id"] = user.id

        cot, it, occ = _escenario(db)
        total_items_antes = cot.total_items
        ocp_a = _ocp(db, "A")
        ocp_b = _ocp(db, "B")

        # ── §1 · ASIGNACIÓN PARCIAL: 1 unidad a la OCP A ────────────────────────
        r = _asignar(ocp_a.id, occ.id, [{"item_id": it.id, "cantidad": 1,
                                         "plazo_dias_prov": 30}])
        check("1a split 1 de 3 a la OCP A → 200 con 1 partición",
              r.status_code == 200 and r.json().get("asignados") == 1
              and len(r.json().get("particiones", [])) == 1, r.text[:300])
        part = (r.json().get("particiones") or [{}])[0]
        id_a = it.id                                   # hermana comprada (A)
        id_b = part.get("item_id_remanente")           # remanente (después: B)
        fa, fb = _fresco(id_a), _fresco(id_b) if id_b else None
        check("1b hermana A 'comprado' qty 1 total $100; remanente 'cerrado' qty 2 total $200",
              fa is not None and fa.estado_item == "comprado" and fa.cantidad == 1
              and fa.total_cotizacion == 100 and fb is not None
              and fb.estado_item == "cerrado" and fb.cantidad == 2
              and fb.total_cotizacion == 200,
              (fa and (fa.estado_item, fa.cantidad, fa.total_cotizacion),
               fb and (fb.estado_item, fb.cantidad, fb.total_cotizacion)))
        check("1c precio unitario IDÉNTICO en ambas mitades ($100, jamás prorrateado)",
              fa.precio_unit_cotizacion == 100 and fb is not None
              and fb.precio_unit_cotizacion == 100,
              (fa.precio_unit_cotizacion, fb and fb.precio_unit_cotizacion))
        _check_familia("1d tras el split", cot.id, esperadas=2)
        db.rollback()
        cot_fresca = db.query(Cotizacion).filter(Cotizacion.id == cot.id).first()
        check("1e cabecera de la cotización INTACTA (total_items sin tocar)",
              cot_fresca.total_items == total_items_antes,
              (cot_fresca.total_items, total_items_antes))
        db_n = SessionLocal()
        try:
            n_vinc_a = (db_n.query(OcProveedorItem)
                        .filter(OcProveedorItem.item_cotizacion_id == id_a).count())
            n_vinc_b = (db_n.query(OcProveedorItem)
                        .filter(OcProveedorItem.item_cotizacion_id == id_b).count())
        finally:
            db_n.close()
        check("1f la hermana A tiene SU vínculo y el remanente CERO (vuelve al panel)",
              n_vinc_a == 1 and n_vinc_b == 0, (n_vinc_a, n_vinc_b))

        # ── §2 · EL REMANENTE ENTERO A LA OCP B + bandeja Seguimiento ───────────
        r = _asignar(ocp_b.id, occ.id, [{"item_id": id_b, "cantidad": 2,
                                         "plazo_dias_prov": 45}])
        check("2a remanente (2) asignado ENTERO a la OCP B → 200 sin clon "
              "(cantidad == cantidad de la línea)",
              r.status_code == 200 and not r.json().get("particiones"), r.text[:300])
        fb = _fresco(id_b)
        check("2b hermana B 'comprado' qty 2 total $200",
              fb is not None and fb.estado_item == "comprado" and fb.cantidad == 2
              and fb.total_cotizacion == 200,
              fb and (fb.estado_item, fb.cantidad, fb.total_cotizacion))
        _check_familia("2c tras asignar a B", cot.id, esperadas=2)
        items_fam, asigs = _familia(cot.id)
        check("2d dos vínculos y cada hermana con SU proveedor (A→A, B→B)",
              len(asigs) == 2
              and {(a.item_cotizacion_id, a.oc_proveedor_id) for a in asigs}
              == {(id_a, ocp_a.id), (id_b, ocp_b.id)},
              [(a.item_cotizacion_id, a.oc_proveedor_id) for a in asigs])

        r = client.get("/api/compras/items/comprados")
        check("2e GET bandeja Seguimiento → 200", r.status_code == 200, r.text[:200])
        bandeja = r.json() if r.status_code == 200 else []
        ids_grupo_a = _ids_en_grupo(_grupo_de(bandeja, ocp_a.id))
        ids_grupo_b = _ids_en_grupo(_grupo_de(bandeja, ocp_b.id))
        check("2f bandeja: la hermana A SOLO bajo la OCP A (jamás en la de B)",
              id_a in ids_grupo_a and id_a not in ids_grupo_b,
              (sorted(ids_grupo_a & {id_a, id_b}), sorted(ids_grupo_b & {id_a, id_b})))
        check("2g bandeja: la hermana B SOLO bajo la OCP B (jamás en la de A)",
              id_b in ids_grupo_b and id_b not in ids_grupo_a,
              (sorted(ids_grupo_a & {id_a, id_b}), sorted(ids_grupo_b & {id_a, id_b})))

        # ── §3 · PREPARAR ambas hermanas ────────────────────────────────────────
        r = client.post("/api/compras/items/preparar", json={"item_ids": [id_a, id_b]})
        check("3a preparar ambas hermanas → 200 con updated 2",
              r.status_code == 200 and r.json().get("updated") == 2, r.text[:200])
        fa, fb = _fresco(id_a), _fresco(id_b)
        check("3b ambas 'preparado' y las cantidades no se movieron (1 y 2)",
              fa.estado_item == "preparado" and fb.estado_item == "preparado"
              and fa.cantidad == 1 and fb.cantidad == 2,
              ((fa.estado_item, fa.cantidad), (fb.estado_item, fb.cantidad)))
        _check_familia("3c tras preparar", cot.id, esperadas=2)
        r = client.get("/api/compras/items/preparados")
        prep = {d["id"]: d for d in (r.json() if r.status_code == 200 else [])
                if d.get("id") in (id_a, id_b)}
        check("3d bandeja preparados: cada hermana con SU oc_proveedor_id (sin cruces)",
              r.status_code == 200 and len(prep) == 2
              and prep[id_a]["oc_proveedor_id"] == ocp_a.id
              and prep[id_b]["oc_proveedor_id"] == ocp_b.id,
              {k: v.get("oc_proveedor_id") for k, v in prep.items()})

        # ── §4 · PRE-EMBARQUE consolidado + cierre → EMBARQUE ───────────────────
        r = client.post("/api/compras/pre-embarques",
                        json={"item_ids": [id_a, id_b], "notas": f"{MARK} consolidado"})
        check("4a pre-embarque con las 2 hermanas → 201", r.status_code == 201,
              (r.status_code, r.text[:200]))
        pre_id = r.json().get("id")
        fa, fb = _fresco(id_a), _fresco(id_b)
        check("4b ambas 'pre_embarcado'",
              fa.estado_item == "pre_embarcado" and fb.estado_item == "pre_embarcado",
              (fa.estado_item, fb.estado_item))
        r = client.post(f"/api/compras/pre-embarques/{pre_id}/cerrar", json={
            "awb_numero": f"{MARK}-AWB-1",
            "invox_items": [
                {"oc_proveedor_id": ocp_a.id, "numero_invox": f"{MARK}-INV-A"},
                {"oc_proveedor_id": ocp_b.id, "numero_invox": f"{MARK}-INV-B"},
            ]})
        check("4c cerrar pre-embarque → 201 con embarque nuevo",
              r.status_code == 201 and r.json().get("id"), (r.status_code, r.text[:200]))
        emb_id = r.json().get("id")
        fa, fb = _fresco(id_a), _fresco(id_b)
        check("4d ambas 'embarcado' con cantidades intactas (1 y 2, cierre sin split)",
              fa.estado_item == "embarcado" and fb.estado_item == "embarcado"
              and fa.cantidad == 1 and fb.cantidad == 2,
              ((fa.estado_item, fa.cantidad), (fb.estado_item, fb.cantidad)))
        _check_familia("4e tras embarcar", cot.id, esperadas=2)
        db_n = SessionLocal()
        try:
            emb_row = db_n.query(Embarque).filter(Embarque.id == emb_id).first()
            eis = (db_n.query(EmbarqueItem)
                   .filter(EmbarqueItem.embarque_id == emb_id).all())
            ei_map = {e.item_cotizacion_id: e for e in eis}
        finally:
            db_n.close()
        check("4f EmbarqueItem conserva la OCP de CADA hermana (A→A, B→B)",
              len(eis) == 2 and ei_map[id_a].oc_proveedor_id == ocp_a.id
              and ei_map[id_b].oc_proveedor_id == ocp_b.id,
              {k: v.oc_proveedor_id for k, v in ei_map.items()})
        check("4g factura_comercial nombra el INVOX de cada OCP",
              f"{MARK}-INV-A" in (emb_row.factura_comercial or "")
              and f"{MARK}-INV-B" in (emb_row.factura_comercial or ""),
              emb_row.factura_comercial)

        # ── §5 · TRÁNSITO + RECEPCIÓN EN BODEGA ─────────────────────────────────
        r = client.put(f"/api/compras/embarques-list/{emb_id}",
                       json={"estado": "en_transito"})
        check("5a embarque a 'en_transito' (paso manual real) → 200",
              r.status_code == 200, r.text[:200])
        r = client.post(f"/api/bodega/embarques/{emb_id}/recibir")
        check("5b abrir recepción → 200/201", r.status_code in (200, 201), r.text[:200])
        rec_id = r.json().get("recepcion_id") or r.json().get("id")
        for ei_id, qty in ((ei_map[id_a].id, 1), (ei_map[id_b].id, 2)):
            r = client.patch(f"/api/bodega/recepciones/{rec_id}/items/0",
                             json={"embarque_item_id": ei_id,
                                   "estado_recepcion": "completo",
                                   "qty_recibida": qty})
            check(f"5c marcar 'completo' qty {qty} → 200", r.status_code == 200,
                  r.text[:200])
        r = client.post(f"/api/bodega/recepciones/{rec_id}/cerrar", json={"forzar": False})
        check("5d cerrar recepción → 200", r.status_code == 200, r.text[:200])
        fa, fb = _fresco(id_a), _fresco(id_b)
        check("5e ambas 'en_bodega' sin reclamos fantasma",
              fa.estado_item == "en_bodega" and fb.estado_item == "en_bodega",
              (fa.estado_item, fb.estado_item))
        db_n = SessionLocal()
        try:
            n_recl = (db_n.query(ReclamoProveedor)
                      .filter(ReclamoProveedor.item_cotizacion_id.in_([id_a, id_b]))
                      .count())
        finally:
            db_n.close()
        check("5f cero reclamos (la recepción fue completa por hermana)",
              n_recl == 0, n_recl)
        r = client.get(f"/api/despachos/oc-clientes/{occ.id}")
        det = {x["id"]: x for x in r.json().get("items", [])} if r.status_code == 200 else {}
        check("5g Despachos ve a CADA hermana con SU disponible (A:1, B:2)",
              r.status_code == 200 and det.get(id_a, {}).get("qty_disponible") == 1
              and det.get(id_b, {}).get("qty_disponible") == 2,
              {k: v.get("qty_disponible") for k, v in det.items()})
        _check_familia("5h tras la recepción", cot.id, esperadas=2)

        # ── §6 · DESPACHO POR HERMANA (el tope es el de CADA una) ───────────────
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": occ.id,
            "items": [{"item_cotizacion_id": id_a, "qty_despachada": 2}]})
        check("6a despachar 2 de la hermana A (qty 1) → 400 "
              "(la familia NO presta unidades entre hermanas)",
              r.status_code == 400, (r.status_code, r.text[:200]))
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": occ.id,
            "items": [{"item_cotizacion_id": id_a, "qty_despachada": 1}]})
        check("6b despacho de la hermana A (1) → 200", r.status_code == 200,
              r.text[:200])
        desp_a = r.json().get("id")
        r = client.post("/api/despachos/", json={
            "oc_cliente_id": occ.id,
            "items": [{"item_cotizacion_id": id_b, "qty_despachada": 2}]})
        check("6c despacho de la hermana B (2) → 200", r.status_code == 200,
              r.text[:200])
        desp_b = r.json().get("id")
        for did, iid, nombre in ((desp_a, id_a, "A"), (desp_b, id_b, "B")):
            j = client.get(f"/api/despachos/{did}").json()
            check(f"6d despacho {nombre} contiene SOLO su hermana",
                  [x["item_cotizacion_id"] for x in j.get("items", [])] == [iid], j)
        r = client.post(f"/api/despachos/{desp_a}/cerrar")
        check("6e cerrar despacho A → 200", r.status_code == 200, r.text[:200])
        fa, fb = _fresco(id_a), _fresco(id_b)
        check("6f A 'despachado', B sigue 'en_bodega' (cada hermana con su vida)",
              fa.estado_item == "despachado" and fb.estado_item == "en_bodega",
              (fa.estado_item, fb.estado_item))
        db_n = SessionLocal()
        try:
            emb_estado = db_n.query(Embarque.estado).filter(Embarque.id == emb_id).scalar()
        finally:
            db_n.close()
        check("6g el embarque ESPERA a la hermana B (sigue en_bodega)",
              emb_estado == "en_bodega", emb_estado)
        r = client.post(f"/api/despachos/{desp_b}/cerrar")
        check("6h cerrar despacho B → 200", r.status_code == 200, r.text[:200])
        fb = _fresco(id_b)
        db_n = SessionLocal()
        try:
            emb_estado = db_n.query(Embarque.estado).filter(Embarque.id == emb_id).scalar()
        finally:
            db_n.close()
        check("6i B 'despachado' y el embarque completa a 'despachado'",
              fb.estado_item == "despachado" and emb_estado == "despachado",
              (fb.estado_item, emb_estado))
        _check_familia("6j tras despachar", cot.id, esperadas=2)

        # ── §7 · FIRMA: A completa, B PARCIAL 1 de 2 (cruce con la feature nueva) ─
        r = client.post(f"/api/despachos/{desp_a}/firmar",
                        json={"numero_guia": f"{MARK}-GUIA-A", "archivo": archivo})
        check("7a guía A (qty 1) firmada COMPLETA → 200", r.status_code == 200,
              r.text[:200])
        db_n = SessionLocal()
        try:
            di_b = (db_n.query(DespachoItem)
                    .filter(DespachoItem.despacho_id == desp_b).first())
            di_b_id = di_b.id
        finally:
            db_n.close()
        motivo = f"{MARK} una caja perdida por el courier"
        r = client.post(f"/api/despachos/{desp_b}/firmar",
                        json={"numero_guia": f"{MARK}-GUIA-B", "archivo": archivo,
                              "items": [{"despacho_item_id": di_b_id, "qty_firmada": 1}],
                              "motivo_faltante": motivo})
        check("7b guía B (qty 2) firmada PARCIAL 1 de 2 con motivo → 200 "
              "y faltante_total 1",
              r.status_code == 200 and r.json().get("faltante_total") == 1,
              (r.status_code, r.text[:300]))
        j = client.get(f"/api/despachos/{desp_b}").json()
        check("7c GET guía B: qty_firmada 1 de 2 y el motivo en cabecera",
              j.get("items", [{}])[0].get("qty_firmada") == 1
              and j.get("faltante_motivo") == motivo, j)
        _check_familia("7d tras las firmas (la firma no toca cantidades)",
                       cot.id, esperadas=2)

        # ── §8 · FACTURACIÓN por guía + por_facturar honesto ────────────────────
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": occ.id, "despacho_id": desp_a,
                              "numero_factura": f"{MARK}-F-A", "plazo_dias": 30})
        check("8a factura de la guía A → 200 con UNA línea qty 1 × $100.000",
              r.status_code == 200 and len(r.json().get("items", [])) == 1
              and float(r.json()["items"][0]["cantidad"]) == 1.0
              and float(r.json()["items"][0]["precio_unit_neto"]) == PRECIO_VENTA_UNIT,
              (r.status_code, r.text[:300]))
        fac_a = r.json() if r.status_code == 200 else {}
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": occ.id, "despacho_id": desp_b,
                              "numero_factura": f"{MARK}-F-B", "plazo_dias": 30})
        check("8b factura de la guía B → 200 con lo FIRMADO (1, no las 2 despachadas)",
              r.status_code == 200 and len(r.json().get("items", [])) == 1
              and float(r.json()["items"][0]["cantidad"]) == 1.0,
              (r.status_code, r.text[:300]))
        fac_b = r.json() if r.status_code == 200 else {}
        neto_esperado = PRECIO_VENTA_UNIT  # 1 unidad por factura
        check("8c cada factura congela SU neto ($100.000), su IVA (19%) y el bruto",
              abs(float(fac_a.get("monto_neto") or 0) - neto_esperado) <= 1
              and abs(float(fac_b.get("monto_neto") or 0) - neto_esperado) <= 1
              and abs(float(fac_a.get("iva") or 0) - round(neto_esperado * 0.19)) <= 1
              and abs(float(fac_a.get("monto_bruto") or 0)
                      - (float(fac_a.get("monto_neto") or 0)
                         + float(fac_a.get("iva") or 0))) <= 1,
              (fac_a.get("monto_neto"), fac_b.get("monto_neto"),
               fac_a.get("iva"), fac_a.get("monto_bruto")))

        # Σ facturado == Σ vendido − faltante declarado (3 − 1 = 2 unidades)
        db_n = SessionLocal()
        try:
            qty_fact = [float(x[0] or 0) for x in
                        db_n.query(ContFacturaClienteItem.cantidad)
                        .join(ContFacturaCliente,
                              ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
                        .filter(ContFacturaCliente.oc_cliente_id == occ.id).all()]
        finally:
            db_n.close()
        check("8d Σ facturado == 2 unidades (vendido 3 − faltante firmado 1)",
              sum(qty_fact) == 2.0 and len(qty_fact) == 2, qty_fact)

        r = client.get(f"/api/contabilidad/ventas/{occ.id}")
        res = (r.json().get("resumen") or {}) if r.status_code == 200 else {}
        check("8e por_facturar 0 y faltante_declarado 1 (el detalle no miente)",
              r.status_code == 200 and res.get("por_facturar_clp") == 0
              and res.get("faltante_declarado") == 1
              and res.get("mercaderia_pendiente_clp") == 0, res)
        r = client.get(f"/api/contabilidad/ventas/{occ.id}/despachos-facturables")
        check("8f selector vacío: ambas guías quedaron facturadas por completo",
              r.status_code == 200 and r.json() == [], r.text[:200])
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": occ.id, "despacho_id": desp_b,
                              "numero_factura": f"{MARK}-F-B2"})
        check("8g re-derivar la guía B → 409 (sin doble facturación)",
              r.status_code == 409, (r.status_code, r.text[:200]))
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": occ.id, "numero_factura": f"{MARK}-F-X",
                              "items": [{"item_cotizacion_id": id_b,
                                         "despacho_item_id": di_b_id, "cantidad": 1}]})
        check("8h el faltante de B tampoco sale por ítems explícitos → 409 (tope)",
              r.status_code == 409, (r.status_code, r.text[:200]))

        # 8i (lector de cruces 2026-08-22): la GUÍA CRUZADA — facturar declarando
        # la guía A pero colando la línea de la guía B. Antes pasaba la validación
        # (solo se chequeaba que el di fuera de la OC): la factura referenciaba una
        # guía por mercadería que viajó en otra, y con hermanas del mismo N° de
        # parte el precio podía salir del congelado equivocado. Un documento
        # tributario no se arma con la referencia equivocada ni por payload directo.
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": occ.id, "despacho_id": desp_a,
                              "numero_factura": f"{MARK}-FCRUZ",
                              "items": [{"item_cotizacion_id": id_b,
                                         "despacho_item_id": di_b_id,
                                         "cantidad": 1}],
                              "plazo_dias": 30})
        check("8i ★ SONDA guía cruzada: declarar la guía A con la línea de la guía "
              "B → rechazado nombrando la guía dueña",
              r.status_code in (400, 409) and "OTRA guía" in r.text,
              (r.status_code, r.text[:220]))

        # ── §9 · INVARIANTES FINALES DE LA FAMILIA ──────────────────────────────
        _check_familia("9a al final del viaje", cot.id, esperadas=2)
        items_fam, asigs = _familia(cot.id)
        check("9b 2 hermanas 'despachado' y los 2 vínculos siguen sin cruzarse",
              all(i.estado_item == "despachado" for i in items_fam)
              and {(a.item_cotizacion_id, a.oc_proveedor_id) for a in asigs}
              == {(id_a, ocp_a.id), (id_b, ocp_b.id)},
              ([(i.id, i.estado_item) for i in items_fam],
               [(a.item_cotizacion_id, a.oc_proveedor_id) for a in asigs]))
        check("9c la venta facturó $%d netos (2 × $100.000): plata conservada"
              % (2 * int(PRECIO_VENTA_UNIT)),
              abs(float(fac_a.get("monto_neto") or 0)
                  + float(fac_b.get("monto_neto") or 0)
                  - 2 * PRECIO_VENTA_UNIT) <= 2,
              (fac_a.get("monto_neto"), fac_b.get("monto_neto")))

    finally:
        try:
            os.remove(os.path.join(docs_dir, archivo))
        except OSError:
            pass
        db.close()
        _limpiar()
        cont._precios_de_cotizacion = _orig_precios
        print("Cleanup OK")
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_ruta_compra_parcial():
    run()


if __name__ == "__main__":
    run()
