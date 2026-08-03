"""Paridad de Embarques Pricing Grupo AM ← MonzaParts (ola 1-E, 2026-07-30).

Cubre los 6 endurecimientos que Monza ya tenía y a Grupo AM le faltaban. Grupo AM es
la marca que emite documentos tributarios REALES y la MÁS expuesta en este módulo:
un embarque suyo CONSOLIDA varias OC de proveedor, cada una con su propia moneda.

  EP-01  El payload no tenía UN SOLO `Field`: un monto NEGATIVO en "Otros" restaba del
         total que capitaliza y `cerrar_pricing` lo CONGELABA (solo exige costo > 0).
  EP-02  No avisaba que el embarque trae órdenes en MÁS DE UNA MONEDA mientras aplica
         UN SOLO TC (un ítem EUR convertido a ~940 en vez de ~1100 = ~17% de FOB
         subvaluado, en silencio y congelado). Aviso, NO bloqueo: el embarque ya llegó.
  EP-06  `integration` leía `cfg.tipo_cambio_eur`, columna que NO EXISTÍA → el getattr
         devolvía SIEMPRE 0, y el detalle solo servía `tc_config` si la moneda era USD.
  EP-11  Un override con `embarque_item_id` de OTRO embarque se guardaba callado.
  EP-15  `_persist_snapshot` borraba sin `flush()` antes de re-insertar.
  EP-16  Nadie lockeaba la cabecera: dos POST /cerrar simultáneos congelaban DOS costos
         distintos para el mismo embarque.

SONDAS DE PODER DISCRIMINANTE verificadas quitando cada arreglo (detalle en el reporte
de la ola): EP-01 → 6 checks rojos · EP-02 → 5 rojos · EP-06 → 4 rojos · EP-11 → 1 rojo
· EP-16 → las 3 rondas de concurrencia rojas (dos 200). EP-15 no es discriminante y se
declara como tal más abajo.

Datos MARCADOS + limpieza + verificación por DELTAS con sesión nueva. No emite ni toca
ningún documento tributario: este módulo no habla con el SII.

Corre con:  cd backend && ./venv/bin/python -m pytest embarques_pricing/tests/test_paridad_pricing.py -q
(también:   ./venv/bin/python embarques_pricing/tests/test_paridad_pricing.py)
"""
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    User, Cotizacion, ItemCotizacion, OcProveedor, OcProveedorItem,
    Embarque, EmbarqueItem,
)
from embarques_pricing.models import (  # noqa: E402
    EmbarquePricing, EmbarquePricingGasto, EmbarquePricingItem,
)
from embarques_pricing.router import router  # noqa: E402
from embarques_pricing.integration import get_cfg, tc_de_config  # noqa: E402
from embarques_pricing.service import total_gastos_que_capitalizan  # noqa: E402

MARK = "__TEST_EP_PARIDAD__"
USER_EMAIL = f"{MARK}@test.local"
RONDAS = 3  # la carrera es probabilística: varias rondas para no dar falsos verdes


# El override CONSULTA la base con la MISMA sesión del request, igual que auth.py en
# producción: así el read view de la transacción nace ANTES de cualquier lock y la
# carrera de EP-16 es la real. Con un lambda que no toca la base, el with_for_update()
# sería la primera sentencia del request y la carrera nunca aparecería.
def _cu(db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == USER_EMAIL).first()
    if u is None:                                  # sin seed: objeto mínimo minero
        return SimpleNamespace(id=None, empresa="mineria")
    return u


app = FastAPI()
app.include_router(router, prefix="/api")          # igual que main.py
app.dependency_overrides[get_current_user] = _cu
cli = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def approx(a, b, tol=1.0):
    return abs(float(a) - float(b)) <= tol


def _url(emb_id: int) -> str:
    return f"/api/embarques-pricing/{emb_id}"


# ─── Seed / limpieza ──────────────────────────────────────────────────────────
def _purge(db: Session) -> None:
    """Borra TODO lo marcado, en orden seguro de FK (idempotente)."""
    embs = db.query(Embarque).filter(Embarque.numero.like(f"{MARK}%")).all()
    for emb in embs:
        pr = db.query(EmbarquePricing).filter(EmbarquePricing.embarque_id == emb.id).first()
        if pr:
            db.query(EmbarquePricingItem).filter(
                EmbarquePricingItem.pricing_id == pr.id).delete(synchronize_session=False)
            db.query(EmbarquePricingGasto).filter(
                EmbarquePricingGasto.pricing_id == pr.id).delete(synchronize_session=False)
            db.query(EmbarquePricing).filter(
                EmbarquePricing.id == pr.id).delete(synchronize_session=False)
        db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == emb.id).delete(synchronize_session=False)
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


def _item(db, cot_id, parte, peso, precio):
    it = ItemCotizacion(cotizacion_id=cot_id, numero_parte=parte, descripcion=f"DESC {parte}",
                        cantidad=1, peso_unit_lbs=peso, precio_unit_cotizacion=precio)
    db.add(it)
    db.flush()
    return it


def seed():
    """4 embarques: USD normal (2 OC USD) · monedas MEZCLADAS (USD+EUR) · EUR puro ·
    y uno extra para probar la pertenencia del override."""
    db = SessionLocal()
    try:
        _purge(db)
        db.add(User(email=USER_EMAIL, nombre=MARK, hashed_password="x", empresa="mineria"))
        cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} Cliente")
        db.add(cot)
        db.flush()

        ocpA = OcProveedor(numero=f"{MARK}-OCP-A", proveedor="FLORIDA ENGINE", moneda="USD")
        ocpB = OcProveedor(numero=f"{MARK}-OCP-B", proveedor="PROV B", moneda="USD")
        ocpE = OcProveedor(numero=f"{MARK}-OCP-EUR", proveedor="BAUKAT GMBH", moneda="EUR")
        for o in (ocpA, ocpB, ocpE):
            db.add(o)
        db.flush()

        out = {}

        # emb1: USD, 2 OC (A y B) las DOS en USD → NO debe avisar moneda mezclada.
        emb1 = Embarque(numero=f"{MARK}-EMB1", estado="en_bodega", forwarder="LATAM Cargo")
        db.add(emb1)
        db.flush()
        for parte, peso, ocp in (("EP-A1", 1.0, ocpA), ("EP-A2", 3.0, ocpB)):
            it = _item(db, cot.id, parte, peso, 100)
            db.add(OcProveedorItem(oc_proveedor_id=ocp.id, item_cotizacion_id=it.id))
            db.add(EmbarqueItem(embarque_id=emb1.id, item_cotizacion_id=it.id,
                                oc_proveedor_id=ocp.id))
        db.flush()
        out["emb1"] = emb1.id

        # embMix: consolida una OC USD y una OC EUR → aviso defensivo visible.
        embM = Embarque(numero=f"{MARK}-EMBMIX", estado="en_bodega", forwarder="LATAM Cargo")
        db.add(embM)
        db.flush()
        for parte, peso, ocp in (("EP-M1", 2.0, ocpA), ("EP-M2", 2.0, ocpE)):
            it = _item(db, cot.id, parte, peso, 100)
            db.add(OcProveedorItem(oc_proveedor_id=ocp.id, item_cotizacion_id=it.id))
            db.add(EmbarqueItem(embarque_id=embM.id, item_cotizacion_id=it.id,
                                oc_proveedor_id=ocp.id))
        db.flush()
        out["embMix"] = embM.id

        # embEUR: solo OC EUR (Baukat) → TC sembrado y sugerido desde Config.
        embE = Embarque(numero=f"{MARK}-EMBEUR", estado="en_bodega", forwarder="BAUKAT")
        db.add(embE)
        db.flush()
        itE = _item(db, cot.id, "EP-E1", 1.0, 50)
        db.add(OcProveedorItem(oc_proveedor_id=ocpE.id, item_cotizacion_id=itE.id))
        db.add(EmbarqueItem(embarque_id=embE.id, item_cotizacion_id=itE.id,
                            oc_proveedor_id=ocpE.id))
        db.flush()
        out["embEUR"] = embE.id

        db.commit()
        # ids de los embarque_items (para los overrides y la prueba de pertenencia)
        out["ei_emb1"] = [x[0] for x in db.query(EmbarqueItem.id)
                          .filter(EmbarqueItem.embarque_id == out["emb1"])
                          .order_by(EmbarqueItem.id.asc()).all()]
        out["ei_embMix"] = [x[0] for x in db.query(EmbarqueItem.id)
                            .filter(EmbarqueItem.embarque_id == out["embMix"])
                            .order_by(EmbarqueItem.id.asc()).all()]
        print(f"[seed] emb1={out['emb1']} embMix={out['embMix']} embEUR={out['embEUR']}")
        return out
    finally:
        db.close()


def _residuos():
    """Verificación por DELTAS con una CONEXIÓN NUEVA: 0 filas marcadas sobreviven."""
    with engine.connect() as conn:
        emb = conn.execute(text("SELECT COUNT(*) FROM embarques WHERE numero LIKE :m"),
                           {"m": f"{MARK}%"}).scalar()
        ocp = conn.execute(text("SELECT COUNT(*) FROM oc_proveedor WHERE numero LIKE :m"),
                           {"m": f"{MARK}%"}).scalar()
        cot = conn.execute(text("SELECT COUNT(*) FROM cotizaciones WHERE numero LIKE :m"),
                           {"m": f"{MARK}%"}).scalar()
        usr = conn.execute(text("SELECT COUNT(*) FROM users WHERE email = :e"),
                           {"e": USER_EMAIL}).scalar()
    return int(emb or 0) + int(ocp or 0) + int(cot or 0) + int(usr or 0)


def _snapshot_db(emb_id: int):
    """Snapshot persistido leído con conexión NUEVA (n filas, Σ costo)."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT COUNT(i.id), COALESCE(SUM(i.costo_total_clp), 0), MAX(p.estado) "
            "FROM emb_pricing p LEFT JOIN emb_pricing_item i ON i.pricing_id = p.id "
            "WHERE p.embarque_id = :e"), {"e": emb_id}).fetchone()
    return int(row[0] or 0), float(row[1] or 0), row[2]


def _en_paralelo(fn, n=2):
    """Dispara n llamadas simultáneas (barrera) y devuelve sus resultados."""
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


# ─── Checks ───────────────────────────────────────────────────────────────────
def run(ids):
    emb1, embMix, embEUR = ids["emb1"], ids["embMix"], ids["embEUR"]

    # ── EP-01 · montos >= 0 y strings con tope (rechazo en la API, no en MySQL) ──
    # Por qué importa: `total_gastos_que_capitalizan` SUMA los netos, así que un
    # negativo RESTA del pozo que se prorratea a todos los ítems.
    sano = total_gastos_que_capitalizan([{"monto_neto": 340_000, "capitaliza": True}])
    con_negativo = total_gastos_que_capitalizan([
        {"monto_neto": 340_000, "capitaliza": True},
        {"monto_neto": -500_000, "capitaliza": True},   # "Otros" con signo menos
    ])
    check("EP-01 el daño existe: un neto negativo RESTA del total capitalizable "
          f"({sano:.0f} → {con_negativo:.0f})", con_negativo < 0 < sano, (sano, con_negativo))

    r = cli.put(_url(emb1), json={"tc_valor": -5})
    check("EP-01 tc_valor negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"shipping_clp": -1})
    check("EP-01 shipping_clp negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"shipping_me": -1})
    check("EP-01 shipping_me negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"gastos": [{"tipo": "otros", "monto_neto": -500_000}]})
    check("EP-01 gasto 'otros' con monto NEGATIVO → 422 (antes se congelaba)",
          r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"gastos": [{"tipo": "agencia", "iva": -1}]})
    check("EP-01 iva negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"items": [{"embarque_item_id": ids["ei_emb1"][0],
                                            "fob_unit": -1, "fob_manual": True}]})
    check("EP-01 fob_unit negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"items": [{"embarque_item_id": ids["ei_emb1"][0],
                                            "peso_unit_lbs": -2, "peso_manual": True}]})
    check("EP-01 peso_unit_lbs negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"gastos": [{"tipo": "agencia", "nro_factura": "F" * 101}]})
    check("EP-01 nro_factura de 101 chars → 422 (columna String(100), antes truncaba)",
          r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"gastos": [{"tipo": "agencia", "banco": "B" * 101}]})
    check("EP-01 banco de 101 chars → 422", r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"gastos": [{"tipo": "agencia", "glosa": "G" * 121}]})
    check("EP-01 glosa de 121 chars → 422", r.status_code == 422, r.status_code)
    r = cli.put(_url(emb1), json={"gastos": [{"tipo": "agencia", "fecha_factura": "9" * 31}]})
    check("EP-01 fecha_factura de 31 chars → 422", r.status_code == 422, r.status_code)
    # …y lo legítimo sigue pasando (el guard no rompe la operación normal)
    r = cli.put(_url(emb1), json={
        "tc_tipo": "manual", "tc_valor": 962, "flete_en_me": False, "shipping_clp": 40_000,
        "gastos": [{"tipo": "agencia", "monto_neto": 160_000, "iva": 30_400,
                    "nro_factura": "F-100", "banco": "Banco de Chile",
                    "fecha_factura": "2026-07-30"}],
    })
    check("EP-01 el guardado legítimo sigue en 200", r.status_code == 200, r.text[:200])
    d1 = r.json() if r.status_code == 200 else {}
    check("EP-01 el gasto legítimo capitaliza 160.000",
          approx(d1.get("totales_gastos", {}).get("total_capitaliza", 0), 160_000),
          d1.get("totales_gastos"))

    # ── EP-02 · aviso de moneda mezclada (visible, NO bloqueante) ───────────────
    r = cli.get(_url(emb1))
    check("EP-02 el detalle SIEMPRE trae la llave 'advertencias'",
          r.status_code == 200 and "advertencias" in r.json(), r.text[:200])
    check("EP-02 dos OC de proveedor las dos en USD → sin advertencias",
          r.json().get("advertencias") == [], r.json().get("advertencias"))

    r = cli.get(_url(embMix))
    advs = r.json().get("advertencias") or [] if r.status_code == 200 else []
    # Este embarque dispara DOS avisos y los dos son correctos: (1) el costeo va en USD y
    # una de sus OC está en EUR, y (2) las OC no se ponen de acuerdo entre ellas. Antes
    # solo existía el (2), que es justo el caso que el dueño dice que no pasa.
    check("EP-02 embarque que consolida OC en USD y en EUR → aparecen los avisos",
          len(advs) >= 1, advs)
    check("EP-02 los avisos nombran las DOS monedas y la que usa el costo",
          bool(advs) and any("USD" in a and "EUR" in a for a in advs), advs)
    check("EP-02 avisa que el costeo está en OTRA moneda que la orden (el caso que SÍ pasa)",
          any("orden(es) de proveedor de este embarque están en" in a for a in advs), advs)
    check("EP-02 y avisa que las órdenes traen más de una moneda entre ellas",
          any("más de una moneda" in a for a in advs), advs)
    r = cli.put(_url(embMix), json={"tc_valor": 1000, "flete_en_me": False,
                                    "shipping_clp": 10_000})
    check("EP-02 el aviso NO bloquea el guardado (200)", r.status_code == 200, r.text[:200])
    check("EP-02 el aviso sigue visible después de guardar",
          len(r.json().get("advertencias") or []) >= 1, r.json().get("advertencias"))
    r = cli.post(f"{_url(embMix)}/cerrar")
    check("EP-02 el aviso NO bloquea el cierre (200): la mercadería ya llegó",
          r.status_code == 200, r.text[:200])
    check("EP-02 el aviso sigue visible con el pricing CERRADO",
          len(r.json().get("advertencias") or []) >= 1, r.json().get("advertencias"))

    # ── EP-06 · TC del EURO en Config (la columna que no existía) ───────────────
    db = SessionLocal()
    try:
        cfg = get_cfg(db)
        tc_usd_cfg = float(getattr(cfg, "tipo_cambio_usd", 0) or 0)
        tc_eur_cfg = float(getattr(cfg, "tipo_cambio_eur", 0) or 0)
    finally:
        db.close()
    check("EP-06 Config ya tiene tipo_cambio_eur > 0 (antes el getattr daba SIEMPRE 0)",
          tc_eur_cfg > 0, tc_eur_cfg)
    check("EP-06 tc_de_config separa las monedas (no sirve el TC USD en un embarque EUR)",
          approx(tc_de_config(SimpleNamespace(tipo_cambio_usd=940, tipo_cambio_eur=1100), "EUR"), 1100)
          and approx(tc_de_config(SimpleNamespace(tipo_cambio_usd=940, tipo_cambio_eur=1100), "USD"), 940))

    r = cli.get(_url(embEUR))
    p = r.json().get("pricing", {}) if r.status_code == 200 else {}
    check("EP-06 embarque EUR: el detalle SUGIERE el TC EUR de Config (antes 0)",
          approx(p.get("tc_config", 0), tc_eur_cfg) and p.get("tc_config", 0) > 0, p.get("tc_config"))
    check("EP-06 embarque EUR: el pricing NACE con ese TC y tc_tipo='config'",
          approx(p.get("tc_valor", 0), tc_eur_cfg) and p.get("tc_tipo") == "config",
          (p.get("tc_valor"), p.get("tc_tipo")))
    check("EP-06 y la moneda del embarque EUR se detectó bien", p.get("moneda") == "EUR",
          p.get("moneda"))
    r = cli.get(_url(emb1))
    check("EP-06 no hay regresión en USD: sigue sugiriendo el TC USD de Config",
          approx(r.json()["pricing"]["tc_config"], tc_usd_cfg) and tc_usd_cfg > 0,
          r.json()["pricing"]["tc_config"])

    # ── EP-11 · el override tiene que ser de ESTE embarque ──────────────────────
    ajeno = ids["ei_embMix"][0]
    r = cli.put(_url(emb1), json={"items": [{"embarque_item_id": ajeno,
                                            "fob_unit": 777, "fob_manual": True}]})
    check("EP-11 override con embarque_item_id de OTRO embarque → 400 (antes: fila huérfana)",
          r.status_code == 400, (r.status_code, r.text[:150]))
    r = cli.put(_url(emb1), json={"items": [{"embarque_item_id": 999_999_999,
                                            "fob_unit": 777, "fob_manual": True}]})
    check("EP-11 override con embarque_item_id inexistente → 400", r.status_code == 400,
          r.status_code)
    propio = ids["ei_emb1"][0]
    r = cli.put(_url(emb1), json={"items": [{"embarque_item_id": propio,
                                            "fob_unit": 777, "fob_manual": True}]})
    ok = r.status_code == 200 and any(
        x["embarque_item_id"] == propio and approx(x["fob_unit"], 777) for x in r.json()["items"])
    check("EP-11 el override del ítem PROPIO sigue funcionando", ok, r.text[:200])

    # ── EP-15 · re-guardar no duplica el snapshot ───────────────────────────────
    # NOTA HONESTA: esta sonda NO es discriminante. `Query.delete()` emite el DELETE
    # de inmediato, así que el flush() agregado es defensa en profundidad (paridad con
    # Monza) y quitarlo no pone el check en rojo. Se deja como invariante de cuadre.
    cli.put(_url(emb1), json={"tc_valor": 962, "shipping_clp": 40_000})
    n, suma, estado = _snapshot_db(emb1)
    check("EP-15 tras re-guardar, el snapshot tiene 1 fila por ítem (sin duplicados)",
          n == len(ids["ei_emb1"]), (n, len(ids["ei_emb1"])))
    check("EP-15 y el Σ del snapshot en BD cuadra con el total que devolvió la API",
          approx(suma, cli.get(_url(emb1)).json()["totales"]["costo_total_clp"], tol=2.0),
          (suma, estado))

    # ── EP-16 · dos cierres simultáneos NO congelan dos costos distintos ────────
    for ronda in range(RONDAS):
        rr = cli.post(f"{_url(emb1)}/reabrir")
        if rr.status_code != 200:
            check(f"EP-16 ronda {ronda}: reabrir 200", False, rr.text[:150])
            break
        res = _en_paralelo(lambda _i: cli.post(f"{_url(emb1)}/cerrar"), 2)
        codes = sorted(getattr(x, "status_code", 0) if not isinstance(x, tuple) else -1
                       for x in res)
        check(f"EP-16 ronda {ronda}: dos POST /cerrar simultáneos → un 200 y un 409 "
              "(sin lock salían DOS 200 y el 2º pisaba el snapshot del 1º)",
              codes == [200, 409], (codes, [str(x)[:120] for x in res]))
        n2, suma2, estado2 = _snapshot_db(emb1)
        ganador = next((x for x in res
                        if not isinstance(x, tuple) and x.status_code == 200), None)
        check(f"EP-16 ronda {ronda}: queda UN solo snapshot, y es el del ganador",
              n2 == len(ids["ei_emb1"]) and estado2 == "cerrado" and ganador is not None
              and approx(suma2, ganador.json()["totales"]["costo_total_clp"], tol=2.0),
              (n2, suma2, estado2))
    cli.post(f"{_url(emb1)}/reabrir")


def cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        _purge(db)
    except Exception as e:                           # noqa: BLE001
        db.rollback()
        print(f"⚠️  cleanup falló: {e}")
    finally:
        db.close()


def test_paridad_embarques_pricing_ga():
    """Wrapper para pytest: llama a run() DIRECTAMENTE (el candado
    tests_infra/test_suites_visibles.py exige la llamada literal; envolverla en un
    helper cuenta como wrapper hueco y la suite quedaría invisible al gate)."""
    ids = seed()
    try:
        run(ids)
    finally:
        cleanup()
    resto = _residuos()
    print(f"[cleanup] filas MARCADAS que sobreviven: {resto}")
    assert not _fails and resto == 0, f"fallas={_fails} residuos={resto}"


if __name__ == "__main__":
    _ids = seed()
    try:
        run(_ids)
    finally:
        cleanup()
    _resto = _residuos()
    print(f"[cleanup] filas MARCADAS que sobreviven: {_resto}")
    print()
    if _fails or _resto:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails} · residuos={_resto}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
