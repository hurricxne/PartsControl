"""Factoring de MonzaParts: guard SII en la ENTRADA y reversión auditada en la SALIDA.

EL AGUJERO QUE CIERRA
    `set_factoring` de MonzaParts NO pedía folio del SII — era el único camino de plata
    que no lo hacía (la cobranza manual y la aplicación de adelantos sí). Se podía ceder
    al factor una acreencia que el SII nunca conoció, y esa fila quedaba después cerrada
    por los CUATRO lados: no se podía liquidar, ni editar a 0, ni eliminar la factura
    (`eliminar_factura` rechaza toda factura con factoring), y la aplicación automática de
    adelantos devolvía 0. Plata del factor amarrada a un documento inexistente y el cupo
    facturable de la mercadería secuestrado para siempre.

LAS DOS MITADES VAN JUNTAS A PROPÓSITO
    Cerrar la entrada sin abrir una salida deja atrapadas las filas que ya existen; abrir
    la salida sin cerrar la entrada es un trapeador bajo una llave abierta. §1-§2 prueban
    el guard nuevo, §4-§9 la reversión.

SONDAS DE PODER DISCRIMINANTE
    · §1a/§2a FALLABAN antes de esta entrega: el guard no existía y el POST devolvía 200.
    · §3 prueba que el guard NO bloquea de más (factura con folio → factoring normal).
      Sin esta sección, un guard que rechaza SIEMPRE pasaría §1 y §2.
    · §5 exige que la puerta se NIEGUE a abrir donde el guard no bloquea: es la mitad que
      impide borrar una cesión al factor que era real.
    · §10 verifica que la reversión no se lleve por delante las cobranzas del CLIENTE.

Sin red y sin emitir: el estado del SII se SIMULA escribiendo la fila DTE a mano.
Datos con MARK propio y limpieza verificada con sesión nueva.

Corre con:
  ./venv/bin/python -m pytest monza_contabilidad/tests/test_revertir_factoring.py -q
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.router import router  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContCobranza, MonzaContFactoring,
    MonzaContFacturaClienteItem, MonzaContAdelanto,
)
from monza_contabilidad.service import MEDIO_FACT_ADELANTO  # noqa: E402

MARK = "__T_MREVF__"
CURRENT = {"empresa": "automotriz", "id": 1}
API = "/api/monza/contabilidad"

app = FastAPI()
app.include_router(router)


def _cu(db: Session = Depends(get_db)):
    """Auth REALISTA: la lectura en la MISMA sesión del request abre el read view antes de
    cualquier with_for_update(), igual que auth.get_current_user en producción."""
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _dte_disponible() -> bool:
    """El módulo DTE es opcional en una BD a medias: sin él la suite se SALTA en vez de
    fallar (mismo criterio que test_ra_ancla_dte_borrado)."""
    db = SessionLocal()
    try:
        from monza_wasabil_dte.models import MonzaWasabilDte  # noqa: F401
        db.execute(text("SELECT 1 FROM monza_wasabil_dte LIMIT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"SKIP | módulo monza_wasabil_dte no disponible en esta BD: {e}")
        return False
    finally:
        db.close()


def _seed_venta(db, sufijo, *, cantidad=10, precio=100000):
    """Venta vendida con 1 ítem despachado y su guía (neto 1.000.000 / bruto 1.190.000)."""
    cli = db.query(mm.MonzaCliente).filter(mm.MonzaCliente.nombre == f"{MARK} Cliente").first()
    if cli is None:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
    neto = cantidad * precio
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-C{sufijo}", cliente_id=cli.id, estado="vendida",
        total_neto=neto, iva_monto=round(neto * 0.19), total_bruto=round(neto * 1.19),
        iva_pct=19, forma_pago="credito", oc_cliente=f"OC-{sufijo}",
    )
    db.add(cot); db.flush()
    item = mm.MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion="Filtro", numero_parte=f"FP-{sufijo}",
        cantidad=cantidad, precio_unitario_clp=precio, subtotal_clp=neto,
        estado_linea="despachado",
    )
    db.add(item); db.flush()
    desp = mm.MonzaDespacho(numero=f"{MARK}-DSP-{sufijo}", cotizacion_id=cot.id,
                            estado="despachado", numero_guia=f"G-{sufijo}",
                            cliente_nombre=cli.nombre)
    db.add(desp); db.flush()
    di = mm.MonzaDespachoItem(despacho_id=desp.id, item_id=item.id, qty_despachada=cantidad)
    db.add(di); db.flush()
    db.commit()
    return cot.id, item.id, di.id


def _facturar(db, cot_id, item_id, di_id, sufijo, *, con_folio: bool, cantidad=10):
    """Factura por HTTP (el camino real). `con_folio=False` la deja SIN N° local, como la
    deja la ventana de la emisión electrónica (la vía manual exige N°: se pone y se quita)."""
    r = client.post(f"{API}/facturas", json={
        "cotizacion_id": cot_id,
        "items": [{"item_cotizacion_id": item_id, "despacho_item_id": di_id,
                   "cantidad": cantidad}],
        "numero_factura": f"{MARK}-F{sufijo}", "plazo_dias": 30,
    })
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    if not con_folio:
        f = db.query(MonzaContFacturaCliente).filter(MonzaContFacturaCliente.id == fid).first()
        f.numero_factura = None
        db.commit()
    return fid


def _sembrar_dte(db, factura_id, *, status_id, folio=None, uuid="uuid-revf"):
    """Estado del SII SIMULADO: la fila escrita a mano (jamás se llama a Wasabil)."""
    from monza_wasabil_dte.models import MonzaWasabilDte
    dte = MonzaWasabilDte(empresa="automotriz", tipo_dte=33, factura_id=factura_id,
                          uuid=uuid, status_id=status_id, folio=folio)
    db.add(dte)
    db.commit()
    return dte.id


def _factoring_legado(db, factura_id, *, monto=500000):
    """Fila de factoring creada SALTÁNDOSE el guard: es exactamente lo que hay hoy en una
    base donde la cesión se registró antes de que el guard existiera. Se agrega también su
    cobranza de adelanto, como la habría creado `set_factoring`."""
    fac = MonzaContFactoring(factura_id=factura_id, empresa_factoring=f"{MARK} Factor SA",
                             id_operacion="OP-9001", monto_adelantado=monto,
                             retencion=100000, estado="vigente")
    db.add(fac)
    db.add(MonzaContCobranza(factura_id=factura_id, monto=monto,
                             medio=MEDIO_FACT_ADELANTO,
                             observaciones=f"{MARK} adelanto factoring"))
    db.commit()
    return fac.id


def limpiar(db):
    db.rollback()
    for f in (db.query(MonzaContFacturaCliente)
              .filter(MonzaContFacturaCliente.numero_cotizacion.like(f"{MARK}%")).all()):
        try:
            from monza_wasabil_dte.models import MonzaWasabilDte
            db.query(MonzaWasabilDte).filter(MonzaWasabilDte.factura_id == f.id).delete(
                synchronize_session=False)
        except ImportError:
            pass
        db.query(MonzaContFactoring).filter(MonzaContFactoring.factura_id == f.id).delete(
            synchronize_session=False)
        db.query(MonzaContCobranza).filter(MonzaContCobranza.factura_id == f.id).delete(
            synchronize_session=False)
        db.query(MonzaContFacturaClienteItem).filter(
            MonzaContFacturaClienteItem.factura_id == f.id).delete(synchronize_session=False)
        db.delete(f)
    db.flush()
    for cot in db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.numero.like(f"{MARK}%")).all():
        for d in db.query(mm.MonzaDespacho).filter(
                mm.MonzaDespacho.cotizacion_id == cot.id).all():
            db.query(mm.MonzaDespachoItem).filter(
                mm.MonzaDespachoItem.despacho_id == d.id).delete(synchronize_session=False)
            db.delete(d)
        db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id == cot.id).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacionItem).filter(
            mm.MonzaCotizacionItem.cotizacion_id == cot.id).delete(synchronize_session=False)
        db.delete(cot)
    db.flush()
    db.query(mm.MonzaCliente).filter(mm.MonzaCliente.nombre.like(f"{MARK}%")).delete(
        synchronize_session=False)
    db.commit()


def verificar_limpieza():
    """Sesión NUEVA (regla de la casa): una sesión reutilizada serviría su snapshot."""
    db2 = SessionLocal()
    try:
        restos = (
            db2.query(mm.MonzaCotizacion).filter(
                mm.MonzaCotizacion.numero.like(f"{MARK}%")).count()
            + db2.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.numero_cotizacion.like(f"{MARK}%")).count()
            + db2.query(mm.MonzaCliente).filter(mm.MonzaCliente.nombre.like(f"{MARK}%")).count()
        )
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db2.close()
    print("Cleanup OK (verificado con sesión nueva)")


def _factoring_de(db, fid):
    return db.query(MonzaContFactoring).filter(MonzaContFactoring.factura_id == fid).first()


def _cobranzas_de(db, fid):
    return db.query(MonzaContCobranza).filter(MonzaContCobranza.factura_id == fid).all()


def run():
    if not _dte_disponible():
        return
    db = SessionLocal()
    limpiar(db)
    try:
        CURRENT["empresa"] = "automotriz"
        cuerpo = {"empresa_factoring": f"{MARK} Factor SA", "id_operacion": "OP-1",
                  "monto_adelantado": 500000, "costo_factoring": 10000}

        # ── 1) GUARD DE ENTRADA: sin documento ante el SII no se cede al factor ──────
        cot1, it1, di1 = _seed_venta(db, "1")
        f1 = _facturar(db, cot1, it1, di1, "1", con_folio=False)
        _sembrar_dte(db, f1, status_id=4)          # RECHAZADO: el SII no lo conoce
        r = client.post(f"{API}/facturas/{f1}/factoring", json=cuerpo)
        check("1a factura sin folio y con DTE no emitido → NO se puede ceder al factor (409)",
              r.status_code == 409, f"{r.status_code} {r.text[:180]}")
        check("1b el mensaje dice qué esperar (el folio) y cómo (Reintentar)",
              "folio" in r.text.lower() and "reintentar" in r.text.lower(), r.text[:200])
        check("1c y NO quedó ninguna fila de factoring", _factoring_de(db, f1) is None)

        # ── 2) El mismo guard en LIQUIDAR ────────────────────────────────────────────
        _factoring_legado(db, f1)                  # fila legada, saltándose el guard
        r = client.post(f"{API}/facturas/{f1}/factoring/liquidar")
        check("2a liquidar un factoring contra un documento inexistente → 409",
              r.status_code == 409, f"{r.status_code} {r.text[:180]}")
        db.expire_all()
        check("2b el factoring sigue VIGENTE (no se liquidó a medias)",
              (_factoring_de(db, f1) or SimpleNamespace(estado=None)).estado == "vigente")

        # ── 3) El guard NO bloquea de más: con folio, el factoring es normal ─────────
        # Sin esta sección, un guard que rechazara SIEMPRE pasaría §1 y §2.
        cot3, it3, di3 = _seed_venta(db, "3")
        f3 = _facturar(db, cot3, it3, di3, "3", con_folio=True)
        _sembrar_dte(db, f3, status_id=3, folio="5003", uuid="uuid-revf3")
        r = client.post(f"{API}/facturas/{f3}/factoring", json=cuerpo)
        check("3a factura CON folio del SII → el factoring se registra (200)",
              r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        check("3b y la fila existe", _factoring_de(db, f3) is not None)

        # ── 4) REVERTIR el zombi: la salida, exactamente donde el guard bloquea ──────
        cobs_antes = len(_cobranzas_de(db, f1))
        r = client.post(f"{API}/facturas/{f1}/factoring/revertir",
                        json={"motivo": "cesión registrada contra un DTE rechazado"})
        check("4a revertir el factoring zombi → 200", r.status_code == 200,
              f"{r.status_code} {r.text[:220]}")
        db.expire_all()
        check("4b la fila de factoring DESAPARECIÓ (si sobrevive, la factura sigue imborrable)",
              _factoring_de(db, f1) is None)
        cobs = _cobranzas_de(db, f1)
        check("4c la cobranza del factor también se fue",
              not any(c.medio == MEDIO_FACT_ADELANTO for c in cobs),
              [(c.medio, c.monto) for c in cobs])
        check("4d delta de cobranzas == -1", len(cobs) == cobs_antes - 1,
              (cobs_antes, len(cobs)))
        f1_row = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id == f1).first()
        check("4e la factura ya no está 'factorizada'",
              f1_row.estado_pago != "factorizada", f1_row.estado_pago)
        check("4f la traza quedó EN EL PRODUCTO (observaciones de la factura)",
              "Factoring REVERTIDO" in (f1_row.observaciones or ""), f1_row.observaciones)
        check("4g con el motivo que escribió el operador",
              "DTE rechazado" in (f1_row.observaciones or ""), f1_row.observaciones)
        check("4h y la respuesta devuelve la traza para la UI",
              (r.json().get("factoring_revertido") or {}).get("monto_adelantado") == 500000,
              r.json().get("factoring_revertido"))

        # ── 4-bis) Y ahora la factura SE PUEDE eliminar (el cupo se libera) ──────────
        # Es el punto del ejercicio: el zombi dejaba la mercadería secuestrada.
        check("4i sin factoring, ya no hay fila que haga imborrable la factura",
              _factoring_de(db, f1) is None)

        # ── 5) La puerta se NIEGA donde el guard no bloquea (cesión REAL) ────────────
        r = client.post(f"{API}/facturas/{f3}/factoring/revertir",
                        json={"motivo": "quiero borrar una cesión que sí existe"})
        check("5a factura CON folio: revertir → 409 (la cesión es un hecho financiero real)",
              r.status_code == 409, f"{r.status_code} {r.text[:200]}")
        check("5b y explica las dos salidas legítimas (liquidar / corregir)",
              "liquida" in r.text.lower() and "corrige" in r.text.lower(), r.text[:260])
        db.expire_all()
        check("5c el factoring real SOBREVIVE intacto", _factoring_de(db, f3) is not None)

        # ── 6) El motivo es obligatorio y no se satisface con espacios ───────────────
        cot6, it6, di6 = _seed_venta(db, "6")
        f6 = _facturar(db, cot6, it6, di6, "6", con_folio=False)
        _sembrar_dte(db, f6, status_id=2, uuid="uuid-revf6")   # procesando
        _factoring_legado(db, f6)
        r = client.post(f"{API}/facturas/{f6}/factoring/revertir", json={"motivo": "       "})
        check("6a motivo de puros espacios → 400 (min_length de Pydantic no los ve)",
              r.status_code in (400, 422), f"{r.status_code} {r.text[:160]}")
        db.expire_all()
        check("6b y no se revirtió nada", _factoring_de(db, f6) is not None)

        # ── 7) Sin operación de factoring no hay nada que revertir ───────────────────
        cot7, it7, di7 = _seed_venta(db, "7")
        f7 = _facturar(db, cot7, it7, di7, "7", con_folio=False)
        _sembrar_dte(db, f7, status_id=4, uuid="uuid-revf7")
        r = client.post(f"{API}/facturas/{f7}/factoring/revertir",
                        json={"motivo": "no existe la operación"})
        check("7 factura sin factoring → 404", r.status_code == 404,
              f"{r.status_code} {r.text[:160]}")

        # ── 8) Candado de conciliación bancaria ──────────────────────────────────────
        from monza_tesoreria.models import MonzaTesConciliacionIngreso
        cob_fact = next(c for c in _cobranzas_de(db, f6) if c.medio == MEDIO_FACT_ADELANTO)
        conc = MonzaTesConciliacionIngreso(cobranza_id=cob_fact.id)
        db.add(conc)
        db.commit()
        r = client.post(f"{API}/facturas/{f6}/factoring/revertir",
                        json={"motivo": "intento con el abono ya conciliado"})
        check("8a con el abono del factor conciliado con el banco → 409",
              r.status_code == 409, f"{r.status_code} {r.text[:200]}")
        check("8b y dice dónde desconciliarlo", "tesorer" in r.text.lower(), r.text[:200])
        db.expire_all()
        check("8c el factoring sigue ahí", _factoring_de(db, f6) is not None)
        db.query(MonzaTesConciliacionIngreso).filter(
            MonzaTesConciliacionIngreso.id == conc.id).delete(synchronize_session=False)
        db.commit()

        # ── 9) Reversión múltiple: la nota se ACUMULA, no se pisa ────────────────────
        r = client.post(f"{API}/facturas/{f6}/factoring/revertir",
                        json={"motivo": "primera reversion de la f6"})
        check("9a desconciliado, ahora sí revierte", r.status_code == 200, r.text[:200])
        _factoring_legado(db, f6, monto=200000)
        r = client.post(f"{API}/facturas/{f6}/factoring/revertir",
                        json={"motivo": "segunda reversion de la f6"})
        check("9b una segunda reversión también funciona", r.status_code == 200, r.text[:200])
        db.expire_all()
        f6_row = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id == f6).first()
        obs = f6_row.observaciones or ""
        check("9c las DOS trazas conviven (la nota se acumula, no se pisa)",
              "primera reversion" in obs and "segunda reversion" in obs, obs[-300:])

        # ── 10) No se lleva por delante las cobranzas del CLIENTE ────────────────────
        cot10, it10, di10 = _seed_venta(db, "10")
        f10 = _facturar(db, cot10, it10, di10, "10", con_folio=False)
        _sembrar_dte(db, f10, status_id=4, uuid="uuid-revf10")
        _factoring_legado(db, f10)
        db.add(MonzaContCobranza(factura_id=f10, monto=70000, medio="transferencia",
                                 observaciones=f"{MARK} pago real del cliente"))
        db.commit()
        r = client.post(f"{API}/facturas/{f10}/factoring/revertir",
                        json={"motivo": "revertir sin tocar el pago del cliente"})
        check("10a revierte", r.status_code == 200, r.text[:200])
        db.expire_all()
        cobs10 = _cobranzas_de(db, f10)
        check("10b la cobranza REAL del cliente sobrevive",
              any(c.medio == "transferencia" and float(c.monto) == 70000 for c in cobs10),
              [(c.medio, float(c.monto)) for c in cobs10])
        check("10c y sólo se fue la del factor",
              not any(c.medio == MEDIO_FACT_ADELANTO for c in cobs10),
              [(c.medio, float(c.monto)) for c in cobs10])
        f10_row = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id == f10).first()
        check("10d los totales se recalcularon con el pago real (saldo = bruto - 70.000)",
              abs(float(f10_row.saldo or 0) - (float(f10_row.monto_bruto or 0) - 70000)) < 1.0,
              (f10_row.monto_bruto, f10_row.saldo))

    finally:
        limpiar(db)
        db.close()
    verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_revertir_factoring_monza():
    run()


if __name__ == "__main__":
    run()
