"""Compras nacionales MonzaParts: deploy a medias (M2-BD) y el guard anti-embarque de
`preparar-parcial` medido por ENDPOINT (B1-BD).

DOS AGUJEROS QUE ESTA SUITE CIERRA

1. M2-BD — Anular una recepción cerrada preguntaba por `monza_cont_compra_item` con un
   `try/except ImportError` que protege el IMPORT, no la QUERY. El fallo real de un
   deploy sin `monza_compras_contab.init_db` es MySQL **1146** (ProgrammingError), y el
   retry del endpoint solo atrapa OperationalError 1213/1205 → el operador recibía un
   **500** al anular. MonzaParts está MÁS expuesta que Grupo AM: monza_compras_contab se
   importa DENTRO del gate MONZA_CONTAB_ENABLED (main.py), o sea DESPUÉS del create_all,
   así que `monza_cont_compra_item` NUNCA se autocrea. Ahora
   `_costeo_por_item_disponible` pregunta ANTES de tomar cualquier lock (su rollback de
   rescate soltaría los locks del guard) y la comprobación se apaga sola.

2. B1-BD — El guard anti-embarque de `POST /items/preparar-parcial` no tenía sonda de
   ENDPOINT: el test que existía llamaba `_rechazar_items_nacionales` como FUNCIÓN, así
   que borrar la línea `_rechazar_items_nacionales(db, ids)` de
   `monza_router_abastecimiento::_preparar_parcial_tx` no ponía rojo NINGÚN test de
   Monza. Un ítem de compra nacional se colaría al pipeline de embarque por HTTP y
   además rompería la disjunción que hace correcto el UNION del tope físico en Despachos
   (fuente embarque + fuente nacional nunca suman el MISMO ítem).

SONDAS DE PODER DISCRIMINANTE (dentro de la suite, no de palabra):
  · 3 · se neutraliza `_rechazar_items_nacionales` y el MISMO POST cuela el ítem
    nacional al pipeline (200 + línea partida): es el rojo que hoy nadie veía.
  · 6 · se apaga el arreglo (`_costeo_por_item_disponible` → True) con la tabla
    fantasma puesta y la anulación vuelve a responder **500**.

La tabla inexistente NO se toca en la base: se monkeypatchea `MonzaContCompraItem` por
una clase mapeada sobre un nombre que no existe, en su PROPIO `declarative_base` para
que ningún `Base.metadata.create_all()` de otra suite la cree.

Datos MARCADOS + limpieza total en `finally` + verificación con SESIÓN NUEVA.

Corre con:  ./venv/bin/python -m pytest monza_recepcion_nacional/tests/test_deploy_parcial_y_guard_parcial.py -q
(también:   ./venv/bin/python monza_recepcion_nacional/tests/test_deploy_parcial_y_guard_parcial.py)
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Column, Float, Integer, text  # noqa: E402
from sqlalchemy.orm import Session, declarative_base  # noqa: E402

from database import SessionLocal, engine, Base, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor, MonzaLog,
)
import monza_compras_contab.models as mcc_models  # noqa: E402  (fantasma, secciones 5/6)
import monza_recepcion_nacional.router as mrn_mod  # noqa: E402  (sonda 6)
import monza_router_abastecimiento as abast  # noqa: E402  (sonda 3)
from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem  # noqa: E402
from monza_recepcion_nacional.models import MonzaRecepcionNacional  # noqa: E402
from monza_recepcion_nacional.router import router as mrn_router  # noqa: E402
from monza_router_abastecimiento import router as abast_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_MRNDP__"
EMAIL = f"{MARK}@test.invalid"
PRECIO = 12345          # 6×p + 4×p == 10×p exacto (regla de oro del split)

_fails: list = []


# ── Tabla FANTASMA para reproducir el MySQL 1146 ──────────────────────────────
# En su propio Base: si viviera en el Base del proyecto, cualquier
# `Base.metadata.create_all()` de otra suite la CREARÍA en la base de verdad.
_BaseFantasma = declarative_base()


class _MonzaContCompraItemFantasma(_BaseFantasma):
    """Espejo de `monza_cont_compra_item` sobre un nombre de tabla que NO existe."""
    __tablename__ = "monza_cont_compra_item__inexistente_test"
    id = Column(Integer, primary_key=True)
    compra_id = Column(Integer)
    item_cotizacion_id = Column(Integer)
    cantidad = Column(Float)


app = FastAPI()
# Los routers monza ya traen su prefijo /api/monza/... — se montan SIN prefix.
app.include_router(mrn_router)
app.include_router(abast_router)


# Auth REALISTA (lección G13): la lectura abre el read view de MySQL en la MISMA sesión
# del request, ANTES de cualquier with_for_update(), igual que en producción.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=None, email=EMAIL, empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
# raise_server_exceptions=False: un 500 tiene que llegar como status 500 y no como
# excepción — "no revienta con 500" ES lo que comprueban las secciones 5 y 6.
client = TestClient(app, raise_server_exceptions=False)


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + "| " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ─── Semilla / limpieza ──────────────────────────────────────────────────────
def _escenario(db, cantidades=(10,), tipo_origen="nacional", estado_linea="comprado"):
    """Cliente + cotización vendida + N ítems + OC proveedor (tipo_origen).
    El vínculo ítem↔OC en Monza es directo (oc_proveedor_id), sin OcProveedorItem."""
    suf = uuid.uuid4().hex[:6].upper()
    cli = MonzaCliente(nombre=MARK)
    db.add(cli)
    db.flush()
    # numero es String(20): el ancla de limpieza es el CLIENTE marcado.
    cot = MonzaCotizacion(numero=f"CT-MDP-{suf}", cliente_id=cli.id, estado="vendida")
    db.add(cot)
    db.flush()
    ocp = MonzaOcProveedor(numero=f"{MARK}-OCP-{suf}", numero_oc=f"{MARK}-PROV-DOC",
                           proveedor_nombre=f"{MARK} PROVEEDOR", moneda="CLP",
                           tipo_origen=tipo_origen)
    db.add(ocp)
    db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = MonzaCotizacionItem(cotizacion_id=cot.id, numero_parte=f"P-{MARK}-{n}",
                                 descripcion=f"Parte {n}", cantidad=cant,
                                 estado_linea=estado_linea, oc_proveedor_id=ocp.id,
                                 precio_unitario_clp=PRECIO, subtotal_clp=cant * PRECIO)
        db.add(it)
        items.append(it)
    db.commit()
    for obj in [cot, ocp] + items:
        db.refresh(obj)
    return cot, ocp, items


def _registrar(ocp_id, lineas, cerrar=True):
    return client.post("/api/monza/recepcion-nacional", json={
        "oc_proveedor_id": ocp_id, "numero_guia_proveedor": "G-1",
        "fecha": "2026-07-28", "cerrar": cerrar,
        "items": [{"item_cotizacion_id": i, "qty_recibida": q, "estado_recepcion": e}
                  for i, q, e in lineas],
    })


def _parcial(pedidos):
    return client.post("/api/monza/abastecimiento/items/preparar-parcial",
                       json={"items": pedidos})


def _linea(db, item_id):
    """estado_linea re-leído tras rollback (la sesión del test puede mentir)."""
    db.rollback()
    return db.query(MonzaCotizacionItem.estado_linea).filter(
        MonzaCotizacionItem.id == item_id).scalar()


def _n_items_cot(db, cot_id):
    db.rollback()
    return db.query(MonzaCotizacionItem).filter(
        MonzaCotizacionItem.cotizacion_id == cot_id).count()


def _limpiar(db):
    db.rollback()
    S = "fetch"
    cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
               .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
               .filter(MonzaCliente.nombre == MARK).all()]
    item_ids = ([r[0] for r in db.query(MonzaCotizacionItem.id)
                 .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).all()]
                if cot_ids else [])
    ocp_ids = [r[0] for r in db.query(MonzaOcProveedor.id)
               .filter(MonzaOcProveedor.numero.like(f"{MARK}%")).all()]
    if ocp_ids:
        for rec in db.query(MonzaRecepcionNacional).filter(
                MonzaRecepcionNacional.oc_proveedor_id.in_(ocp_ids)).all():
            db.delete(rec)   # CASCADE borra sus líneas
        db.flush()
    if item_ids:
        db.query(MonzaContCompraItem).filter(
            MonzaContCompraItem.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=S)
    for c in db.query(MonzaContCompra).filter(
            MonzaContCompra.numero_documento.like(f"{MARK}%")).all():
        db.delete(c)
    db.flush()
    db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
    if item_ids:
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.id.in_(item_ids)).delete(synchronize_session=S)
    if cot_ids:
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=S)
    if ocp_ids:
        db.query(MonzaOcProveedor).filter(
            MonzaOcProveedor.id.in_(ocp_ids)).delete(synchronize_session=S)
    db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
    db.commit()


def _con_costeo_vivo(db, item_id, cantidad=10):
    """Una compra ACTIVA que costea el ítem: es lo que normalmente bloquea la
    anulación con 409 (y lo que el 1146 hacía reventar con 500)."""
    compra = MonzaContCompra(origen="NACIONAL", tipo_gasto="cogs",
                             numero_documento=f"{MARK}-FAC-{uuid.uuid4().hex[:5]}",
                             monto_neto=100000, monto_total_clp=119000, anulado=False)
    db.add(compra)
    db.flush()
    db.add(MonzaContCompraItem(compra_id=compra.id, item_cotizacion_id=item_id,
                               cantidad=cantidad, costo_total_clp=100000))
    db.commit()


def run():
    resto = -1
    db = SessionLocal()
    try:
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 1 · B1-BD · el guard anti-embarque, por HTTP, en preparar-parcial
        # ══════════════════════════════════════════════════════════════════════
        cot, ocp, (it_n,) = _escenario(db, (10,))
        r = _parcial([{"item_id": it_n.id, "cantidad": 6}])
        check("1a POST /items/preparar-parcial con ítem NACIONAL → 400 con el mensaje "
              "que manda a 'Registrar entrega nacional'",
              r.status_code == 400 and "NACIONAL" in r.json()["detail"]
              and "entrega nacional" in r.json()["detail"], r.text[:250])
        check("1b el ítem nacional sigue 'comprado' (no se preparó nada)",
              _linea(db, it_n.id) == "comprado")
        check("1c y NO se partió la línea (no nació ningún remanente)",
              _n_items_cot(db, cot.id) == 1, _n_items_cot(db, cot.id))
        # La línea COMPLETA (sin cantidad) tampoco: el guard va antes de todo.
        r = _parcial([{"item_id": it_n.id}])
        check("1d la vía sin cantidad (línea completa) también → 400",
              r.status_code == 400 and "NACIONAL" in r.json()["detail"], r.text[:200])
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 2 · Regresión: un ítem INTERNACIONAL se sigue partiendo igual
        # ══════════════════════════════════════════════════════════════════════
        cot, ocp, (it_i,) = _escenario(db, (10,), tipo_origen="internacional")
        r = _parcial([{"item_id": it_i.id, "cantidad": 6}])
        check("2a ítem INTERNACIONAL parcial 6/10 → 200 (sin regresión)",
              r.status_code == 200 and r.json().get("partidos") == 1, r.text[:250])
        check("2b la línea quedó preparada y nació el remanente",
              _linea(db, it_i.id) == "preparado" and _n_items_cot(db, cot.id) == 2,
              _n_items_cot(db, cot.id))
        _limpiar(db)

        # ── SONDA 3: sin la llamada al guard, el nacional SE CUELA ─────────────
        cot, ocp, (it_s,) = _escenario(db, (10,))
        original = abast._rechazar_items_nacionales
        try:
            abast._rechazar_items_nacionales = lambda db_, ids: None
            r = _parcial([{"item_id": it_s.id, "cantidad": 6}])
        finally:
            abast._rechazar_items_nacionales = original
        check("3 SONDA: borrando la llamada _rechazar_items_nacionales(db, ids), el "
              "MISMO POST cuela el ítem nacional al pipeline de embarque (200 + línea "
              "partida) → el check 1a discrimina de verdad",
              r.status_code == 200 and _linea(db, it_s.id) == "preparado"
              and _n_items_cot(db, cot.id) == 2,
              f"{r.status_code} {r.text[:200]}")
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 4 · Control: con la tabla de costeo PRESENTE el guard hace su trabajo
        # ══════════════════════════════════════════════════════════════════════
        cot, ocp, (it,) = _escenario(db, (10,))
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")]).json()["id"]
        db.rollback()
        _con_costeo_vivo(db, it.id)
        r = client.delete(f"/api/monza/recepcion-nacional/{rec_id}")
        check("4 control: con la tabla presente, anular con costeo vivo → 409",
              r.status_code == 409 and "coste" in r.json()["detail"].lower(), r.text[:250])

        # ══════════════════════════════════════════════════════════════════════
        # 5 · M2-BD · con la tabla de costeo INEXISTENTE (1146) → 200, no 500
        # ══════════════════════════════════════════════════════════════════════
        orig_cci = mcc_models.MonzaContCompraItem
        try:
            mcc_models.MonzaContCompraItem = _MonzaContCompraItemFantasma
            r = client.delete(f"/api/monza/recepcion-nacional/{rec_id}")
        finally:
            mcc_models.MonzaContCompraItem = orig_cci
        check("5a sin la tabla de costeo (1146): anular responde 200, no 500",
              r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        db.rollback()
        check("5b y la anulación se completó: recepción borrada e ítem revertido a "
              "'comprado'",
              db.get(MonzaRecepcionNacional, rec_id) is None
              and _linea(db, it.id) == "comprado")
        _limpiar(db)

        # ── SONDA 6: apagando el arreglo, vuelve el 500 ────────────────────────
        cot, ocp, (it2,) = _escenario(db, (10,))
        rec2_id = _registrar(ocp.id, [(it2.id, 10, "completo")]).json()["id"]
        db.rollback()
        orig_disp = mrn_mod._costeo_por_item_disponible
        try:
            # El código de ANTES: se asume que la tabla está y la query se ejecuta.
            mrn_mod._costeo_por_item_disponible = lambda db_: True
            mcc_models.MonzaContCompraItem = _MonzaContCompraItemFantasma
            r = client.delete(f"/api/monza/recepcion-nacional/{rec2_id}")
        finally:
            mcc_models.MonzaContCompraItem = orig_cci
            mrn_mod._costeo_por_item_disponible = orig_disp
        check("6 SONDA: sin la comprobación previa, el 1146 vuelve a tumbar la "
              "anulación con 500 (el 200 de 5a lo prueba el arreglo)",
              r.status_code == 500, f"{r.status_code} {r.text[:200]}")
        db.rollback()
        check("6b y con el 500 la recepción sigue viva (nada quedó a medias)",
              db.get(MonzaRecepcionNacional, rec2_id) is not None)
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        # Verificación con SESIÓN NUEVA: la del test arrastra su read view.
        db2 = SessionLocal()
        try:
            resto = db2.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).count()
            resto += db2.query(MonzaOcProveedor).filter(
                MonzaOcProveedor.numero.like(f"{MARK}%")).count()
            resto += db2.query(MonzaContCompra).filter(
                MonzaContCompra.numero_documento.like(f"{MARK}%")).count()
            print(f"[cleanup] filas MARCADAS que sobreviven: {resto}")
        finally:
            db2.close()

    assert not _fails and resto == 0, f"fallas={_fails} residuos={resto}"
    print("\n=== TODO OK ===")


def test_monza_deploy_parcial_y_guard_antiembarque_parcial():
    """Wrapper de una línea: sin esto pytest no descubre run() (patrón de la casa; ya
    hubo DOS suites invisibles por olvidarlo)."""
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
