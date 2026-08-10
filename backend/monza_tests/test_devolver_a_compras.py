"""DEVOLVER A COMPRAS desde Seguimiento — el caso BACK ORDER (proveedor Baukat).

EL PROBLEMA
-----------
El pipeline de abastecimiento era de UNA SOLA VÍA:
    por_comprar → comprado → preparado → embarcado → en_bodega
Se emite la OC a Baukat, la línea queda 'comprado' y en Seguimiento, y días después el
proveedor avisa que está en BACK ORDER. Esa mercadería quedaba trabada en Seguimiento
esperando algo que no iba a llegar, sin forma de volver a comprarla a otro proveedor.

LO QUE ESTA SUITE FIJA
----------------------
  1. TOTAL: 'comprado' → 'por_comprar', desligado de su OC (oc_proveedor_id = None), y
     reaparece en el panel de compras.
  2. PARCIAL: 4 de 10 vuelven, 6 siguen compradas CON su OC. Con el INVARIANTE DE PLATA
     de la regla de oro del split: Σ cantidad y Σ subtotal se conservan exactos — partir
     una línea toca la foto de precios congelada de la venta y la cabecera no se
     recalcula nunca más.
  3. Los CANDADOS, uno por riesgo real:
     · estado: solo desde 'comprado' (lo preparado/embarcado ya salió del proveedor);
     · PLATA: con la factura del proveedor ya costeada en Cuentas por Pagar → 409;
     · recepción: con mercadería ya recibida → 409;
     · motivo obligatorio (esta es la única transición hacia atrás: sin motivo nadie
       sabe después si fue back order, error o cancelación);
     · cantidad mayor que la línea → 400 explícito (nunca el clamp silencioso).
  4. SONDA DE PODER DISCRIMINANTE (§6): si el endpoint olvidara desligar la OC, la
     línea devuelta seguiría colgando de la OC vieja. El check lo detecta.

Datos MARCADOS y limpieza total al terminar (incluidas las notificaciones, que no
llevan marca y se cuelgan por entidad).

Corre con:  ./venv/bin/python -m pytest monza_tests/test_devolver_a_compras.py -q
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
    MonzaLog, MonzaNotificacion,
)
from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem  # noqa: E402
from monza_router_abastecimiento import router as abastecimiento_router  # noqa: E402

MARK = "__TEST_BACKORDER__"
EMAIL = f"{MARK}@test.invalid"
PRECIO = 12345.0   # 6×p + 4×p == 10×p exacto en float

app = FastAPI()
app.include_router(abastecimiento_router)


# Auth REALISTA (lección G13): lee en la MISMA sesión del request, como producción, así
# el read view nace antes de cualquier FOR UPDATE.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=None, email=EMAIL, empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails: list = []


def check(nombre, ok, extra=""):
    print(("OK  " if ok else "FAIL") + " | " + nombre + ("" if ok else f"  -> {str(extra)[:300]}"))
    if not ok:
        _fails.append(nombre)


def _seed(cantidad: int = 10) -> tuple:
    """Venta cerrada con UNA línea en 'por_comprar' (lista para comprar)."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} Cliente", rut="76.086.428-5")
        db.add(cli)
        db.flush()
        cot = MonzaCotizacion(
            numero=f"BO-{uuid.uuid4().hex[:8]}", cliente_id=cli.id, estado="vendida",
            total_neto=cantidad * PRECIO, iva_pct=19,
            total_bruto=cantidad * PRECIO * 1.19, oc_cliente="OC-BO-1",
        )
        db.add(cot)
        db.flush()
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion=f"{MARK} Filtro", numero_parte="BAU-001",
            cantidad=cantidad, precio_unitario_clp=PRECIO,
            subtotal_clp=int(cantidad * PRECIO), estado_linea="por_comprar",
        )
        db.add(it)
        db.commit()
        return cli.id, cot.id, it.id
    finally:
        db.close()


def _comprar(item_ids: list) -> int:
    r = client.post("/api/monza/abastecimiento/comprar", json={
        "item_ids": item_ids, "proveedor_nombre": "Baukat GmbH", "pais": "Alemania",
        "moneda": "EUR", "tipo_origen": "internacional"})
    assert r.status_code == 200, r.text
    return r.json()["ocp_id"]


def _devolver(items, motivo="Back order confirmado por Baukat"):
    return client.post("/api/monza/abastecimiento/items/devolver-a-compras",
                       json={"items": items, "motivo": motivo})


def _lineas(cot_id: int) -> list:
    """Lectura con CONEXIÓN NUEVA (la sesión del test arrastra su read view)."""
    db = SessionLocal()
    try:
        return db.execute(text(
            "SELECT id, cantidad, subtotal_clp, estado_linea, oc_proveedor_id "
            "FROM monza_cotizacion_items WHERE cotizacion_id = :c ORDER BY id"
        ), {"c": cot_id}).fetchall()
    finally:
        db.close()


def _simular_factura_de_cliente(cot_id: int, item_id: int):
    """Factura de cliente VIVA que congela esa línea (lo que lee el guard de documentos).

    Devuelve el id, o None si el módulo de contabilidad no está en esta base — ahí los
    checks se omiten explícitamente en vez de dar un verde falso."""
    db = SessionLocal()
    try:
        from monza_contabilidad.models import (
            MonzaContFacturaCliente, MonzaContFacturaClienteItem,
        )
        f = MonzaContFacturaCliente(cotizacion_id=cot_id, numero_factura=f"BO-{uuid.uuid4().hex[:6]}",
                                    tipo_doc="factura", es_anticipo=0)
        db.add(f)
        db.flush()
        db.add(MonzaContFacturaClienteItem(factura_id=f.id, item_cotizacion_id=item_id,
                                           cantidad=10, precio_unit_neto=PRECIO,
                                           total_neto=10 * PRECIO))
        db.commit()
        _simulados["facturas"].append(f.id)
        return f.id
    except Exception as e:
        db.rollback()
        print(f"  · no se pudo simular la factura: {type(e).__name__}: {str(e)[:120]}")
        return None
    finally:
        db.close()


def _simular_recepcion(item_id: int, *, estado_recepcion: str, estado: str):
    """Entrega nacional con una línea sobre ese ítem, en el estado pedido."""
    db = SessionLocal()
    try:
        from monza_recepcion_nacional.models import (
            MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
        )
        rec = MonzaRecepcionNacional(estado=estado)
        db.add(rec)
        db.flush()
        db.add(MonzaRecepcionNacionalItem(
            recepcion_id=rec.id, item_cotizacion_id=item_id,
            qty_recibida=0 if estado_recepcion == "no_llego" else 10,
            estado_recepcion=estado_recepcion))
        db.commit()
        _simulados["recepciones"].append(rec.id)
        return True
    except Exception as e:
        db.rollback()
        print(f"  · no se pudo simular la recepción: {type(e).__name__}: {str(e)[:120]}")
        return False
    finally:
        db.close()


_simulados = {"facturas": [], "recepciones": []}


def _limpiar_simulados():
    db = SessionLocal()
    try:
        for fid in _simulados["facturas"]:
            db.execute(text("DELETE FROM monza_cont_factura_cliente_item WHERE factura_id = :f"), {"f": fid})
            db.execute(text("DELETE FROM monza_cont_factura_cliente WHERE id = :f"), {"f": fid})
        for rid in _simulados["recepciones"]:
            db.execute(text("DELETE FROM monza_recepcion_nacional_item WHERE recepcion_id = :r"), {"r": rid})
            db.execute(text("DELETE FROM monza_recepcion_nacional WHERE id = :r"), {"r": rid})
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        cots = [r[0] for r in db.execute(text(
            "SELECT c.id FROM monza_cotizaciones c JOIN monza_clientes cl "
            "ON cl.id = c.cliente_id WHERE cl.nombre LIKE :m"), {"m": f"{MARK}%"})]
        if cots:
            ids_txt = ",".join(str(c) for c in cots)
            items = [r[0] for r in db.execute(text(
                f"SELECT id FROM monza_cotizacion_items WHERE cotizacion_id IN ({ids_txt})"))]
            if items:
                it_txt = ",".join(str(i) for i in items)
                db.execute(text(f"DELETE FROM monza_cont_compra_item WHERE item_cotizacion_id IN ({it_txt})"))
            db.execute(text(f"DELETE FROM monza_cotizacion_items WHERE cotizacion_id IN ({ids_txt})"))
            db.execute(text(f"DELETE FROM monza_cotizaciones WHERE id IN ({ids_txt})"))
        db.execute(text("DELETE FROM monza_cont_compra WHERE acreedor LIKE :m"), {"m": f"{MARK}%"})
        db.execute(text("DELETE FROM monza_oc_proveedor WHERE proveedor_nombre = 'Baukat GmbH' "
                        "AND asesor_email = :e"), {"e": EMAIL})
        db.execute(text("DELETE FROM monza_clientes WHERE nombre LIKE :m"), {"m": f"{MARK}%"})
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=False)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.titulo.like("%devueltos a compras%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_devolver_a_compras():
    try:
        # ══ 1) DEVOLUCIÓN TOTAL ═══════════════════════════════════════════════
        _cli, cot_id, item_id = _seed(10)
        ocp_id = _comprar([item_id])
        r = _devolver([{"item_id": item_id}])
        check("1a devolución total responde 200", r.status_code == 200, r.text[:250])
        filas = _lineas(cot_id)
        check("1b sigue habiendo UNA sola línea (nada se partió)", len(filas) == 1, filas)
        if filas:
            _id, cant, sub, estado, ocp = filas[0]
            check("1c la línea volvió a 'por_comprar'", estado == "por_comprar", estado)
            check("1d SONDA: quedó DESLIGADA de la OC del proveedor", ocp is None, ocp)
            check("1e la cantidad no cambió", int(cant) == 10, cant)
        # Reaparece en el panel de compras (ambos endpoints devuelven LISTA directa).
        r = client.get("/api/monza/abastecimiento/por-comprar")
        ids = [i["id"] for i in r.json()] if r.status_code == 200 else []
        check("1f reaparece en el panel de compras", item_id in ids, (r.status_code, ids[:5]))
        # …y ya NO está en seguimiento
        r = client.get("/api/monza/abastecimiento/seguimiento")
        segs = [i["id"] for i in r.json()] if r.status_code == 200 else []
        check("1g y desapareció de Seguimiento", item_id not in segs, segs[:5])

        # ══ 2) DEVOLUCIÓN PARCIAL + INVARIANTE DE PLATA ═══════════════════════
        _cli2, cot2, item2 = _seed(10)
        ocp2 = _comprar([item2])
        r = _devolver([{"item_id": item2, "cantidad": 4}])
        check("2a parcial responde 200", r.status_code == 200, r.text[:250])
        filas = _lineas(cot2)
        check("2b ahora hay DOS líneas hermanas", len(filas) == 2, filas)
        if len(filas) == 2:
            por_estado = {f[3]: f for f in filas}
            dev = por_estado.get("por_comprar")
            queda = por_estado.get("comprado")
            check("2c la devuelta lleva 4 y está sin OC",
                  dev is not None and int(dev[1]) == 4 and dev[4] is None, dev)
            check("2d la que sigue comprada lleva 6 y CONSERVA su OC",
                  queda is not None and int(queda[1]) == 6 and queda[4] == ocp2, (queda, ocp2))
            # INVARIANTE DE PLATA de la regla de oro del split
            check("2e Σ cantidad se conserva (4+6 = 10)",
                  sum(int(f[1]) for f in filas) == 10, filas)
            check("2f Σ subtotal se conserva (la venta no cambia de monto)",
                  abs(sum(float(f[2] or 0) for f in filas) - 10 * PRECIO) < 1.0,
                  [f[2] for f in filas])

        # ══ 3) CANDADO DE ESTADO ══════════════════════════════════════════════
        _cli3, _cot3, item3 = _seed(5)
        _comprar([item3])
        rp = client.post("/api/monza/abastecimiento/preparar", json={"item_ids": [item3]})
        check("3a (arreglo) el ítem se prepara", rp.status_code == 200, rp.text[:150])
        r = _devolver([{"item_id": item3}])
        check("3b un ítem PREPARADO no se devuelve (400)", r.status_code == 400, r.text[:200])
        check("3c …y el mensaje dice en qué estado está",
              "preparado" in r.text.lower(), r.text[:200])

        # ══ 4) CANDADO DE PLATA (Cuentas por Pagar) ═══════════════════════════
        _cli4, _cot4, item4 = _seed(8)
        ocp4 = _comprar([item4])
        db = SessionLocal()
        try:
            compra = MonzaContCompra(acreedor=f"{MARK} Baukat", numero_documento="F-BAU-99",
                                     oc_proveedor_id=ocp4, estado_pago="pendiente")
            db.add(compra)
            db.flush()
            db.add(MonzaContCompraItem(compra_id=compra.id, item_cotizacion_id=item4,
                                       oc_proveedor_id=ocp4, cantidad=8,
                                       costo_total_clp=100000))
            db.commit()
        finally:
            db.close()
        r = _devolver([{"item_id": item4}])
        check("4a con la compra ya costeada NO se devuelve (409)", r.status_code == 409, r.text[:250])
        check("4b …y el mensaje manda a corregir la compra",
              "compra" in r.text.lower(), r.text[:250])
        filas = _lineas(_cot4)
        check("4c el ítem quedó INTACTO (sigue comprado, con su OC)",
              len(filas) == 1 and filas[0][3] == "comprado" and filas[0][4] == ocp4, filas)

        # ══ 5) MOTIVO Y CANTIDAD ══════════════════════════════════════════════
        _cli5, _cot5, item5 = _seed(10)
        _comprar([item5])
        r = _devolver([{"item_id": item5}], motivo="  ")
        check("5a sin motivo real no se devuelve", r.status_code in (400, 422), r.text[:200])
        r = _devolver([{"item_id": item5, "cantidad": 99}])
        check("5b cantidad mayor que la línea → 400 explícito (no clamp silencioso)",
              r.status_code == 400 and "excede" in r.text.lower(), r.text[:200])
        r = _devolver([{"item_id": item5, "cantidad": 0}])
        check("5c cantidad 0 se RECHAZA (no significa 'todo')", r.status_code == 400, r.text[:200])
        filas = _lineas(_cot5)
        check("5d tras los 3 rechazos el ítem sigue intacto",
              len(filas) == 1 and filas[0][3] == "comprado", filas)

        # ══ 5.e) SONDA: la devolución TOTAL también consulta DOCUMENTOS ═══════
        # Hallazgo HIGH del multienjambre: el guard de documentos vivía dentro del
        # guard del split, que solo corre cuando algo se parte. Una devolución
        # COMPLETA no parte nada, así que un ítem con factura/guía viva volvía a
        # 'por_comprar' sin que nadie lo frenara. Se simula el documento escribiendo
        # la factura de cliente que lee _rechazar_split_sobre_documento.
        _cli7, cot7, item7 = _seed(10)
        _comprar([item7])
        fact_id = _simular_factura_de_cliente(cot7, item7)
        if fact_id:
            r = _devolver([{"item_id": item7}])            # TOTAL: no parte nada
            check("5e SONDA: con factura viva, la devolución TOTAL se bloquea (409)",
                  r.status_code == 409, (r.status_code, r.text[:220]))
            filas = _lineas(cot7)
            check("5f …y el ítem quedó intacto (sigue comprado)",
                  len(filas) == 1 and filas[0][3] == "comprado", filas)
            r = _devolver([{"item_id": item7, "cantidad": 4}])   # PARCIAL: ya se frenaba
            check("5g y la parcial sigue bloqueada igual", r.status_code == 409, r.status_code)
        else:
            print("  · 5e/5f/5g omitidos: no se pudo simular la factura de cliente")

        # ══ 5.h) La recepción CERRADA sin mercadería NO atrapa el back order ══
        # Hallazgo HIGH: el guard miraba TODAS las recepciones. Una entrega nacional
        # ya cerrada en la que el proveedor NO mandó nada deja la línea en 'comprado'
        # con su fila de recepción — y ese es justo el caso que hay que devolver.
        _cli8, cot8, item8 = _seed(10)
        _comprar([item8])
        if _simular_recepcion(item8, estado_recepcion="no_llego", estado="cerrada"):
            r = _devolver([{"item_id": item8}])
            check("5h SONDA: recepción CERRADA sin mercadería NO atrapa el back order (200)",
                  r.status_code == 200, (r.status_code, r.text[:220]))
            filas = _lineas(cot8)
            check("5i …y la línea volvió a compras",
                  len(filas) == 1 and filas[0][3] == "por_comprar", filas)
        else:
            print("  · 5h/5i omitidos: módulo de recepción nacional no disponible")

        # ══ 5.j) …pero una entrega ABIERTA sí bloquea ═════════════════════════
        _cli9, cot9, item9 = _seed(10)
        _comprar([item9])
        if _simular_recepcion(item9, estado_recepcion="completo", estado="abierta"):
            r = _devolver([{"item_id": item9}])
            check("5j una entrega nacional EN CURSO sí bloquea (409)",
                  r.status_code == 409 and "curso" in r.text.lower(), (r.status_code, r.text[:220]))
            filas = _lineas(cot9)
            check("5k …y el ítem quedó intacto", len(filas) == 1 and filas[0][3] == "comprado", filas)
        else:
            print("  · 5j/5k omitidos: módulo de recepción nacional no disponible")

        # ══ 6) LA OC INFORMA CUÁNTAS LÍNEAS LE QUEDAN ═════════════════════════
        _cli6, _cot6, item6 = _seed(10)
        ocp6 = _comprar([item6])
        r = _devolver([{"item_id": item6, "cantidad": 3}])
        ocs = r.json().get("ocs", []) if r.status_code == 200 else []
        check("6 la respuesta informa el estado de la OC tocada",
              any(o["ocp_id"] == ocp6 and o["items_vivos"] == 1 for o in ocs), ocs)

    finally:
        _limpiar_simulados()   # antes de _limpiar: las facturas cuelgan de la cotización
        _limpiar()

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")


if __name__ == "__main__":
    test_devolver_a_compras()
    print("OK")
