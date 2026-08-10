"""Venta a CLIENTE PARTICULAR (2026-08-08): sin orden de compra, el respaldo es la cotización.

EL PROBLEMA
-----------
Un consumidor final no emite orden de compra, pero el cierre la exigía sí o sí. Y no
alcanzaba con saltarse ese 400: `oc_cliente` + `oc_fecha` son la REFERENCIA 801 del SII
en todos los DTE de la venta (guía 52 y factura 33), así que una venta cerrada sin esos
datos quedaba imposible de despachar con guía y de facturar.

LA SOLUCIÓN QUE ESTO FIJA
-------------------------
La casilla «Cliente particular (sin OC)» hace que el documento de respaldo sea NUESTRA
cotización: se graban su N° y su FECHA DE EMISIÓN. La referencia 801 sigue apuntando a
un documento real y verificable.

Lo que se fija, punto por punto:
  1. Sin OC y sin la marca → 400, y el mensaje NOMBRA la salida (el operador que ve el
     error es justo el que no sabe qué poner).
  2. Con la marca → cierra, y `oc_cliente` es el N° de la cotización y `oc_fecha` SU
     fecha de emisión — no la de hoy: folio y fecha tienen que ser del MISMO documento
     o la referencia 801 cita un papel que no existe.
  3. La marca queda PERSISTIDA y se sirve en el detalle (si no, la pantalla no puede
     reabrir el cierre con la casilla puesta y la OC parece un error de digitación).
  4. SONDA ANTI-BYPASS: con un DTE vivo, marcar la casilla NO puede reescribir la
     referencia 801 — tiene que chocar con el mismo 409 (guard W6) que una edición
     manual. Es el candado que impediría cambiar la referencia de una venta ya
     facturada.
  5. La OC real del cliente sigue mandando cuando no hay marca (no se rompe el flujo
     de empresa), y desmarcar vuelve a exigirla.

Datos MARCADOS y limpieza total al terminar.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_cliente_sin_oc.py -q
"""
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaLog, MonzaNotificacion,
)
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402

MARK = "test-sin-oc"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(cotizaciones_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz", rol="admin")
client = TestClient(app)

_creados = {"cots": [], "clientes": []}


def _seed(sufijo: str) -> tuple:
    """Cotización ENVIADA (el único estado desde el que se cierra) con un ítem."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} Particular {sufijo}", rut="76.086.428-5")
        db.add(cli)
        db.flush()
        cot = MonzaCotizacion(
            numero=f"{MARK}-{sufijo}-{uuid.uuid4().hex[:4]}", cliente_id=cli.id,
            estado="enviada", total_neto=100_000, iva_monto=19_000,
            total_bruto=119_000, iva_pct=19, forma_pago="contado",
        )
        db.add(cot)
        db.flush()
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion=f"Pieza {sufijo}", numero_parte=f"NP-{sufijo}",
            cantidad=1, precio_unitario_clp=100_000, subtotal_clp=100_000,
            estado_linea="cotizado",
        )
        db.add(it)
        db.commit()
        _creados["cots"].append(cot.id)
        _creados["clientes"].append(cli.id)
        return cot.id, cot.numero, cot.fecha_creacion.date().isoformat()
    finally:
        db.close()


def _cot(cot_id: int) -> MonzaCotizacion:
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    finally:
        db.close()


def _cerrar(cot_id: int, **extra):
    body = {"estado": "vendida"}
    body.update(extra)
    return client.patch(f"/api/monza/cotizaciones/{cot_id}", json=body)


def _limpiar():
    db = SessionLocal()
    try:
        for cid in _creados["cots"]:
            db.query(MonzaCotizacionItem).filter(
                MonzaCotizacionItem.cotizacion_id == cid).delete(synchronize_session=False)
            db.execute(__import__("sqlalchemy").text(
                "DELETE FROM monza_cotizacion_cierre WHERE cotizacion_id = :c"), {"c": cid})
            db.query(MonzaCotizacion).filter(
                MonzaCotizacion.id == cid).delete(synchronize_session=False)
        if _creados["clientes"]:
            db.query(MonzaCliente).filter(
                MonzaCliente.id.in_(_creados["clientes"])).delete(synchronize_session=False)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=False)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.titulo.like(f"%{MARK}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_cliente_sin_oc():
    fails = []

    def check(nombre, ok, detalle=None):
        if not ok:
            fails.append(nombre)
            print(f"  ✗ {nombre} → {detalle}")
        else:
            print(f"  ✓ {nombre}")

    try:
        # ── 1) Sin OC y sin marca: 400 que ENSEÑA la salida ───────────────────
        cot_id, _numero, _fecha = _seed("A")
        r = _cerrar(cot_id)
        check("1a sin OC no cierra (400)", r.status_code == 400, r.text[:160])
        detalle = r.json().get("detail", "")
        check("1b el error nombra la casilla del cliente particular",
              "particular" in detalle.lower(), detalle)
        check("1c …y la venta NO quedó cerrada", (_cot(cot_id).estado or "") == "enviada",
              _cot(cot_id).estado)

        # ── 2) Con la marca: cierra usando la cotización como respaldo ────────
        r = _cerrar(cot_id, cliente_sin_oc=True)
        check("2a con la marca cierra (200)", r.status_code == 200, r.text[:200])
        c = _cot(cot_id)
        check("2b la venta quedó vendida", (c.estado or "") == "vendida", c.estado)
        check("2c la OC es el N° de ESTA cotización", (c.oc_cliente or "") == c.numero,
              (c.oc_cliente, c.numero))
        check("2d la fecha de OC es la de EMISIÓN de la cotización (no hoy)",
              c.oc_fecha == c.fecha_creacion.date(), (c.oc_fecha, c.fecha_creacion))
        check("2e la marca quedó persistida", int(c.cliente_sin_oc or 0) == 1, c.cliente_sin_oc)

        # ── 3) El detalle la sirve (la pantalla reabre con la casilla puesta) ──
        r = client.get(f"/api/monza/cotizaciones/{cot_id}")
        check("3 el detalle expone cliente_sin_oc=true",
              r.status_code == 200 and r.json().get("cliente_sin_oc") is True,
              r.json().get("cliente_sin_oc") if r.status_code == 200 else r.text[:120])

        # ── 4) SONDA ANTI-BYPASS: con DTE vivo, la casilla choca con el guard ─
        # Se simula el DTE vivo escribiendo la fila que lee _dte_vivo_de_la_venta, que es
        # exactamente lo que hace el guard W6 en producción.
        cot2_id, numero2, _f2 = _seed("B")
        r = _cerrar(cot2_id, oc_cliente="OC-REAL-777", oc_fecha="2026-08-01")
        check("4a la OC REAL del cliente sigue mandando sin la marca",
              r.status_code == 200 and (_cot(cot2_id).oc_cliente or "") == "OC-REAL-777",
              (r.status_code, _cot(cot2_id).oc_cliente))
        dte_ok = _simular_dte_vivo(cot2_id)
        if dte_ok:
            r = client.patch(f"/api/monza/cotizaciones/{cot2_id}",
                             json={"cliente_sin_oc": True})
            check("4b SONDA: con DTE vivo, marcar la casilla NO reescribe la referencia 801 (409)",
                  r.status_code == 409, (r.status_code, r.text[:200]))
            check("4c …y la OC real quedó intacta",
                  (_cot(cot2_id).oc_cliente or "") == "OC-REAL-777", _cot(cot2_id).oc_cliente)
        else:
            print("  · 4b/4c omitidos: el módulo DTE de Monza no está instalado en esta BD")

        # ── 5) Desmarcar vuelve a exigir la OC del cliente ────────────────────
        cot3_id, _n3, _f3 = _seed("C")
        r = _cerrar(cot3_id, cliente_sin_oc=False)
        check("5 desmarcada, sigue exigiendo la OC (400)", r.status_code == 400, r.text[:160])

    finally:
        _borrar_dte_simulado()
        _limpiar()

    if fails:
        raise AssertionError(f"{len(fails)} checks fallaron: {fails}")


def _simular_dte_vivo(cot_id: int) -> bool:
    """Escribe una fila de DTE 33 EMITIDO para esa venta (lo que lee el guard W6).

    Devuelve False si el módulo DTE no está instalado en esta base: el resto de la suite
    sigue siendo válido y los dos checks del guard se omiten explícitamente (mejor eso
    que un verde falso)."""
    db = SessionLocal()
    try:
        from monza_wasabil_dte.models import MonzaWasabilDte, STATUS_EMITIDO
        from monza_wasabil_dte.service import TIPO_DOC_FACTURA
        from monza_contabilidad.models import MonzaContFacturaCliente
        f = MonzaContFacturaCliente(cotizacion_id=cot_id, numero_factura=f"{MARK}-F1",
                                    tipo_doc="factura", es_anticipo=0)
        db.add(f)
        db.flush()
        db.add(MonzaWasabilDte(factura_id=f.id, tipo_dte=TIPO_DOC_FACTURA,
                               status_id=STATUS_EMITIDO, folio="9999",
                               empresa="automotriz"))
        db.commit()
        _creados.setdefault("facturas", []).append(f.id)
        return True
    except Exception as e:  # tabla/módulo ausente en esta BD
        db.rollback()
        print(f"  · no se pudo simular el DTE vivo: {type(e).__name__}")
        return False
    finally:
        db.close()


def _borrar_dte_simulado():
    db = SessionLocal()
    try:
        from sqlalchemy import text
        for fid in _creados.get("facturas", []):
            db.execute(text("DELETE FROM monza_wasabil_dte WHERE factura_id = :f"), {"f": fid})
            db.execute(text("DELETE FROM monza_cont_factura_cliente WHERE id = :f"), {"f": fid})
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    test_cliente_sin_oc()
    print("OK")
