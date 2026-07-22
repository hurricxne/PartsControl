"""Test de integración del módulo Recepción Nacional contra la DB local.

Monta el router en una app efímera (sin tocar main.py), sobreescribe la auth para
simular usuarios de distintas empresas, ejerce todos los flujos (registrar entrega,
cerrar, anular con sus guards direccionalmente seguros, pendientes, scope) y LIMPIA
todo lo que creó al terminar (deja la DB intacta). Patrón del programador:
check(name, cond, extra) + MARK único + limpieza total en finally + db.rollback()
antes de re-leer (REPEATABLE READ) + wrapper def test_...() para pytest.

Corre con:  ./venv/bin/python recepcion_nacional/tests/test_integration.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, OcProveedor, OcProveedorItem,
    Despacho, DespachoItem,
)
import embarques_pricing.models  # noqa: E402,F401  (emb_pricing_gasto: FK destino de cont_compra)
from compras_contab.models import ContCompra, ContCompraItem  # noqa: E402
from recepcion_nacional.models import RecepcionNacional, RecepcionNacionalItem  # noqa: E402
from recepcion_nacional.router import router as rn_router  # noqa: E402
from routers.compras import router as compras_router  # noqa: E402  (guard anti-embarque)

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_RN__"
CURRENT = {"empresa": "mineria", "id": None}

from fastapi import Depends  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from database import get_db  # noqa: E402

app = FastAPI()
app.include_router(rn_router, prefix="/api")
app.include_router(compras_router, prefix="/api")
# Auth REALISTA: además de devolver el usuario, hace una lectura en la MISMA sesión del
# request, igual que auth.get_current_user en producción. Ese SELECT abre el read view de
# MySQL (REPEATABLE READ) ANTES de cualquier with_for_update(), que es la condición real
# bajo la que corren los endpoints. Con un lambda "seco", el lock terminaba siendo la
# PRIMERA sentencia y el snapshot nacía DESPUÉS del lock: las carreras de plata quedaban
# invisibles para los tests.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _escenario(db, cantidades=(10,), tipo_origen="nacional", estado_item="comprado"):
    """Cotización + N ítems + OC cliente + OC proveedor (tipo_origen) + asignaciones."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} CLIENTE",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = ItemCotizacion(cotizacion_id=cot.id, item_num=n,
                            numero_parte=f"P-{MARK}-{n}", descripcion=f"Parte {n}",
                            cantidad=cant, estado_item=estado_item)
        db.add(it); items.append(it)
    db.flush()
    occ = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OCC", fecha_oc="2026-07-18")
    db.add(occ); db.flush()
    ocp = OcProveedor(numero=f"{MARK}-OCP", numero_oc=f"{MARK}-PROV-DOC",
                      proveedor=f"{MARK} PROVEEDOR", moneda="CLP", tipo_origen=tipo_origen)
    db.add(ocp); db.flush()
    opis = []
    for it in items:
        opi = OcProveedorItem(oc_proveedor_id=ocp.id, oc_cliente_id=occ.id,
                              item_cotizacion_id=it.id)
        db.add(opi); opis.append(opi)
    db.commit()
    for obj in [cot, occ, ocp] + items + opis:
        db.refresh(obj)
    return cot, occ, ocp, items, opis


def _registrar(ocp_id, lineas, cerrar=True, numero_guia="G-1", fecha="2026-07-18"):
    """lineas: [(item_cotizacion_id, qty, estado_recepcion), ...]"""
    body = {
        "oc_proveedor_id": ocp_id, "numero_guia_proveedor": numero_guia,
        "fecha": fecha, "cerrar": cerrar,
        "items": [{"item_cotizacion_id": i, "qty_recibida": q, "estado_recepcion": e}
                  for i, q, e in lineas],
    }
    return client.post("/api/recepcion-nacional", json=body)


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    item_ids = ([i.id for i in db.query(ItemCotizacion)
                 .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all()] if cot_ids else [])
    ocps = db.query(OcProveedor).filter(OcProveedor.numero.like(f"{MARK}%")).all()
    ocp_ids = [o.id for o in ocps]
    occs = ([o.id for o in db.query(OcCliente)
             .filter(OcCliente.cotizacion_id.in_(cot_ids)).all()] if cot_ids else [])

    # Recepciones nacionales de esas OC (CASCADE borra sus líneas al delete la cabecera)
    if ocp_ids:
        for rec in db.query(RecepcionNacional).filter(
                RecepcionNacional.oc_proveedor_id.in_(ocp_ids)).all():
            db.delete(rec)
        db.flush()
    # cont_compra_item + cont_compra de prueba
    if item_ids:
        db.query(ContCompraItem).filter(
            ContCompraItem.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=False)
    for c in db.query(ContCompra).filter(ContCompra.numero_documento.like(f"{MARK}%")).all():
        db.delete(c)
    db.flush()
    # Despachos de esas OC cliente
    if occs:
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(occs)).all()]
        if desp_ids:
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
    if ocp_ids:
        db.query(OcProveedorItem).filter(
            OcProveedorItem.oc_proveedor_id.in_(ocp_ids)).delete(synchronize_session=False)
        db.query(OcProveedor).filter(OcProveedor.id.in_(ocp_ids)).delete(synchronize_session=False)
    if occs:
        db.query(OcCliente).filter(OcCliente.id.in_(occs)).delete(synchronize_session=False)
    if cot_ids:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.commit()


def run():
    db = SessionLocal()
    try:
        _limpiar(db)
        CURRENT["empresa"] = "mineria"

        # ═══ 1. Registrar entrega sobre OC INTERNACIONAL → 400 ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,), tipo_origen="internacional")
        r = _registrar(ocp.id, [(it.id, 10, "completo")])
        check("registrar sobre OC internacional → 400",
              r.status_code == 400 and "internacional" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 2. Ítem que no pertenece a la OC → 400 ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        r = _registrar(ocp.id, [(it.id + 9_000_000, 5, "completo")])
        check("ítem ajeno a la OC → 400",
              r.status_code == 400 and "no pertenece" in r.json()["detail"], r.text)
        _limpiar(db)

        # ═══ 3. Ítem en estado no recibible (embarcado) → 400 ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,), estado_item="embarcado")
        r = _registrar(ocp.id, [(it.id, 5, "completo")])
        check("ítem 'embarcado' no recibible → 400", r.status_code == 400, r.text)
        _limpiar(db)

        # ═══ 4. Validaciones de línea ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        r = _registrar(ocp.id, [(it.id, -3, "completo")])
        check("qty negativa rechazada (400/422)", r.status_code in (400, 422), r.text)
        r = _registrar(ocp.id, [(it.id, 0, "completo")])
        check("utilizable con qty 0 → 400", r.status_code == 400, r.text)
        r = _registrar(ocp.id, [(it.id, 5, "xxx")])
        check("estado_recepcion inválido → 400", r.status_code == 400, r.text)
        r = _registrar(ocp.id, [(it.id, 3, "completo"), (it.id, 2, "completo")])
        check("ítem repetido en el payload → 400", r.status_code == 400, r.text)
        _limpiar(db)

        # ═══ 5. Cierre: solo utilizables con qty>0 pasan a en_bodega ═══
        cot, occ, ocp, (a, b, c, d), _ = _escenario(db, (10, 10, 10, 10))
        r = _registrar(ocp.id, [
            (a.id, 10, "completo"),           # utilizable qty>0 → en_bodega
            (b.id, 0, "no_llego"),            # 0 → sigue comprado
            (c.id, 0, "danado_no_utilizable"),# 0 → sigue comprado
            (d.id, 0, "faltante"),            # faltante qty 0 → 400 (utilizable con 0)
        ])
        check("faltante con qty 0 en el lote → 400 (todo el lote rechazado)",
              r.status_code == 400, r.text)
        # Ahora un lote válido: a completo 10, b no_llego 0, c danado_no_utilizable 0
        r = _registrar(ocp.id, [
            (a.id, 10, "completo"), (b.id, 0, "no_llego"), (c.id, 0, "danado_no_utilizable")])
        check("registrar lote mixto válido → 200", r.status_code == 200, r.text)
        db.rollback()
        check("cierre: 'completo' qty10 → en_bodega",
              db.get(ItemCotizacion, a.id).estado_item == "en_bodega")
        check("cierre: 'no_llego' → sigue comprado",
              db.get(ItemCotizacion, b.id).estado_item == "comprado")
        check("cierre: 'danado_no_utilizable' → sigue comprado",
              db.get(ItemCotizacion, c.id).estado_item == "comprado")
        _limpiar(db)

        # ═══ 6. Registrar ABIERTA, luego cerrar aparte ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        r = _registrar(ocp.id, [(it.id, 10, "completo")], cerrar=False)
        check("registrar abierta → 200", r.status_code == 200, r.text)
        rec_id = r.json()["id"]
        db.rollback()
        check("abierta: ítem sigue comprado (aún no suma)",
              db.get(ItemCotizacion, it.id).estado_item == "comprado")
        r = client.post(f"/api/recepcion-nacional/{rec_id}/cerrar")
        check("cerrar recepción abierta → 200", r.status_code == 200, r.text)
        r = client.post(f"/api/recepcion-nacional/{rec_id}/cerrar")
        check("cerrar de nuevo → 400 (ya cerrada)", r.status_code == 400, r.text)
        db.rollback()
        check("tras cerrar: ítem en_bodega",
              db.get(ItemCotizacion, it.id).estado_item == "en_bodega")
        _limpiar(db)

        # ═══ 7. Entregas SUCESIVAS (remanente) ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        check("entrega 1 (4) → 200", _registrar(ocp.id, [(it.id, 4, "faltante")]).status_code == 200)
        db.rollback()
        check("tras entrega parcial: ítem en_bodega (remanente por recibir)",
              db.get(ItemCotizacion, it.id).estado_item == "en_bodega")
        # el ítem ahora en_bodega SIGUE siendo recibible (entrega sucesiva)
        check("entrega 2 (6) sobre en_bodega → 200",
              _registrar(ocp.id, [(it.id, 6, "completo")]).status_code == 200)
        rp = client.get(f"/api/recepcion-nacional/pendientes/{ocp.id}").json()
        fila = next((x for x in rp["items"] if x["item_cotizacion_id"] == it.id), None)
        check("pendientes: recibido acumulado = 10, remanente 0",
              fila and fila["recibido"] == 10 and fila["remanente"] == 0, fila)
        _limpiar(db)

        # ═══ 8. Anular ABIERTA → borra sin guardas ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")], cerrar=False).json()["id"]
        r = client.delete(f"/api/recepcion-nacional/{rec_id}")
        check("anular abierta → 200", r.status_code == 200, r.text)
        db.rollback()
        check("anular abierta: la recepción ya no existe",
              db.get(RecepcionNacional, rec_id) is None)
        _limpiar(db)

        # ═══ 9. Anular CERRADA segura → revierte en_bodega→comprado + borra líneas ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")]).json()["id"]
        db.rollback()
        check("pre-anulación: ítem en_bodega",
              db.get(ItemCotizacion, it.id).estado_item == "en_bodega")
        r = client.delete(f"/api/recepcion-nacional/{rec_id}")
        check("anular cerrada segura → 200", r.status_code == 200, r.text)
        db.rollback()
        check("anular cerrada: ítem revierte a 'comprado'",
              db.get(ItemCotizacion, it.id).estado_item == "comprado")
        check("anular cerrada: recepción borrada (CASCADE líneas)",
              db.get(RecepcionNacional, rec_id) is None
              and db.query(RecepcionNacionalItem).filter(
                  RecepcionNacionalItem.recepcion_id == rec_id).count() == 0)
        _limpiar(db)

        # ═══ 10. Anular CERRADA con despacho dependiente → 409 ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")]).json()["id"]
        db.rollback()
        dsp = Despacho(numero_despacho=f"{MARK}-DSP", oc_cliente_id=occ.id,
                       estado="en_preparacion")
        db.add(dsp); db.flush()
        db.add(DespachoItem(despacho_id=dsp.id, item_cotizacion_id=it.id, qty_despachada=10))
        db.commit()
        r = client.delete(f"/api/recepcion-nacional/{rec_id}")
        check("anular cerrada con despacho dependiente → 409",
              r.status_code == 409 and "despach" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 11. Anular CERRADA con costeo dependiente → 409 ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")]).json()["id"]
        db.rollback()
        compra = ContCompra(empresa="mineria", origen="NACIONAL", tipo_gasto="cogs",
                            numero_documento=f"{MARK}-FAC", monto_neto=100000,
                            monto_total_clp=119000, anulado=False)
        db.add(compra); db.flush()
        db.add(ContCompraItem(compra_id=compra.id, item_cotizacion_id=it.id, cantidad=10,
                              costo_total_clp=100000))
        db.commit()
        r = client.delete(f"/api/recepcion-nacional/{rec_id}")
        check("anular cerrada con costeo dependiente → 409",
              r.status_code == 409 and "coste" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 12. Scope por empresa: automotriz → 403 en todo ═══
        cot, occ, ocp, (it,), _ = _escenario(db, (10,))
        CURRENT["empresa"] = "automotriz"
        check("automotriz NO puede registrar (403)",
              _registrar(ocp.id, [(it.id, 10, "completo")]).status_code == 403)
        check("automotriz NO puede ver pendientes (403)",
              client.get(f"/api/recepcion-nacional/pendientes/{ocp.id}").status_code == 403)
        check("automotriz NO puede listar (403)",
              client.get("/api/recepcion-nacional").status_code == 403)
        CURRENT["empresa"] = "mineria"
        _limpiar(db)

        # ═══ 13. GUARD ANTI-EMBARQUE: un ítem nacional NO entra al pipeline de
        #     embarque por NINGÚN camino (hallazgo del dueño: el botón global
        #     "Preparar seleccionados" se los llevaba; el backend es la autoridad) ═══
        cot, occ, ocp, (it_n,), _ = _escenario(db, (10,))
        r = client.post("/api/compras/items/preparar", json={"item_ids": [it_n.id]})
        check("preparar ítem nacional → 400 con mensaje claro",
              r.status_code == 400 and "NACIONAL" in r.json()["detail"]
              and "entrega nacional" in r.json()["detail"], r.text)
        r = client.post("/api/compras/items/preparar-parcial",
                        json={"items": [{"item_id": it_n.id, "cantidad": 4}]})
        check("preparar-parcial ítem nacional → 400", r.status_code == 400, r.text)
        db.rollback()
        check("el ítem nacional sigue 'comprado' (no se preparó nada)",
              db.query(ItemCotizacion.estado_item).filter(
                  ItemCotizacion.id == it_n.id).scalar() == "comprado")
        # Ni siquiera un ítem nacional YA preparado (dato legado/escape previo)
        # puede colarse a un pre-embarque:
        from models.models import PreEmbarque
        pre = PreEmbarque(numero=f"{MARK}-PRE", estado="en_preparacion")
        db.add(pre); db.commit(); db.refresh(pre)
        r = client.post(f"/api/compras/pre-embarques/{pre.id}/items",
                        json={"item_id": it_n.id})
        check("agregar ítem nacional a pre-embarque → 400",
              r.status_code == 400 and "NACIONAL" in r.json()["detail"], r.text)
        db.delete(db.query(PreEmbarque).filter(PreEmbarque.id == pre.id).first())
        db.commit()
        # Cuarto camino (detectado por el certificador): CREAR un pre-embarque con
        # item_ids también debe rechazar el ítem nacional (mismo guard).
        pre_antes = db.query(PreEmbarque).count()
        r = client.post("/api/compras/pre-embarques", json={"item_ids": [it_n.id]})
        db.rollback()
        check("crear pre-embarque con ítem nacional → 400 y NO crea nada",
              r.status_code == 400 and "NACIONAL" in r.json()["detail"]
              and db.query(PreEmbarque).count() == pre_antes
              and db.query(ItemCotizacion.estado_item).filter(
                  ItemCotizacion.id == it_n.id).scalar() == "comprado", r.text)
        _limpiar(db)
        # Regresión: un ítem INTERNACIONAL se prepara como siempre
        cot, occ, ocp_i, (it_i,), _ = _escenario(db, (10,), tipo_origen="internacional")
        r = client.post("/api/compras/items/preparar", json={"item_ids": [it_i.id]})
        check("preparar ítem internacional sigue OK (200)",
              r.status_code == 200 and r.json().get("updated") == 1, r.text)
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_recepcion_nacional_integration():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
