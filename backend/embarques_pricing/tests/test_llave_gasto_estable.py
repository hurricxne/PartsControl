"""La llave del gasto de embarque es ESTABLE: re-guardar el pricing NO desengancha la CxP.

EL AGUJERO QUE ESTA SUITE CIERRA (crítico de plata, Grupo AM emite documentos REALES)
--------------------------------------------------------------------------------------
El PUT del pricing BORRABA las 6 filas de `emb_pricing_gasto` y las RE-INSERTABA con PK
NUEVAS cada vez que el payload traía `gastos` (y el front SIEMPRE los manda). La FK real
`cont_compra_ibfk_4 (emb_pricing_gasto_id → emb_pricing_gasto)` es **DELETE_RULE = SET
NULL** (verificado en information_schema), así que ese borrado ponía en **NULL la llave de
la compra YA REGISTRADA**. Todo el anti-duplicado cuelga de esa llave:

    registrar la compra del forwarder  → 200 (y el 2° intento inmediato → 409, OK)
    guardar el pricing para corregir el TC → el gasto tiene id NUEVO
                                          → la compra quedó con la llave en NULL
                                          → el overlay vuelve a decir "no registrado"
    registrar otra vez                  → 200  ⇒  Σ CxP = 380.800 por una factura de 190.400

Arquitectura del arreglo (no parche): la identidad del gasto es la LLAVE NATURAL
`(pricing_id, tipo)` y el PUT hace **UPSERT** sobre ella en vez de delete + re-insert, así
las PK sobreviven al re-guardado. La llave natural se determinó empíricamente y quedó
declarada en la BD (`uq_emb_pricing_gasto_tipo`): los DOS únicos escritores de la tabla
(`integration.seed_gastos` y este PUT) recorren `GASTOS_CATALOGO`, que son 6 tipos FIJOS,
y el PUT colapsa el payload por tipo (`{g.tipo: g for g in payload.gastos}`) → nunca hay
dos líneas "Otros". Medido en la BD: 0 duplicados de (pricing_id, tipo).

La FK se DEJA en SET NULL a propósito (no se pasa a RESTRICT): `emb_pricing` cuelga de
`embarques` con ON DELETE CASCADE, así que con RESTRICT el día que Logística borrara un
embarque con una CxP registrada el borrado FALLARÍA — se rompería la integración no
invasiva con Logística, y encima exigiría recrear la FK (migración destructiva) sobre la
tabla contable de la marca que emite documentos reales. Con identidad estable el borrado
no ocurre nunca, que es la defensa correcta.

SONDAS (poder discriminante, medido quitando el arreglo — ver el reporte de la ola):
se ejercita el COMPORTAMIENTO por HTTP contra los routers REALES de Pricing y de
Compras/CxP; no hay una sola línea de introspección de código.

Datos MARCADOS + limpieza en `finally` + verificación por DELTAS con conexión NUEVA.
No emite ni toca ningún documento tributario: este módulo no habla con el SII.

Corre con:  cd backend && ./venv/bin/python -m pytest embarques_pricing/tests/test_llave_gasto_estable.py -q
(también:   ./venv/bin/python embarques_pricing/tests/test_llave_gasto_estable.py)
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    User, Cotizacion, ItemCotizacion, OcProveedor, OcProveedorItem, Embarque, EmbarqueItem,
)
from embarques_pricing.models import (  # noqa: E402
    EmbarquePricing, EmbarquePricingGasto, EmbarquePricingItem,
)
from embarques_pricing.router import router as pricing_router  # noqa: E402
from compras_contab.router import router as compras_router  # noqa: E402
from compras_contab.models import (  # noqa: E402
    ContCompra, ContEgreso, ContEgresoDetalle, ContCompraItem,
)

MARK = "__TEST_EP_LLAVE__"
USER_EMAIL = f"{MARK}@test.local"
# La factura del forwarder que NO se debe poder cargar dos veces.
NETO, IVA = 160_000.0, 30_400.0
BRUTO = NETO + IVA          # 190.400 — el Σ CxP correcto


def _cu(db: Session = Depends(get_db)):
    """Auth realista: consulta la base con la MISMA sesión del request (igual que auth.py)."""
    u = db.query(User).filter(User.email == USER_EMAIL).first()
    if u is None:
        raise RuntimeError("seed ausente")
    return u


app = FastAPI()
app.include_router(pricing_router, prefix="/api")     # /api/embarques-pricing
app.include_router(compras_router, prefix="/api")     # /api/compras-contab  (igual que main.py)
app.dependency_overrides[get_current_user] = _cu
cli = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _url(emb_id: int) -> str:
    return f"/api/embarques-pricing/{emb_id}"


# ─── Seed / limpieza ──────────────────────────────────────────────────────────
def _purge(db: Session) -> None:
    """Borra TODO lo marcado en orden seguro de FK (idempotente)."""
    # 1) CxP marcadas (y sus hijos, por si un cambio futuro genera egresos)
    compras = db.query(ContCompra).filter(ContCompra.referencia.like(f"{MARK}%")).all()
    egreso_ids = set()
    for c in compras:
        for d in db.query(ContEgresoDetalle).filter(ContEgresoDetalle.compra_id == c.id).all():
            egreso_ids.add(d.egreso_id)
            db.delete(d)
        db.query(ContCompraItem).filter(
            ContCompraItem.compra_id == c.id).delete(synchronize_session=False)
        db.flush()
        db.delete(c)
        db.flush()
    for eid in egreso_ids:
        eg = db.query(ContEgreso).filter(ContEgreso.id == eid).first()
        if eg and not db.query(ContEgresoDetalle).filter(
                ContEgresoDetalle.egreso_id == eid).first():
            db.delete(eg)
    db.flush()
    # 2) Embarques marcados + su pricing
    for emb in db.query(Embarque).filter(Embarque.numero.like(f"{MARK}%")).all():
        pr = db.query(EmbarquePricing).filter(EmbarquePricing.embarque_id == emb.id).first()
        if pr:
            db.query(EmbarquePricingItem).filter(
                EmbarquePricingItem.pricing_id == pr.id).delete(synchronize_session=False)
            db.query(EmbarquePricingGasto).filter(
                EmbarquePricingGasto.pricing_id == pr.id).delete(synchronize_session=False)
            db.query(EmbarquePricing).filter(
                EmbarquePricing.id == pr.id).delete(synchronize_session=False)
        db.query(EmbarqueItem).filter(
            EmbarqueItem.embarque_id == emb.id).delete(synchronize_session=False)
        db.flush()
        db.delete(emb)
        db.flush()
    for o in db.query(OcProveedor).filter(OcProveedor.numero.like(f"{MARK}%")).all():
        db.query(OcProveedorItem).filter(
            OcProveedorItem.oc_proveedor_id == o.id).delete(synchronize_session=False)
        db.delete(o)
        db.flush()
    for c in db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all():
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == c.id).delete(synchronize_session=False)
        db.delete(c)
        db.flush()
    db.query(User).filter(User.email == USER_EMAIL).delete(synchronize_session=False)
    db.commit()


def seed():
    """Un embarque USD con 2 ítems (los suficientes para que el costo landed sea > 0)."""
    db = SessionLocal()
    try:
        _purge(db)
        db.add(User(email=USER_EMAIL, nombre=MARK, hashed_password="x", empresa="mineria"))
        cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} Cliente")
        db.add(cot)
        db.flush()
        ocp = OcProveedor(numero=f"{MARK}-OCP", proveedor="FLORIDA ENGINE", moneda="USD")
        db.add(ocp)
        db.flush()
        emb = Embarque(numero=f"{MARK}-EMB", estado="en_bodega", forwarder="LATAM Cargo")
        db.add(emb)
        db.flush()
        for parte, peso in (("LL-1", 2.0), ("LL-2", 5.0)):
            it = ItemCotizacion(cotizacion_id=cot.id, numero_parte=parte,
                                descripcion=f"DESC {parte}", cantidad=1, peso_unit_lbs=peso,
                                precio_unit_cotizacion=100)
            db.add(it)
            db.flush()
            db.add(OcProveedorItem(oc_proveedor_id=ocp.id, item_cotizacion_id=it.id))
            db.add(EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=it.id,
                                oc_proveedor_id=ocp.id))
        db.commit()
        print(f"[seed] embarque={emb.id}")
        return emb.id
    finally:
        db.close()


def _residuos():
    """Verificación por DELTAS con CONEXIÓN NUEVA: 0 filas marcadas sobreviven."""
    with engine.connect() as conn:
        n = 0
        for sql, par in (
            ("SELECT COUNT(*) FROM embarques WHERE numero LIKE :m", {"m": f"{MARK}%"}),
            ("SELECT COUNT(*) FROM oc_proveedor WHERE numero LIKE :m", {"m": f"{MARK}%"}),
            ("SELECT COUNT(*) FROM cotizaciones WHERE numero LIKE :m", {"m": f"{MARK}%"}),
            ("SELECT COUNT(*) FROM cont_compra WHERE referencia LIKE :m", {"m": f"{MARK}%"}),
            ("SELECT COUNT(*) FROM users WHERE email = :e", {"e": USER_EMAIL}),
        ):
            n += int(conn.execute(text(sql), par).scalar() or 0)
    return n


# ─── Lecturas con conexión NUEVA (nada de identity map) ───────────────────────
def _gastos_en_bd(emb_id: int) -> dict:
    """{tipo: (id, neto)} leído fuera de la sesión del request."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT g.tipo, g.id, g.monto_neto FROM emb_pricing_gasto g "
            "JOIN emb_pricing p ON p.id = g.pricing_id WHERE p.embarque_id = :e"),
            {"e": emb_id}).fetchall()
    return {r[0]: (int(r[1]), float(r[2] or 0)) for r in rows}


def _cxp_en_bd() -> list:
    """[(id, emb_pricing_gasto_id, monto_total_clp)] de las CxP marcadas y ACTIVAS."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, emb_pricing_gasto_id, monto_total_clp FROM cont_compra "
            "WHERE referencia LIKE :m AND anulado = 0 ORDER BY id"),
            {"m": f"{MARK}%"}).fetchall()
    return [(int(r[0]), (int(r[1]) if r[1] is not None else None), float(r[2] or 0)) for r in rows]


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
    """La id del gasto de AGENCIA tal como la ve la pantalla de Compras/CxP AHORA.

    Es la id que el front manda al hacer clic en «Registrar como compra». Usar la id
    VIEJA en su lugar sería un falso verde: el backend contestaría 400 «no existe» y el
    409 nunca se pondría a prueba.
    """
    r = cli.get("/api/compras-contab/costos-embarque")
    if r.status_code != 200:
        return fallback
    fila = next((x for x in (r.json().get("costos") or [])
                 if x.get("tipo") == "agencia"
                 and str(x.get("embarque_numero") or "").startswith(MARK)), None)
    return int(fila["id"]) if fila else fallback


def _compra_del_gasto(emb_id: int, gasto_id: int) -> dict:
    """Alta de la CxP del forwarder desde el overlay de embarques.

    `numero_documento` va VACÍO a propósito: es el caso real (las 6 líneas seed nacen con
    `nro_factura=None`) y deja que el ÚNICO freno sea la llave del gasto. Con un número
    tecleado, el dedup por (RUT, N° documento) taparía el agujero y la sonda daría un
    falso verde.
    """
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
    # ── 0 · el pricing nace con sus 6 líneas y el PUT las deja cargadas ─────────
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
    r = cli.post("/api/compras-contab", json=_compra_del_gasto(emb_id, id_agencia))
    check("1 la CxP del gasto de embarque se registra → 200", r.status_code == 200,
          r.text[:250])
    compra_id = (r.json() or {}).get("id") if r.status_code == 200 else None
    check("1 y devuelve el id de la compra", isinstance(compra_id, int), r.text[:150])

    r = cli.post("/api/compras-contab", json=_compra_del_gasto(emb_id, id_agencia))
    check("1 el 2° intento INMEDIATO del mismo gasto → 409 (el anti-duplicado funciona)",
          r.status_code == 409, (r.status_code, r.text[:200]))

    r = cli.get("/api/compras-contab/costos-embarque")
    fila = next((x for x in (r.json().get("costos") or []) if x["id"] == id_agencia), None)
    check("1 el overlay marca el gasto como REGISTRADO (compra_id != None)",
          fila is not None and fila.get("compra_id") == compra_id,
          (r.status_code, fila))

    # ── 2 · EL CAMINO DEL BUG: re-guardar el pricing (corregir el TC) ───────────
    # Esto es lo que hace el contador todos los días, y el front SIEMPRE manda `gastos`.
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

    r = cli.get("/api/compras-contab/costos-embarque")
    fila = next((x for x in (r.json().get("costos") or []) if x["id"] == id_agencia), None)
    check("2 el overlay SIGUE diciendo 'registrado' después de re-guardar el pricing "
          "(si vuelve a decir 'no registrado', el operador lo carga de nuevo con 2 clics)",
          fila is not None and fila.get("compra_id") == compra_id, fila)

    # ── 3 · el anti-duplicado sigue vivo DESPUÉS del re-guardado ────────────────
    # OJO — se usa la id que el OVERLAY REPORTA AHORA, no la vieja: es lo que manda la
    # pantalla al hacer clic en "Registrar como compra" tras recargar. Con la id vieja el
    # backend contestaría 400 "no existe" y la sonda daría un FALSO VERDE tapando el bug.
    id_ui = _gasto_de_overlay(id_agencia)
    r = cli.post("/api/compras-contab", json=_compra_del_gasto(emb_id, id_ui))
    check("3 volver a registrar el gasto TAL COMO LO MANDA LA PANTALLA → 409 "
          "(con la llave inestable salía 200 y nacía la CxP DUPLICADA)",
          r.status_code == 409, (r.status_code, id_ui, r.text[:200]))

    cxp = _cxp_en_bd()
    total = round(sum(x[2] for x in cxp), 0)
    check(f"3 Σ CxP ACTIVA = {BRUTO:.0f} (una sola factura del forwarder, no el doble)",
          len(cxp) == 1 and abs(total - BRUTO) <= 1.0,
          {"n_compras": len(cxp), "suma": total, "esperado": BRUTO})

    # ── 4 · la ronda completa aguanta varios re-guardados seguidos ──────────────
    for i in range(3):
        rr = cli.put(_url(emb_id), json={**_payload_gastos(NETO), "tc_valor": 940 + i})
        if rr.status_code != 200:
            check(f"4 re-guardado #{i} → 200", False, rr.text[:150])
            break
    g2 = _gastos_en_bd(emb_id)
    cxp = _cxp_en_bd()
    r = cli.post("/api/compras-contab", json=_compra_del_gasto(emb_id, _gasto_de_overlay(id_agencia)))
    check("4 tras 3 re-guardados más: mismas ids, misma llave, sigue el 409 y Σ CxP intacta",
          {t: v[0] for t, v in g2.items()} == ids0 and len(cxp) == 1
          and cxp[0][1] == id_agencia and r.status_code == 409
          and abs(round(sum(x[2] for x in cxp), 0) - BRUTO) <= 1.0,
          {"ids": {t: v[0] for t, v in g2.items()}, "cxp": cxp, "post": r.status_code,
           "suma": round(sum(x[2] for x in cxp), 0)})

    # ── 5 · anular la CxP SÍ libera el gasto (el flujo legítimo no se rompió) ───
    r = cli.post(f"/api/compras-contab/{compra_id}/anular",
                 json={"motivo": f"{MARK} prueba de reversa"})
    check("5 anular la CxP → 200", r.status_code == 200, r.text[:200])
    id_ui = _gasto_de_overlay(id_agencia)
    r = cli.get("/api/compras-contab/costos-embarque")
    fila = next((x for x in (r.json().get("costos") or []) if x["id"] == id_ui), None)
    check("5 con la CxP anulada el overlay vuelve a 'no registrado'",
          fila is not None and fila.get("compra_id") is None, fila)
    r = cli.post("/api/compras-contab", json=_compra_del_gasto(emb_id, id_ui))
    check("5 y el gasto se puede volver a registrar → 200 (anular es la salida legítima)",
          r.status_code == 200, (r.status_code, r.text[:200]))
    cxp = _cxp_en_bd()
    check("5 queda UNA sola CxP activa por el gasto",
          len([x for x in cxp]) == 1 and abs(round(sum(x[2] for x in cxp), 0) - BRUTO) <= 1.0,
          cxp)

    # ── 6 · la llave natural está DECLARADA en la BD (no solo en el código) ─────
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT COUNT(DISTINCT index_name) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'emb_pricing_gasto' "
            "AND index_name = 'uq_emb_pricing_gasto_tipo' AND non_unique = 0")).scalar()
    check("6 existe el UNIQUE (pricing_id, tipo) en emb_pricing_gasto "
          "(si falta: correr embarques_pricing.init_db)", int(n or 0) == 1, n)
    # …y la BD RECHAZA de verdad una 2ª línea del mismo tipo (conducta, no metadato).
    db = SessionLocal()
    try:
        pr = db.query(EmbarquePricing).filter(EmbarquePricing.embarque_id == emb_id).first()
        db.add(EmbarquePricingGasto(pricing_id=pr.id, tipo="agencia", glosa="DUP",
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


def test_llave_gasto_embarque_estable_ga():
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
