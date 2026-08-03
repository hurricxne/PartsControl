"""Las DOS puertas del adelanto responden IGUAL (refutación ALTO-1 de la auditoría).

En MonzaParts el adelanto se registra por dos endpoints que son la MISMA regla de negocio
sobre la MISMA fila (`monza_cont_adelanto`):

  · POST /api/monza/contabilidad/ventas/{id}/adelanto/verificar   (Contabilidad)
  · POST /api/monza/tesoreria/aprobaciones/{id}/aprobar           (Tesorería — y en Monza
    ES la puerta principal: la ORDEN del adelanto la da Tesorería, Ventas quedó de lectura)

ALTO-1: M2 (aceptar el adelanto NO PACTADO, pct_adelanto = 0) se arregló en Contabilidad y
la copia de Tesorería siguió respondiendo 400 «Esta venta no tiene un adelanto informado por
Comercial» — o sea, se arregló la puerta que el operador NO usa, y el depósito de un cliente
que nadie pactó seguía sin poder registrarse. Es la deuda M5 en vivo: dos copias de una
regla de plata y el arreglo puesto en una sola.

Ahora la regla vive UNA vez (monza_contabilidad/router.py: `validar_venta_para_adelanto` y
`validar_adelanto_editable`) y las dos puertas la llaman. Esta suite es la SONDA DE
DIVERGENCIA: corre los mismos 5 escenarios contra AMBAS puertas y exige el MISMO código de
respuesta. Si alguien vuelve a poner un guard en un solo lado (o cambia un mensaje en uno),
se pone roja. Es conductual y por HTTP: no lee el código fuente ni cuenta strings.

Escenarios: (1) adelanto NO PACTADO → 200 · (2) monto > total de la venta → 400 ·
(3) venta que no está vendida → 400 · (4) plata ya aplicada a una factura → 409 ·
(5) adelanto ya conciliado con un abono del banco → 409.
Y además: el adelanto no pactado APARECE en la cola `aprobadas` de /aprobaciones (antes la
query filtraba pct_adelanto > 0, así que quedaba registrable pero invisible).

ESTILO de la casa (test_paridad_2b.py): datos MARCADOS, `cleanup()` antes y después,
verificación por DELTAS con sesión nueva, auth realista, `check()` que acumula. NO toca el
SII ni Wasabil.

Corre con:
  cd backend && ./venv/bin/python -m pytest monza_tesoreria/tests/test_r5_paridad_adelanto.py -q
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContAdelanto, MonzaContCobranza, MonzaContFacturaCliente,
    MonzaContFacturaClienteItem, MonzaContFactoring,
)
from monza_contabilidad.router import router as contab_router  # noqa: E402
from monza_tesoreria.router import router as tes_router  # noqa: E402
from monza_tesoreria.models import (  # noqa: E402
    MonzaTesConciliacion, MonzaTesConciliacionIngreso, MonzaTesCuentaBancaria,
    MonzaTesMovimiento,
)
import monza_compras_contab.models  # noqa: E402,F401

MARK = "__TEST_R5T__"
CONTAB = "/api/monza/contabilidad"
TES = "/api/monza/tesoreria"

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()


def _cu(db: Session = Depends(get_db)):
    """Auth REALISTA: una lectura en la MISMA sesión del request, para que el read view de
    MySQL nazca ANTES de los with_for_update()."""
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, empresa="automotriz", email="t@monzaparts.cl", rol="admin")


app.include_router(contab_router)
app.include_router(tes_router)
app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []
_S = {"cli": None, "cots": {}, "cuenta": None}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Siembra ────────────────────────────────────────────────────────────────────
def _venta(db, key, *, pct=0, estado="vendida", precio=100000):
    bruto = round(precio * 1.19)
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-{key}", cliente_id=_S["cli"], estado=estado,
        total_neto=precio, iva_monto=bruto - precio, total_bruto=bruto, iva_pct=19,
        forma_pago="credito", pct_adelanto=pct,
        oc_cliente=f"OC-{key}", oc_fecha=date(2026, 7, 1),
    )
    db.add(cot); db.flush()
    _S["cots"][key] = cot.id
    it = mm.MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion=f"Repuesto {key}", numero_parte=f"NP-{key}",
        cantidad=1, precio_unitario_clp=precio, subtotal_clp=precio,
        estado_linea="por_comprar")
    db.add(it); db.flush()
    return cot.id


def seed():
    db = SessionLocal()
    try:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
        _S["cli"] = cli.id
        _venta(db, "NPC", pct=0)                     # no pactado, por Contabilidad
        _venta(db, "NPT", pct=0)                     # no pactado, por Tesorería
        _venta(db, "TOP", pct=50)                    # monto sobre el total
        _venta(db, "EST", pct=50, estado="propuesta")  # venta que no está vendida
        _venta(db, "APL", pct=50)                    # plata ya aplicada
        _venta(db, "CON", pct=50)                    # conciliado con el banco
        db.commit()
    finally:
        db.close()


def _adel_db(cot_key):
    db = SessionLocal()
    try:
        a = (db.query(MonzaContAdelanto)
             .filter(MonzaContAdelanto.cotizacion_id == _S["cots"][cot_key]).first())
        return None if a is None else {
            "id": a.id, "estado": a.estado, "monto": float(a.monto or 0),
            "monto_aplicado": float(a.monto_aplicado or 0)}
    finally:
        db.close()


def _post_contab(cot_key, payload):
    return client.post(f"{CONTAB}/ventas/{_S['cots'][cot_key]}/adelanto/verificar", json=payload)


def _post_tes(cot_key, payload):
    return client.post(f"{TES}/aprobaciones/{_S['cots'][cot_key]}/aprobar", json=payload)


def _par(nombre, esperado, payload, *, cot_c, cot_t, frase=None):
    """Corre el MISMO escenario por las dos puertas y exige el MISMO código.
    `cot_c`/`cot_t` van separadas cuando el escenario ESCRIBE (si no, la segunda llamada
    sería una re-verificación y no el mismo caso)."""
    rc = _post_contab(cot_c, payload)
    rt = _post_tes(cot_t, payload)
    check(f"{nombre}: Contabilidad (verificar) -> {esperado}",
          rc.status_code == esperado, rc.text[:220])
    check(f"{nombre}: Tesorería (aprobar) -> {esperado}",
          rt.status_code == esperado, rt.text[:220])
    check(f"{nombre}: SONDA DE DIVERGENCIA — las dos puertas responden igual",
          rc.status_code == rt.status_code, (rc.status_code, rt.status_code, rt.text[:220]))
    if frase:
        check(f"{nombre}: y con el mismo motivo en las dos",
              frase in rc.text and frase in rt.text, (rc.text[:160], rt.text[:160]))
    return rc, rt


# ── Escenarios ─────────────────────────────────────────────────────────────────
def _bloque_no_pactado():
    """ALTO-1 / M2: el cliente depositó un adelanto que Comercial nunca pactó. Su plata no
    puede quedar en el banco sin destino esperando un PATCH de otra área."""
    pago = {"monto": 30000, "fecha_pago": "2026-07-02", "banco": "BancoY"}
    rc, rt = _par("NP (adelanto NO PACTADO)", 200, pago, cot_c="NPC", cot_t="NPT")
    check("NP: SONDA — Tesorería registró el adelanto no pactado",
          (_adel_db("NPT") or {}).get("monto") == 30000, _adel_db("NPT"))
    check("NP: y Contabilidad también", (_adel_db("NPC") or {}).get("monto") == 30000,
          _adel_db("NPC"))
    if rt.status_code == 200:
        j = rt.json()
        check("NP: la venta pasa a 'verificado' aunque pct sea 0",
              j.get("estado_adelanto") == "verificado" and j.get("pct_adelanto") == 0, j)

    # Y la plata registrada tiene que VERSE en la cola de Tesorería (antes la query pedía
    # pct_adelanto > 0, así que el no pactado era registrable pero invisible).
    r = client.get(f"{TES}/aprobaciones")
    ids_ap = [x["cotizacion_id"] for x in r.json().get("aprobadas", [])] if r.status_code == 200 else []
    ids_pa = [x["cotizacion_id"] for x in r.json().get("por_aprobar", [])] if r.status_code == 200 else []
    check("NP: SONDA — el adelanto no pactado APARECE en 'aprobadas'",
          _S["cots"]["NPT"] in ids_ap, (r.status_code, ids_ap[:5]))
    check("NP: y NO ensucia la cola 'por_aprobar' (esa sigue pidiendo % informado)",
          _S["cots"]["NPT"] not in ids_pa and _S["cots"]["NPC"] not in ids_pa, ids_pa[:5])


def _bloque_tope_del_total():
    """El tope REAL del adelanto (≤ total de la venta) es el que quedó cuidando la puerta:
    tiene que seguir vivo en las dos, y con el mismo texto."""
    _par("TOP (monto > total de la venta)", 400, {"monto": 500000},
         cot_c="TOP", cot_t="TOP", frase="no puede exceder el total de la venta")
    check("TOP: no se registró ningún adelanto", _adel_db("TOP") is None, _adel_db("TOP"))


def _bloque_venta_no_vendida():
    """Registrar plata de una venta que no está cerrada: 400 en las dos puertas (el verbo
    del mensaje es lo único que cambia: 'verificar' / 'aprobar')."""
    rc, rt = _par("EST (la venta no está vendida)", 400, {"monto": 10000},
                  cot_c="EST", cot_t="EST", frase="debe estar vendida")
    check("EST: Contabilidad dice 'verificar' y Tesorería 'aprobar'",
          "verificar el adelanto" in rc.text and "aprobar el adelanto" in rt.text,
          (rc.text[:120], rt.text[:120]))
    check("EST: no se registró ningún adelanto", _adel_db("EST") is None, _adel_db("EST"))


def _bloque_plata_ya_aplicada():
    """Editar el monto de un adelanto cuya plata ya está dentro de una factura: 409 en las
    dos (si no, el invariante monto_aplicado == Σ cobranzas 'adelanto' se rompe)."""
    cot = _S["cots"]["APL"]
    r = client.post(f"{CONTAB}/facturas", json={
        "cotizacion_id": cot, "sin_guia": True, "numero_factura": f"{MARK}-FAPL"})
    check("APL: factura previa -> 200", r.status_code == 200, r.text[:200])
    r = _post_contab("APL", {"monto": 59500})
    check("APL: verificar (se aplica retroactivo) -> 200", r.status_code == 200, r.text[:200])
    check("APL: monto_aplicado = 59.500", (_adel_db("APL") or {}).get("monto_aplicado") == 59500,
          _adel_db("APL"))
    _par("APL (plata ya aplicada)", 409, {"monto": 20000}, cot_c="APL", cot_t="APL",
         frase="revierta esa cobranza")
    check("APL: el monto NO se cambió", (_adel_db("APL") or {}).get("monto") == 59500,
          _adel_db("APL"))


def _bloque_ya_conciliado():
    """Editar el monto de un adelanto ya cruzado con un abono de la cartola: 409 en las dos
    (dejaría el movimiento bancario apuntando a otro monto)."""
    r = _post_tes("CON", {"monto": 59500})
    check("CON: Tesorería aprueba -> 200", r.status_code == 200, r.text[:200])
    adel = _adel_db("CON")
    if not adel:
        check("CON: adelanto sembrado", False, "no se creó el adelanto")
        return
    db = SessionLocal()
    try:
        cta = MonzaTesCuentaBancaria(banco=f"{MARK} Banco", nombre=f"{MARK} Cta", moneda="CLP")
        db.add(cta); db.flush()
        _S["cuenta"] = cta.id
        mov = MonzaTesMovimiento(cuenta_id=cta.id, fecha=date(2026, 7, 1),
                                 glosa=f"{MARK} abono", tipo="abono", monto=59500,
                                 conciliado=True)
        db.add(mov); db.flush()
        db.add(MonzaTesConciliacion(movimiento_id=mov.id, adelanto_id=adel["id"],
                                    monto_conciliado_clp=59500))
        db.commit()
    finally:
        db.close()
    _par("CON (ya conciliado con el banco)", 409, {"monto": 40000}, cot_c="CON", cot_t="CON",
         frase="desconcilie el abono en Tesorería")
    check("CON: el monto NO se cambió", (_adel_db("CON") or {}).get("monto") == 59500,
          _adel_db("CON"))


def run():
    _bloque_no_pactado()
    _bloque_tope_del_total()
    _bloque_venta_no_vendida()
    _bloque_plata_ya_aplicada()
    _bloque_ya_conciliado()
    print()
    if _fails:
        print(f"=== {len(_fails)} FALLO(S): {_fails} ===")
        return False
    print("=== TODO OK ===")
    return True


# ── Limpieza + verificación por DELTAS ─────────────────────────────────────────
def cleanup():
    """Barre por MARCA (no por los ids del proceso): un corte anterior no puede dejar la
    suite roja para siempre."""
    db = SessionLocal()
    try:
        db.rollback()
        cot_ids = [r[0] for r in db.query(mm.MonzaCotizacion.id)
                   .filter(mm.MonzaCotizacion.numero.like(f"{MARK}%")).all()]
        if cot_ids:
            adel_ids = [a.id for a in db.query(MonzaContAdelanto)
                        .filter(MonzaContAdelanto.cotizacion_id.in_(cot_ids)).all()]
            fac_ids = [f.id for f in db.query(MonzaContFacturaCliente)
                       .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all()]
            if adel_ids:
                db.query(MonzaTesConciliacion).filter(
                    MonzaTesConciliacion.adelanto_id.in_(adel_ids)).delete(synchronize_session=False)
            if fac_ids:
                cob_ids = [c.id for c in db.query(MonzaContCobranza)
                           .filter(MonzaContCobranza.factura_id.in_(fac_ids)).all()]
                if cob_ids:
                    db.query(MonzaTesConciliacionIngreso).filter(
                        MonzaTesConciliacionIngreso.cobranza_id.in_(cob_ids)
                    ).delete(synchronize_session=False)
                db.query(MonzaContFacturaClienteItem).filter(
                    MonzaContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(MonzaContCobranza).filter(
                    MonzaContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(MonzaContFactoring).filter(
                    MonzaContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.flush()
                db.query(MonzaContFacturaCliente).filter(
                    MonzaContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
            if adel_ids:
                db.query(MonzaContAdelanto).filter(
                    MonzaContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaCotizacionItem).filter(
                mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaCotizacion).filter(
                mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        movs = [m[0] for m in db.query(MonzaTesMovimiento.id)
                .filter(MonzaTesMovimiento.glosa.like(f"{MARK}%")).all()]
        if movs:
            db.query(MonzaTesConciliacion).filter(
                MonzaTesConciliacion.movimiento_id.in_(movs)).delete(synchronize_session=False)
            db.query(MonzaTesMovimiento).filter(
                MonzaTesMovimiento.id.in_(movs)).delete(synchronize_session=False)
        db.query(MonzaTesCuentaBancaria).filter(
            MonzaTesCuentaBancaria.banco.like(f"{MARK}%")).delete(synchronize_session=False)
        db.query(mm.MonzaCliente).filter(
            mm.MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=False)
        db.commit()
        print("Cleanup OK")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print("Cleanup parcial:", e)
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    faltan = []
    try:
        if db.query(mm.MonzaCotizacion).filter(
                mm.MonzaCotizacion.numero.like(f"{MARK}%")).count():
            faltan.append("monza_cotizaciones")
        if db.query(mm.MonzaCliente).filter(
                mm.MonzaCliente.nombre.like(f"{MARK}%")).count():
            faltan.append("monza_clientes")
        if db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.numero_factura.like(f"{MARK}%")).count():
            faltan.append("monza_cont_factura_cliente")
        if db.query(MonzaTesCuentaBancaria).filter(
                MonzaTesCuentaBancaria.banco.like(f"{MARK}%")).count():
            faltan.append("monza_tes_cuenta_bancaria")
        if db.query(MonzaTesMovimiento).filter(
                MonzaTesMovimiento.glosa.like(f"{MARK}%")).count():
            faltan.append("monza_tes_movimiento")
    finally:
        db.close()
    return faltan


def test_r5_paridad_adelanto():
    """Wrapper de UNA LÍNEA para pytest: sin él la suite sería INVISIBLE al gate."""
    cleanup()
    seed()
    ok = False
    try:
        ok = run()
    finally:
        cleanup()
        residuos = _verificar_limpieza()
    assert not residuos, f"la limpieza dejó residuos en: {residuos}"
    assert ok, f"fallas: {_fails}"


if __name__ == "__main__":
    cleanup()
    seed()
    ok = False
    try:
        ok = run()
    finally:
        cleanup()
        print("residuos:", _verificar_limpieza())
    sys.exit(0 if ok else 1)
