"""Asignación PARCIAL a OC-Proveedor (split en el punto de compra) — Grupo AM.

LO QUE ESTA SUITE PROTEGE (endpoint POST /compras/oc-proveedor/{id}/items-parcial):
  §1  Invariantes del split: precio unitario COPIADO idéntico, total RECALCULADO en
      ambas mitades, unitarios copiados sin dividir, cabecera intacta, Σ cantidad y
      Σ total == línea original; remanente 'cerrado' y con CERO vínculo; bitácora
      «asignadas 1 de 3, remanente 2» en Notificacion.
  §1b El helper _clone_item_cotizacion: default clona el vínculo (los 2 callers
      legados intactos) y clonar_vinculo=False NO lo clona (la vía nueva).
  §2  El remanente es asignable a una SEGUNDA OC de otro proveedor (doble split).
  §3  Contrato sin los 4 vicios heredados: 0 → 422 (jamás falsy→"todo"), exceso →
      422 (jamás clamp), negativa → 422, no-entero → 422, id inexistente → 404,
      id de otra cotización → 422, ids repetidos → 422, body vacío → 422 — y en
      TODOS los rechazos la base queda intacta.
  §4  Línea entera (cantidad None o == cantidad) = sin clon, camino legado; el guard
      de coherencia NO rebota la asignación entera de una línea con total divergente
      (solo el split mueve plata).
  §5  Línea 'comprado' → 409 «la reasignación parcial no existe todavía»; línea
      'cerrado' con vínculo huérfano (dato inconsistente) → 409 fail-closed; estado
      ajeno ('preparado') → 422.
  §6  Guard de coherencia del total: |total − qty×precio| > 0.01 → 400 sin partir;
      total>0 sin precio → 400; total None se PRESERVA en ambas mitades (con y sin
      precio: no se inventa plata).
  §7  restaurar_version: si el conjunto de líneas ya no calza con la foto (hubo
      split después), la restauración se bloquea con 409 y las cantidades no se
      tocan; control: con la foto calzada restaura normal.
  §8  CARRERA: dos requests simultáneos sobre la misma línea → uno gana (200), el
      otro 409, y JAMÁS se inventan unidades (Σ cantidad de la familia == original,
      un solo vínculo).

SONDAS CON PODER DISCRIMINANTE (verificadas por mutación al construir la suite:
desactivar el arreglo → rojo → restaurar → verde; evidencia en el reporte del
encargo). Trampas evitadas: cada sonda de rechazo afirma el DETALLE del error (no
solo el status) para probar que murió en la capa que importa, y §1b prueba el
parámetro del clon a nivel de unidad porque en el flujo real de primera asignación
el vínculo no existe todavía (la sonda de integración sola sería vacua).

Datos MARCADOS y limpieza verificada con sesión nueva.
Corre con:  ./venv/bin/python -m pytest routers/tests/test_asignacion_parcial.py -q
"""
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import json as _json  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, CotizacionVersion, ItemCotizacion, Notificacion,
    OcCliente, OcProveedor, OcProveedorItem, User,
)
# PIN del módulo raíz `notificaciones`: routers/tests/ es paquete (su __init__.py evita
# la colisión de nombres con monza_tests/ en el gate completo), así que pytest inserta
# routers/ en sys.path y el ROUTER homónimo routers/notificaciones.py puede sombrear al
# módulo raíz cuando compras.py hace su import DENTRO del request (la bitácora del split
# usa crear_notificacion, que solo existe en el módulo raíz). Importarlo acá, con
# backend/ al frente del path, deja la resolución correcta cacheada en sys.modules para
# todo el proceso.
import notificaciones as _notif_raiz  # noqa: E402
assert hasattr(_notif_raiz, "crear_notificacion"), (
    f"el módulo notificaciones resolvió a {_notif_raiz.__file__} (sombra del router): "
    "el PIN debe correr con backend/ primero en sys.path")

from routers.compras import router as compras_router, _clone_item_cotizacion  # noqa: E402
from routers.cotizador import router as cotizador_router  # noqa: E402

MARK = "__TEST_ASIGPARC__"
CURRENT = {"id": None, "empresa": "mineria"}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(compras_router, prefix="/api")
app.include_router(cotizador_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"])
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Fábricas de datos MARCADOS ────────────────────────────────────────────────────

def _cot(db, sufijo):
    cot = Cotizacion(numero=f"{MARK}-{sufijo}", cliente=MARK, user_id=CURRENT["id"])
    db.add(cot)
    db.flush()
    occ = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}",
                    fecha_oc="2026-08-01")
    db.add(occ)
    db.commit()
    db.refresh(cot)
    db.refresh(occ)
    return cot, occ


def _linea(db, cot, num=1, cantidad=3.0, precio=100.0, total="auto",
           estado="cerrado", **kw):
    if total == "auto":
        total = None if precio is None else cantidad * precio
    it = ItemCotizacion(
        cotizacion_id=cot.id, item_num=num, descripcion=f"{MARK} desc {num}",
        numero_parte=f"{MARK}-P{num}", marca="CAT", cantidad=cantidad,
        precio_unit_cotizacion=precio, total_cotizacion=total, estado_item=estado,
        peso_unit_lbs=2.5, margen_pct=35.0, precio_finning=123.0, precio_cat=99.0,
        nombre_cat=f"{MARK} ncat", plazo="stock", plazo_entrega_min=5,
        plazo_entrega_max=10, **kw)
    db.add(it)
    db.commit()
    db.refresh(it)
    return it


def _ocp(db, sufijo):
    o = OcProveedor(numero=f"{MARK}-OCP-{sufijo}", proveedor=f"{MARK} prov {sufijo}")
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _post(ocp_id, occ_id, items):
    return client.post(f"/api/compras/oc-proveedor/{ocp_id}/items-parcial",
                       json={"oc_cliente_id": occ_id, "items": items})


def _item_fresco(item_id):
    """Relee un ítem con SESIÓN NUEVA (nada de identity map viejo)."""
    db = SessionLocal()
    try:
        return db.query(ItemCotizacion).filter(ItemCotizacion.id == item_id).first()
    finally:
        db.close()


def _familia(cot_id, numero_parte):
    """Todas las hermanas de una línea (sesión nueva) + sus vínculos."""
    db = SessionLocal()
    try:
        items = (db.query(ItemCotizacion)
                 .filter(ItemCotizacion.cotizacion_id == cot_id,
                         ItemCotizacion.numero_parte == numero_parte)
                 .order_by(ItemCotizacion.id.asc()).all())
        asigs = (db.query(OcProveedorItem)
                 .filter(OcProveedorItem.item_cotizacion_id.in_(
                     [i.id for i in items] or [0]))
                 .all())
        return items, asigs
    finally:
        db.close()


def _en_paralelo(fn, n=2):
    """Dispara n llamadas simultáneas (patrón de test_concurrencia_plata)."""
    barrera = threading.Barrier(n)
    salida = [None] * n

    def _worker(i):
        barrera.wait()
        try:
            salida[i] = fn(i)
        except Exception as e:  # noqa: BLE001
            salida[i] = ("EXC", repr(e))

    hilos = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return salida


# ── Limpieza ─────────────────────────────────────────────────────────────────────

def _limpiar():
    db = SessionLocal()
    try:
        db.rollback()
        cot_ids = [c.id for c in db.query(Cotizacion)
                   .filter(Cotizacion.numero.like(f"{MARK}%")).all()]
        if cot_ids:
            item_ids = [i.id for i in db.query(ItemCotizacion)
                        .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all()]
            if item_ids:
                db.query(OcProveedorItem).filter(
                    OcProveedorItem.item_cotizacion_id.in_(item_ids)
                ).delete(synchronize_session=False)
            occ_ids = [o.id for o in db.query(OcCliente)
                       .filter(OcCliente.cotizacion_id.in_(cot_ids)).all()]
            if occ_ids:
                db.query(OcProveedorItem).filter(
                    OcProveedorItem.oc_cliente_id.in_(occ_ids)
                ).delete(synchronize_session=False)
                db.query(OcCliente).filter(
                    OcCliente.id.in_(occ_ids)).delete(synchronize_session=False)
            db.query(CotizacionVersion).filter(
                CotizacionVersion.cotizacion_id.in_(cot_ids)
            ).delete(synchronize_session=False)
            db.query(ItemCotizacion).filter(
                ItemCotizacion.cotizacion_id.in_(cot_ids)
            ).delete(synchronize_session=False)
            db.query(Cotizacion).filter(
                Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        ocp_ids = [o.id for o in db.query(OcProveedor)
                   .filter(OcProveedor.numero.like(f"{MARK}%")).all()]
        if ocp_ids:
            db.query(Notificacion).filter(
                Notificacion.entidad_tipo == "oc_proveedor",
                Notificacion.entidad_id.in_(ocp_ids)
            ).delete(synchronize_session=False)
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
            + db.query(User).filter(User.email.like(f"{MARK}%")).count()
        )
        assert restos == 0, f"limpieza incompleta: quedan {restos} filas {MARK}"
    finally:
        db.close()


# ═════════════════════════════════════════════════════════════════════════════════

def run():
    _limpiar()
    db = SessionLocal()
    user = User(email=f"{MARK}@test.invalid", nombre=MARK, hashed_password="x",
                is_active=1, empresa="mineria")
    db.add(user)
    db.commit()
    db.refresh(user)
    CURRENT["id"] = user.id

    try:
        # ── §1 · SPLIT BÁSICO + INVARIANTES ─────────────────────────────────────
        cot, occ = _cot(db, "S1")
        it = _linea(db, cot, num=1, cantidad=3.0, precio=100.0)
        total_items_antes = cot.total_items
        ocp_a = _ocp(db, "A")
        ocp_b = _ocp(db, "B")

        r = _post(ocp_a.id, occ.id, [{"item_id": it.id, "cantidad": 1,
                                      "plazo_dias_prov": 30,
                                      "plazo_entrega_max": 45}])
        check("1a split 1 de 3 → 200 con partición en la respuesta",
              r.status_code == 200 and r.json().get("asignados") == 1
              and len(r.json().get("particiones", [])) == 1, r.text)
        part = (r.json().get("particiones") or [{}])[0]
        rem_id = part.get("item_id_remanente")
        check("1b la respuesta trae qty 1 asignada / 2 remanente",
              part.get("qty_asignada") == 1 and part.get("qty_remanente") == 2, part)

        orig = _item_fresco(it.id)
        rem = _item_fresco(rem_id) if rem_id else None
        check("1c el trozo asignado queda 'comprado' con cantidad 1 y total 100",
              orig is not None and orig.estado_item == "comprado"
              and orig.cantidad == 1 and orig.total_cotizacion == 100,
              orig and (orig.estado_item, orig.cantidad, orig.total_cotizacion))
        check("1d el remanente conserva 'cerrado', cantidad 2, total 200",
              rem is not None and rem.estado_item == "cerrado"
              and rem.cantidad == 2 and rem.total_cotizacion == 200,
              rem and (rem.estado_item, rem.cantidad, rem.total_cotizacion))
        check("1e precio unitario IDÉNTICO en ambas mitades (jamás prorrateado)",
              rem is not None and orig.precio_unit_cotizacion == 100
              and rem.precio_unit_cotizacion == 100,
              (orig.precio_unit_cotizacion, rem and rem.precio_unit_cotizacion))
        check("1f unitarios copiados sin dividir (peso, margen, finning, cat, nombre)",
              rem is not None and rem.peso_unit_lbs == 2.5 and rem.margen_pct == 35.0
              and rem.precio_finning == 123.0 and rem.precio_cat == 99.0
              and rem.nombre_cat == f"{MARK} ncat",
              rem and (rem.peso_unit_lbs, rem.margen_pct, rem.precio_finning,
                       rem.precio_cat, rem.nombre_cat))
        check("1g Σ cantidad == 3 y Σ total == 300 (nada inventado ni perdido)",
              rem is not None and (orig.cantidad + rem.cantidad) == 3
              and (orig.total_cotizacion + rem.total_cotizacion) == 300,
              rem and (orig.cantidad + rem.cantidad,
                       orig.total_cotizacion + rem.total_cotizacion))
        check("1h plazo_entrega_max del body fue SOLO al trozo asignado "
              "(el remanente conserva el previo)",
              orig.plazo_entrega_max == 45 and rem is not None
              and rem.plazo_entrega_max == 10,
              (orig.plazo_entrega_max, rem and rem.plazo_entrega_max))

        db.rollback()
        asig = (db.query(OcProveedorItem)
                .filter(OcProveedorItem.item_cotizacion_id == it.id).first())
        check("1i vínculo del trozo asignado: OCP A + plazo 30 + fecha_asignacion",
              asig is not None and asig.oc_proveedor_id == ocp_a.id
              and asig.oc_cliente_id == occ.id and asig.plazo_dias_prov == 30
              and asig.fecha_asignacion is not None,
              asig and (asig.oc_proveedor_id, asig.plazo_dias_prov))
        n_asig_rem = (db.query(OcProveedorItem)
                      .filter(OcProveedorItem.item_cotizacion_id == rem_id).count()
                      if rem_id else -1)
        check("1j el remanente tiene CERO vínculo (vuelve al panel)",
              n_asig_rem == 0, n_asig_rem)

        cot_fresca = db.query(Cotizacion).filter(Cotizacion.id == cot.id).first()
        check("1k cabecera INTACTA (total_items y pricing_snapshot sin tocar)",
              cot_fresca.total_items == total_items_antes
              and cot_fresca.pricing_snapshot is None,
              (cot_fresca.total_items, total_items_antes))

        notif = (db.query(Notificacion)
                 .filter(Notificacion.entidad_tipo == "oc_proveedor",
                         Notificacion.entidad_id == ocp_a.id).first())
        check("1l bitácora del split: «asignadas 1 de 3, remanente 2»",
              notif is not None and "asignadas 1 de 3" in (notif.mensaje or "")
              and "remanente 2" in (notif.mensaje or ""),
              notif and notif.mensaje)

        # ── §1b · EL HELPER: clonar_vinculo a nivel de unidad ───────────────────
        # En el flujo real de PRIMERA asignación el vínculo no existe al momento del
        # clon (se crea después), así que la sonda de integración 1j sería vacua
        # frente a una regresión del parámetro. Acá se prueba el helper directo con
        # un ítem que SÍ tiene vínculo: default lo clona (callers legados intactos),
        # clonar_vinculo=False no.
        cot1b, occ1b = _cot(db, "S1B")
        it1b = _linea(db, cot1b, num=1, cantidad=4.0, precio=10.0, estado="comprado")
        db.add(OcProveedorItem(oc_proveedor_id=ocp_a.id, oc_cliente_id=occ1b.id,
                               item_cotizacion_id=it1b.id, plazo_dias_prov=7))
        db.commit()
        it1b_obj = db.query(ItemCotizacion).filter(ItemCotizacion.id == it1b.id).first()
        clon_def = _clone_item_cotizacion(it1b_obj, 1.0, "comprado", db)
        db.commit()
        n_def = (db.query(OcProveedorItem)
                 .filter(OcProveedorItem.item_cotizacion_id == clon_def.id).count())
        check("1b-1 default: el clon HEREDA el vínculo (contrato de los 2 callers "
              "legados preparar-parcial y cierre de pre-embarque)", n_def == 1, n_def)
        clon_sin = _clone_item_cotizacion(it1b_obj, 1.0, "cerrado", db,
                                          clonar_vinculo=False)
        db.commit()
        n_sin = (db.query(OcProveedorItem)
                 .filter(OcProveedorItem.item_cotizacion_id == clon_sin.id).count())
        check("1b-2 clonar_vinculo=False: el clon nace SIN vínculo (la vía nueva)",
              n_sin == 0, n_sin)

        # ── §2 · REMANENTE ASIGNABLE A UNA SEGUNDA OC (doble split) ─────────────
        r = _post(ocp_b.id, occ.id, [{"item_id": rem_id, "cantidad": 1}])
        check("2a el remanente se asigna parcial a la OCP B → 200",
              r.status_code == 200, r.text)
        rem2_id = (r.json().get("particiones") or [{}])[0].get("item_id_remanente")
        familia, asigs = _familia(cot.id, f"{MARK}-P1")
        check("2b la familia queda en 3 hermanas: 1 (A), 1 (B), 1 libre",
              len(familia) == 3, [f.id for f in familia])
        check("2c Σ cantidad de la familia == 3 y Σ total == 300 tras el doble split",
              sum(f.cantidad for f in familia) == 3
              and sum(f.total_cotizacion for f in familia) == 300,
              (sum(f.cantidad for f in familia),
               sum(f.total_cotizacion for f in familia)))
        check("2d dos vínculos (A y B) y el nieto libre sigue 'cerrado' sin vínculo",
              len(asigs) == 2
              and {a.oc_proveedor_id for a in asigs} == {ocp_a.id, ocp_b.id}
              and rem2_id not in {a.item_cotizacion_id for a in asigs}
              and _item_fresco(rem2_id).estado_item == "cerrado",
              ([(a.item_cotizacion_id, a.oc_proveedor_id) for a in asigs], rem2_id))

        # ── §3 · CONTRATO SIN LOS 4 VICIOS ──────────────────────────────────────
        cot3, occ3 = _cot(db, "S3")
        it3 = _linea(db, cot3, num=1, cantidad=3.0, precio=50.0)

        def _sin_mutacion(nombre_check):
            f = _item_fresco(it3.id)
            n_hermanas = len(_familia(cot3.id, f"{MARK}-P1")[0])
            check(nombre_check,
                  f.estado_item == "cerrado" and f.cantidad == 3 and n_hermanas == 1,
                  (f.estado_item, f.cantidad, n_hermanas))

        r = _post(ocp_a.id, occ3.id, [{"item_id": it3.id, "cantidad": 0}])
        check("3a cantidad 0 → 422 explícito (jamás el falsy que asigna todo)",
              r.status_code == 422 and "mayor que 0" in r.text, (r.status_code, r.text))
        _sin_mutacion("3a-bis …y la línea quedó intacta (cerrado, qty 3, sin clon)")

        r = _post(ocp_a.id, occ3.id, [{"item_id": it3.id, "cantidad": -1}])
        check("3b cantidad negativa → 422", r.status_code == 422
              and "mayor que 0" in r.text, (r.status_code, r.text))

        r = _post(ocp_a.id, occ3.id, [{"item_id": it3.id, "cantidad": 1.5}])
        check("3c cantidad no entera → 422 (unidades físicas enteras)",
              r.status_code == 422 and "enteras" in r.text, (r.status_code, r.text))

        r = _post(ocp_a.id, occ3.id, [{"item_id": it3.id, "cantidad": 99}])
        check("3d exceso (99 de 3) → 422 (jamás clamp silencioso)",
              r.status_code == 422 and "excede" in r.text, (r.status_code, r.text))
        _sin_mutacion("3d-bis …y sin clamp: la línea sigue cerrado/qty 3/sin clon")

        r = _post(ocp_a.id, occ3.id, [{"item_id": 999999999, "cantidad": 1}])
        check("3e id inexistente → 404 explícito (jamás continue mudo)",
              r.status_code == 404 and "inexistente" in r.text, (r.status_code, r.text))

        r = _post(ocp_a.id, occ.id, [{"item_id": it3.id, "cantidad": 1}])
        check("3f id de otra cotización → 422 explícito",
              r.status_code == 422 and "otra cotización" in r.text,
              (r.status_code, r.text))
        _sin_mutacion("3f-bis …y la línea ajena quedó intacta")

        r = _post(ocp_a.id, occ3.id, [{"item_id": it3.id, "cantidad": 1},
                                      {"item_id": it3.id, "cantidad": 1}])
        check("3g ids repetidos → 422 (no burlan el tope por línea)",
              r.status_code == 422 and "repetidos" in r.text, (r.status_code, r.text))

        r = _post(ocp_a.id, occ3.id, [])
        check("3h body sin ítems → 422", r.status_code == 422
              and "Sin ítems" in r.text, (r.status_code, r.text))

        r = _post(999999999, occ3.id, [{"item_id": it3.id, "cantidad": 1}])
        check("3i OCP inexistente → 404", r.status_code == 404
              and "OC-Proveedor" in r.text, (r.status_code, r.text))

        r = _post(ocp_a.id, 999999999, [{"item_id": it3.id, "cantidad": 1}])
        check("3j OC-Cliente inexistente → 404", r.status_code == 404
              and "OC-Cliente" in r.text, (r.status_code, r.text))
        _sin_mutacion("3k tras TODOS los rechazos la línea sigue virgen")

        # ── §4 · LÍNEA ENTERA = CAMINO LEGADO (sin clon) ────────────────────────
        r = _post(ocp_a.id, occ3.id, [{"item_id": it3.id, "plazo_dias_prov": 15}])
        f = _item_fresco(it3.id)
        n_hermanas = len(_familia(cot3.id, f"{MARK}-P1")[0])
        check("4a cantidad None = línea entera: 200, sin clon, comprado, qty intacta",
              r.status_code == 200 and not r.json().get("particiones")
              and f.estado_item == "comprado" and f.cantidad == 3
              and f.total_cotizacion == 150 and n_hermanas == 1,
              (r.status_code, f.estado_item, f.cantidad, n_hermanas))

        it4 = _linea(db, cot3, num=2, cantidad=2.0, precio=80.0)
        r = _post(ocp_a.id, occ3.id, [{"item_id": it4.id, "cantidad": 2}])
        f = _item_fresco(it4.id)
        n_hermanas = len(_familia(cot3.id, f"{MARK}-P2")[0])
        check("4b cantidad == cantidad de la línea: 200 y sin clon",
              r.status_code == 200 and not r.json().get("particiones")
              and f.estado_item == "comprado" and f.cantidad == 2 and n_hermanas == 1,
              (r.status_code, f.estado_item, f.cantidad, n_hermanas))

        # Guard de coherencia NO aplica a la línea entera: total divergente del Excel
        # se asigna completa sin rebotar (no se recalcula nada, no se mueve plata).
        it4b = _linea(db, cot3, num=3, cantidad=3.0, precio=300.0, total=1000.0)
        r = _post(ocp_a.id, occ3.id, [{"item_id": it4b.id}])
        f = _item_fresco(it4b.id)
        check("4c línea entera con total divergente → 200 y el total NO se toca "
              "(el guard de coherencia es solo para el split)",
              r.status_code == 200 and f.estado_item == "comprado"
              and f.total_cotizacion == 1000.0, (r.status_code, f.total_cotizacion))

        # ── §5 · ESTADOS ────────────────────────────────────────────────────────
        cot5, occ5 = _cot(db, "S5")
        it5 = _linea(db, cot5, num=1, cantidad=3.0, precio=10.0, estado="comprado")
        db.add(OcProveedorItem(oc_proveedor_id=ocp_a.id, oc_cliente_id=occ5.id,
                               item_cotizacion_id=it5.id))
        db.commit()
        r = _post(ocp_b.id, occ5.id, [{"item_id": it5.id, "cantidad": 1}])
        check("5a línea 'comprado' + cantidad parcial → 409 «la reasignación parcial "
              "no existe todavía»",
              r.status_code == 409 and "reasignación parcial" in r.text,
              (r.status_code, r.text))
        r = _post(ocp_b.id, occ5.id, [{"item_id": it5.id}])
        check("5b línea 'comprado' + línea entera por la vía nueva → también 409 "
              "(v1 opera SOLO sobre 'cerrado'; mover entero es del endpoint legado)",
              r.status_code == 409 and "reasignación parcial" in r.text,
              (r.status_code, r.text))
        f = _item_fresco(it5.id)
        check("5c …y la línea comprada quedó intacta (qty 3, un solo vínculo)",
              f.cantidad == 3
              and db.query(OcProveedorItem)
                    .filter(OcProveedorItem.item_cotizacion_id == it5.id).count() == 1,
              f.cantidad)

        # Dato inconsistente: 'cerrado' con vínculo huérfano → fail-closed 409.
        it5b = _linea(db, cot5, num=2, cantidad=3.0, precio=10.0, estado="cerrado")
        db.add(OcProveedorItem(oc_proveedor_id=ocp_a.id, oc_cliente_id=occ5.id,
                               item_cotizacion_id=it5b.id))
        db.commit()
        r = _post(ocp_b.id, occ5.id, [{"item_id": it5b.id, "cantidad": 1}])
        check("5d 'cerrado' con vínculo huérfano (dato inconsistente) → 409 "
              "fail-closed, jamás un segundo vínculo",
              r.status_code == 409 and "vínculo" in r.text, (r.status_code, r.text))
        n_asigs_5b = (db.query(OcProveedorItem)
                      .filter(OcProveedorItem.item_cotizacion_id == it5b.id).count())
        check("5d-bis …y sigue habiendo UN solo vínculo", n_asigs_5b == 1, n_asigs_5b)

        it5c = _linea(db, cot5, num=3, cantidad=3.0, precio=10.0, estado="preparado")
        r = _post(ocp_b.id, occ5.id, [{"item_id": it5c.id, "cantidad": 1}])
        check("5e estado ajeno ('preparado') → 422 explícito (partir ahí es trabajo "
              "de preparar-parcial, no de este endpoint)",
              r.status_code == 422 and "cerrado" in r.text, (r.status_code, r.text))

        # ── §6 · GUARD DE COHERENCIA DEL TOTAL ──────────────────────────────────
        cot6, occ6 = _cot(db, "S6")
        it6 = _linea(db, cot6, num=1, cantidad=3.0, precio=300.0, total=1000.0)
        r = _post(ocp_a.id, occ6.id, [{"item_id": it6.id, "cantidad": 1}])
        f = _item_fresco(it6.id)
        check("6a total divergente (1000 ≠ 3×300) + split → 400 «no cuadra» "
              "y NO se parte (partir movería plata: 300+600=900)",
              r.status_code == 400 and "no cuadra" in r.text
              and f.estado_item == "cerrado" and f.cantidad == 3
              and f.total_cotizacion == 1000.0, (r.status_code, r.text))

        it6b = _linea(db, cot6, num=2, cantidad=3.0, precio=None, total=500.0)
        r = _post(ocp_a.id, occ6.id, [{"item_id": it6b.id, "cantidad": 1}])
        check("6b total > 0 sin precio unitario + split → 400 explícito",
              r.status_code == 400 and "sin precio" in r.text, (r.status_code, r.text))

        it6c = _linea(db, cot6, num=3, cantidad=3.0, precio=None, total=None)
        r = _post(ocp_a.id, occ6.id, [{"item_id": it6c.id, "cantidad": 1}])
        rem6c = (r.json().get("particiones") or [{}])[0].get("item_id_remanente")
        f, frem = _item_fresco(it6c.id), _item_fresco(rem6c) if rem6c else None
        check("6c total None + precio None: el split PRESERVA None en ambas mitades "
              "(no se inventa plata)",
              r.status_code == 200 and f.total_cotizacion is None
              and frem is not None and frem.total_cotizacion is None
              and f.precio_unit_cotizacion is None
              and frem.precio_unit_cotizacion is None,
              (r.status_code, f.total_cotizacion, frem and frem.total_cotizacion))

        it6d = _linea(db, cot6, num=4, cantidad=3.0, precio=100.0, total=None)
        r = _post(ocp_a.id, occ6.id, [{"item_id": it6d.id, "cantidad": 1}])
        rem6d = (r.json().get("particiones") or [{}])[0].get("item_id_remanente")
        f, frem = _item_fresco(it6d.id), _item_fresco(rem6d) if rem6d else None
        check("6d total None con precio: None se preserva igual (el helper viejo "
              "habría inventado qty×precio en el clon)",
              r.status_code == 200 and f.total_cotizacion is None
              and frem is not None and frem.total_cotizacion is None
              and frem.precio_unit_cotizacion == 100.0,
              (r.status_code, f.total_cotizacion, frem and frem.total_cotizacion))

        # ── §7 · RESTAURAR_VERSION: guard anti-split ────────────────────────────
        cot7, occ7 = _cot(db, "S7")
        it7 = _linea(db, cot7, num=1, cantidad=3.0, precio=100.0)
        it7b = _linea(db, cot7, num=2, cantidad=5.0, precio=20.0)
        snap = {"items": [
            {"id": it7.id, "cantidad": 4.0, "precio_unit_cotizacion": 100.0},
            {"id": it7b.id, "cantidad": 5.0, "precio_unit_cotizacion": 20.0},
        ]}
        db.add(CotizacionVersion(cotizacion_id=cot7.id, version_num=1,
                                 descripcion=MARK, snapshot_json=_json.dumps(snap)))
        db.commit()

        r = client.post(f"/api/cotizador/{cot7.id}/versiones/1/restaurar")
        f = _item_fresco(it7.id)
        check("7a control: con la foto CALZADA la restauración funciona "
              "(cantidad 3 → 4 según el snapshot)",
              r.status_code == 200 and f.cantidad == 4, (r.status_code, f.cantidad))

        # volver a 3 (total consistente) y PARTIR la línea → la foto ya no calza
        db.query(ItemCotizacion).filter(ItemCotizacion.id == it7.id).update(
            {"cantidad": 3.0, "total_cotizacion": 300.0})
        db.commit()
        r = _post(ocp_a.id, occ7.id, [{"item_id": it7.id, "cantidad": 1}])
        check("7b split de por medio → 200 (setup)", r.status_code == 200, r.text)
        rem7 = (r.json().get("particiones") or [{}])[0].get("item_id_remanente")

        r = client.post(f"/api/cotizador/{cot7.id}/versiones/1/restaurar")
        f, frem = _item_fresco(it7.id), _item_fresco(rem7) if rem7 else None
        check("7c restaurar DESPUÉS del split → 409 que explica (reponer cantidades "
              "duplicaría unidades y plata)",
              r.status_code == 409 and "duplicaría" in r.text, (r.status_code, r.text))
        check("7d …y las cantidades NO se tocaron (1 asignada + 2 remanente, no 4+2)",
              f.cantidad == 1 and frem is not None and frem.cantidad == 2,
              (f.cantidad, frem and frem.cantidad))

        # ── §8 · LA CARRERA ─────────────────────────────────────────────────────
        # Dos requests simultáneos parten la MISMA línea de 3: el lock FOR UPDATE
        # serializa — el perdedor relee ('comprado') y rebota 409. Sin el lock, ambos
        # leerían cantidad=3, cada uno clonaría su remanente y Σ > 3 (unidades
        # inventadas). Se repite para darle ventana real a la carrera.
        for ronda in range(3):
            cot8, occ8 = _cot(db, f"S8R{ronda}")
            it8 = _linea(db, cot8, num=1, cantidad=3.0, precio=10.0)
            # FOTO PLANA antes de los hilos: los objetos ORM viven en la sesión
            # PRINCIPAL y sus atributos expiran con cada commit; tocarlos desde un
            # hilo dispara un refresh sobre la MISMA conexión pymysql desde dos
            # hilos a la vez y la corrompe (InterfaceError (0, '')). Los hilos
            # reciben ints, jamás objetos de sesión.
            it8_id, occ8_id, cot8_id = it8.id, occ8.id, cot8.id
            ocp_ids = (ocp_a.id, ocp_b.id)

            def _tiro(i):
                qty = 1 if i == 0 else 2
                resp = _post(ocp_ids[i], occ8_id, [{"item_id": it8_id, "cantidad": qty}])
                return resp.status_code

            salida = _en_paralelo(_tiro, n=2)
            familia8, asigs8 = _familia(cot8_id, f"{MARK}-P1")
            suma = sum(fi.cantidad for fi in familia8)
            check(f"8-{ronda}a un ganador (200) y un perdedor (409): {salida}",
                  sorted(salida) == [200, 409], salida)
            check(f"8-{ronda}b Σ cantidad de la familia == 3 (JAMÁS unidades "
                  "inventadas) y UN solo vínculo",
                  suma == 3 and len(asigs8) == 1,
                  (suma, len(asigs8), [fi.cantidad for fi in familia8]))

        # ── §9 · EL GUARD DEL EDITOR (hallazgo MEDIO de la revisión adversarial) ─
        # El editor del cotizador podía editar/borrar una línea YA en el pipeline:
        # editar el precio de la hermana comprada descuadra la familia, y borrarla
        # reventaba 500 por la FK de oc_proveedor_items (sin ondelete) — o hacía
        # desaparecer unidades de una venta con OC emitida. El guard bloquea SOLO lo
        # comprometido; la 'cerrado' sin vínculo sigue editable (decisión del dueño
        # pendiente, anotada en el guard).
        cot9, occ9 = _cot(db, "S9")
        it9 = _linea(db, cot9, num=1, cantidad=3.0, precio=100.0)
        r = _post(ocp_a.id, occ9.id, [{"item_id": it9.id, "cantidad": 1}])
        check("9a setup: split 1 de 3", r.status_code == 200, r.text[:200])
        rem9 = (r.json().get("particiones") or [{}])[0].get("item_id_remanente")

        r = client.put(f"/api/cotizador/{cot9.id}/items/{it9.id}",
                       json={"precio_unit_cotizacion": 150.0})
        check("9b editar el PRECIO de la hermana COMPRADA → 409 (la familia se "
              "descuadraría)", r.status_code == 409 and "flujo de compras" in r.text,
              (r.status_code, r.text[:160]))
        r = client.delete(f"/api/cotizador/{cot9.id}/items/{it9.id}")
        check("9c borrar la hermana COMPRADA → 409 explícito (antes: 500 por FK o "
              "unidades desaparecidas)", r.status_code == 409,
              (r.status_code, r.text[:160]))
        f9 = _item_fresco(it9.id)
        check("9d …y la línea quedó intacta (precio 100, cantidad 1, comprado)",
              f9 is not None and f9.precio_unit_cotizacion == 100.0
              and f9.cantidad == 1 and f9.estado_item == "comprado",
              (f9.precio_unit_cotizacion, f9.cantidad, f9.estado_item) if f9 else None)
        r = client.put(f"/api/cotizador/{cot9.id}/items/{rem9}",
                       json={"plazo_entrega_max": 45})
        check("9e la hermana 'cerrado' SIN vínculo sigue editable (el guard no "
              "bloquea de más)", r.status_code == 200, (r.status_code, r.text[:160]))

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_asignacion_parcial():
    run()


if __name__ == "__main__":
    run()
