"""Regresión de la PARIDAD 2-B del módulo Tesorería MonzaParts (hallazgos T3, T4, M5,
T14 + reflejo de la anulación de adelantos).

Qué asegura cada bloque:
  1. T3 — la aplicación del adelanto NO se decide con la columna `saldo` PERSISTIDA. Una
     factura con el saldo cacheado en 0 (pero sin ninguna cobranza real) seguía SALIENDO
     del reparto y el cliente veía deuda que ya había pagado. Ahora la plata se decide
     desde las cobranzas leídas bajo lock.
  2. T4 — una BOLETA sin folio NO consulta el módulo DTE. El guard SII de Tesorería no
     filtraba `tipo_doc`, así que en un deploy a medias respondía 503 y bloqueaba justo
     la aprobación que destraba Abastecimiento. Se verifica con un espía sobre
     `_dte_factura_no_emitido`, y que en una FACTURA sin folio el guard SÍ se consulta
     (si no, el check no probaría nada).
  3. M5 — UNA sola fuente de verdad: Tesorería delega la regla por factura en
     monza_contabilidad._aplicar_adelanto y ya no mantiene su propio loop gemelo.
  4. T14 — /aprobaciones pagina de verdad (corte en SQL, totales aparte, `n_por_aprobar`
     = total de la cola).
  5. Anulación de adelantos (columna `estado`, que agrega monza_contabilidad): Tesorería
     la REFLEJA en las 6 puertas que tocan plata (cola de aprobación, pendientes,
     sugerencias, conciliar, flujo de caja, KPIs) y re-aprobar REACTIVA la fila anulada
     (el UNIQUE deja un adelanto por venta: la fila se reusa, no se reemplaza).

Datos MARCADOS (__TEST_MT2B__) + limpieza total en try/finally; verificación con SESIÓN
NUEVA. La anulación se simula escribiendo la columna `estado` en la BD (no por su
endpoint) a propósito: este módulo no debe depender de la ruta que expone otro módulo.
Requiere la BD local (igual que las demás suites del módulo).

Corre con:  cd backend && ./venv/bin/python -m pytest monza_tesoreria/tests/test_paridad_2b.py -q
(también:   ./venv/bin/python monza_tesoreria/tests/test_paridad_2b.py)
"""
import inspect
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
import monza_contabilidad.router as mcr  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContAdelanto, MonzaContCobranza, MonzaContFacturaCliente,
)
from monza_contabilidad.service import MEDIO_ADELANTO, ADEL_ANULADO, ADEL_APROBADO  # noqa: E402
from monza_tesoreria.router import (  # noqa: E402
    router, PAGE_SIZE_MAX, _aplicar_adelanto_a_facturas,
)
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesCartola, MonzaTesConciliacion, MonzaTesConciliacionIngreso,
    MonzaTesCuentaBancaria, MonzaTesMovimiento,
)
# init_db es idempotente: asegura la migración aditiva del módulo en la BD local.
import monza_tesoreria.init_db as _init_db  # noqa: E402

_init_db.main()

MARK = "__TEST_MT2B__"
API = "/api/monza/tesoreria"

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


app = FastAPI()
app.include_router(router)


# Auth REALISTA (mismo patrón que las otras suites del módulo): la lectura en la MISMA
# sesión del request abre el read view ANTES de cualquier with_for_update().
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)


# ─── Limpieza de los datos MARCADOS ────────────────────────────────────────────
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
    # 2) Ventas del MARK: se borra POR cotizacion_id (no por N° de factura) porque esta
    #    suite crea facturas SIN folio a propósito (el caso del guard SII).
    cot_ids = [c.id for c in db.query(mm.MonzaCotizacion)
               .filter(mm.MonzaCotizacion.numero.like(MARK + "%")).all()]
    if cot_ids:
        for f in db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all():
            cob_ids = [c.id for c in f.cobranzas]
            if cob_ids:
                db.query(MonzaTesConciliacionIngreso).filter(
                    MonzaTesConciliacionIngreso.cobranza_id.in_(cob_ids)).delete(synchronize_session=False)
            db.delete(f)  # cascade: items + cobranzas + factoring
        db.flush()
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


def cleanup():
    db = SessionLocal()
    try:
        _purge(db)
        db.commit()
    finally:
        db.close()


# ─── Helpers de siembra ────────────────────────────────────────────────────────
def _seed_venta(db, sufijo: str, *, pct_adelanto=50) -> int:
    cli = mm.MonzaCliente(nombre=f"{MARK} Cliente {sufijo}", rut="33.333.333-3")
    db.add(cli)
    db.flush()
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-{sufijo}", cliente_id=cli.id, estado="vendida",
        total_neto=1000000, iva_monto=190000, total_bruto=1190000, iva_pct=19,
        forma_pago="50_adelanto", pct_adelanto=pct_adelanto, adelanto_verificado=0,
    )
    db.add(cot)
    db.flush()
    return cot.id


def _mk_factura(db, cot_id, *, folio, bruto, tipo_doc="factura",
                saldo=None, estado_pago="por_cobrar") -> int:
    """Factura de la venta directo en BD (acá se prueba SOLO Tesorería).
    `saldo`/`estado_pago` se pueden MENTIR a propósito: es el caso de T3."""
    f = MonzaContFacturaCliente(
        cotizacion_id=cot_id, numero_factura=folio, tipo_doc=tipo_doc,
        cliente_nombre=f"{MARK} Cliente",
        fecha_emision=date(2026, 6, 1),
        monto_neto=round(bruto / 1.19, 2), iva=round(bruto - bruto / 1.19, 2),
        monto_bruto=bruto, monto_pagado=0,
        saldo=(bruto if saldo is None else saldo), estado_pago=estado_pago,
    )
    db.add(f)
    db.flush()
    return f.id


def _cobranzas_adelanto(db, factura_id) -> list:
    return [c for c in db.query(MonzaContCobranza)
            .filter(MonzaContCobranza.factura_id == factura_id).all()
            if c.medio == MEDIO_ADELANTO]


def _aprobar(cot_id, monto, **extra):
    payload = {"monto": monto, "fecha_pago": "2026-07-01",
               "banco": "Santander", "numero_operacion": f"{MARK}-OP"}
    payload.update(extra)
    return client.post(f"{API}/aprobaciones/{cot_id}/aprobar", json=payload)


def run():
    # ═══ 1. T3 · el saldo PERSISTIDO no decide la plata ═════════════════════════
    db = SessionLocal()
    try:
        cot_t3 = _seed_venta(db, "T3")
        # Saldo cacheado en 0 y estado 'pagada' MINTIENDO: no hay ninguna cobranza real,
        # el cliente sigue debiendo los 400.000. Antes el filtro `saldo > TOL` la sacaba
        # del reparto y el adelanto nunca la descontaba.
        f_t3 = _mk_factura(db, cot_t3, folio=f"{MARK}-T3F", bruto=400000.0,
                           saldo=0.0, estado_pago="pagada")
        db.commit()
    finally:
        db.close()

    r = _aprobar(cot_t3, 400000)
    check("T3: aprobar responde 200", r.status_code == 200, r.text)
    body = r.json() if r.status_code == 200 else {}
    check("T3: la factura con saldo cacheado en 0 SÍ entra al reparto (aplicado 400.000)",
          body.get("aplicado_ahora_clp") == 400000.0, body.get("aplicado_ahora_clp"))
    db = SessionLocal()
    try:
        cobs = _cobranzas_adelanto(db, f_t3)
        check("T3: quedó UNA cobranza 'adelanto' por 400.000",
              len(cobs) == 1 and float(cobs[0].monto) == 400000.0,
              [float(c.monto) for c in cobs])
        f = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.id == f_t3).first()
        check("T3: la factura queda con pagado 400.000 y saldo 0 REAL",
              float(f.monto_pagado) == 400000.0 and float(f.saldo) == 0.0,
              (float(f.monto_pagado), float(f.saldo)))
        adel = db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id == cot_t3).first()
        check("T3: INVARIANTE monto_aplicado == Σ cobranzas 'adelanto'",
              abs(float(adel.monto_aplicado) - sum(float(c.monto) for c in cobs)) < 0.01,
              (float(adel.monto_aplicado), sum(float(c.monto) for c in cobs)))
    finally:
        db.close()

    # ═══ 2. T4 · la BOLETA sin folio no consulta el módulo DTE ══════════════════
    db = SessionLocal()
    try:
        cot_bol = _seed_venta(db, "T4B")
        f_bol = _mk_factura(db, cot_bol, folio=None, bruto=200000.0, tipo_doc="boleta")
        cot_fac = _seed_venta(db, "T4F")
        f_fac = _mk_factura(db, cot_fac, folio=None, bruto=200000.0, tipo_doc="factura")
        db.commit()
    finally:
        db.close()

    consultas = []
    _orig_guard = mcr._dte_factura_no_emitido

    def _espia(db_, factura_id):
        consultas.append(factura_id)
        return False

    mcr._dte_factura_no_emitido = _espia
    try:
        r = _aprobar(cot_bol, 200000)
        check("T4: aprobar con una BOLETA sin folio responde 200 (no 503)",
              r.status_code == 200, r.text)
        check("T4: una BOLETA sin folio NO consulta el módulo DTE",
              f_bol not in consultas, consultas)
        check("T4: la plata igual entra a la boleta (no se la salta)",
              r.json().get("aplicado_ahora_clp") == 200000.0 if r.status_code == 200 else False,
              r.text)
        # No vacuidad: en una FACTURA sin folio el guard SÍ se consulta.
        r = _aprobar(cot_fac, 200000)
        check("T4: en una FACTURA sin folio el guard SII SÍ se consulta",
              r.status_code == 200 and f_fac in consultas, (r.status_code, consultas))
    finally:
        mcr._dte_factura_no_emitido = _orig_guard

    # ═══ 3. M5 · una sola fuente de verdad ═════════════════════════════════════
    src = inspect.getsource(_aplicar_adelanto_a_facturas)
    check("M5: delega la regla por factura en monza_contabilidad._aplicar_adelanto",
          "_aplicar_adelanto(db, cot, factura" in src, src)
    check("M5: ya no fabrica la cobranza por su cuenta (loop gemelo eliminado)",
          "MonzaContCobranza(" not in src, "sigue construyendo la cobranza acá")
    check("M5: ya no filtra por la columna `saldo` persistida",
          "MonzaContFacturaCliente.saldo" not in src, "sigue el filtro por saldo")
    check("M5: conserva el orden anticipo-primero (COALESCE + id asc)",
          "coalesce(MonzaContFacturaCliente.es_anticipo" in src
          and "MonzaContFacturaCliente.id.asc()" in src, src)

    # ═══ 4. T14 · /aprobaciones pagina ═════════════════════════════════════════
    r0 = client.get(f"{API}/aprobaciones?page_size=500")
    total_antes = r0.json().get("total_por_aprobar", 0) if r0.status_code == 200 else -1
    db = SessionLocal()
    try:
        mios = [_seed_venta(db, f"P{i}") for i in range(3)]
        db.commit()
    finally:
        db.close()
    r = client.get(f"{API}/aprobaciones?page_size=2")
    check("T14: /aprobaciones responde 200", r.status_code == 200, r.text)
    b1 = r.json() if r.status_code == 200 else {}
    check("T14: la página trae a lo más page_size filas",
          len(b1.get("por_aprobar", [])) == 2, len(b1.get("por_aprobar", [])))
    check("T14: total_por_aprobar cuenta TODA la cola, no la página",
          b1.get("total_por_aprobar") == total_antes + 3,
          (b1.get("total_por_aprobar"), total_antes))
    check("T14: n_por_aprobar (badge) == total de la cola",
          b1.get("n_por_aprobar") == b1.get("total_por_aprobar"),
          (b1.get("n_por_aprobar"), b1.get("total_por_aprobar")))
    check("T14: publica page / page_size / total_aprobadas",
          {"page", "page_size", "total_aprobadas"}.issubset(b1), list(b1.keys()))
    b2 = client.get(f"{API}/aprobaciones?page=2&page_size=2").json()
    ids1 = [x["cotizacion_id"] for x in b1.get("por_aprobar", [])]
    ids2 = [x["cotizacion_id"] for x in b2.get("por_aprobar", [])]
    check("T14: las páginas no se solapan", not (set(ids1) & set(ids2)), (ids1, ids2))
    check("T14: las 3 ventas nuevas salen entre las dos páginas (nada se pierde)",
          set(mios).issubset(set(ids1) | set(ids2)), (mios, ids1, ids2))
    b3 = client.get(f"{API}/aprobaciones?page=0&page_size=99999").json()
    check("T14: page_size se topea en PAGE_SIZE_MAX y page mínima es 1",
          b3.get("page") == 1 and b3.get("page_size") == PAGE_SIZE_MAX,
          (b3.get("page"), b3.get("page_size")))

    # ═══ 5. Anulación del adelanto reflejada en Tesorería ══════════════════════
    db = SessionLocal()
    try:
        cot_an = _seed_venta(db, "AN")
        db.commit()
    finally:
        db.close()
    r = _aprobar(cot_an, 100000)
    check("ANUL: adelanto aprobado (sin facturas → queda sin aplicar)",
          r.status_code == 200, r.text)
    db = SessionLocal()
    try:
        adel_an = db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id == cot_an).first()
        adel_an_id = adel_an.id
        check("ANUL: aprobar deja el estado en 'aprobado'",
              adel_an.estado == ADEL_APROBADO, adel_an.estado)
    finally:
        db.close()

    # Cuenta + abono de cartola por el mismo monto (para sugerencias / conciliar).
    cuenta_id = client.post(f"{API}/cuentas",
                            json={"banco": f"{MARK} Banco", "moneda": "CLP"}).json()["id"]
    mov_id = client.post(f"{API}/movimientos",
                         json={"cuenta_id": cuenta_id, "fecha": "2026-07-01",
                               "tipo": "abono", "monto": 100000,
                               "referencia": f"{MARK}-ABONO"}).json()["id"]
    sug = client.get(f"{API}/movimientos/{mov_id}/sugerencias").json()
    check("ANUL: antes de anular, el adelanto SÍ se sugiere para el abono",
          any(s.get("adelanto_id") == adel_an_id for s in sug.get("sugerencias", [])),
          sug.get("sugerencias"))
    fc_antes = client.get(f"{API}/flujo-caja").json()["adelantos_recibidos_sin_aplicar"]
    res_antes = client.get(f"{API}/resumen").json()["adelantos_sin_conciliar"]
    ap_antes = client.get(f"{API}/aprobaciones?page_size=500").json()

    # ANULACIÓN simulada como la escribe monza_contabilidad (estado + cortafuego a 0):
    # no se llama a su endpoint a propósito — este módulo no depende de esa ruta.
    db = SessionLocal()
    try:
        db.query(MonzaContAdelanto).filter(MonzaContAdelanto.id == adel_an_id).update(
            {MonzaContAdelanto.estado: ADEL_ANULADO}, synchronize_session=False)
        db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id == cot_an).update(
            {mm.MonzaCotizacion.adelanto_verificado: 0}, synchronize_session=False)
        db.commit()
    finally:
        db.close()

    ap = client.get(f"{API}/aprobaciones?page_size=500").json()
    en_aprobadas = {x["cotizacion_id"] for x in ap.get("aprobadas", [])}
    en_por_aprobar = {x["cotizacion_id"] for x in ap.get("por_aprobar", [])}
    check("ANUL: la venta SALE de 'aprobadas'", cot_an not in en_aprobadas, sorted(en_aprobadas))
    check("ANUL: la venta VUELVE a 'por_aprobar'", cot_an in en_por_aprobar, sorted(en_por_aprobar))
    check("ANUL: la cola por aprobar creció en 1 (la venta reabierta)",
          ap.get("total_por_aprobar") == ap_antes.get("total_por_aprobar") + 1,
          (ap.get("total_por_aprobar"), ap_antes.get("total_por_aprobar")))

    pend = client.get(f"{API}/adelantos-pendientes").json()
    check("ANUL: el adelanto anulado NO figura en adelantos-pendientes",
          all(a.get("adelanto_id") != adel_an_id for a in pend.get("adelantos", [])),
          pend.get("total"))

    sug = client.get(f"{API}/movimientos/{mov_id}/sugerencias").json()
    check("ANUL: el adelanto anulado NO se sugiere para el abono",
          all(s.get("adelanto_id") != adel_an_id for s in sug.get("sugerencias", [])),
          sug.get("sugerencias"))

    r = client.post(f"{API}/movimientos/{mov_id}/conciliar", json={"adelanto_id": adel_an_id})
    check("ANUL: conciliar contra un adelanto anulado → 409 accionable",
          r.status_code == 409 and "anulado" in r.text.lower(), (r.status_code, r.text))

    fc = client.get(f"{API}/flujo-caja").json()["adelantos_recibidos_sin_aplicar"]
    check("ANUL: sale del flujo de caja (adelantos recibidos sin aplicar −100.000)",
          round(fc_antes["monto"] - fc["monto"], 2) == 100000.0 and fc["n"] == fc_antes["n"] - 1,
          (fc_antes, fc))
    res = client.get(f"{API}/resumen").json()["adelantos_sin_conciliar"]
    check("ANUL: sale del KPI adelantos_sin_conciliar", res == res_antes - 1, (res_antes, res))

    # Re-aprobar: única vuelta desde 'anulado' (el UNIQUE deja UN adelanto por venta).
    r = _aprobar(cot_an, 100000)
    check("ANUL: re-aprobar responde 200 (reactiva la fila anulada)", r.status_code == 200, r.text)
    db = SessionLocal()
    try:
        adel = db.query(MonzaContAdelanto).filter(MonzaContAdelanto.id == adel_an_id).first()
        cot = db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id == cot_an).first()
        check("ANUL: la fila se REUSA (mismo id, estado 'aprobado')",
              adel is not None and adel.estado == ADEL_APROBADO,
              None if adel is None else adel.estado)
        check("ANUL: el cortafuego de Abastecimiento vuelve a destrabarse (verificado=1)",
              int(cot.adelanto_verificado or 0) == 1, cot.adelanto_verificado)
        n = db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id == cot_an).count()
        check("ANUL: sigue habiendo UN solo adelanto para la venta", n == 1, n)
    finally:
        db.close()
    ap = client.get(f"{API}/aprobaciones?page_size=500").json()
    check("ANUL: reactivado, la venta vuelve a 'aprobadas'",
          cot_an in {x["cotizacion_id"] for x in ap.get("aprobadas", [])},
          sorted({x["cotizacion_id"] for x in ap.get("aprobadas", [])}))


def test_monza_tesoreria_paridad_2b():
    """Wrapper para pytest (mismo patrón que test_retry_deadlock.py): sin él la suite
    sería INVISIBLE al gate rutinario."""
    cleanup()
    try:
        run()
    finally:
        cleanup()
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    cleanup()
    try:
        run()
    finally:
        cleanup()
    print()
    if _fails:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
