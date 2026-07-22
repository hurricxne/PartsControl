"""Tests de CONCURRENCIA del flujo de plata (auditoría 2026-07-21).

Por qué existe este archivo aparte: las demás suites sustituyen `get_current_user`
por un lambda que NO consulta la base. Con ese atajo, el `with_for_update()` del
endpoint termina siendo la PRIMERA sentencia del request y el snapshot de MySQL
(REPEATABLE READ) nace DESPUÉS del lock — así que las carreras nunca aparecen.

En PRODUCCIÓN no es así: el router de contabilidad se declara con
`dependencies=[Depends(require_empresa("mineria"))]`, que depende de
`get_current_user`, que hace `db.query(User)` sobre la MISMA sesión. Ese SELECT abre
el read view ANTES de cualquier lock, y desde ahí toda lectura PLANA (las relaciones
perezosas `factura.cobranzas` / `factura.factoring`) sirve datos VIEJOS: no ve lo que
la transacción gemela acaba de commitear. El lock serializa, pero el tope miente.

Estos tests reproducen esa condición REAL (el override también consulta la BD) y
verifican los topes de plata bajo carrera:
  1. dos cobranzas simultáneas por el saldo completo → solo UNA entra.
  2. dos adelantos aprobados en paralelo → no se aplica más que el saldo.
  3. borrar una cobranza mientras entra otra → saldo persistido correcto.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_concurrencia_plata.py -q
"""
import os
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem, User,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContAdelanto,
    ContFactoring,
)
from tesoreria.models import ConciliacionIngreso  # noqa: E402
import routers.contabilidad as cont  # noqa: E402
from database import get_db  # noqa: E402

MARK = "__TEST_CONCPLATA__"
RONDAS = 4  # la carrera es probabilística: varias rondas para no dar falsos verdes

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(cont.router, prefix="/api")


# El override recibe la MISMA sesión del request (Depends(get_db)) y hace una lectura
# PLANA, igual que auth.py: así el read view nace antes de los locks, como en producción.
# Sin este detalle, estos tests darían verde aunque el bug siguiera vivo.
from fastapi import Depends  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402


def _current_user_real(db: Session = Depends(get_db)):
    u = db.query(User).order_by(User.id.asc()).first()
    if u is None:                       # entorno sin usuarios: objeto mínimo
        from types import SimpleNamespace
        return SimpleNamespace(id=None, empresa="mineria")
    u.empresa = "mineria"               # el candado del router exige minería
    return u


app.dependency_overrides[get_current_user] = _current_user_real
client = TestClient(app)

PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0)} for i in items}
    neto = sum(cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0)) for i in items)
    return items, pmap, {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
                         "total_con_iva_clp": neto + cont._iva_clp(neto)}


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear_venta(db, sufijo="", *, precio=10000.0, cantidad=10):
    cot = Cotizacion(numero=f"{MARK}-COT{sufijo}", cliente=f"{MARK} CLIENTE",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro", cantidad=cantidad, estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC{sufijo}", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    estado="despachado", guia_firmada=1, numero_guia=f"G{oc.id}")
    db.add(desp); db.flush()
    db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id, qty_despachada=cantidad))
    db.commit()
    PRECIOS.clear(); PRECIOS.update({it.id: precio})
    return cot, oc, desp, it


def _factura_emitida(db, oc, desp, folio):
    r = client.post("/api/contabilidad/facturas", json={
        "oc_cliente_id": oc.id, "despacho_id": desp.id,
        "numero_factura": folio, "tipo_doc": "factura", "fecha_emision": "2026-07-21",
    })
    assert r.status_code == 200, r.text
    db.rollback()
    return db.query(ContFacturaCliente).filter(
        ContFacturaCliente.numero_factura == folio).first()


def _estado_real(factura_id):
    """Estado de la factura leído con una CONEXIÓN NUEVA y SQL crudo.

    Imprescindible: la sesión del test también arrastra su propio read view de
    REPEATABLE READ, así que releer con ella (aunque se haga rollback) puede devolver
    valores VIEJOS y hacer fallar un test cuya implementación está correcta — exactamente
    el mismo espejismo que estos tests persiguen en el código de producción."""
    from database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        f = conn.execute(text("SELECT monto_pagado, saldo, estado_pago "
                              "FROM cont_factura_cliente WHERE id=:i"), {"i": factura_id}).fetchone()
        cs = conn.execute(text("SELECT id, monto, medio, adelanto_id "
                               "FROM cont_cobranza WHERE factura_id=:i"), {"i": factura_id}).fetchall()
    return {"pagado": float(f[0] or 0), "saldo": float(f[1] or 0), "estado": f[2],
            "cobranzas": [(c[0], float(c[1]), c[2], c[3]) for c in cs],
            "suma": sum(float(c[1]) for c in cs)}


def _adelantos_reales(oc_id):
    from database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, monto, monto_aplicado FROM cont_adelanto "
                                 "WHERE oc_cliente_id=:i"), {"i": oc_id}).fetchall()
    return [(r[0], float(r[1] or 0), float(r[2] or 0)) for r in rows]


def _en_paralelo(fn, n=2):
    """Dispara n llamadas simultáneas y devuelve sus resultados."""
    barrera = threading.Barrier(n)
    salida = [None] * n

    def _worker(i):
        barrera.wait()
        try:
            salida[i] = fn(i)
        except Exception as e:                      # noqa: BLE001
            salida[i] = ("EXC", repr(e))

    hilos = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return salida


def _limpiar(db):
    db.rollback()
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    if not cots:
        return
    oc_ids = [oc.id for oc in db.query(OcCliente)
              .filter(OcCliente.cotizacion_id.in_([c.id for c in cots])).all()]
    if oc_ids:
        adel_ids = [a.id for a in db.query(ContAdelanto)
                    .filter(ContAdelanto.oc_cliente_id.in_(oc_ids)).all()]
        fac_ids = [f.id for f in db.query(ContFacturaCliente)
                   .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
        if adel_ids:
            db.query(ConciliacionIngreso).filter(
                ConciliacionIngreso.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            cob_ids = [c.id for c in db.query(ContCobranza)
                       .filter(ContCobranza.factura_id.in_(fac_ids)).all()]
            if cob_ids:
                db.query(ConciliacionIngreso).filter(
                    ConciliacionIngreso.cobranza_id.in_(cob_ids)).delete(synchronize_session=False)
            db.query(ContCobranza).filter(
                ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFactoring).filter(
                ContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFacturaClienteItem).filter(
                ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
        if adel_ids:
            db.query(ContAdelanto).filter(
                ContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(ContFacturaCliente).filter(
                ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
        if desp_ids:
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(
                Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
    for c in cots:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == c.id).delete(synchronize_session=False)
        db.delete(c)
    db.commit()


def test_concurrencia_flujo_plata():
    cont._precios_de_cotizacion = _fake_precios
    db = SessionLocal()
    try:
        _limpiar(db)

        # ═══ 1 · dos COBRANZAS simultáneas por el saldo completo ═══
        # El modal precarga el monto con el saldo entero, así que dos operadores (o un
        # doble submit) mandan exactamente el mismo monto: el caso por defecto.
        for ronda in range(RONDAS):
            _limpiar(db)
            cot, oc, desp, it = _crear_venta(db, sufijo=f"A{ronda}")
            fac = _factura_emitida(db, oc, desp, f"{MARK[-6:]}A{ronda}")
            bruto = float(fac.monto_bruto)
            res = _en_paralelo(lambda i: client.post(
                f"/api/contabilidad/facturas/{fac.id}/cobranzas",
                json={"monto": bruto, "fecha": "2026-07-21", "medio": "transferencia"},
            ).status_code)
            st = _estado_real(fac.id)
            check(f"1.{ronda} dos cobranzas simultáneas: solo UNA entra (sin sobre-cobro)",
                  abs(st["suma"] - bruto) < 0.01, f"http={res} Σcobranzas={st['suma']} bruto={bruto}")
            check(f"1.{ronda} saldo/monto_pagado persistidos cuadran con las cobranzas",
                  abs(st["pagado"] - st["suma"]) < 0.01
                  and abs(st["saldo"] - (bruto - st["suma"])) < 0.01,
                  f"pagado={st['pagado']} saldo={st['saldo']} Σ={st['suma']}")

        # ═══ 2 · dos ADELANTOS aprobados en paralelo sobre la misma factura ═══
        # Cada uno alcanza para el saldo completo: aplicarlos ambos consumiría plata del
        # cliente contra una factura ya cubierta y el excedente se evaporaría.
        for ronda in range(RONDAS):
            _limpiar(db)
            cot, oc, desp, it = _crear_venta(db, sufijo=f"B{ronda}")
            fac = _factura_emitida(db, oc, desp, f"{MARK[-6:]}B{ronda}")
            bruto = float(fac.monto_bruto)
            a1 = ContAdelanto(oc_cliente_id=oc.id, empresa="mineria", estado="aprobado",
                              monto_esperado=bruto, monto=bruto, monto_aplicado=0)
            a2 = ContAdelanto(oc_cliente_id=oc.id, empresa="mineria", estado="aprobado",
                              monto_esperado=bruto, monto=bruto, monto_aplicado=0)
            db.add_all([a1, a2]); db.commit()
            oc_id = oc.id

            def _aplicar(_i):
                s = SessionLocal()
                try:
                    o = s.query(OcCliente).filter(OcCliente.id == oc_id).with_for_update().first()
                    f = s.query(ContFacturaCliente).filter(
                        ContFacturaCliente.id == fac.id).populate_existing().with_for_update().first()
                    n = cont._aplicar_adelantos_pendientes(s, o, f, usuario_id=None)
                    s.commit()
                    return n
                except Exception as e:                # noqa: BLE001
                    s.rollback(); return ("EXC", repr(e))
                finally:
                    s.close()

            res = _en_paralelo(_aplicar)
            st = _estado_real(fac.id)
            adels = _adelantos_reales(oc.id)
            check(f"2.{ronda} dos adelantos en paralelo: no se aplica más que el saldo",
                  st["suma"] <= bruto + 0.01, f"Σaplicado={st['suma']} bruto={bruto} res={res}")
            # INVARIANTE MAESTRO: monto_aplicado == Σ cobranzas medio='adelanto' del adelanto
            for aid, _monto, aplicado in adels:
                suma_a = sum(m for (_i, m, medio, adel_id) in st["cobranzas"] if adel_id == aid)
                check(f"2.{ronda} invariante monto_aplicado == Σ cobranzas (adelanto {aid})",
                      abs(aplicado - suma_a) < 0.01, f"monto_aplicado={aplicado} Σ={suma_a}")
            # el excedente NO se evapora: lo no aplicado sigue disponible
            pendiente = sum(m - ap for (_i, m, ap) in adels)
            check(f"2.{ronda} el adelanto no aplicado queda disponible (no se evapora)",
                  abs(pendiente - (2 * bruto - st["suma"])) < 0.01,
                  f"pendiente={pendiente} esperado={2 * bruto - st['suma']}")
            check(f"2.{ronda} saldo persistido cuadra con lo aplicado",
                  abs(st["pagado"] - st["suma"]) < 0.01,
                  f"pagado={st['pagado']} Σ={st['suma']}")

        # ═══ 3 · BORRAR una cobranza mientras entra otra ═══
        for ronda in range(RONDAS):
            _limpiar(db)
            cot, oc, desp, it = _crear_venta(db, sufijo=f"C{ronda}")
            fac = _factura_emitida(db, oc, desp, f"{MARK[-6:]}C{ronda}")
            bruto = float(fac.monto_bruto)
            mitad = round(bruto / 2, 2)
            r = client.post(f"/api/contabilidad/facturas/{fac.id}/cobranzas",
                            json={"monto": mitad, "fecha": "2026-07-21", "medio": "transferencia"})
            assert r.status_code == 200, r.text
            db.rollback()
            cob0 = db.query(ContCobranza).filter(ContCobranza.factura_id == fac.id).first()

            def _op(i):
                if i == 0:
                    return client.delete(
                        f"/api/contabilidad/facturas/{fac.id}/cobranzas/{cob0.id}").status_code
                return client.post(f"/api/contabilidad/facturas/{fac.id}/cobranzas",
                                   json={"monto": mitad, "fecha": "2026-07-21",
                                         "medio": "transferencia"}).status_code

            res = _en_paralelo(_op)
            st = _estado_real(fac.id)
            check(f"3.{ronda} tras borrar‖cobrar, el saldo PERSISTIDO cuadra con las cobranzas",
                  abs(st["pagado"] - st["suma"]) < 0.01
                  and abs(st["saldo"] - (bruto - st["suma"])) < 0.01,
                  f"pagado={st['pagado']} saldo={st['saldo']} Σreal={st['suma']} http={res}")

        assert not _fails, f"{len(_fails)} fallos: {_fails}"
    finally:
        cont._precios_de_cotizacion = _orig_precios
        _limpiar(db)
        db.close()


if __name__ == "__main__":
    test_concurrencia_flujo_plata()
    print("\nTODO OK" if not _fails else f"\n{len(_fails)} FALLOS")
