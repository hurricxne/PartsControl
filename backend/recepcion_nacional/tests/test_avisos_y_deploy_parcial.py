"""Recepción nacional: aviso a Ventas al cerrar (M1-BD) y deploy a medias (M2-BD).

DOS AGUJEROS QUE ESTA SUITE CIERRA

1. M1-BD — La vía EMBARQUE avisaba «OC Cliente N lista para despacho» al cerrar la
   recepción (`routers/bodega.py::_evaluar_ocs_cliente`, reglas B6/B7) y la vía
   NACIONAL no avisaba nada (`grep -rn "notif" backend/recepcion_nacional/` → 0). El
   proveedor chileno entregaba, la OC quedaba completa y Ventas no se enteraba hasta el
   barrido de las 06:00 del día siguiente. Ahora `_avisar_ventas_listas` reusa la
   variante por `item_ids` de las MISMAS reglas, en los dos caminos de cierre
   (`POST ''` con `cerrar=true` y `POST /{id}/cerrar`).

2. M2-BD — Anular una recepción cerrada preguntaba por `cont_compra_item` sin ningún
   `try`: en un deploy donde no se corrió `compras_contab.init_db`, MySQL devuelve 1146
   y el operador recibía un **500** al intentar anular. Ahora la comprobación de costeo
   se apaga sola (sin costeo desplegado no hay nada que proteger).

SONDAS DE PODER DISCRIMINANTE (verificadas quitando el arreglo):
  · sin la llamada a `_avisar_ventas_listas` → secciones 1 y 2 ROJAS (0 avisos).
  · sin el `except ProgrammingError` de `_costeo_por_item_disponible` → sección 6 ROJA
    (la anulación revienta con 500 en vez de responder 200).

La tabla inexistente NO se toca en la base: se monkeypatchea `ContCompraItem` por una
clase mapeada sobre un nombre que no existe, en su PROPIO `declarative_base` para que
`Base.metadata.create_all` de otras suites jamás la cree.

Datos MARCADOS + limpieza total en `finally` + verificación por deltas (las
notificaciones se borran por `id > snapshot`).

Corre con:  ./venv/bin/python -m pytest recepcion_nacional/tests/test_avisos_y_deploy_parcial.py -q
(también:   ./venv/bin/python recepcion_nacional/tests/test_avisos_y_deploy_parcial.py)
"""
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Column, Float, Integer, text  # noqa: E402
from sqlalchemy.orm import Session, declarative_base  # noqa: E402

import routers.bodega as bodega_mod  # noqa: E402  (se monkeypatchea en la sección 5)
import compras_contab.models as cc_models  # noqa: E402  (se monkeypatchea en la 6)
from auth import get_current_user  # noqa: E402
from database import SessionLocal, engine, Base, get_db  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, Notificacion, OcCliente, OcProveedor, OcProveedorItem,
)
import embarques_pricing.models  # noqa: E402,F401  (emb_pricing_gasto: FK de cont_compra)
from compras_contab.models import ContCompra, ContCompraItem  # noqa: E402
from recepcion_nacional.models import RecepcionNacional  # noqa: E402
from recepcion_nacional.router import router as rn_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_RNAV__"
CURRENT = {"empresa": "mineria", "id": None}
_SNAP = {"id": None}
_fails: list = []


# ── Tabla FANTASMA para reproducir el MySQL 1146 ──────────────────────────────
# En su propio Base: si viviera en el Base del proyecto, cualquier
# `Base.metadata.create_all()` de otra suite la CREARÍA en la base de verdad.
_BaseFantasma = declarative_base()


class _ContCompraItemFantasma(_BaseFantasma):
    """Espejo de `cont_compra_item` sobre un nombre de tabla que NO existe."""
    __tablename__ = "cont_compra_item__inexistente_test"
    id = Column(Integer, primary_key=True)
    compra_id = Column(Integer)
    item_cotizacion_id = Column(Integer)
    cantidad = Column(Float)


app = FastAPI()
app.include_router(rn_router, prefix="/api")


def _cu(db: Session = Depends(get_db)):
    """Auth REALISTA: la lectura abre el read view de MySQL ANTES de los locks,
    igual que `auth.get_current_user` en producción."""
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
# raise_server_exceptions=False: un 500 tiene que llegar como status 500 y no como
# excepción, porque "no revienta con 500" ES lo que la sección 6 comprueba.
client = TestClient(app, raise_server_exceptions=False)


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _snapshot():
    db = SessionLocal()
    try:
        _SNAP["id"] = db.query(Notificacion.id).order_by(
            Notificacion.id.desc()).limit(1).scalar() or 0
    finally:
        db.close()


def _borrar_notifs():
    if _SNAP["id"] is None:
        return
    db = SessionLocal()
    try:
        db.query(Notificacion).filter(
            Notificacion.id > _SNAP["id"]).delete(synchronize_session="fetch")
        db.commit()
    finally:
        db.close()


def _avisos(oc_id, regla=None):
    db = SessionLocal()
    try:
        q = db.query(Notificacion).filter(
            Notificacion.id > _SNAP["id"],
            Notificacion.entidad_tipo == "oc_cliente",
            Notificacion.entidad_id == oc_id,
        )
        if regla:
            q = q.filter(Notificacion.regla == regla)
        return q.order_by(Notificacion.id).all()
    finally:
        db.close()


def _escenario(db, cantidades=(10,), fecha_entrega=None, sufijo="A"):
    """Cotización + ítems 'comprado' + OC cliente + OC proveedor NACIONAL + asignaciones."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} CLIENTE",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = ItemCotizacion(cotizacion_id=cot.id, item_num=n,
                            numero_parte=f"P-{MARK}-{sufijo}-{n}", descripcion=f"Parte {n}",
                            cantidad=cant, estado_item="comprado")
        db.add(it); items.append(it)
    db.flush()
    occ = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OCC-{sufijo}",
                    fecha_oc="2026-07-18", fecha_entrega=fecha_entrega)
    db.add(occ); db.flush()
    ocp = OcProveedor(numero=f"{MARK}-OCP-{sufijo}", numero_oc=f"{MARK}-PROV-{sufijo}",
                      proveedor=f"{MARK} PROVEEDOR", moneda="CLP", tipo_origen="nacional")
    db.add(ocp); db.flush()
    for it in items:
        db.add(OcProveedorItem(oc_proveedor_id=ocp.id, oc_cliente_id=occ.id,
                               item_cotizacion_id=it.id))
    db.commit()
    for obj in [cot, occ, ocp] + items:
        db.refresh(obj)
    return cot, occ, ocp, items


def _registrar(ocp_id, lineas, cerrar=True):
    return client.post("/api/recepcion-nacional", json={
        "oc_proveedor_id": ocp_id, "numero_guia_proveedor": "G-1",
        "fecha": "2026-07-18", "cerrar": cerrar,
        "items": [{"item_cotizacion_id": i, "qty_recibida": q, "estado_recepcion": e}
                  for i, q, e in lineas],
    })


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    item_ids = ([i.id for i in db.query(ItemCotizacion)
                 .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all()] if cot_ids else [])
    ocps = db.query(OcProveedor).filter(OcProveedor.numero.like(f"{MARK}%")).all()
    ocp_ids = [o.id for o in ocps]

    if ocp_ids:
        for rec in db.query(RecepcionNacional).filter(
                RecepcionNacional.oc_proveedor_id.in_(ocp_ids)).all():
            db.delete(rec)
        db.flush()
    if item_ids:
        db.query(ContCompraItem).filter(
            ContCompraItem.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=False)
    for c in db.query(ContCompra).filter(ContCompra.numero_documento.like(f"{MARK}%")).all():
        db.delete(c)
    db.flush()
    if ocp_ids:
        db.query(OcProveedorItem).filter(
            OcProveedorItem.oc_proveedor_id.in_(ocp_ids)).delete(synchronize_session=False)
        db.query(OcProveedor).filter(
            OcProveedor.id.in_(ocp_ids)).delete(synchronize_session=False)
    if cot_ids:
        db.query(OcCliente).filter(
            OcCliente.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(Cotizacion).filter(
            Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.commit()
    _borrar_notifs()


def _boom(*a, **kw):
    raise RuntimeError("falla inyectada por el test de la notificación")


def run():
    _snapshot()
    db = SessionLocal()
    try:
        _limpiar(db)
        _snapshot()
        CURRENT["empresa"] = "mineria"
        hoy = date.today()

        # ═══ 1. Entrega que COMPLETA la OC → «lista para despacho» ════════════
        cot, occ, ocp, (a, b) = _escenario(db, (10, 5), sufijo="FULL")
        check("sin cerrar todavía: ninguna OC avisada", _avisos(occ.id) == [])
        r = _registrar(ocp.id, [(a.id, 10, "completo"), (b.id, 5, "completo")])
        check("registrar entrega completa → 200", r.status_code == 200, r.text)
        n = _avisos(occ.id, "oc_lista_despacho")
        check("la entrega nacional avisa «OC lista para despacho»", len(n) == 1,
              f"{len(n)} aviso(s) — sin el aviso portado esto da 0")
        if n:
            av = n[0]
            check("aviso: título con el N° de OC",
                  av.titulo == f"OC Cliente {occ.numero_oc} lista para despacho", av.titulo)
            check("aviso: rol ventas, severidad info, link a Cierre de Venta",
                  (av.destinatario_rol, av.severidad, av.link)
                  == ("ventas", "info", "/cierre-venta"),
                  (av.destinatario_rol, av.severidad, av.link))
        # Idempotencia reusada: el mismo evento no puede dejar 2 filas en la campana
        from recepcion_nacional.router import _avisar_ventas_listas
        _avisar_ventas_listas(db, [a.id, b.id])
        check("segundo aviso del mismo evento no duplica (idempotencia de 24 h)",
              len(_avisos(occ.id, "oc_lista_despacho")) == 1,
              len(_avisos(occ.id, "oc_lista_despacho")))
        _limpiar(db)

        # ═══ 2. Recepción ABIERTA: avisa al CERRARLA, no antes ════════════════
        cot, occ, ocp, (it,) = _escenario(db, (10,), sufijo="AB")
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")], cerrar=False).json()["id"]
        check("recepción abierta: todavía NO avisa (nada está en bodega)",
              _avisos(occ.id) == [], [x.titulo for x in _avisos(occ.id)])
        r = client.post(f"/api/recepcion-nacional/{rec_id}/cerrar")
        check("cerrar la recepción abierta → 200", r.status_code == 200, r.text)
        check("al cerrar aparte también avisa «lista para despacho»",
              len(_avisos(occ.id, "oc_lista_despacho")) == 1,
              [x.titulo for x in _avisos(occ.id)])
        _limpiar(db)

        # ═══ 3. Entrega PARCIAL con plazo a 1 día → «plazo crítico» ═══════════
        cot, occ, ocp, (a, b) = _escenario(db, (10, 5), sufijo="PARC",
                                           fecha_entrega=hoy + timedelta(days=1))
        r = _registrar(ocp.id, [(a.id, 10, "completo"), (b.id, 0, "no_llego")])
        check("registrar entrega parcial → 200", r.status_code == 200, r.text)
        check("la entrega parcial con plazo apretado avisa «plazo crítico»",
              len(_avisos(occ.id, "plazo_critico_3d")) == 1,
              [x.titulo for x in _avisos(occ.id)])
        check("la entrega parcial NO avisa «lista para despacho»",
              _avisos(occ.id, "oc_lista_despacho") == [])
        _limpiar(db)

        # ═══ 4. Entrega parcial con plazo HOLGADO → ningún aviso ══════════════
        cot, occ, ocp, (a, b) = _escenario(db, (10, 5), sufijo="HOLG",
                                           fecha_entrega=hoy + timedelta(days=30))
        _registrar(ocp.id, [(a.id, 10, "completo"), (b.id, 0, "no_llego")])
        check("parcial con plazo lejos: no avisa nada (no es spam)",
              _avisos(occ.id) == [], [x.titulo for x in _avisos(occ.id)])
        _limpiar(db)

        # ═══ 5. Un fallo del aviso NO puede tumbar la recepción ══════════════
        cot, occ, ocp, (it,) = _escenario(db, (10,), sufijo="BOOM")
        original = bodega_mod._evaluar_ocs_cliente_por_items
        try:
            bodega_mod._evaluar_ocs_cliente_por_items = _boom
            r = _registrar(ocp.id, [(it.id, 10, "completo")])
        finally:
            bodega_mod._evaluar_ocs_cliente_por_items = original
        check("aviso reventado → la recepción igual responde 200",
              r.status_code == 200, r.text)
        db.rollback()
        check("aviso reventado → el ítem quedó en bodega (la entrega se guardó)",
              db.get(ItemCotizacion, it.id).estado_item == "en_bodega")
        check("aviso reventado → no quedó aviso a medias", _avisos(occ.id) == [])
        _limpiar(db)

        # ═══ 6. Deploy a medias: sin `cont_compra_item` la anulación NO revienta ═
        cot, occ, ocp, (it,) = _escenario(db, (10,), sufijo="1146")
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")]).json()["id"]
        db.rollback()
        # Un costeo VIVO que normalmente bloquea la anulación con 409
        compra = ContCompra(empresa="mineria", origen="NACIONAL", tipo_gasto="cogs",
                            numero_documento=f"{MARK}-FAC", monto_neto=100000,
                            monto_total_clp=119000, anulado=False)
        db.add(compra); db.flush()
        db.add(ContCompraItem(compra_id=compra.id, item_cotizacion_id=it.id, cantidad=10,
                              costo_total_clp=100000))
        db.commit()
        # Control: con la tabla presente el guard hace su trabajo
        r = client.delete(f"/api/recepcion-nacional/{rec_id}")
        check("control: con la tabla presente, anular con costeo vivo → 409",
              r.status_code == 409 and "coste" in r.json()["detail"].lower(), r.text)
        # Ahora la MISMA anulación con la tabla de costeo inexistente (MySQL 1146)
        orig_cci = cc_models.ContCompraItem
        try:
            cc_models.ContCompraItem = _ContCompraItemFantasma
            r = client.delete(f"/api/recepcion-nacional/{rec_id}")
        finally:
            cc_models.ContCompraItem = orig_cci
        check("sin la tabla de costeo (1146): anular responde 200, no 500",
              r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        db.rollback()
        check("sin la tabla de costeo: la recepción se borró y el ítem revirtió",
              db.get(RecepcionNacional, rec_id) is None
              and db.get(ItemCotizacion, it.id).estado_item == "comprado")
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        # Limpieza verificada con SESIÓN NUEVA
        db2 = SessionLocal()
        try:
            assert db2.query(Notificacion).filter(
                Notificacion.id > (_SNAP["id"] or 0)).count() == 0
            assert db2.query(Cotizacion).filter(
                Cotizacion.numero.like(f"{MARK}%")).count() == 0
            assert db2.query(OcProveedor).filter(
                OcProveedor.numero.like(f"{MARK}%")).count() == 0
            assert db2.query(ContCompra).filter(
                ContCompra.numero_documento.like(f"{MARK}%")).count() == 0
        finally:
            db2.close()
        print("Cleanup OK (verificado con sesión nueva)")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_avisos_y_deploy_parcial_recepcion_nacional():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
