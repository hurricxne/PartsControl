"""E2E del VIAJE DE LA PLATA — espejo Monza de tests_contabilidad/test_viaje_de_la_plata.py.

Sigue UNA venta MonzaParts de punta a punta y verifica, con NÚMEROS en cada paso, las
tres preguntas del negocio: cómo se GUARDA la información, cómo se PAGA, y cómo VIAJA
por Tesorería.

Adaptaciones de modelo respecto de Grupo AM (la especificación es el test GA):
  - La VENTA es la cotización misma (estado 'vendida'): no hay OcCliente.
  - El adelanto vía A lo INFORMA Comercial con pct_adelanto en la cotización y lo
    APRUEBA Tesorería (POST /aprobaciones/{cot}/aprobar), que ahora lo aplica
    RETROACTIVAMENTE a las facturas ya emitidas (endurecimiento de este enjambre).
  - No hay factura de ANTICIPO ni tramo SII/Wasabil (roadmap F7/F5-F6): la factura se
    emite en modo manual con folio digitado — hoy OBLIGATORIO para tipo 'factura'.

Cadena que recorre (misma plata, dos direcciones de aplicación del adelanto):

    Comercial informa adelanto 50% (pct en la venta; aún NO es plata)
        → se emite la 1ª factura (guía parcial) ANTES de aprobar → nace sin cobranzas
        → Tesorería APRUEBA → el adelanto se aplica RETROACTIVO a esa factura
        → 2ª factura (resto de la guía) → el adelanto NO se aplica de nuevo (consumido)
        → el cliente paga los saldos    → cobranzas
        → Tesorería concilia los abonos del banco contra ESAS cobranzas
        → el depósito del adelanto se concilia por su propia vía (abono ↔ adelanto)
        → KPIs de Contabilidad y de Tesorería cuentan lo MISMO

Invariantes verificados (los cinco que costaron plata en la auditoría GA 2026-07-21):
  1. monto_aplicado del adelanto == Σ cobranzas medio='adelanto' ligadas a la venta.
  2. Σ brutos de las facturas de la venta == mercadería vendida (no se cobra dos veces).
  3. Las cobranzas medio='adelanto' NO son conciliables como abono nuevo (esa plata ya
     entró cuando Tesorería aprobó el adelanto; su vía es abono ↔ adelanto).
  4. Un abono del banco no se concilia dos veces (ni una cobranza con dos abonos).
  5. saldo / monto_pagado / estado_pago persistidos cuadran SIEMPRE con las filas reales.

Todo dato lleva MARK y se borra en finally. Las verificaciones leen con CONEXIÓN NUEVA
(engine.connect): la sesión del test arrastra su propio snapshot y puede mentir en
ambas direcciones.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_viaje_de_la_plata.py -q -s
(también:   ./venv/bin/python monza_tests/test_viaje_de_la_plata.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContAdelanto,
)
from monza_contabilidad.router import router as contab_router  # noqa: E402
from monza_tesoreria.router import router as tes_router  # noqa: E402
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesCuentaBancaria, MonzaTesCartola, MonzaTesMovimiento, MonzaTesConciliacion,
    MonzaTesConciliacionIngreso,
)

# monza_cotizaciones.numero es String(20): el MARK se mantiene corto a propósito.
MARK = "__TEST_MVDP__"

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()


# Auth REALISTA (consulta la BD en la misma sesión, como producción): sin esto el
# snapshot nacería después de los locks y las verificaciones serían optimistas.
# Espejo del _cu de monza_tesoreria/tests/test_integration.py.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz")


app.include_router(contab_router)
app.include_router(tes_router)
app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK   | " if cond else "FAIL | ") + name + ("" if cond else f"  -> {str(extra)[:280]}"))
    if not cond:
        _fails.append(name)


# ─── Lecturas de verificación: SIEMPRE con conexión nueva ─────────────────────
def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _factura_real(fid):
    r = _sql("SELECT monto_neto,iva,monto_bruto,monto_pagado,saldo,estado_pago,numero_factura "
             "FROM monza_cont_factura_cliente WHERE id=:i", i=fid)[0]
    cobs = _sql("SELECT id,monto,medio FROM monza_cont_cobranza WHERE factura_id=:i", i=fid)
    return {"neto": float(r[0] or 0), "iva": float(r[1] or 0), "bruto": float(r[2] or 0),
            "pagado": float(r[3] or 0), "saldo": float(r[4] or 0), "estado": r[5],
            "folio": r[6],
            "cobranzas": [(c[0], float(c[1]), c[2]) for c in cobs],
            "suma_cob": sum(float(c[1]) for c in cobs)}


def _adelanto_real(cot_id):
    rows = _sql("SELECT id,monto,monto_aplicado FROM monza_cont_adelanto "
                "WHERE cotizacion_id=:c", c=cot_id)
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "monto": float(r[1] or 0), "aplicado": float(r[2] or 0)}


def _crear_venta(db, *, cantidad=10, precio=100_000.0):
    """Venta de 1 ítem con DOS guías 'despachado' (6 + 4 u): permite facturar en dos
    tandas y ejercitar el adelanto en AMBAS direcciones (retroactivo y en emisión)."""
    cli = mm.MonzaCliente(nombre=f"{MARK} AUTOMOTORA X", rut="11.111.111-1")  # RUT válido: hoy es requisito de la factura
    db.add(cli); db.flush()
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-COT", cliente_id=cli.id, estado="vendida",
        total_neto=cantidad * precio, iva_monto=round(cantidad * precio * 0.19, 0),
        total_bruto=round(cantidad * precio * 1.19, 0), iva_pct=19,
        forma_pago="50_adelanto", pct_adelanto=50, adelanto_verificado=0,
        oc_cliente=f"{MARK}-OC",
    )
    db.add(cot); db.flush()
    it = mm.MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion="FILTRO ACEITE", numero_parte="A0001802609",
        cantidad=cantidad, precio_unitario_clp=precio, subtotal_clp=cantidad * precio,
        estado_linea="despachado",
    )
    db.add(it); db.flush()
    d1 = mm.MonzaDespacho(numero=f"{MARK}-DSP1", cotizacion_id=cot.id,
                          estado="despachado", numero_guia=f"G-{MARK}-1",
                          cliente_nombre=cli.nombre,
                          # regla 2026-08-06: sin firma no se factura (el gate tiene suite propia)
                          guia_firmada=1)
    db.add(d1); db.flush()
    db.add(mm.MonzaDespachoItem(despacho_id=d1.id, item_id=it.id, qty_despachada=6))
    d2 = mm.MonzaDespacho(numero=f"{MARK}-DSP2", cotizacion_id=cot.id,
                          estado="despachado", numero_guia=f"G-{MARK}-2",
                          cliente_nombre=cli.nombre,
                          # regla 2026-08-06: sin firma no se factura (el gate tiene suite propia)
                          guia_firmada=1)
    db.add(d2); db.flush()
    db.add(mm.MonzaDespachoItem(despacho_id=d2.id, item_id=it.id, qty_despachada=4))
    db.commit()
    return cot.id, d1.id, d2.id, it.id


def _limpiar(db):
    """Borra cualquier residuo del MARK en orden seguro de FK (idempotente)."""
    db.rollback()
    # 1) Ventas del MARK: enlaces de conciliación → cobranzas/facturas (cascade) →
    #    adelantos → despachos → ítems → cotización → cliente.
    cots = db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    if cot_ids:
        adel_ids = [a.id for a in db.query(MonzaContAdelanto)
                    .filter(MonzaContAdelanto.cotizacion_id.in_(cot_ids)).all()]
        if adel_ids:
            db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
        facturas = (db.query(MonzaContFacturaCliente)
                    .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all())
        for f in facturas:
            cob_ids = [c.id for c in f.cobranzas]
            if cob_ids:
                db.query(MonzaTesConciliacionIngreso).filter(
                    MonzaTesConciliacionIngreso.cobranza_id.in_(cob_ids)).delete(synchronize_session=False)
            db.delete(f)  # cascade ORM: items + cobranzas + factoring
        db.flush()
        if adel_ids:
            db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
        desp_ids = [d.id for d in db.query(mm.MonzaDespacho)
                    .filter(mm.MonzaDespacho.cotizacion_id.in_(cot_ids)).all()]
        if desp_ids:
            db.query(mm.MonzaDespachoItem).filter(
                mm.MonzaDespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaDespacho).filter(
                mm.MonzaDespacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacionItem).filter(
            mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.query(mm.MonzaCliente).filter(
        mm.MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=False)
    # 2) Tesorería: conciliaciones → movimientos → cartolas → cuentas del MARK.
    ctas = db.query(MonzaTesCuentaBancaria).filter(
        MonzaTesCuentaBancaria.banco.like(f"{MARK}%")).all()
    if ctas:
        cids = [c.id for c in ctas]
        mov_ids = [m.id for m in db.query(MonzaTesMovimiento)
                   .filter(MonzaTesMovimiento.cuenta_id.in_(cids)).all()]
        if mov_ids:
            db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.movimiento_id.in_(mov_ids)).delete(synchronize_session=False)
            db.query(MonzaTesConciliacionIngreso).filter(
                MonzaTesConciliacionIngreso.movimiento_id.in_(mov_ids)).delete(synchronize_session=False)
            db.query(MonzaTesMovimiento).filter(
                MonzaTesMovimiento.id.in_(mov_ids)).delete(synchronize_session=False)
        db.query(MonzaTesCartola).filter(
            MonzaTesCartola.cuenta_id.in_(cids)).delete(synchronize_session=False)
        db.query(MonzaTesCuentaBancaria).filter(
            MonzaTesCuentaBancaria.id.in_(cids)).delete(synchronize_session=False)
    db.commit()


def test_viaje_de_la_plata():
    db = SessionLocal()
    try:
        _limpiar(db)
        # KPIs ANTES de sembrar: el test mide DELTAS, no valores absolutos — la base
        # real del dueño tiene sus propias facturas y un total fijo daría falso rojo.
        kpis_antes = client.get("/api/monza/contabilidad/kpis").json()
        flujo_antes = client.get("/api/monza/tesoreria/flujo-caja").json()
        # Venta: 10 u × $100.000 = $1.000.000 neto → $1.190.000 bruto; adelanto 50%.
        cot_id, d1_id, d2_id, _it_id = _crear_venta(db)
        NETO_VENTA, BRUTO_VENTA, ADELANTO = 1_000_000.0, 1_190_000.0, 595_000.0

        print("\n───────── 1 · COMERCIAL INFORMA EL ADELANTO (aún NO es plata) ─────────")
        r = client.get("/api/monza/tesoreria/aprobaciones")
        check("1 cola de aprobaciones responde", r.status_code == 200, r.text)
        fila = next((x for x in r.json().get("por_aprobar", [])
                     if x.get("cotizacion_id") == cot_id), None)
        check("1 la venta aparece POR APROBAR (informada por Comercial)", fila is not None)
        check("1 estado 'por_verificar' y monto sugerido = 595.000 (50%)",
              fila and fila["estado_adelanto"] == "por_verificar"
              and fila["monto_sugerido_clp"] == ADELANTO, fila)
        check("1 sin registro de adelanto todavía (informado ≠ plata)",
              _adelanto_real(cot_id) is None, _adelanto_real(cot_id))

        print("\n───────── 2 · 1ª FACTURA (guía parcial) ANTES DE APROBAR ─────────")
        r = client.post("/api/monza/contabilidad/facturas",
                        json={"cotizacion_id": cot_id, "despacho_id": d1_id,
                              # plazo LARGO a propósito: con 30 días este test murió
                              # solo el 2026-08-10 (vencimiento 08-09 < hoy → estado
                              # 'vencida' en vez de 'por_cobrar'/'parcial'). Las fechas
                              # de emisión y cobranza son HISTORIA y se quedan fijas;
                              # lo único que no puede envejecer es el vencimiento.
                              "numero_factura": f"{MARK}-F1", "plazo_dias": 3650,
                              "fecha_emision": "2026-07-10"})
        check("2 factura 1 emitida (6 u, con folio obligatorio)", r.status_code == 200, r.text)
        fac1_id = r.json()["id"]
        f1 = _factura_real(fac1_id)
        check("2 bruto = 714.000 (600.000 + IVA) y folio guardado",
              f1["bruto"] == 714_000.0 and f1["folio"] == f"{MARK}-F1", f1)
        check("2 nace SIN cobranzas y con saldo = bruto (el adelanto aún no es plata)",
              f1["cobranzas"] == [] and f1["saldo"] == f1["bruto"]
              and f1["estado"] == "por_cobrar", f1)

        print("\n───────── 3 · TESORERÍA APRUEBA → APLICA RETROACTIVO ─────────")
        r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar",
                        json={"monto": ADELANTO, "fecha_pago": "2026-07-05",
                              "banco": "BCI", "numero_operacion": f"{MARK}-OP1"})
        check("3 Tesorería aprueba el adelanto", r.status_code == 200, r.text)
        check("3 queda 'verificado' y APLICÓ 595.000 AHORA (retroactivo a la factura 1)",
              r.status_code == 200 and r.json().get("estado_adelanto") == "verificado"
              and r.json().get("aplicado_ahora_clp") == ADELANTO, r.text)
        a = _adelanto_real(cot_id)
        check("3 adelanto registrado con el monto real confirmado",
              a and a["monto"] == ADELANTO, a)
        f1 = _factura_real(fac1_id)
        cobs_adel_f1 = [c for c in f1["cobranzas"] if c[2] == "adelanto"]
        check("3 la factura 1 recibió la cobranza medio='adelanto' (595.000)",
              len(cobs_adel_f1) == 1 and cobs_adel_f1[0][1] == ADELANTO, f1)
        check("3 saldo factura 1 = 714.000 − 595.000 = 119.000 ('parcial')",
              f1["saldo"] == 119_000.0 and f1["estado"] == "parcial", f1)
        check("3 INVARIANTE 1: monto_aplicado == Σ cobranzas medio='adelanto'",
              a and abs(a["aplicado"] - sum(c[1] for c in cobs_adel_f1)) < 0.01,
              (a, cobs_adel_f1))

        print("\n───────── 4 · 2ª FACTURA (resto): el adelanto NO se aplica de nuevo ─────────")
        r = client.post("/api/monza/contabilidad/facturas",
                        json={"cotizacion_id": cot_id, "despacho_id": d2_id,
                              # plazo largo por la misma razón que F1 (no envejecer)
                              "numero_factura": f"{MARK}-F2", "plazo_dias": 3650,
                              "fecha_emision": "2026-07-12"})
        check("4 factura 2 emitida (4 u)", r.status_code == 200, r.text)
        fac2_id = r.json()["id"]
        f2 = _factura_real(fac2_id)
        check("4 bruto factura 2 = 476.000 (400.000 + IVA)", f2["bruto"] == 476_000.0, f2)
        check("4 el adelanto NO se aplica de nuevo (ya se consumió en la factura 1)",
              f2["cobranzas"] == [] and f2["saldo"] == f2["bruto"], f2)
        f1 = _factura_real(fac1_id)
        check("4 INVARIANTE 2: Σ brutos facturados == mercadería vendida (1.190.000)",
              abs((f1["bruto"] + f2["bruto"]) - BRUTO_VENTA) < 0.01,
              f"f1 {f1['bruto']} + f2 {f2['bruto']} = {f1['bruto'] + f2['bruto']}")
        a = _adelanto_real(cot_id)
        check("4 el adelanto sigue aplicado por 595.000 exactos (ni un peso más)",
              a and abs(a["aplicado"] - ADELANTO) < 0.01, a)

        print("\n───────── 5 · EL CLIENTE PAGA LOS SALDOS ─────────")
        saldo1, saldo2 = f1["saldo"], f2["saldo"]  # 119.000 y 476.000
        r = client.post(f"/api/monza/contabilidad/facturas/{fac1_id}/cobranzas",
                        json={"monto": saldo1, "fecha": "2026-07-20",
                              "medio": "transferencia", "banco": "BCI",
                              "numero_operacion": f"{MARK}-OP2"})
        check("5 cobranza del saldo de la factura 1 registrada", r.status_code == 200, r.text)
        r = client.post(f"/api/monza/contabilidad/facturas/{fac2_id}/cobranzas",
                        json={"monto": saldo2, "fecha": "2026-07-21",
                              "medio": "transferencia", "banco": "BCI",
                              "numero_operacion": f"{MARK}-OP3"})
        check("5 cobranza del saldo de la factura 2 registrada", r.status_code == 200, r.text)
        f1, f2 = _factura_real(fac1_id), _factura_real(fac2_id)
        check("5 INVARIANTE 5: saldo/pagado/estado persistidos cuadran con las filas (f1)",
              abs(f1["pagado"] - f1["suma_cob"]) < 0.01 and f1["saldo"] == 0
              and f1["estado"] == "pagada", f1)
        check("5 INVARIANTE 5: saldo/pagado/estado persistidos cuadran con las filas (f2)",
              abs(f2["pagado"] - f2["suma_cob"]) < 0.01 and f2["saldo"] == 0
              and f2["estado"] == "pagada", f2)
        # Cuánto puso REALMENTE el cliente en toda la venta
        depositado = ADELANTO + saldo1 + saldo2
        check("5 lo depositado por el cliente == total de la venta (1.190.000)",
              abs(depositado - BRUTO_VENTA) < 0.01,
              f"adelanto {ADELANTO} + pagos {saldo1}+{saldo2} = {depositado}")

        print("\n───────── 6 · TESORERÍA: CONCILIACIÓN BANCARIA ─────────")
        r = client.post("/api/monza/tesoreria/cuentas",
                        json={"banco": f"{MARK}-BCI", "nombre": "Cta Cte", "moneda": "CLP"})
        check("6 cuenta bancaria creada", r.status_code == 200, r.text)
        cta_id = r.json()["id"]
        # abonos del banco: los dos pagos del cliente + el depósito del adelanto
        mov_pago1 = client.post("/api/monza/tesoreria/movimientos",
                                json={"cuenta_id": cta_id, "fecha": "2026-07-20",
                                      "tipo": "abono", "monto": saldo1,
                                      "glosa": f"{MARK} pago cliente f1"}).json()["id"]
        mov_pago2 = client.post("/api/monza/tesoreria/movimientos",
                                json={"cuenta_id": cta_id, "fecha": "2026-07-21",
                                      "tipo": "abono", "monto": saldo2,
                                      "glosa": f"{MARK} pago cliente f2"}).json()["id"]
        mov_adel = client.post("/api/monza/tesoreria/movimientos",
                               json={"cuenta_id": cta_id, "fecha": "2026-07-05",
                                     "tipo": "abono", "monto": ADELANTO,
                                     "glosa": f"{MARK} adelanto cliente"}).json()["id"]
        # INVARIANTE 3: la aplicación del adelanto NO es conciliable como abono nuevo
        pend = client.get("/api/monza/tesoreria/cobranzas-pendientes",
                          params={"q": MARK}).json()["cobranzas"]
        medios = [c.get("medio") for c in pend]
        ids_pend = [c.get("cobranza_id") for c in pend]
        cob_adel_id = next(c[0] for c in f1["cobranzas"] if c[2] == "adelanto")
        cob1 = next(c[0] for c in f1["cobranzas"] if c[2] == "transferencia")
        cob2 = next(c[0] for c in f2["cobranzas"] if c[2] == "transferencia")
        check("6 INVARIANTE 3: la cobranza medio='adelanto' NO se ofrece como abono nuevo",
              "adelanto" not in medios and cob_adel_id not in ids_pend, (medios, ids_pend))
        check("6 las cobranzas de los pagos reales SÍ están pendientes de conciliar",
              cob1 in ids_pend and cob2 in ids_pend, (cob1, cob2, ids_pend))
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov_adel}/conciliar",
                        json={"cobranza_id": cob_adel_id})
        check("6 INVARIANTE 3: conciliar la cobranza 'adelanto' → 400 (va por su vía)",
              r.status_code == 400, r.text)
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov_pago1}/conciliar",
                        json={"cobranza_id": cob1})
        check("6 abono ↔ cobranza f1 conciliado", r.status_code == 200, r.text)
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov_pago2}/conciliar",
                        json={"cobranza_id": cob2})
        check("6 abono ↔ cobranza f2 conciliado", r.status_code == 200, r.text)
        # INVARIANTE 4: ni el mismo abono dos veces…
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov_pago1}/conciliar",
                        json={"cobranza_id": cob1})
        check("6 INVARIANTE 4: el mismo abono no se concilia dos veces (409)",
              r.status_code == 409, r.text)
        # …ni la misma cobranza contra otro abono (depósito duplicado en cartola)
        mov_dup = client.post("/api/monza/tesoreria/movimientos",
                              json={"cuenta_id": cta_id, "fecha": "2026-07-22",
                                    "tipo": "abono", "monto": saldo1,
                                    "glosa": f"{MARK} abono duplicado"}).json()["id"]
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov_dup}/conciliar",
                        json={"cobranza_id": cob1})
        check("6 INVARIANTE 4: la misma cobranza no acepta un segundo abono (409)",
              r.status_code == 409, r.text)

        print("\n───────── 7 · EL DEPÓSITO DEL ADELANTO VA POR SU PROPIA VÍA ─────────")
        adel_id = _adelanto_real(cot_id)["id"]
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov_adel}/conciliar",
                        json={"adelanto_id": adel_id})
        check("7 abono ↔ adelanto conciliado (el depósito real del anticipo)",
              r.status_code == 200, r.text)
        n_adel = _sql("SELECT COUNT(*) FROM monza_tes_conciliacion WHERE adelanto_id=:a",
                      a=adel_id)[0][0]
        n_cob = _sql("SELECT COUNT(*) FROM monza_tes_conciliacion_ingreso "
                     "WHERE cobranza_id IN (:c1, :c2)", c1=cob1, c2=cob2)[0][0]
        check("7 exactamente 1 enlace de adelanto + 2 de cobranza, sin duplicar",
              n_adel == 1 and n_cob == 2, (n_adel, n_cob))

        print("\n───────── 8 · LOS TABLEROS DICEN LO MISMO ─────────")
        k = client.get("/api/monza/contabilidad/kpis").json()
        d_fact = k.get("facturado_clp", 0) - kpis_antes.get("facturado_clp", 0)
        d_cobrar = k.get("por_cobrar_clp", 0) - kpis_antes.get("por_cobrar_clp", 0)
        check("8 el KPI facturado subió exactamente el bruto de esta venta (1.190.000)",
              abs(d_fact - BRUTO_VENTA) < 1.0, f"delta={d_fact}")
        check("8 el KPI por cobrar no se movió (la venta quedó saldada)",
              abs(d_cobrar) < 1.0, f"delta={d_cobrar}")
        r = client.get("/api/monza/tesoreria/flujo-caja")
        check("8 el flujo de caja de Tesorería responde", r.status_code == 200, r.text)
        fc = r.json()
        # Delta 0 en ambos lados: la venta terminó saldada y el adelanto 100% aplicado.
        d_pc = (sum(fc["por_cobrar"][b]["monto"] for b in fc["buckets"])
                - sum(flujo_antes["por_cobrar"][b]["monto"] for b in flujo_antes["buckets"]))
        check("8 por_cobrar del flujo no se movió (facturas saldadas)",
              abs(d_pc) < 1.0, f"delta={d_pc}")
        d_sin_aplicar = (fc["adelantos_recibidos_sin_aplicar"]["monto"]
                         - flujo_antes["adelantos_recibidos_sin_aplicar"]["monto"])
        check("8 'recibidos sin aplicar' no se movió (el adelanto quedó 100% aplicado)",
              abs(d_sin_aplicar) < 1.0, f"delta={d_sin_aplicar}")
        r = client.get("/api/monza/tesoreria/resumen")
        check("8 el resumen de Tesorería responde con los KPIs de adelantos",
              r.status_code == 200 and "adelantos_sin_conciliar" in r.json(),
              r.text)

        print("\n───────── 9 · CIERRE: NADA QUEDA COLGANDO ─────────")
        f1, f2 = _factura_real(fac1_id), _factura_real(fac2_id)
        a = _adelanto_real(cot_id)
        check("9 ambas facturas pagadas y con folio",
              f1["estado"] == "pagada" and f2["estado"] == "pagada"
              and f1["folio"] and f2["folio"], (f1["estado"], f2["estado"]))
        check("9 el adelanto quedó aplicado por completo, sin remanente fantasma",
              a and abs(a["aplicado"] - a["monto"]) < 0.01, a)
        total_cobrado = f1["suma_cob"] + f2["suma_cob"]
        check("9 Σ cobranzas de la venta == total facturado (nadie pagó de más ni de menos)",
              abs(total_cobrado - BRUTO_VENTA) < 0.01,
              f"f1 {f1['suma_cob']} + f2 {f2['suma_cob']} = {total_cobrado}")

        print()
        assert not _fails, f"{len(_fails)} fallos: {_fails}"
    finally:
        _limpiar(db)
        db.close()


if __name__ == "__main__":
    test_viaje_de_la_plata()
    print("\nTODO OK" if not _fails else f"\n{len(_fails)} FALLOS")
