"""Test de integración del módulo Recepción Nacional MonzaParts contra la DB local.

Espejo de recepcion_nacional/tests/test_integration.py (Grupo AM), adaptado al
modelo Monza: sin tabla OcProveedorItem (pertenencia vía
MonzaCotizacionItem.oc_proveedor_id directo) y estado de línea en `estado_linea`.

Monta el router en una app efímera (sin tocar main.py), sobreescribe la auth para
simular usuarios de distintas empresas, ejerce todos los flujos (registrar entrega,
cerrar, anular con sus guards direccionalmente seguros, pendientes, scope, guard
anti-embarque) y LIMPIA todo lo que creó al terminar (deja la DB intacta). Patrón
del programador: check(name, cond, extra) + MARK único + limpieza total en finally
+ db.rollback() antes de re-leer (REPEATABLE READ) + wrapper def test_...() para
pytest.

Corre con:  ./venv/bin/python monza_recepcion_nacional/tests/test_integration.py
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, Base, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
    MonzaDespacho, MonzaDespachoItem, MonzaEmbarque, MonzaEmbarqueItem, MonzaLog,
)
from monza_recepcion_nacional.models import (  # noqa: E402
    MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
)
from monza_recepcion_nacional.router import router as mrn_router  # noqa: E402
from monza_router_abastecimiento import router as abastecimiento_router  # noqa: E402
from monza_router_logistica import router as logistica_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_MRN__"
EMAIL = f"{MARK}@test.invalid"
CURRENT = {"empresa": "automotriz", "id": None}

app = FastAPI()
# Los routers monza ya traen su prefijo /api/monza/... — se montan SIN prefix.
app.include_router(mrn_router)
app.include_router(abastecimiento_router)   # guard anti-embarque (/preparar)
app.include_router(logistica_router)        # guard anti-embarque (crear_embarque)


# Auth REALISTA: además de devolver el usuario, hace una lectura en la MISMA sesión
# del request, igual que auth.get_current_user en producción. Ese SELECT abre el
# read view de MySQL (REPEATABLE READ) ANTES de cualquier with_for_update(), que es
# la condición real bajo la que corren los endpoints. Con un lambda "seco", el lock
# terminaba siendo la PRIMERA sentencia y el snapshot nacía DESPUÉS del lock: las
# carreras de plata quedaban invisibles para los tests (lección G13).
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


def _escenario(db, cantidades=(10,), tipo_origen="nacional", estado_linea="comprado"):
    """Cliente + cotización vendida + N ítems + OC proveedor (tipo_origen).
    Adaptación Monza: la asignación ítem↔OC es directa (oc_proveedor_id)."""
    suf = uuid.uuid4().hex[:6].upper()
    cli = MonzaCliente(nombre=MARK)
    db.add(cli); db.flush()
    # numero corto (la columna es String(20)); el ancla de limpieza es el cliente MARK.
    cot = MonzaCotizacion(numero=f"CT-MRN-{suf}", cliente_id=cli.id, estado="vendida")
    db.add(cot); db.flush()
    ocp = MonzaOcProveedor(numero=f"{MARK}-OCP-{suf}", numero_oc=f"{MARK}-PROV-DOC",
                           proveedor_nombre=f"{MARK} PROVEEDOR", moneda="CLP",
                           tipo_origen=tipo_origen)
    db.add(ocp); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = MonzaCotizacionItem(cotizacion_id=cot.id,
                                 numero_parte=f"P-{MARK}-{n}", descripcion=f"Parte {n}",
                                 cantidad=cant, estado_linea=estado_linea,
                                 oc_proveedor_id=ocp.id)
        db.add(it); items.append(it)
    db.commit()
    for obj in [cot, ocp] + items:
        db.refresh(obj)
    return cot, ocp, items


def _registrar(ocp_id, lineas, cerrar=True, numero_guia="G-1", fecha="2026-07-28"):
    """lineas: [(item_cotizacion_id, qty, estado_recepcion), ...]"""
    body = {
        "oc_proveedor_id": ocp_id, "numero_guia_proveedor": numero_guia,
        "fecha": fecha, "cerrar": cerrar,
        "items": [{"item_cotizacion_id": i, "qty_recibida": q, "estado_recepcion": e}
                  for i, q, e in lineas],
    }
    return client.post("/api/monza/recepcion-nacional", json=body)


def _limpiar(db):
    db.rollback()
    S = "fetch"
    # Ancla por el cliente MARK (el numero de la cotización es corto, no cabe la marca).
    cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
               .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
               .filter(MonzaCliente.nombre == MARK).all()]
    item_ids = ([i.id for i in db.query(MonzaCotizacionItem)
                 .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).all()]
                if cot_ids else [])
    ocps = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.numero.like(f"{MARK}%")).all()
    ocp_ids = [o.id for o in ocps]

    # Recepciones nacionales de esas OC (CASCADE borra sus líneas al delete la cabecera)
    if ocp_ids:
        for rec in db.query(MonzaRecepcionNacional).filter(
                MonzaRecepcionNacional.oc_proveedor_id.in_(ocp_ids)).all():
            db.delete(rec)
        db.flush()
    # monza_cont_compra_item + monza_cont_compra de prueba (solo si C4 ya desplegó
    # el costeo por ítem — contrato F8; el import falla limpio antes de la Ola 2).
    try:
        from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem
        if item_ids:
            db.query(MonzaContCompraItem).filter(
                MonzaContCompraItem.item_cotizacion_id.in_(item_ids)
            ).delete(synchronize_session=S)
        for c in db.query(MonzaContCompra).filter(
                MonzaContCompra.numero_documento.like(f"{MARK}%")).all():
            db.delete(c)
        db.flush()
    except ImportError:
        pass
    # Despachos de prueba (numero UNIQUE marcado)
    dsp_ids = [d.id for d in db.query(MonzaDespacho)
               .filter(MonzaDespacho.numero.like(f"{MARK}%")).all()]
    if dsp_ids:
        db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id.in_(dsp_ids)).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.id.in_(dsp_ids)).delete(synchronize_session=S)
    # Embarques colados por accidente (el guard debe impedirlos): se rastrean por
    # los ítems del test, no por numero (crear_embarque genera EMB-AAAA-#### propio).
    if item_ids:
        emb_ids = [r[0] for r in db.query(MonzaEmbarqueItem.embarque_id)
                   .filter(MonzaEmbarqueItem.item_id.in_(item_ids)).all()]
        if emb_ids:
            db.query(MonzaEmbarqueItem).filter(
                MonzaEmbarqueItem.embarque_id.in_(emb_ids)).delete(synchronize_session=S)
            db.query(MonzaEmbarque).filter(
                MonzaEmbarque.id.in_(emb_ids)).delete(synchronize_session=S)
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


def _linea(db, item_id):
    """estado_linea re-leído tras rollback (la sesión del test puede mentir)."""
    db.rollback()
    return db.query(MonzaCotizacionItem.estado_linea).filter(
        MonzaCotizacionItem.id == item_id).scalar()


def run():
    db = SessionLocal()
    try:
        _limpiar(db)
        CURRENT["empresa"] = "automotriz"

        # ═══ 1. Registrar entrega sobre OC INTERNACIONAL → 400 ═══
        cot, ocp, (it,) = _escenario(db, (10,), tipo_origen="internacional")
        r = _registrar(ocp.id, [(it.id, 10, "completo")])
        check("registrar sobre OC internacional → 400",
              r.status_code == 400 and "internacional" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 2. Ítem que no pertenece a la OC → 400 (vía oc_proveedor_id directo) ═══
        cot, ocp, (it,) = _escenario(db, (10,))
        cot_b, ocp_b, (it_b,) = _escenario(db, (5,))
        r = _registrar(ocp.id, [(it_b.id, 5, "completo")])
        check("ítem de OTRA OC → 400 'no pertenece'",
              r.status_code == 400 and "no pertenece" in r.json()["detail"], r.text)
        r = _registrar(ocp.id, [(it.id + 9_000_000, 5, "completo")])
        check("ítem inexistente → 400 'no pertenece'",
              r.status_code == 400 and "no pertenece" in r.json()["detail"], r.text)
        _limpiar(db)

        # ═══ 3. Ítem en estado no recibible (embarcado) → 400 ═══
        cot, ocp, (it,) = _escenario(db, (10,), estado_linea="embarcado")
        r = _registrar(ocp.id, [(it.id, 5, "completo")])
        check("ítem 'embarcado' no recibible → 400", r.status_code == 400, r.text)
        _limpiar(db)

        # ═══ 4. Validaciones de línea ═══
        cot, ocp, (it,) = _escenario(db, (10,))
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
        cot, ocp, (a, b, c, d) = _escenario(db, (10, 10, 10, 10))
        r = _registrar(ocp.id, [
            (a.id, 10, "completo"),            # utilizable qty>0 → en_bodega
            (b.id, 0, "no_llego"),             # 0 → sigue comprado
            (c.id, 0, "danado_no_utilizable"), # 0 → sigue comprado
            (d.id, 0, "faltante"),             # faltante qty 0 → 400 (utilizable con 0)
        ])
        check("faltante con qty 0 en el lote → 400 (todo el lote rechazado)",
              r.status_code == 400, r.text)
        # Ahora un lote válido: a completo 10, b no_llego 0, c danado_no_utilizable 0
        r = _registrar(ocp.id, [
            (a.id, 10, "completo"), (b.id, 0, "no_llego"), (c.id, 0, "danado_no_utilizable")])
        check("registrar lote mixto válido → 200", r.status_code == 200, r.text)
        check("cierre: 'completo' qty10 → en_bodega", _linea(db, a.id) == "en_bodega")
        check("cierre: 'no_llego' → sigue comprado", _linea(db, b.id) == "comprado")
        check("cierre: 'danado_no_utilizable' → sigue comprado",
              _linea(db, c.id) == "comprado")
        _limpiar(db)

        # ═══ 6. Registrar ABIERTA, luego cerrar aparte ═══
        cot, ocp, (it,) = _escenario(db, (10,))
        r = _registrar(ocp.id, [(it.id, 10, "completo")], cerrar=False)
        check("registrar abierta → 200", r.status_code == 200, r.text)
        rec_id = r.json()["id"]
        check("abierta: ítem sigue comprado (aún no suma)", _linea(db, it.id) == "comprado")
        r = client.post(f"/api/monza/recepcion-nacional/{rec_id}/cerrar")
        check("cerrar recepción abierta → 200", r.status_code == 200, r.text)
        r = client.post(f"/api/monza/recepcion-nacional/{rec_id}/cerrar")
        check("cerrar de nuevo → 400 (ya cerrada)", r.status_code == 400, r.text)
        check("tras cerrar: ítem en_bodega", _linea(db, it.id) == "en_bodega")
        _limpiar(db)

        # ═══ 7. Entregas SUCESIVAS (remanente) ═══
        cot, ocp, (it,) = _escenario(db, (10,))
        check("entrega 1 (4) → 200",
              _registrar(ocp.id, [(it.id, 4, "faltante")]).status_code == 200)
        check("tras entrega parcial: ítem en_bodega (remanente por recibir)",
              _linea(db, it.id) == "en_bodega")
        # el ítem ahora en_bodega SIGUE siendo recibible (entrega sucesiva)
        check("entrega 2 (6) sobre en_bodega → 200",
              _registrar(ocp.id, [(it.id, 6, "completo")]).status_code == 200)
        rp = client.get(f"/api/monza/recepcion-nacional/pendientes/{ocp.id}").json()
        fila = next((x for x in rp["items"] if x["item_cotizacion_id"] == it.id), None)
        check("pendientes: recibido acumulado = 10, remanente 0",
              fila and fila["recibido"] == 10 and fila["remanente"] == 0, fila)
        _limpiar(db)

        # ═══ 8. Anular ABIERTA → borra sin guardas ═══
        cot, ocp, (it,) = _escenario(db, (10,))
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")], cerrar=False).json()["id"]
        r = client.delete(f"/api/monza/recepcion-nacional/{rec_id}")
        check("anular abierta → 200", r.status_code == 200, r.text)
        db.rollback()
        check("anular abierta: la recepción ya no existe",
              db.get(MonzaRecepcionNacional, rec_id) is None)
        _limpiar(db)

        # ═══ 9. Anular CERRADA segura → revierte en_bodega→comprado + borra líneas ═══
        cot, ocp, (it,) = _escenario(db, (10,))
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")]).json()["id"]
        check("pre-anulación: ítem en_bodega", _linea(db, it.id) == "en_bodega")
        r = client.delete(f"/api/monza/recepcion-nacional/{rec_id}")
        check("anular cerrada segura → 200", r.status_code == 200, r.text)
        check("anular cerrada: ítem revierte a 'comprado'", _linea(db, it.id) == "comprado")
        db.rollback()
        check("anular cerrada: recepción borrada (CASCADE líneas)",
              db.get(MonzaRecepcionNacional, rec_id) is None
              and db.query(MonzaRecepcionNacionalItem).filter(
                  MonzaRecepcionNacionalItem.recepcion_id == rec_id).count() == 0)
        _limpiar(db)

        # ═══ 10. Anular CERRADA con despacho dependiente → 409 ═══
        cot, ocp, (it,) = _escenario(db, (10,))
        rec_id = _registrar(ocp.id, [(it.id, 10, "completo")]).json()["id"]
        db.rollback()
        dsp = MonzaDespacho(numero=f"{MARK}-DSP-{uuid.uuid4().hex[:6].upper()}",
                            cotizacion_id=cot.id, estado="en_preparacion")
        db.add(dsp); db.flush()
        db.add(MonzaDespachoItem(despacho_id=dsp.id, item_id=it.id, qty_despachada=10))
        db.commit()
        r = client.delete(f"/api/monza/recepcion-nacional/{rec_id}")
        check("anular cerrada con despacho dependiente → 409",
              r.status_code == 409 and "despach" in r.json()["detail"].lower(), r.text)
        _limpiar(db)

        # ═══ 11. Anular CERRADA con costeo dependiente → 409 ═══
        # CONTRATO F8 (Ola 2): monza_cont_compra_item lo crea monza_compras_contab
        # (C4). Antes de eso el import falla limpio y el bloque queda SKIP — el gate
        # completo de la Ola 2 lo corre entero.
        try:
            from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem
            _costeo_disponible = True
        except ImportError:
            _costeo_disponible = False
            print("SKIP | bloque 11 (costeo dependiente): monza_cont_compra_item "
                  "aún no desplegado (C4/Ola 2)")
        if _costeo_disponible:
            cot, ocp, (it,) = _escenario(db, (10,))
            rec_id = _registrar(ocp.id, [(it.id, 10, "completo")]).json()["id"]
            db.rollback()
            compra = MonzaContCompra(origen="NACIONAL", tipo_gasto="cogs",
                                     numero_documento=f"{MARK}-FAC", monto_neto=100000,
                                     monto_total_clp=119000, anulado=False)
            db.add(compra); db.flush()
            db.add(MonzaContCompraItem(compra_id=compra.id, item_cotizacion_id=it.id,
                                       cantidad=10, costo_total_clp=100000))
            db.commit()
            r = client.delete(f"/api/monza/recepcion-nacional/{rec_id}")
            check("anular cerrada con costeo dependiente → 409",
                  r.status_code == 409 and "coste" in r.json()["detail"].lower(), r.text)
            _limpiar(db)

        # ═══ 12. Scope por empresa (INVERTIDO vs GA): mineria → 403 en todo ═══
        cot, ocp, (it,) = _escenario(db, (10,))
        CURRENT["empresa"] = "mineria"
        check("mineria NO puede registrar (403)",
              _registrar(ocp.id, [(it.id, 10, "completo")]).status_code == 403)
        check("mineria NO puede ver pendientes (403)",
              client.get(f"/api/monza/recepcion-nacional/pendientes/{ocp.id}").status_code == 403)
        check("mineria NO puede listar (403)",
              client.get("/api/monza/recepcion-nacional").status_code == 403)
        CURRENT["empresa"] = "automotriz"
        _limpiar(db)

        # ═══ 13. GUARD ANTI-EMBARQUE: un ítem nacional NO entra al pipeline de
        #     embarque por NINGUNA de las 2 entradas Monza (preparar / crear
        #     embarque). El backend es la autoridad; la UI además oculta. ═══
        cot, ocp, (it_n,) = _escenario(db, (10,))
        r = client.post("/api/monza/abastecimiento/preparar", json={"item_ids": [it_n.id]})
        check("preparar ítem nacional → 400 con mensaje claro",
              r.status_code == 400 and "NACIONAL" in r.json()["detail"]
              and "entrega nacional" in r.json()["detail"], r.text)
        check("el ítem nacional sigue 'comprado' (no se preparó nada)",
              _linea(db, it_n.id) == "comprado")
        # Segunda entrada: crear embarque en Logística tampoco puede colarlo,
        # y NO debe quedar ningún embarque creado.
        db.rollback()
        emb_antes = db.query(MonzaEmbarque).count()
        r = client.post("/api/monza/logistica/embarques", json={"item_ids": [it_n.id]})
        db.rollback()
        check("crear embarque con ítem nacional → 400 y NO crea nada",
              r.status_code == 400 and "NACIONAL" in r.json()["detail"]
              and db.query(MonzaEmbarque).count() == emb_antes
              and _linea(db, it_n.id) == "comprado", r.text)
        _limpiar(db)
        # Regresión: un ítem INTERNACIONAL se prepara como siempre
        cot, ocp_i, (it_i,) = _escenario(db, (10,), tipo_origen="internacional")
        r = client.post("/api/monza/abastecimiento/preparar", json={"item_ids": [it_i.id]})
        check("preparar ítem internacional sigue OK (200)",
              r.status_code == 200 and r.json().get("preparados") == 1, r.text)
        _limpiar(db)

    finally:
        _limpiar(db)
        db.close()
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_monza_recepcion_nacional_integration():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
