"""Test de integración del módulo Tesorería MonzaParts contra la BD local.

Monta el router en apps efímeras (sin tocar main.py), simula usuarios automotriz y
mineria, y ejerce el flujo completo:
  · Aprobaciones: venta con adelanto 50% → aparece por aprobar → Tesorería APRUEBA →
    el cortafuego de Abastecimiento queda destrabado (adelanto_verificado=1) y la
    venta pasa a "aprobadas"; validaciones (sobre-tope 400, sin adelanto 400).
  · Conciliación: cuenta bancaria CRUD, importar cartola CSV (anti-duplicados),
    sugerencias y cruce cargo↔egreso de Compras / abono↔adelanto / abono↔cobranza;
    desconciliar; borrados protegidos (incluida cobranza conciliada → 409).
  · Por pagar + aprobación de pago desde Tesorería (POST /pagos).
  · Flujo de caja y resumen.
  · Candado de empresa (mineria → 403).
Limpia todo lo que creó (deja la BD intacta).

Corre con:  cd backend && ./venv/bin/python monza_tesoreria/tests/test_integration.py
"""
import io
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContAdelanto, MonzaContFacturaCliente, MonzaContCobranza,
)
from monza_contabilidad.router import router as contab_router  # noqa: E402
from monza_compras_contab.models import (  # noqa: E402
    MonzaContCompra, MonzaContEgreso, MonzaContEgresoDetalle,
)
from monza_compras_contab.router import router as compras_router  # noqa: E402
from monza_tesoreria.router import router  # noqa: E402
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesCuentaBancaria, MonzaTesCartola, MonzaTesMovimiento, MonzaTesConciliacion,
    MonzaTesConciliacionIngreso,
)

MARK = "__TEST_MTES__"

Base.metadata.create_all(bind=engine, checkfirst=True)  # asegura monza_tes_conciliacion_ingreso

app = FastAPI()
app.include_router(router)
app.include_router(compras_router)  # para sembrar una compra+egreso vía API real
app.include_router(contab_router)   # para el guard de borrado de cobranza conciliada
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, empresa="automotriz")
client = TestClient(app)

app_min = FastAPI()
app_min.include_router(router)
app_min.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=2, empresa="mineria")
client_min = TestClient(app_min)

_fails = []
_seed = {"cli_id": None, "cot_id": None}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _purge(db):
    """Borra cualquier residuo del MARK en orden seguro de FK (idempotente)."""
    # 1) Tesorería: conciliaciones → movimientos → cartolas → cuentas del MARK.
    cuentas = db.query(MonzaTesCuentaBancaria).filter(
        MonzaTesCuentaBancaria.banco.like(MARK + "%")).all()
    cuenta_ids = [c.id for c in cuentas]
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
    # 2) Compras del MARK (y sus egresos), sembradas vía API de compras.
    compras = db.query(MonzaContCompra).filter(MonzaContCompra.acreedor.like(MARK + "%")).all()
    compra_ids = [c.id for c in compras]
    if compra_ids:
        egreso_ids = [d.egreso_id for d in db.query(MonzaContEgresoDetalle)
                      .filter(MonzaContEgresoDetalle.compra_id.in_(compra_ids)).all()]
        if egreso_ids:
            db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.egreso_id.in_(egreso_ids)).delete(synchronize_session=False)
            db.query(MonzaContEgresoDetalle).filter(
                MonzaContEgresoDetalle.egreso_id.in_(egreso_ids)).delete(synchronize_session=False)
            db.query(MonzaContEgreso).filter(
                MonzaContEgreso.id.in_(egreso_ids)).delete(synchronize_session=False)
        db.query(MonzaContCompra).filter(
            MonzaContCompra.id.in_(compra_ids)).delete(synchronize_session=False)
    # 3) Ventas del MARK: adelantos (y sus enlaces) → cotización → cliente.
    cots = db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.numero.like(MARK + "%")).all()
    cot_ids = [c.id for c in cots]
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
    # 4) Facturas/cobranzas del MARK (creadas directo en DB para el cruce abono↔cobranza):
    #    primero sus enlaces de conciliación de ingreso, luego la factura (cascade borra
    #    items + cobranzas + factoring).
    facturas = db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.numero_factura.like(MARK + "%")).all()
    for f in facturas:
        cob_ids = [c.id for c in f.cobranzas]
        if cob_ids:
            db.query(MonzaTesConciliacionIngreso).filter(
                MonzaTesConciliacionIngreso.cobranza_id.in_(cob_ids)).delete(synchronize_session=False)
        db.delete(f)
    db.flush()
    db.query(mm.MonzaCliente).filter(mm.MonzaCliente.nombre.like(MARK + "%")).delete(synchronize_session=False)


def _mk_factura_con_cobranza(db, *, folio, bruto, cobranza_monto, cobranza_fecha):
    """Crea factura + cobranza Monza directo en DB (el flujo real exige venta→guía→
    factura; acá probamos SOLO Tesorería). Devuelve (factura_id, cobranza_id) PLANOS —
    ids, no instancias ORM (DetachedInstanceError al usarlas tras cerrar la sesión)."""
    f = MonzaContFacturaCliente(
        numero_factura=folio, tipo_doc="factura",
        cliente_nombre=f"{MARK} Cliente Factura",
        fecha_emision=date(2026, 6, 1), fecha_vencimiento=date(2026, 6, 30),
        monto_neto=round(bruto / 1.19, 2), iva=round(bruto - bruto / 1.19, 2),
        monto_bruto=bruto, monto_pagado=cobranza_monto,
        saldo=round(bruto - cobranza_monto, 2),
        estado_pago=("pagada" if cobranza_monto >= bruto else "parcial"),
    )
    db.add(f)
    db.flush()
    c = MonzaContCobranza(factura_id=f.id, fecha=cobranza_fecha, monto=cobranza_monto,
                          medio="transferencia", banco="Santander",
                          numero_operacion=f"{MARK}-OP")
    db.add(c)
    db.flush()
    ids = (f.id, c.id)
    db.commit()
    return ids


def seed():
    """Siembra una venta con adelanto 50% informado por Comercial (sin verificar)."""
    db = SessionLocal()
    try:
        _purge(db)
        db.commit()
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="33.333.333-3")
        db.add(cli); db.flush()
        cot = mm.MonzaCotizacion(
            numero=f"{MARK}-COT", cliente_id=cli.id, estado="vendida",
            total_neto=1000000, iva_monto=190000, total_bruto=1190000, iva_pct=19,
            forma_pago="50_adelanto", pct_adelanto=50, adelanto_verificado=0,
        )
        db.add(cot); db.flush()
        _seed.update(cli_id=cli.id, cot_id=cot.id)
        db.commit()
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        _purge(db)
        db.commit()
    finally:
        db.close()


def run():
    cot_id = _seed["cot_id"]
    SUGERIDO = 595000.0  # 50% de 1.190.000

    # ═══ 1. APROBACIONES ═══
    r = client.get("/api/monza/tesoreria/aprobaciones")
    check("GET aprobaciones 200", r.status_code == 200, r.text)
    fila = next((x for x in r.json()["por_aprobar"] if x["cotizacion_id"] == cot_id), None)
    check("venta con adelanto aparece por aprobar", fila is not None)
    if fila:
        check("estado por_verificar", fila["estado_adelanto"] == "por_verificar", fila)
        check("monto sugerido = 595.000 (50%)", fila["monto_sugerido_clp"] == SUGERIDO, fila)

    # Validaciones antes de aprobar
    r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar",
                    json={"monto": 99999999})
    check("aprobar sobre el total → 400", r.status_code == 400, r.status_code)
    r = client.post("/api/monza/tesoreria/aprobaciones/99999999/aprobar", json={"monto": 100})
    check("cotización inexistente → 404", r.status_code == 404, r.status_code)

    # APROBAR (la orden de Tesorería)
    r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar",
                    json={"monto": SUGERIDO, "fecha_pago": "2026-07-01",
                          "banco": "Santander", "numero_operacion": "OP-50"})
    check("aprobar 200", r.status_code == 200, r.text)
    check("estado pasa a verificado", r.json()["estado_adelanto"] == "verificado", r.json())

    # El cortafuego queda destrabado (adelanto_verificado=1 en la cotización)
    db = SessionLocal()
    try:
        cot = db.query(mm.MonzaCotizacion).filter(mm.MonzaCotizacion.id == cot_id).first()
        check("cortafuego destrabado (adelanto_verificado=1)", int(cot.adelanto_verificado or 0) == 1)
        adel = db.query(MonzaContAdelanto).filter(MonzaContAdelanto.cotizacion_id == cot_id).first()
        check("registro de adelanto creado", adel is not None and float(adel.monto) == SUGERIDO)
        adel_id = adel.id
    finally:
        db.close()

    r = client.get("/api/monza/tesoreria/aprobaciones")
    apro = next((x for x in r.json()["aprobadas"] if x["cotizacion_id"] == cot_id), None)
    check("aparece en aprobadas", apro is not None)
    check("aún sin conciliar con banco", apro and apro["adelanto"]["conciliado_banco"] is False, apro)

    # ═══ 2. CONCILIACIÓN ═══
    # Cuenta bancaria
    r = client.post("/api/monza/tesoreria/cuentas",
                    json={"banco": f"{MARK} Banco", "nombre": "Cta principal", "moneda": "CLP"})
    check("crear cuenta 200", r.status_code == 200, r.text)
    cuenta_id = r.json()["id"]

    # Compra+egreso reales vía la API de Compras (crédito 80.000 + pago inline)
    r = client.post("/api/monza/compras-contab", json={
        "tipo_gasto": "gasto_operacional", "acreedor": f"{MARK} Proveedor",
        "monto_neto": 80000, "afecto_iva": False, "condicion_pago": "contado",
        "numero_documento": f"{MARK}-F1", "fecha": "2026-07-01",
    })
    check("seed compra contado 200", r.status_code == 200, r.text)
    egreso_id = r.json()["pagos"][0]["egreso_id"]

    # Importar cartola: cargo 80.000 (calza con el egreso) + abono 595.000 (calza adelanto)
    csv_content = (
        "Fecha;Detalle;Cargos;Abonos\n"
        "01-07-2026;TRANSFERENCIA PROVEEDOR;80.000;\n"
        "01-07-2026;ABONO CLIENTE ADELANTO;;595.000\n"
        "02-07-2026;COMISION BANCO;5.000;\n"
    ).encode("utf-8")
    r = client.post("/api/monza/tesoreria/cartolas/importar",
                    data={"cuenta_id": str(cuenta_id), "nombre": f"{MARK} Cartola"},
                    files={"file": ("cartola.csv", io.BytesIO(csv_content), "text/csv")})
    check("importar cartola 200", r.status_code == 200, r.text)
    check("3 movimientos importados", r.json()["n_importados"] == 3, r.json())

    # ANTI-DUPLICADOS: reimportar la MISMA cartola (doble clic) → 409, nada se duplica
    r = client.post("/api/monza/tesoreria/cartolas/importar",
                    data={"cuenta_id": str(cuenta_id), "nombre": f"{MARK} Cartola bis"},
                    files={"file": ("cartola.csv", io.BytesIO(csv_content), "text/csv")})
    check("reimportar misma cartola → 409", r.status_code == 409, r.status_code)

    r = client.get("/api/monza/tesoreria/movimientos", params={"cuenta_id": cuenta_id})
    movs = r.json()["movimientos"]
    check("listar movimientos", len(movs) == 3, len(movs))
    mov_cargo = next(m for m in movs if m["tipo"] == "cargo" and m["monto"] == 80000.0)
    mov_abono = next(m for m in movs if m["tipo"] == "abono")
    mov_comision = next(m for m in movs if m["monto"] == 5000.0)

    # Sugerencias: cargo → egreso de Compras
    r = client.get(f"/api/monza/tesoreria/movimientos/{mov_cargo['id']}/sugerencias")
    sug = r.json()["sugerencias"]
    check("sugerencia de egreso para el cargo", any(s["egreso_id"] == egreso_id for s in sug), sug)

    # Sugerencias: abono → adelanto aprobado
    r = client.get(f"/api/monza/tesoreria/movimientos/{mov_abono['id']}/sugerencias")
    sug = r.json()["sugerencias"]
    check("sugerencia de adelanto para el abono", any(s["adelanto_id"] == adel_id for s in sug), sug)

    # Conciliar cargo ↔ egreso (montos deben calzar; el egreso queda conciliado)
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_cargo['id']}/conciliar",
                    json={"egreso_id": egreso_id})
    check("conciliar cargo↔egreso 200", r.status_code == 200, r.text)
    check("mov conciliado con destino egreso", r.json()["conciliado"] and r.json()["destino"]["clase"] == "egreso", r.json())
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_comision['id']}/conciliar",
                    json={"egreso_id": egreso_id})
    check("egreso ya conciliado → 409", r.status_code == 409, r.status_code)
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_comision['id']}/conciliar",
                    json={"adelanto_id": adel_id})
    check("adelanto contra CARGO → 400", r.status_code == 400, r.status_code)

    # Conciliar abono ↔ adelanto
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_abono['id']}/conciliar",
                    json={"adelanto_id": adel_id})
    check("conciliar abono↔adelanto 200", r.status_code == 200, r.text)
    check("destino adelanto con venta", r.json()["destino"]["clase"] == "adelanto"
          and r.json()["destino"]["cotizacion_id"] == cot_id, r.json())
    r = client.get("/api/monza/tesoreria/aprobaciones")
    apro = next((x for x in r.json()["aprobadas"] if x["cotizacion_id"] == cot_id), None)
    check("aprobada ahora conciliada con banco", apro and apro["adelanto"]["conciliado_banco"] is True, apro)

    # Payload inválido (ambos / ninguno)
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_comision['id']}/conciliar",
                    json={"egreso_id": egreso_id, "adelanto_id": adel_id})
    check("egreso+adelanto a la vez → 422", r.status_code == 422, r.status_code)
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_comision['id']}/conciliar", json={})
    check("sin destino → 422", r.status_code == 422, r.status_code)

    # Borrados protegidos
    r = client.delete(f"/api/monza/tesoreria/movimientos/{mov_cargo['id']}")
    check("borrar mov conciliado → 409", r.status_code == 409, r.status_code)
    cartola_id = movs[0]["cartola_id"]
    r = client.delete(f"/api/monza/tesoreria/cartolas/{cartola_id}")
    check("borrar cartola con conciliados → 409", r.status_code == 409, r.status_code)
    r = client.delete(f"/api/monza/tesoreria/cuentas/{cuenta_id}")
    check("borrar cuenta con movimientos → 409", r.status_code == 409, r.status_code)

    # Desconciliar cargo → egreso liberado
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_cargo['id']}/desconciliar")
    check("desconciliar 200", r.status_code == 200, r.text)
    db = SessionLocal()
    try:
        eg = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == egreso_id).first()
        check("egreso liberado", eg is not None and not eg.conciliado)
    finally:
        db.close()

    # Egresos pendientes vuelve a incluirlo
    r = client.get("/api/monza/tesoreria/egresos-pendientes", params={"q": MARK})
    check("egreso vuelve a pendientes", any(e["egreso_id"] == egreso_id for e in r.json()["egresos"]), r.json())

    # ═══ 3. ABONO ↔ COBRANZA (ingresos de caja de Facturas y Cobranzas) ═══
    db = SessionLocal()
    try:
        fact_id, cob_id = _mk_factura_con_cobranza(
            db, folio=f"{MARK}-FC1", bruto=77000.0,
            cobranza_monto=77000.0, cobranza_fecha=date(2026, 7, 4))
        # cobranza 60.000 ≠ abono 50.000: sirve para probar el rechazo por monto distinto
        _fact2_id, cob2_id = _mk_factura_con_cobranza(
            db, folio=f"{MARK}-FC2", bruto=200000.0,
            cobranza_monto=60000.0, cobranza_fecha=date(2026, 7, 2))
    finally:
        db.close()

    csv_cob = (
        "Fecha;Detalle;Cargos;Abonos\n"
        "04-07-2026;TRANSFERENCIA CLIENTE;;77.000\n"
        "02-07-2026;DEPOSITO CLIENTE;;50.000\n"
    ).encode("utf-8")
    r = client.post("/api/monza/tesoreria/cartolas/importar",
                    data={"cuenta_id": str(cuenta_id), "nombre": f"{MARK} Cartola 2"},
                    files={"file": ("cartola2.csv", io.BytesIO(csv_cob), "text/csv")})
    check("importar cartola 2 (abonos de cobranza)",
          r.status_code == 200 and r.json()["n_importados"] == 2, r.text)

    movs2 = client.get("/api/monza/tesoreria/movimientos",
                       params={"cuenta_id": cuenta_id}).json()["movimientos"]
    abono77 = next((m for m in movs2 if m["tipo"] == "abono" and m["monto"] == 77000.0), None)
    abono50 = next((m for m in movs2 if m["tipo"] == "abono" and m["monto"] == 50000.0), None)
    check("existen abonos 77.000 y 50.000", abono77 is not None and abono50 is not None)

    # Sugerencias del abono → incluye la cobranza FC1 con clase 'cobranza'
    sugs = client.get(f"/api/monza/tesoreria/movimientos/{abono77['id']}/sugerencias").json()["sugerencias"]
    check("sugerencia abono incluye cobranza FC1",
          any(s.get("cobranza_id") == cob_id and s.get("clase") == "cobranza" for s in sugs), sugs)

    # Conciliar abono ↔ cobranza (el 'conciliado' de la cobranza se deriva del enlace)
    r = client.post(f"/api/monza/tesoreria/movimientos/{abono77['id']}/conciliar",
                    json={"cobranza_id": cob_id})
    check("conciliar abono↔cobranza 200 + destino cobranza",
          r.status_code == 200 and (r.json().get("destino") or {}).get("clase") == "cobranza", r.text)

    # La misma cobranza no se concilia dos veces
    r = client.post(f"/api/monza/tesoreria/movimientos/{abono50['id']}/conciliar",
                    json={"cobranza_id": cob_id})
    check("cobranza ya conciliada → 409", r.status_code == 409, r.status_code)

    # Montos distintos → 400
    r = client.post(f"/api/monza/tesoreria/movimientos/{abono50['id']}/conciliar",
                    json={"cobranza_id": cob2_id})
    check("montos distintos abono↔cobranza → 400", r.status_code == 400, r.status_code)

    # cobranzas-pendientes: excluye la conciliada, incluye la pendiente
    pend = client.get("/api/monza/tesoreria/cobranzas-pendientes", params={"q": MARK}).json()["cobranzas"]
    pend_ids = {p["cobranza_id"] for p in pend}
    check("cobranzas-pendientes excluye conciliada / incluye pendiente",
          cob_id not in pend_ids and cob2_id in pend_ids, pend_ids)

    # Cobranza medio='adelanto' (la APLICACIÓN contable de un adelanto ya recibido):
    # NO es un depósito bancario nuevo → no aparece en pendientes ni se puede conciliar
    # (su plata se concilia por la vía abono↔adelanto). Evita el doble conteo.
    db = SessionLocal()
    try:
        cesp = MonzaContCobranza(factura_id=fact_id, fecha=date(2026, 6, 5), monto=33000,
                                 medio="adelanto", banco="Santander",
                                 numero_operacion=f"{MARK}-OP-ADEL")
        db.add(cesp)
        db.flush()
        cesp_id = cesp.id
        db.commit()
    finally:
        db.close()
    pend2 = client.get("/api/monza/tesoreria/cobranzas-pendientes", params={"q": MARK}).json()["cobranzas"]
    check("cobranza medio='adelanto' excluida de pendientes",
          cesp_id not in {p["cobranza_id"] for p in pend2}, cesp_id)
    r = client.post("/api/monza/tesoreria/movimientos",
                    json={"cuenta_id": cuenta_id, "fecha": "2026-06-05", "glosa": f"{MARK} abono espejo",
                          "tipo": "abono", "monto": 33000})
    mov_esp = r.json()["id"]
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_esp}/conciliar", json={"cobranza_id": cesp_id})
    check("conciliar cobranza medio='adelanto' → 400", r.status_code == 400, r.status_code)

    # Guard de integridad en Facturas y Cobranzas: no borrar una cobranza conciliada
    r = client.delete(f"/api/monza/contabilidad/facturas/{fact_id}/cobranzas/{cob_id}")
    check("borrar cobranza conciliada → 409", r.status_code == 409, r.text)

    # Guard espejo: el ADELANTO ya conciliado con el banco no se re-aprueba/re-verifica
    # (editar su monto dejaría el cruce bancario apuntando a otro monto)
    r = client.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar", json={"monto": 100000})
    check("re-aprobar adelanto conciliado → 409", r.status_code == 409, r.status_code)
    r = client.post(f"/api/monza/contabilidad/ventas/{cot_id}/adelanto/verificar", json={"monto": 100000})
    check("re-verificar (contab) adelanto conciliado → 409", r.status_code == 409, r.status_code)

    # Desconciliar el abono libera la cobranza → ahora sí se puede borrar
    r = client.post(f"/api/monza/tesoreria/movimientos/{abono77['id']}/desconciliar")
    check("desconciliar abono↔cobranza 200",
          r.status_code == 200 and r.json()["conciliado"] is False, r.text)
    r = client.delete(f"/api/monza/contabilidad/facturas/{fact_id}/cobranzas/{cob_id}")
    check("borrar cobranza liberada → 200", r.status_code == 200, r.text)

    # ═══ 4. POR PAGAR + APROBACIÓN DE PAGO DESDE TESORERÍA ═══
    # Compra a crédito vencida (plazo 0 días sobre una fecha pasada) → bucket 'vencido'
    venc = (date.today() - timedelta(days=3)).isoformat()
    r = client.post("/api/monza/compras-contab", json={
        "tipo_gasto": "otros", "acreedor": f"{MARK} ProvD", "monto_neto": 40000,
        "afecto_iva": False, "condicion_pago": "credito", "numero_documento": f"{MARK}-D",
        "fecha": venc, "plazo_dias": 0,
    })
    check("seed compra crédito vencida 200", r.status_code == 200, r.text)
    d = r.json()
    pp = client.get("/api/monza/tesoreria/por-pagar", params={"q": MARK}).json()
    fila = next((c for c in pp["compras"] if c["compra_id"] == d["id"]), None)
    check("por-pagar incluye compra D vencida (bucket + estado en vivo)",
          fila is not None and fila["bucket"] == "vencido" and fila["estado_pago"] == "vencido", fila)
    check("por-pagar buckets suman", pp["buckets"]["vencido"]["n"] >= 1, pp["buckets"])

    # Tesorería aprueba el pago (POST /pagos) → compra pagada (reusa _crear_egreso)
    r = client.post("/api/monza/tesoreria/pagos", json={
        "fecha": date.today().isoformat(), "medio": "transferencia",
        "beneficiario": f"{MARK} ProvD",
        "detalles": [{"compra_id": d["id"], "monto_clp": 40000}],
    })
    check("aprobar pago 200", r.status_code == 200 and r.json()["monto_total_clp"] == 40000.0, r.text)
    check("compra D pagada tras aprobar",
          client.get(f"/api/monza/compras-contab/{d['id']}").json()["estado_pago"] == "pagado")

    # Sobre-pago rechazado (tope por saldo de _crear_egreso)
    r = client.post("/api/monza/tesoreria/pagos", json={
        "detalles": [{"compra_id": d["id"], "monto_clp": 999999}],
    })
    check("sobre-pago → 400", r.status_code == 400, r.status_code)

    # ═══ 5. DEDUP PARCIAL + MOVIMIENTOS MANUALES ═══
    # Archivo con 1 fila ya existente + 1 nueva → importa SOLO la nueva e informa el resto
    csv_mix = (
        "Fecha;Detalle;Cargos;Abonos\n"
        "02-07-2026;COMISION BANCO;5.000;\n"      # ya existe (cartola 1) → se omite
        "03-07-2026;NUEVO CARGO;12.345;\n"        # nueva → se importa
    ).encode("utf-8")
    r = client.post("/api/monza/tesoreria/cartolas/importar",
                    data={"cuenta_id": str(cuenta_id), "nombre": f"{MARK} Cartola mix"},
                    files={"file": ("mix.csv", io.BytesIO(csv_mix), "text/csv")})
    rj = r.json() if r.status_code == 200 else {}
    check("dedup parcial: 1 importado + 1 duplicado informado",
          r.status_code == 200 and rj.get("n_importados") == 1 and rj.get("n_duplicados") == 1, r.text)
    cartola_mix_id = (rj.get("cartola") or {}).get("id")

    # n_movimientos de la cartola se mantiene al agregar/borrar movimientos manuales
    r = client.post("/api/monza/tesoreria/movimientos", json={
        "cuenta_id": cuenta_id, "fecha": "2026-07-03", "tipo": "cargo", "monto": 111,
        "cartola_id": cartola_mix_id})
    mv_manual_id = r.json().get("id")
    n1 = next(c for c in client.get("/api/monza/tesoreria/cartolas",
                                    params={"cuenta_id": cuenta_id}).json()
              if c["id"] == cartola_mix_id)["n_movimientos"]
    check("n_movimientos +1 al agregar manual a la cartola", n1 == 2, n1)
    client.delete(f"/api/monza/tesoreria/movimientos/{mv_manual_id}")
    n2 = next(c for c in client.get("/api/monza/tesoreria/cartolas",
                                    params={"cuenta_id": cuenta_id}).json()
              if c["id"] == cartola_mix_id)["n_movimientos"]
    check("n_movimientos -1 al borrar el manual", n2 == 1, n2)

    # Fecha explícita no parseable → 400 (no se silencia a 'hoy')
    r = client.post("/api/monza/tesoreria/movimientos", json={
        "cuenta_id": cuenta_id, "fecha": "31-31-2026", "tipo": "cargo", "monto": 1000})
    check("movimiento con fecha inválida → 400", r.status_code == 400, r.status_code)

    # Guard de moneda: cuenta en USD no participa de la conciliación automática (fase futura)
    cuenta_usd = client.post("/api/monza/tesoreria/cuentas",
                             json={"banco": f"{MARK} Banco USD", "moneda": "USD"}).json()["id"]
    mov_usd = client.post("/api/monza/tesoreria/movimientos",
                          json={"cuenta_id": cuenta_usd, "tipo": "cargo", "monto": 500}).json()["id"]
    r = client.get(f"/api/monza/tesoreria/movimientos/{mov_usd}/sugerencias")
    check("cuenta USD: sugerencias → 400", r.status_code == 400, r.status_code)
    r = client.post(f"/api/monza/tesoreria/movimientos/{mov_usd}/conciliar",
                    json={"egreso_id": egreso_id})
    check("cuenta USD: conciliar → 400", r.status_code == 400, r.status_code)

    # ═══ 6. FLUJO DE CAJA + RESUMEN ═══
    r = client.get("/api/monza/tesoreria/flujo-caja")
    check("flujo-caja 200", r.status_code == 200, r.text)
    fc = r.json()
    check("flujo trae buckets", set(fc["buckets"]) == {"vencido", "d0_7", "d8_30", "d31_60", "d61_mas", "sin_fecha"})
    check("flujo trae por_pagar/por_cobrar/neto", all(k in fc for k in ("por_pagar", "por_cobrar", "neto")))
    check("flujo informa retenciones de factoring aparte", "retenciones_factoring" in fc, list(fc.keys()))
    # Factorizada excluida de por_cobrar (espejo de Grupo AM): comparar el flujo con la
    # factura MARK en 'factorizada' vs 'parcial' — la diferencia debe ser su saldo.
    db = SessionLocal()
    try:
        f2 = db.query(MonzaContFacturaCliente).filter(MonzaContFacturaCliente.id == _fact2_id).first()
        saldo_f2 = float(f2.saldo or 0)
        f2.estado_pago = "factorizada"
        db.commit()
    finally:
        db.close()
    fc2 = client.get("/api/monza/tesoreria/flujo-caja").json()
    total_pc = sum(fc["por_cobrar"][b]["monto"] for b in fc["buckets"])
    total_pc2 = sum(fc2["por_cobrar"][b]["monto"] for b in fc2["buckets"])
    check("factorizada excluida de por_cobrar (delta = saldo)",
          abs((total_pc - total_pc2) - saldo_f2) <= 1, (total_pc, total_pc2, saldo_f2))
    db = SessionLocal()
    try:
        f2 = db.query(MonzaContFacturaCliente).filter(MonzaContFacturaCliente.id == _fact2_id).first()
        f2.estado_pago = "parcial"
        db.commit()
    finally:
        db.close()
    r = client.get("/api/monza/tesoreria/resumen")
    check("resumen 200 y consistente", r.status_code == 200 and r.json()["movimientos_total"] >= 3, r.json())
    check("resumen trae KPIs nuevos (pagos por aprobar + cobranzas sin conciliar)",
          {"pagos_por_aprobar", "monto_por_pagar_clp", "cobranzas_sin_conciliar",
           "por_pagar_vencido_clp"}.issubset(r.json()), list(r.json().keys()))

    # ═══ 7. CANDADO DE EMPRESA ═══
    for ep in ["/aprobaciones", "/cuentas", "/movimientos", "/flujo-caja", "/resumen"]:
        r = client_min.get(f"/api/monza/tesoreria{ep}")
        check(f"candado: mineria GET {ep} → 403", r.status_code == 403, r.status_code)
    r = client_min.post(f"/api/monza/tesoreria/aprobaciones/{cot_id}/aprobar", json={"monto": 1})
    check("candado: mineria APROBAR → 403", r.status_code == 403, r.status_code)


if __name__ == "__main__":
    seed()
    try:
        run()
    finally:
        cleanup()
    print()
    if _fails:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
