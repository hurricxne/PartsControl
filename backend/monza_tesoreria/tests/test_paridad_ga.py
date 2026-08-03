"""Tests de PARIDAD Monza ↔ Grupo AM del módulo Tesorería MonzaParts (pytest-nativo).

Cubre los arreglos de paridad espejados desde tesoreria/ (GA):
  1. aprobar_adelanto APLICA RETROACTIVAMENTE el adelanto a las facturas YA emitidas
     de la venta: factura previa con saldo queda descontada (cobranza medio='adelanto'),
     con CAP POR SALDO actual (no por bruto: una cobranza parcial previa reduce lo
     aplicable) y SALTANDO facturas con factoring vigente.
  2. Snapshot al desconciliar: el egreso recupera EXACTAMENTE la fecha/referencia que
     el operador tenía ingresada antes del cruce (incluso si coincidía con lo del
     banco, caso que la limpieza-por-igualdad histórica perdía).
  3. fecha_pago explícita pero no parseable → 400 (antes caía en silencio a HOY).

Datos MARCADOS (__TEST_MTESPAR__) + limpieza total en try/finally; verificación con
SESIÓN NUEVA. Requiere la BD local (igual que las demás suites del módulo).

Corre con:  cd backend && ./venv/bin/python -m pytest monza_tesoreria/tests/test_paridad_ga.py -q
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContAdelanto, MonzaContCobranza, MonzaContFacturaCliente, MonzaContFactoring,
)
from monza_compras_contab.models import MonzaContEgreso  # noqa: E402
from monza_contabilidad.service import MEDIO_ADELANTO  # noqa: E402
from monza_tesoreria.router import router  # noqa: E402
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesCartola, MonzaTesConciliacion, MonzaTesConciliacionIngreso,
    MonzaTesCuentaBancaria, MonzaTesMovimiento,
)
# La migración aditiva (columnas snapshot en monza_tes_conciliacion) debe existir en
# la BD local ANTES de correr: init_db es idempotente, así que se corre siempre.
import monza_tesoreria.init_db as _init_db  # noqa: E402

_init_db.main()

MARK = "__TEST_MTESPAR__"

app = FastAPI()
app.include_router(router)


# Auth REALISTA (mismo patrón de test_integration.py): además de devolver el usuario,
# hace una lectura en la MISMA sesión del request — abre el read view ANTES de
# cualquier with_for_update(), la condición real bajo la que corren los endpoints.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)


# ─── Helpers de datos MARCADOS ─────────────────────────────────────────────────
def _purge(db):
    """Borra cualquier residuo del MARK en orden seguro de FK (idempotente)."""
    # 1) Tesorería: enlaces → movimientos → cartolas → cuentas del MARK.
    cuenta_ids = [c.id for c in db.query(MonzaTesCuentaBancaria)
                  .filter(MonzaTesCuentaBancaria.banco.like(MARK + "%")).all()]
    if cuenta_ids:
        mov_ids = [m.id for m in db.query(MonzaTesMovimiento)
                   .filter(MonzaTesMovimiento.cuenta_id.in_(cuenta_ids)).all()]
        if mov_ids:
            db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.movimiento_id.in_(mov_ids)).delete(synchronize_session=False)
            db.query(MonzaTesConciliacionIngreso).filter(
                MonzaTesConciliacionIngreso.movimiento_id.in_(mov_ids)).delete(synchronize_session=False)
            db.query(MonzaTesMovimiento).filter(
                MonzaTesMovimiento.id.in_(mov_ids)).delete(synchronize_session=False)
        db.query(MonzaTesCartola).filter(
            MonzaTesCartola.cuenta_id.in_(cuenta_ids)).delete(synchronize_session=False)
        db.query(MonzaTesCuentaBancaria).filter(
            MonzaTesCuentaBancaria.id.in_(cuenta_ids)).delete(synchronize_session=False)
    # 2) Egresos sembrados directo (sin compra) para el test de snapshot.
    egreso_ids = [e.id for e in db.query(MonzaContEgreso)
                  .filter(MonzaContEgreso.beneficiario.like(MARK + "%")).all()]
    if egreso_ids:
        db.query(MonzaTesConciliacion).filter(
            MonzaTesConciliacion.egreso_id.in_(egreso_ids)).delete(synchronize_session=False)
        db.query(MonzaContEgreso).filter(
            MonzaContEgreso.id.in_(egreso_ids)).delete(synchronize_session=False)
    # 3) Facturas del MARK (cascade borra items + cobranzas + factoring).
    for f in db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.numero_factura.like(MARK + "%")).all():
        cob_ids = [c.id for c in f.cobranzas]
        if cob_ids:
            db.query(MonzaTesConciliacionIngreso).filter(
                MonzaTesConciliacionIngreso.cobranza_id.in_(cob_ids)).delete(synchronize_session=False)
        db.delete(f)
    db.flush()
    # 4) Ventas del MARK: adelantos (y sus enlaces) → ítems → cotización → cliente.
    cot_ids = [c.id for c in db.query(mm.MonzaCotizacion)
               .filter(mm.MonzaCotizacion.numero.like(MARK + "%")).all()]
    if cot_ids:
        adel_ids = [a.id for a in db.query(MonzaContAdelanto)
                    .filter(MonzaContAdelanto.cotizacion_id.in_(cot_ids)).all()]
        if adel_ids:
            db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
            db.query(MonzaContAdelanto).filter(
                MonzaContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacionItem).filter(
            mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    db.query(mm.MonzaCliente).filter(
        mm.MonzaCliente.nombre.like(MARK + "%")).delete(synchronize_session=False)


def _cleanup():
    db = SessionLocal()
    try:
        _purge(db)
        db.commit()
    finally:
        db.close()


def _seed_venta(db, *, pct_adelanto=50) -> int:
    """Cliente con RUT VÁLIDO + venta vendida (el candado de empresa es automotriz)."""
    cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="33.333.333-3")
    db.add(cli)
    db.flush()
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-COT", cliente_id=cli.id, estado="vendida",
        total_neto=1000000, iva_monto=190000, total_bruto=1190000, iva_pct=19,
        forma_pago="50_adelanto", pct_adelanto=pct_adelanto, adelanto_verificado=0,
    )
    db.add(cot)
    db.flush()
    return cot.id


def _mk_factura(db, *, cot_id, folio, bruto, pagado=0.0, factoring_vigente=False) -> int:
    """Factura de la venta directo en DB (el flujo real exige venta→guía→factura; acá
    se prueba SOLO Tesorería). Con `pagado`, agrega una cobranza real previa."""
    saldo = round(bruto - pagado, 2)
    f = MonzaContFacturaCliente(
        cotizacion_id=cot_id, numero_factura=folio, tipo_doc="factura",
        cliente_nombre=f"{MARK} Cliente",
        fecha_emision=date(2026, 6, 1),
        monto_neto=round(bruto / 1.19, 2), iva=round(bruto - bruto / 1.19, 2),
        monto_bruto=bruto, monto_pagado=pagado, saldo=saldo,
        estado_pago=("parcial" if pagado else "por_cobrar"),
    )
    db.add(f)
    db.flush()
    if pagado:
        db.add(MonzaContCobranza(factura_id=f.id, fecha=date(2026, 6, 10), monto=pagado,
                                 medio="transferencia", banco="Santander",
                                 numero_operacion=f"{MARK}-OP-PREVIA"))
    if factoring_vigente:
        db.add(MonzaContFactoring(factura_id=f.id, empresa_factoring=f"{MARK} Factor",
                                  monto_adelantado=round(bruto * 0.9, 2),
                                  retencion=round(bruto * 0.1, 2), estado="vigente"))
    db.flush()
    return f.id


# ═══ 1. Aplicación RETROACTIVA del adelanto al aprobar ═════════════════════════
def test_aprobar_aplica_retroactivo_cap_saldo_y_factoring():
    _cleanup()
    db = SessionLocal()
    try:
        cot_id = _seed_venta(db)
        # Orden por id asc: la factorizada primero (debe SALTARSE aunque tenga saldo).
        f_fact_id = _mk_factura(db, cot_id=cot_id, folio=f"{MARK}-FF", bruto=200000.0,
                                factoring_vigente=True)
        f1_id = _mk_factura(db, cot_id=cot_id, folio=f"{MARK}-F1", bruto=400000.0)
        # F2 con cobranza parcial previa de 100.000: saldo 200.000 (cap por SALDO,
        # no por bruto — con cap por bruto se sobre-aplicaría).
        f2_id = _mk_factura(db, cot_id=cot_id, folio=f"{MARK}-F2", bruto=300000.0,
                            pagado=100000.0)
        db.commit()
    finally:
        db.close()

    try:
        # Aprobar el adelanto 50% (595.000) DESPUÉS de emitidas las facturas.
        r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar",
                        json={"monto": 595000, "fecha_pago": "2026-07-01",
                              "banco": "Santander", "numero_operacion": f"{MARK}-OP"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["estado_adelanto"] == "verificado", body
        # F1 completa (400.000) + F2 capada por saldo (195.000) = 595.000; FF saltada.
        assert body.get("aplicado_ahora_clp") == 595000.0, body

        # Verificación con SESIÓN NUEVA (nada del identity map del request).
        db = SessionLocal()
        try:
            adel = (db.query(MonzaContAdelanto)
                    .filter(MonzaContAdelanto.cotizacion_id == cot_id).first())
            assert adel is not None and float(adel.monto) == 595000.0
            assert float(adel.monto_aplicado) == 595000.0, float(adel.monto_aplicado)

            # F1: descontada completa → pagada, saldo 0, cobranza 'adelanto' de 400.000.
            f1 = db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.id == f1_id).first()
            assert float(f1.saldo) == 0.0 and f1.estado_pago == "pagada", (
                float(f1.saldo), f1.estado_pago)
            adel_f1 = [c for c in f1.cobranzas if c.medio == MEDIO_ADELANTO]
            assert len(adel_f1) == 1 and float(adel_f1[0].monto) == 400000.0

            # F2: cap por SALDO (200.000), pendiente del adelanto era 195.000 →
            # se aplican 195.000 (no 300.000 del bruto): pagado 295.000, saldo 5.000.
            f2 = db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.id == f2_id).first()
            adel_f2 = [c for c in f2.cobranzas if c.medio == MEDIO_ADELANTO]
            assert len(adel_f2) == 1 and float(adel_f2[0].monto) == 195000.0, (
                [float(c.monto) for c in adel_f2])
            assert float(f2.monto_pagado) == 295000.0 and float(f2.saldo) == 5000.0, (
                float(f2.monto_pagado), float(f2.saldo))
            assert f2.estado_pago == "parcial", f2.estado_pago

            # FF (factoring vigente): SALTADA — sin cobranza 'adelanto', saldo intacto.
            ff = db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.id == f_fact_id).first()
            assert not [c for c in ff.cobranzas if c.medio == MEDIO_ADELANTO]
            assert float(ff.saldo) == 200000.0, float(ff.saldo)

            # INVARIANTE: monto_aplicado == Σ cobranzas medio='adelanto' de la venta.
            tot = sum(float(c.monto)
                      for f in (f1, f2, ff) for c in f.cobranzas
                      if c.medio == MEDIO_ADELANTO)
            assert abs(tot - float(adel.monto_aplicado)) < 0.01, (
                tot, float(adel.monto_aplicado))
        finally:
            db.close()

        # Guard existente sigue vivo: re-aprobar con el adelanto ya aplicado → 409.
        r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar",
                        json={"monto": 100000})
        assert r.status_code == 409, r.status_code
    finally:
        _cleanup()


# ═══ 2. Snapshot del egreso restaurado EXACTO al desconciliar ══════════════════
def test_snapshot_restaurado_exacto_al_desconciliar():
    _cleanup()
    db = SessionLocal()
    try:
        # Egresos sueltos (sin compra: conciliar solo mira la fila del egreso), con
        # fecha/ref bancaria INGRESADAS A MANO por el operador en Compras.
        eg1 = MonzaContEgreso(beneficiario=f"{MARK} Prov1", fecha=date(2026, 6, 28),
                              monto_total_clp=80000, medio="transferencia",
                              fecha_mov_bancario=date(2026, 6, 15),
                              referencia_bancaria=f"{MARK}-MANUAL")
        # Caso límite que la limpieza-por-igualdad histórica PERDÍA: lo manual
        # coincide exactamente con lo que traerá la cartola.
        eg2 = MonzaContEgreso(beneficiario=f"{MARK} Prov2", fecha=date(2026, 7, 2),
                              monto_total_clp=50000, medio="transferencia",
                              fecha_mov_bancario=date(2026, 7, 2),
                              referencia_bancaria=f"{MARK}-OP-IGUAL")
        db.add_all([eg1, eg2])
        db.flush()
        eg1_id, eg2_id = eg1.id, eg2.id
        db.commit()
    finally:
        db.close()

    try:
        cuenta_id = client.post("/api/monza/tesoreria/cuentas",
                                json={"banco": f"{MARK} Banco", "moneda": "CLP"}).json()["id"]
        mov1 = client.post("/api/monza/tesoreria/movimientos",
                           json={"cuenta_id": cuenta_id, "fecha": "2026-07-01",
                                 "tipo": "cargo", "monto": 80000,
                                 "referencia": "BCO-REF-1"}).json()["id"]
        mov2 = client.post("/api/monza/tesoreria/movimientos",
                           json={"cuenta_id": cuenta_id, "fecha": "2026-07-02",
                                 "tipo": "cargo", "monto": 50000,
                                 "referencia": f"{MARK}-OP-IGUAL"}).json()["id"]

        # Conciliar: la cartola pisa fecha/ref del egreso, el enlace guarda el snapshot.
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov1}/conciliar",
                        json={"egreso_id": eg1_id})
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            eg = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == eg1_id).first()
            assert eg.conciliado and eg.fecha_mov_bancario == date(2026, 7, 1)
            assert eg.referencia_bancaria == "BCO-REF-1"
            link = db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.egreso_id == eg1_id).first()
            assert link.fecha_egreso_previa == date(2026, 6, 15), link.fecha_egreso_previa
            assert link.referencia_egreso_previa == f"{MARK}-MANUAL"
        finally:
            db.close()

        # Desconciliar: RESTAURA exactamente lo del operador (no limpia, no inventa).
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov1}/desconciliar")
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            eg = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == eg1_id).first()
            assert not eg.conciliado
            assert eg.fecha_mov_bancario == date(2026, 6, 15), eg.fecha_mov_bancario
            assert eg.referencia_bancaria == f"{MARK}-MANUAL", eg.referencia_bancaria
        finally:
            db.close()

        # Caso valores IGUALES a los del banco: antes quedaban en None; ahora se
        # conservan (el snapshot no depende de comparar valores).
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov2}/conciliar",
                        json={"egreso_id": eg2_id})
        assert r.status_code == 200, r.text
        r = client.post(f"/api/monza/tesoreria/movimientos/{mov2}/desconciliar")
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            eg = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == eg2_id).first()
            assert eg.fecha_mov_bancario == date(2026, 7, 2), eg.fecha_mov_bancario
            assert eg.referencia_bancaria == f"{MARK}-OP-IGUAL", eg.referencia_bancaria
        finally:
            db.close()
    finally:
        _cleanup()


# ═══ 3. fecha_pago no parseable → 400 (no cae en silencio a HOY) ═══════════════
def test_fecha_pago_invalida_da_400():
    _cleanup()
    db = SessionLocal()
    try:
        cot_id = _seed_venta(db)
        db.commit()
    finally:
        db.close()

    try:
        r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar",
                        json={"monto": 595000, "fecha_pago": "31-31-2026"})
        assert r.status_code == 400, (r.status_code, r.text)
        assert "fecha_pago" in r.text, r.text
        # Nada quedó escrito: ni adelanto ni cortafuego destrabado.
        db = SessionLocal()
        try:
            adel = (db.query(MonzaContAdelanto)
                    .filter(MonzaContAdelanto.cotizacion_id == cot_id).first())
            assert adel is None
            cot = db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id == cot_id).first()
            assert int(cot.adelanto_verificado or 0) == 0
        finally:
            db.close()

        # La fecha válida sí se respeta (no se pisa por HOY).
        r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar",
                        json={"monto": 595000, "fecha_pago": "2026-07-01"})
        assert r.status_code == 200, r.text
        db = SessionLocal()
        try:
            adel = (db.query(MonzaContAdelanto)
                    .filter(MonzaContAdelanto.cotizacion_id == cot_id).first())
            assert adel is not None and adel.fecha_pago == date(2026, 7, 1)
        finally:
            db.close()
    finally:
        _cleanup()
