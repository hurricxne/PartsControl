"""La llave del gasto de embarque es ESTABLE: re-guardar el pricing NO desengancha la CxP.

Espejo de `embarques_pricing/tests/test_llave_gasto_estable.py` (Grupo AM). En MonzaParts
el agujero era PEOR porque el botón «Registrar como compra» ya está en pantalla
(MonzaComprasPage): después de cualquier re-guardado del pricing la píldora "registrado"
volvía a ser botón y eran DOS CLICS para duplicar la CxP del forwarder.

EL AGUJERO QUE ESTA SUITE CIERRA
--------------------------------
El PUT del pricing BORRABA las 6 filas de `monza_emb_pricing_gasto` y las RE-INSERTABA con
PK NUEVAS cada vez que el payload traía `gastos` (y el front SIEMPRE los manda). La FK real
`monza_cont_compra_ibfk_4 (emb_pricing_gasto_id → monza_emb_pricing_gasto)` es
**DELETE_RULE = SET NULL** (verificado en information_schema), así que ese borrado ponía en
**NULL la llave de la compra YA REGISTRADA** → el overlay volvía a decir "no registrado" →
la MISMA factura del forwarder entraba dos veces.

Arquitectura del arreglo (no parche): la identidad del gasto es la LLAVE NATURAL
`(pricing_id, tipo)` y el PUT hace **UPSERT** sobre ella en vez de delete + re-insert.
La llave quedó declarada en la BD (`uq_monza_emb_pricing_gasto_tipo`): los DOS únicos
escritores (`integration.seed_gastos` y el PUT) recorren `GASTOS_CATALOGO`, que son 6 tipos
FIJOS, y el PUT colapsa el payload por tipo → nunca hay dos líneas "Otros".

La FK se DEJA en SET NULL a propósito (no se pasa a RESTRICT): `monza_emb_pricing` cuelga de
`monza_embarques` con ON DELETE CASCADE, así que con RESTRICT el día que Logística borrara un
embarque con una CxP registrada el borrado FALLARÍA. Con identidad estable el borrado no
ocurre nunca, que es la defensa correcta.

SONDA: se ejercita el COMPORTAMIENTO por HTTP contra los routers REALES de Pricing y de
Compras/CxP de MonzaParts; no hay una sola línea de introspección de código.

Datos MARCADOS + limpieza en `finally` + verificación por DELTAS con conexión NUEVA.
No emite ni toca ningún documento tributario.

Corre con:  cd backend && ./venv/bin/python -m pytest monza_embarques_pricing/tests/test_llave_gasto_estable.py -q
(también:   ./venv/bin/python monza_embarques_pricing/tests/test_llave_gasto_estable.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_embarques_pricing.models import (  # noqa: E402
    MonzaEmbPricing, MonzaEmbPricingGasto, MonzaEmbPricingItem,
)
from monza_embarques_pricing.router import router as pricing_router  # noqa: E402
from monza_compras_contab.router import router as compras_router  # noqa: E402
from monza_compras_contab.models import (  # noqa: E402
    MonzaContCompra, MonzaContEgreso, MonzaContEgresoDetalle, MonzaContCompraItem,
)

MARK = "__T_MEP_LL__"       # corto: monza_cotizaciones.numero es VARCHAR(20)
NETO, IVA = 160_000.0, 30_400.0
BRUTO = NETO + IVA


def _cu(db: Session = Depends(get_db)):
    """Auth realista: toca la base con la MISMA sesión del request (igual que auth.py)."""
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=None, email=f"{MARK}@test.invalid", empresa="automotriz")


app = FastAPI()
app.include_router(pricing_router)      # /api/monza/embarques-pricing (el router trae prefijo)
app.include_router(compras_router)      # /api/monza/compras-contab
app.dependency_overrides[get_current_user] = _cu
cli = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _url(emb_id: int) -> str:
    return f"/api/monza/embarques-pricing/{emb_id}"


# ─── Seed / limpieza ──────────────────────────────────────────────────────────
def _purge(db: Session) -> None:
    """Borra TODO lo marcado en orden seguro (idempotente)."""
    compras = db.query(MonzaContCompra).filter(
        MonzaContCompra.referencia.like(f"{MARK}%")).all()
    egreso_ids = set()
    for c in compras:
        for d in db.query(MonzaContEgresoDetalle).filter(
                MonzaContEgresoDetalle.compra_id == c.id).all():
            egreso_ids.add(d.egreso_id)
            db.delete(d)
        db.query(MonzaContCompraItem).filter(
            MonzaContCompraItem.compra_id == c.id).delete(synchronize_session=False)
        db.flush()
        db.delete(c)
        db.flush()
    for eid in egreso_ids:
        eg = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == eid).first()
        if eg and not db.query(MonzaContEgresoDetalle).filter(
                MonzaContEgresoDetalle.egreso_id == eid).first():
            db.delete(eg)
    db.flush()
    for emb in db.query(mm.MonzaEmbarque).filter(
            mm.MonzaEmbarque.numero.like(f"{MARK}%")).all():
        pr = db.query(MonzaEmbPricing).filter(
            MonzaEmbPricing.embarque_id == emb.id).first()
        if pr:
            db.query(MonzaEmbPricingItem).filter(
                MonzaEmbPricingItem.pricing_id == pr.id).delete(synchronize_session=False)
            db.query(MonzaEmbPricingGasto).filter(
                MonzaEmbPricingGasto.pricing_id == pr.id).delete(synchronize_session=False)
            db.query(MonzaEmbPricing).filter(
                MonzaEmbPricing.id == pr.id).delete(synchronize_session=False)
        db.query(mm.MonzaEmbarqueItem).filter(
            mm.MonzaEmbarqueItem.embarque_id == emb.id).delete(synchronize_session=False)
        db.flush()
        db.delete(emb)
        db.flush()
    for cot in db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.numero.like(f"{MARK}%")).all():
        db.query(mm.MonzaCotizacionItem).filter(
            mm.MonzaCotizacionItem.cotizacion_id == cot.id).delete(synchronize_session=False)
        db.delete(cot)
        db.flush()
    for cliente in db.query(mm.MonzaCliente).filter(
            mm.MonzaCliente.nombre.like(f"{MARK}%")).all():
        db.delete(cliente)
        db.flush()
    db.commit()


def seed():
    """Cotización vendida con 2 ítems USD + embarque marcado."""
    db = SessionLocal()
    try:
        _purge(db)
        cli_row = mm.MonzaCliente(nombre=f"{MARK} Cli")
        db.add(cli_row)
        db.flush()
        cot = mm.MonzaCotizacion(numero=f"{MARK}-COT", cliente_id=cli_row.id,
                                 estado="vendida", iva_pct=19)
        db.add(cot)
        db.flush()
        emb = mm.MonzaEmbarque(numero=f"{MARK}-EMB", estado="en_transito", forwarder="Fastmark")
        db.add(emb)
        db.flush()
        for parte, peso in (("LL-1", 2.0), ("LL-2", 5.0)):
            it = mm.MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"Pieza {parte}",
                                        numero_parte=parte, cantidad=1, costo=100,
                                        moneda="USD", peso_kg=peso, estado_linea="en_transito")
            db.add(it)
            db.flush()
            db.add(mm.MonzaEmbarqueItem(embarque_id=emb.id, item_id=it.id))
        db.commit()
        print(f"[seed] embarque={emb.id}")
        return emb.id
    finally:
        db.close()


def _residuos():
    with engine.connect() as conn:
        n = 0
        for sql in (
            "SELECT COUNT(*) FROM monza_embarques WHERE numero LIKE :m",
            "SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE :m",
            "SELECT COUNT(*) FROM monza_cont_compra WHERE referencia LIKE :m",
            "SELECT COUNT(*) FROM monza_clientes WHERE nombre LIKE :m",
        ):
            n += int(conn.execute(text(sql), {"m": f"{MARK}%"}).scalar() or 0)
    return n


def _gastos_en_bd(emb_id: int) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT g.tipo, g.id, g.monto_neto FROM monza_emb_pricing_gasto g "
            "JOIN monza_emb_pricing p ON p.id = g.pricing_id WHERE p.embarque_id = :e"),
            {"e": emb_id}).fetchall()
    return {r[0]: (int(r[1]), float(r[2] or 0)) for r in rows}


def _cxp_en_bd() -> list:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, emb_pricing_gasto_id, monto_total_clp FROM monza_cont_compra "
            "WHERE referencia LIKE :m AND anulado = 0 ORDER BY id"),
            {"m": f"{MARK}%"}).fetchall()
    return [(int(r[0]), (int(r[1]) if r[1] is not None else None), float(r[2] or 0))
            for r in rows]


def _payload_gastos(neto_agencia: float) -> dict:
    """Lo que manda el front en CADA guardado: las 6 líneas completas."""
    return {
        "tc_tipo": "manual", "tc_valor": 962, "flete_en_me": False, "shipping_clp": 40_000,
        "gastos": [
            {"tipo": "desconsolidacion", "monto_neto": 0, "iva": 0},
            {"tipo": "almacenaje", "monto_neto": 0, "iva": 0},
            {"tipo": "agencia", "monto_neto": neto_agencia, "iva": IVA,
             "nro_factura": None, "banco": "Banco de Chile", "fecha_factura": "2026-07-30"},
            {"tipo": "arancel", "monto_neto": 0, "iva": 0},
            {"tipo": "otros", "monto_neto": 0, "iva": 0},
            {"tipo": "iva_importacion", "monto_neto": 0, "iva": 0},
        ],
    }


def _gasto_de_overlay(fallback: int) -> int:
    """La id del gasto de AGENCIA tal como la ve la pantalla de Compras/CxP AHORA (la que
    el front manda al hacer clic en «Registrar como compra»). Usar la id vieja sería un
    falso verde: el backend contestaría 400 «no existe» y el 409 nunca se pondría a prueba."""
    r = cli.get("/api/monza/compras-contab/costos-embarque")
    if r.status_code != 200:
        return fallback
    fila = next((x for x in (r.json().get("costos") or [])
                 if x.get("tipo") == "agencia"
                 and str(x.get("embarque_numero") or "").startswith(MARK)), None)
    return int(fila["id"]) if fila else fallback


def _compra_del_gasto(emb_id: int, gasto_id: int) -> dict:
    """Alta de la CxP del forwarder desde el overlay. `numero_documento` VACÍO a propósito:
    es el caso real (las 6 líneas seed nacen con nro_factura=None) y deja que el ÚNICO
    freno sea la llave del gasto (con un número tecleado, el dedup por (RUT, N° doc) taparía
    el agujero y la sonda daría un falso verde)."""
    return {
        "tipo_gasto": "cogs", "origen": "EMBARQUE", "categoria": "Aduana/agencia",
        "acreedor": f"{MARK} FORWARDER", "referencia": f"{MARK}-CxP",
        "descripcion": "Agencia de aduana del embarque", "numero_documento": None,
        "moneda": "CLP", "tc": 1, "monto_neto": NETO, "iva": IVA,
        "condicion_pago": "credito", "plazo_dias": 30,
        "embarque_id": emb_id, "emb_pricing_gasto_id": gasto_id,
    }


# ─── Checks ───────────────────────────────────────────────────────────────────
def run(emb_id: int):
    r = cli.get(_url(emb_id))
    check("0 el detalle abre y siembra las 6 líneas de gastos",
          r.status_code == 200 and len(r.json().get("gastos", [])) == 6,
          (r.status_code, r.text[:150]))
    r = cli.put(_url(emb_id), json=_payload_gastos(NETO))
    check("0 el guardado del pricing con la factura de la agencia → 200",
          r.status_code == 200, r.text[:200])
    g0 = _gastos_en_bd(emb_id)
    check("0 quedan las 6 líneas en BD y agencia trae el neto de la factura",
          len(g0) == 6 and g0.get("agencia", (0, 0))[1] == NETO, g0)
    id_agencia = g0["agencia"][0]
    ids0 = {t: v[0] for t, v in g0.items()}

    # ── 1 · registrar la CxP del forwarder desde el overlay ─────────────────────
    r = cli.post("/api/monza/compras-contab", json=_compra_del_gasto(emb_id, id_agencia))
    check("1 la CxP del gasto de embarque se registra → 200", r.status_code == 200,
          r.text[:250])
    compra_id = (r.json() or {}).get("id") if r.status_code == 200 else None
    check("1 y devuelve el id de la compra", isinstance(compra_id, int), r.text[:150])

    r = cli.post("/api/monza/compras-contab", json=_compra_del_gasto(emb_id, id_agencia))
    check("1 el 2° intento INMEDIATO del mismo gasto → 409 (el anti-duplicado funciona)",
          r.status_code == 409, (r.status_code, r.text[:200]))

    r = cli.get("/api/monza/compras-contab/costos-embarque")
    fila = next((x for x in (r.json().get("costos") or []) if x["id"] == id_agencia), None)
    check("1 el overlay marca el gasto como REGISTRADO (compra_id != None)",
          fila is not None and fila.get("compra_id") == compra_id, (r.status_code, fila))

    # ── 2 · EL CAMINO DEL BUG: re-guardar el pricing (corregir el TC) ───────────
    r = cli.put(_url(emb_id), json={**_payload_gastos(NETO), "tc_valor": 970})
    check("2 re-guardar el pricing (corregir el TC) → 200", r.status_code == 200,
          r.text[:200])

    g1 = _gastos_en_bd(emb_id)
    check("2 EL GASTO CONSERVA SU id tras el re-guardado "
          "(antes se borraba y re-insertaba con PK nueva)",
          g1.get("agencia", (None, 0))[0] == id_agencia,
          {"antes": id_agencia, "ahora": g1.get("agencia", (None, 0))[0]})
    check("2 y las 6 líneas conservan TODAS su id (identidad estable, no solo agencia)",
          {t: v[0] for t, v in g1.items()} == ids0,
          {"antes": ids0, "ahora": {t: v[0] for t, v in g1.items()}})
    check("2 sin duplicar filas: siguen siendo 6", len(g1) == 6, g1)
    check("2 y el monto guardado sigue siendo el de la factura",
          g1.get("agencia", (0, 0))[1] == NETO, g1)

    cxp = _cxp_en_bd()
    check("2 LA COMPRA CONSERVA SU LLAVE hacia el gasto "
          "(la FK es SET NULL: el borrado la ponía en NULL)",
          len(cxp) == 1 and cxp[0][1] == id_agencia, cxp)

    r = cli.get("/api/monza/compras-contab/costos-embarque")
    fila = next((x for x in (r.json().get("costos") or []) if x["id"] == id_agencia), None)
    check("2 el overlay SIGUE diciendo 'registrado' después de re-guardar el pricing "
          "(si vuelve a botón, son DOS CLICS para duplicar la CxP)",
          fila is not None and fila.get("compra_id") == compra_id, fila)

    # ── 3 · el anti-duplicado sigue vivo DESPUÉS del re-guardado ────────────────
    id_ui = _gasto_de_overlay(id_agencia)
    r = cli.post("/api/monza/compras-contab", json=_compra_del_gasto(emb_id, id_ui))
    check("3 volver a registrar el gasto TAL COMO LO MANDA LA PANTALLA → 409 "
          "(con la llave inestable salía 200 y nacía la CxP DUPLICADA)",
          r.status_code == 409, (r.status_code, id_ui, r.text[:200]))

    cxp = _cxp_en_bd()
    total = round(sum(x[2] for x in cxp), 0)
    check(f"3 Σ CxP ACTIVA = {BRUTO:.0f} (una sola factura del forwarder, no el doble)",
          len(cxp) == 1 and abs(total - BRUTO) <= 1.0,
          {"n_compras": len(cxp), "suma": total, "esperado": BRUTO})

    # ── 4 · aguanta varios re-guardados seguidos ────────────────────────────────
    for i in range(3):
        rr = cli.put(_url(emb_id), json={**_payload_gastos(NETO), "tc_valor": 940 + i})
        if rr.status_code != 200:
            check(f"4 re-guardado #{i} → 200", False, rr.text[:150])
            break
    g2 = _gastos_en_bd(emb_id)
    cxp = _cxp_en_bd()
    r = cli.post("/api/monza/compras-contab",
                 json=_compra_del_gasto(emb_id, _gasto_de_overlay(id_agencia)))
    check("4 tras 3 re-guardados más: mismas ids, misma llave, sigue el 409 y Σ CxP intacta",
          {t: v[0] for t, v in g2.items()} == ids0 and len(cxp) == 1
          and cxp[0][1] == id_agencia and r.status_code == 409
          and abs(round(sum(x[2] for x in cxp), 0) - BRUTO) <= 1.0,
          {"ids": {t: v[0] for t, v in g2.items()}, "cxp": cxp, "post": r.status_code,
           "suma": round(sum(x[2] for x in cxp), 0)})

    # ── 5 · anular la CxP SÍ libera el gasto (el flujo legítimo no se rompió) ───
    r = cli.post(f"/api/monza/compras-contab/{compra_id}/anular",
                 json={"motivo": f"{MARK} prueba de reversa"})
    check("5 anular la CxP → 200", r.status_code == 200, r.text[:200])
    id_ui = _gasto_de_overlay(id_agencia)
    r = cli.get("/api/monza/compras-contab/costos-embarque")
    fila = next((x for x in (r.json().get("costos") or []) if x["id"] == id_ui), None)
    check("5 con la CxP anulada el overlay vuelve a 'no registrado'",
          fila is not None and fila.get("compra_id") is None, fila)
    r = cli.post("/api/monza/compras-contab", json=_compra_del_gasto(emb_id, id_ui))
    check("5 y el gasto se puede volver a registrar → 200 (anular es la salida legítima)",
          r.status_code == 200, (r.status_code, r.text[:200]))
    cxp = _cxp_en_bd()
    check("5 queda UNA sola CxP activa por el gasto",
          len(cxp) == 1 and abs(round(sum(x[2] for x in cxp), 0) - BRUTO) <= 1.0, cxp)

    # ── 6 · la llave natural está DECLARADA en la BD (no solo en el código) ─────
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(DISTINCT index_name) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'monza_emb_pricing_gasto' "
            "AND index_name = 'uq_monza_emb_pricing_gasto_tipo' AND non_unique = 0")).scalar()
    check("6 existe el UNIQUE (pricing_id, tipo) en monza_emb_pricing_gasto "
          "(si falta: correr monza_embarques_pricing.init_db)", int(n or 0) == 1, n)
    db = SessionLocal()
    try:
        pr = db.query(MonzaEmbPricing).filter(MonzaEmbPricing.embarque_id == emb_id).first()
        db.add(MonzaEmbPricingGasto(pricing_id=pr.id, tipo="agencia", glosa="DUP",
                                    monto_neto=1, iva=0, capitaliza=True, orden=3))
        db.flush()
        db.rollback()
        check("6 la BD RECHAZA una 2ª línea 'agencia' del mismo pricing", False,
              "el INSERT duplicado pasó")
    except Exception as e:                                   # noqa: BLE001
        db.rollback()
        check("6 la BD RECHAZA una 2ª línea 'agencia' del mismo pricing "
              "(la llave natural es real, no una convención)",
              "1062" in str(e) or "Duplicate" in str(e) or "IntegrityError" in type(e).__name__,
              repr(e)[:200])
    finally:
        db.close()


def cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        _purge(db)
    except Exception as e:                                   # noqa: BLE001
        db.rollback()
        print(f"⚠️  cleanup falló: {e}")
    finally:
        db.close()


def test_llave_gasto_embarque_estable_monza():
    """Wrapper para pytest: llama a run() DIRECTAMENTE (el candado
    tests_infra/test_suites_visibles.py exige la llamada literal)."""
    emb_id = seed()
    try:
        run(emb_id)
    finally:
        cleanup()
    resto = _residuos()
    print(f"[cleanup] filas MARCADAS que sobreviven: {resto}")
    assert not _fails and resto == 0, f"fallas={_fails} residuos={resto}"


if __name__ == "__main__":
    _emb = seed()
    try:
        run(_emb)
    finally:
        cleanup()
    _resto = _residuos()
    print(f"[cleanup] filas MARCADAS que sobreviven: {_resto}")
    print()
    if _fails or _resto:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails} · residuos={_resto}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
