"""Concurrencia del flujo de PLATA en MonzaParts (Fase 1 del port desde Grupo AM).

Espejo de backend/tests_contabilidad/test_concurrencia_plata.py, adaptado a los
modelos monza_*. Verifica las carreras que la auditoría 2026-07-21 encontró en Grupo
AM y que en Monza estaban vivas por el mismo motivo estructural:

  1. dos cobranzas simultáneas por el saldo completo → solo UNA entra (sin sobre-cobro).
  2. borrar una cobranza ‖ registrar otra → el saldo PERSISTIDO cuadra con las filas.
  3. revertir DOS cobranzas de adelanto en paralelo → monto_aplicado no sufre un
     "lost update" (era el gap propio de Monza: eliminar_cobranza sostenía solo el lock
     de la factura, mientras los otros escritores de monto_aplicado sostienen el de la
     COTIZACIÓN — locks disjuntos, sin exclusión mutua).
  4. guard de factoring vigente al REVERTIR (era asimétrico: registrar sí lo tenía).

DOS DETALLES DEL ARNÉS QUE SON EL PUNTO (no simplificar):

· El override de `get_current_user` CONSULTA la base en la MISMA sesión, como en
  producción. Con el `lambda` seco de las otras suites, el `FOR UPDATE` pasa a ser la
  primera sentencia del request y el read view nace DESPUÉS del lock: las carreras se
  vuelven invisibles (medido en Grupo AM: 0/4 rondas fallan con el lambda, 4/4 con el
  login real).
· Las verificaciones leen con CONEXIÓN NUEVA y SQL crudo. La sesión del propio test
  arrastra su read view y puede dar falso rojo Y falso verde.

Corre con:  cd backend && ./venv/bin/python monza_contabilidad/tests/test_concurrencia_plata.py
"""
import os
import sys
import threading
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import User  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.router import router as contab_router  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContAdelanto, MonzaContCobranza, MonzaContFacturaCliente, MonzaContFactoring,
)

MARK = "__TEST_MCONC__"
RONDAS = 4          # la carrera es probabilística: varias rondas evitan falsos verdes

app = FastAPI()


# Auth REALISTA: consulta la BD en la misma sesión del request, como producción.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    u = db.query(User).order_by(User.id.asc()).first()
    return SimpleNamespace(id=getattr(u, "id", None), empresa="automotriz",
                           email="t@monzaparts.cl")


app.dependency_overrides[get_current_user] = _cu
app.include_router(contab_router)
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK   | " if cond else "FAIL | ") + name + ("" if cond else f"  -> {str(extra)[:260]}"))
    if not cond:
        _fails.append(name)


# ─── Verificación: SIEMPRE con conexión nueva ─────────────────────────────────
def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _factura_real(fid):
    r = _sql("SELECT monto_bruto,monto_pagado,saldo,estado_pago "
             "FROM monza_cont_factura_cliente WHERE id=:i", i=fid)[0]
    cobs = _sql("SELECT id,monto,medio FROM monza_cont_cobranza WHERE factura_id=:i", i=fid)
    return {"bruto": float(r[0] or 0), "pagado": float(r[1] or 0), "saldo": float(r[2] or 0),
            "estado": r[3], "cobranzas": [(c[0], float(c[1]), c[2]) for c in cobs],
            "suma": sum(float(c[1]) for c in cobs)}


def _adelanto_real(cot_id):
    r = _sql("SELECT id,monto,monto_aplicado FROM monza_cont_adelanto "
             "WHERE cotizacion_id=:i", i=cot_id)
    return {"id": r[0][0], "monto": float(r[0][1] or 0), "aplicado": float(r[0][2] or 0)} if r else None


def _en_paralelo(fn, n=2):
    barrera = threading.Barrier(n)
    salida = [None] * n

    def _worker(i):
        barrera.wait()
        try:
            salida[i] = fn(i)
        except Exception as e:                       # noqa: BLE001
            salida[i] = ("EXC", repr(e))

    hilos = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return salida


# ─── Semilla ──────────────────────────────────────────────────────────────────
def _crear_venta_con_factura(db, sufijo, *, bruto=119000.0):
    cli = mm.MonzaCliente(nombre=f"{MARK} Cliente {sufijo}", rut="22.222.222-2")
    db.add(cli); db.flush()
    neto = round(bruto / 1.19, 2)
    cot = mm.MonzaCotizacion(numero=f"{MARK}-{sufijo}", cliente_id=cli.id, estado="vendida",
                             total_neto=neto, iva_monto=bruto - neto, total_bruto=bruto,
                             iva_pct=19, pct_adelanto=0)
    db.add(cot); db.flush()
    fac = MonzaContFacturaCliente(
        cotizacion_id=cot.id, numero_factura=f"{MARK[-6:]}{sufijo}", tipo_doc="factura",
        fecha_emision=date.today(), monto_neto=neto, iva=bruto - neto, monto_bruto=bruto,
        monto_pagado=0, saldo=bruto, estado_pago="por_cobrar",
    )
    db.add(fac); db.flush()
    db.commit()
    return cot.id, fac.id


def _limpiar(db):
    db.rollback()
    cots = db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    if cot_ids:
        fac_ids = [f.id for f in db.query(MonzaContFacturaCliente)
                   .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all()]
        if fac_ids:
            db.query(MonzaContCobranza).filter(
                MonzaContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(MonzaContFactoring).filter(
                MonzaContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
        db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.query(mm.MonzaCliente).filter(
        mm.MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=False)
    db.commit()


def run():
    db = SessionLocal()
    try:
        _limpiar(db)

        # ═══ 1 · dos COBRANZAS simultáneas por el saldo COMPLETO ═══
        # El modal precarga el saldo entero, así que dos operadores (o un doble submit)
        # mandan exactamente el mismo monto: es el caso por defecto, no un borde raro.
        for r in range(RONDAS):
            _limpiar(db)
            cot_id, fac_id = _crear_venta_con_factura(db, f"A{r}")
            bruto = 119000.0
            res = _en_paralelo(lambda i: client.post(
                f"/api/monza/contabilidad/facturas/{fac_id}/cobranzas",
                json={"monto": bruto, "fecha": "2026-07-21", "medio": "transferencia"},
            ).status_code)
            st = _factura_real(fac_id)
            check(f"1.{r} dos cobranzas simultáneas: solo UNA entra (sin sobre-cobro)",
                  abs(st["suma"] - bruto) < 0.01, f"http={res} Σ={st['suma']} bruto={bruto}")
            check(f"1.{r} saldo/monto_pagado persistidos cuadran con las filas",
                  abs(st["pagado"] - st["suma"]) < 0.01
                  and abs(st["saldo"] - (bruto - st["suma"])) < 0.01,
                  f"pagado={st['pagado']} saldo={st['saldo']} Σ={st['suma']}")

        # ═══ 2 · BORRAR una cobranza ‖ REGISTRAR otra ═══
        for r in range(RONDAS):
            _limpiar(db)
            cot_id, fac_id = _crear_venta_con_factura(db, f"B{r}")
            bruto, mitad = 119000.0, 59500.0
            rr = client.post(f"/api/monza/contabilidad/facturas/{fac_id}/cobranzas",
                             json={"monto": mitad, "fecha": "2026-07-21", "medio": "transferencia"})
            assert rr.status_code == 200, rr.text
            cob0 = _factura_real(fac_id)["cobranzas"][0][0]

            def _op(i):
                if i == 0:
                    return client.delete(
                        f"/api/monza/contabilidad/facturas/{fac_id}/cobranzas/{cob0}").status_code
                return client.post(f"/api/monza/contabilidad/facturas/{fac_id}/cobranzas",
                                   json={"monto": mitad, "fecha": "2026-07-21",
                                         "medio": "transferencia"}).status_code

            res = _en_paralelo(_op)
            st = _factura_real(fac_id)
            check(f"2.{r} tras borrar‖cobrar el saldo PERSISTIDO cuadra con las filas reales",
                  abs(st["pagado"] - st["suma"]) < 0.01
                  and abs(st["saldo"] - (bruto - st["suma"])) < 0.01,
                  f"pagado={st['pagado']} saldo={st['saldo']} Σreal={st['suma']} http={res}")

        # ═══ 3 · REVERTIR dos cobranzas de ADELANTO en paralelo (el gap propio de Monza) ═══
        # Sin el lock del adelanto, los dos UPDATE ciegos de monto_aplicado se pisan y la
        # plata del cliente "reaparece" o se pierde.
        for r in range(RONDAS):
            _limpiar(db)
            cot_id, fac_id = _crear_venta_con_factura(db, f"C{r}")
            # MonzaContAdelanto no tiene columna 'estado': lo verificado se marca con
            # fecha_verificacion (el modelo Monza difiere del de Grupo AM).
            adel = MonzaContAdelanto(cotizacion_id=cot_id, fecha_verificacion=date.today(),
                                     monto=119000.0, monto_aplicado=119000.0)
            db.add(adel); db.flush()
            c1 = MonzaContCobranza(factura_id=fac_id, fecha=date.today(), monto=59500.0,
                                   medio="adelanto")
            c2 = MonzaContCobranza(factura_id=fac_id, fecha=date.today(), monto=59500.0,
                                   medio="adelanto")
            db.add_all([c1, c2]); db.flush()
            ids = (c1.id, c2.id)
            db.commit()

            res = _en_paralelo(lambda i: client.delete(
                f"/api/monza/contabilidad/facturas/{fac_id}/cobranzas/{ids[i]}").status_code)
            st = _factura_real(fac_id)
            a = _adelanto_real(cot_id)
            # INVARIANTE MAESTRO: monto_aplicado == Σ cobranzas medio='adelanto' vivas
            suma_adel = sum(m for (_i, m, medio) in st["cobranzas"] if medio == "adelanto")
            check(f"3.{r} INVARIANTE: monto_aplicado == Σ cobranzas de adelanto vivas",
                  a is not None and abs(a["aplicado"] - suma_adel) < 0.01,
                  f"aplicado={a and a['aplicado']} Σvivas={suma_adel} http={res}")
            check(f"3.{r} el monto_aplicado nunca queda negativo ni por sobre el adelanto",
                  a is not None and -0.01 <= a["aplicado"] <= a["monto"] + 0.01,
                  f"aplicado={a and a['aplicado']} monto={a and a['monto']}")

        # ═══ 4 · guard de FACTORING vigente al revertir (era asimétrico) ═══
        _limpiar(db)
        cot_id, fac_id = _crear_venta_con_factura(db, "D0")
        rr = client.post(f"/api/monza/contabilidad/facturas/{fac_id}/cobranzas",
                         json={"monto": 50000.0, "fecha": "2026-07-21", "medio": "transferencia"})
        assert rr.status_code == 200, rr.text
        cob = _factura_real(fac_id)["cobranzas"][0][0]
        db.add(MonzaContFactoring(factura_id=fac_id, estado="vigente",
                                  empresa_factoring=f"{MARK} Factor", monto_adelantado=60000.0))
        db.commit()
        rr = client.delete(f"/api/monza/contabilidad/facturas/{fac_id}/cobranzas/{cob}")
        check("4 con factoring VIGENTE no se puede revertir una cobranza (409)",
              rr.status_code == 409, f"{rr.status_code} {rr.text[:120]}")
        check("4 la cobranza sigue viva tras el rechazo",
              len(_factura_real(fac_id)["cobranzas"]) == 1)

        # ═══ 5 · medio='adelanto' a mano queda rechazado ═══
        _limpiar(db)
        cot_id, fac_id = _crear_venta_con_factura(db, "E0")
        rr = client.post(f"/api/monza/contabilidad/facturas/{fac_id}/cobranzas",
                         json={"monto": 1000.0, "fecha": "2026-07-21", "medio": "adelanto"})
        check("5 cobranza medio='adelanto' escrita a mano → rechazada (400)",
              rr.status_code == 400, f"{rr.status_code} {rr.text[:120]}")
    finally:
        _limpiar(db)
        db.close()
    return not _fails


def test_monza_concurrencia_plata():
    """Wrapper pytest: sin él la suite era INVISIBLE al gate rutinario 'pytest verde'
    (tenía solo `run()` + el bloque __main__, así que sus 9 checks de carreras de PLATA
    no aparecían en ningún nodeid). Es la misma clase de bug que la auditoría de
    paridad del 28-07 cazó y que se colgó de nuevo; molde: test_aud_guards.py."""
    assert run(), f"fallas: {_fails}"


if __name__ == "__main__":
    ok = run()
    print("\n=== TODO OK ===" if ok else f"\n=== {len(_fails)} FALLOS: {_fails} ===")
    sys.exit(0 if ok else 1)
