"""Paridad de Pricing MonzaParts ↔ Grupo AM: precarga de gastos (EP-10) y lock del
pricing al escribir (EP-16).

DOS AGUJEROS QUE ESTA SUITE CIERRA

1. EP-10 — MonzaParts sembraba las 6 líneas de GASTOS LOCALES en **0** mientras Grupo AM
   las precarga desde Config con su IVA. Si el contador se olvidaba de cargarlas, el
   costo landed salía SIN gastos de internación y `cerrar_pricing` lo CONGELABA igual
   (solo exige costo_total > 0): todos los ítems del embarque quedaban subvaluados, en
   silencio y para siempre. Ahora `seed_gastos` lee MonzaConfig
   (desconsolidado_clp / bodegaje_clp / costo_agencia_minimo_clp) y aplica el IVA de la
   configuración a las 3 líneas afectas.

2. EP-16 — Nadie lockeaba la cabecera del pricing al escribir. El costo landed que se
   congela ES plata: dos `POST /cerrar` simultáneos leían los dos `estado != 'cerrado'`,
   recalculaban los dos y el segundo PISABA el snapshot del primero (dos costos
   distintos congelados para el mismo embarque, sin rastro). Ahora las 3 rutas de
   escritura releen con `populate_existing().with_for_update()` y el 2º cierre recibe
   el 409 de siempre.

SONDAS DE PODER DISCRIMINANTE (van DENTRO de la suite, no de palabra):
  · sección 1d: se re-inyecta el `seed_gastos` VIEJO (las 6 en 0) y se comprueba que el
    MISMO predicado que da verde con el código real da ROJO con el vicio.
  · sección 2c: se fuerza `bloquear=False` (el código de antes) y los DOS cierres
    concurrentes responden 200 — la carrera que el lock cierra, reproducida.
  La carrera se vuelve DETERMINISTA con una compuerta: la 1ª llamada a `_compute_detail`
  duerme, así el 2º request llega SIEMPRE con el 1º dentro de su transacción.

Datos MARCADOS + limpieza total en `finally` + verificación con SESIÓN NUEVA. Los 3
parámetros de MonzaConfig se fotografían y se RESTAURAN (son columnas nuevas, hoy en 0;
la foto se verifica al final). No se toca `iva_pct`: la suite lee el que esté puesto.

Corre con:  ./venv/bin/python -m pytest monza_embarques_pricing/tests/test_paridad_pricing.py -q
(también:   ./venv/bin/python monza_embarques_pricing/tests/test_paridad_pricing.py)
"""
import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
import monza_embarques_pricing.integration as integ  # noqa: E402  (sonda 1d)
import monza_embarques_pricing.router as pr_mod  # noqa: E402  (sondas 2b/2c)
from monza_embarques_pricing.models import (  # noqa: E402
    MonzaEmbPricing, MonzaEmbPricingGasto, MonzaEmbPricingItem,
)

MARK = "__TEST_MEPP__"

app = FastAPI()
app.include_router(pr_mod.router)


# Auth REALISTA (lección G13): la lectura abre el read view de MySQL en la MISMA sesión
# del request, ANTES de cualquier with_for_update(), igual que auth.get_current_user en
# producción. Con un lambda seco el lock sería la 1ª sentencia, el snapshot nacería
# DESPUÉS del lock y la carrera de la sección 2 quedaría invisible.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=1, email=f"{MARK}@test.invalid", empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails: list = []
# Foto de los 3 parámetros de MonzaConfig (se restauran en el finally).
_cfg_foto: dict = {}

# Compuerta de la carrera: la 1ª llamada a _compute_detail de la ronda duerme.
_gate = {"n": 0, "delay": 0.0, "lock": threading.Lock()}
_detail_orig = pr_mod._compute_detail
_seed_gastos_orig = integ.seed_gastos


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + "| " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _detail_con_compuerta(db, embarque, pricing):
    """_compute_detail que hace dormir a la PRIMERA llamada de la ronda: el 2º request
    llega siempre con el 1º dentro de su transacción (carrera determinista)."""
    with _gate["lock"]:
        _gate["n"] += 1
        primera = _gate["n"] == 1
    if primera and _gate["delay"]:
        time.sleep(_gate["delay"])
    return _detail_orig(db, embarque, pricing)


def _seed_gastos_viejo(db, pricing, cfg):
    """El seed de ANTES: las 6 líneas en 0 (sonda de EP-10)."""
    for cat in integ.GASTOS_CATALOGO:
        db.add(MonzaEmbPricingGasto(
            pricing_id=pricing.id, tipo=cat["tipo"], glosa=cat["glosa"],
            monto_neto=0, iva=0, capitaliza=cat["capitaliza"],
            nro_factura=None, orden=cat["orden"],
        ))


# ─── Semilla / limpieza ──────────────────────────────────────────────────────
def _cliente(db):
    cli = db.query(mm.MonzaCliente).filter(mm.MonzaCliente.nombre == f"{MARK} Cli").first()
    if cli is None:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cli")
        db.add(cli)
        db.flush()
    return cli


def _embarque(db, sufijo, *, cant=2, costo=100.0, peso=10.0):
    """Cotización vendida + 1 ítem + embarque marcado. Devuelve el id del embarque."""
    cli = _cliente(db)
    cot = mm.MonzaCotizacion(numero=f"{MARK}-{sufijo}", cliente_id=cli.id,
                             estado="vendida", iva_pct=19)
    db.add(cot)
    db.flush()
    it = mm.MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"Pieza {sufijo}",
                                numero_parte=f"P-{sufijo}", cantidad=cant, costo=costo,
                                moneda="USD", peso_kg=peso, estado_linea="en_transito")
    db.add(it)
    db.flush()
    emb = mm.MonzaEmbarque(numero=f"{MARK}-EMB-{sufijo}", estado="en_transito",
                           forwarder="Fastmark")
    db.add(emb)
    db.flush()
    db.add(mm.MonzaEmbarqueItem(embarque_id=emb.id, item_id=it.id))
    db.commit()
    return emb.id


def _limpiar(db):
    db.rollback()
    S = "fetch"
    emb_ids = [e[0] for e in db.query(mm.MonzaEmbarque.id)
               .filter(mm.MonzaEmbarque.numero.like(f"{MARK}%")).all()]
    if emb_ids:
        pr_ids = [p[0] for p in db.query(MonzaEmbPricing.id)
                  .filter(MonzaEmbPricing.embarque_id.in_(emb_ids)).all()]
        if pr_ids:
            db.query(MonzaEmbPricingItem).filter(
                MonzaEmbPricingItem.pricing_id.in_(pr_ids)).delete(synchronize_session=S)
            db.query(MonzaEmbPricingGasto).filter(
                MonzaEmbPricingGasto.pricing_id.in_(pr_ids)).delete(synchronize_session=S)
            db.query(MonzaEmbPricing).filter(
                MonzaEmbPricing.id.in_(pr_ids)).delete(synchronize_session=S)
        db.query(mm.MonzaEmbarqueItem).filter(
            mm.MonzaEmbarqueItem.embarque_id.in_(emb_ids)).delete(synchronize_session=S)
        db.query(mm.MonzaEmbarque).filter(
            mm.MonzaEmbarque.id.in_(emb_ids)).delete(synchronize_session=S)
    cot_ids = [c[0] for c in db.query(mm.MonzaCotizacion.id)
               .filter(mm.MonzaCotizacion.numero.like(f"{MARK}%")).all()]
    if cot_ids:
        db.query(mm.MonzaCotizacionItem).filter(
            mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=S)
        db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=S)
    db.query(mm.MonzaCliente).filter(
        mm.MonzaCliente.nombre == f"{MARK} Cli").delete(synchronize_session=S)
    db.commit()


def _gasto(detalle, tipo):
    return next((g for g in detalle["gastos"] if g["tipo"] == tipo), None)


def _cerrar_en_paralelo(embarque_id, delay=1.2, stagger=0.35):
    """Dos POST /cerrar solapados. Devuelve la lista de status ordenada."""
    _gate["n"] = 0
    _gate["delay"] = delay
    res: dict = {}

    def _post(k, espera):
        time.sleep(espera)
        r = client.post(f"/api/monza/embarques-pricing/{embarque_id}/cerrar")
        res[k] = (r.status_code, r.text[:200])

    hilos = [threading.Thread(target=_post, args=(0, 0.0)),
             threading.Thread(target=_post, args=(1, stagger))]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=90)
    _gate["delay"] = 0.0
    return [res.get(k, (0, "sin respuesta")) for k in (0, 1)]


def run():
    resto, cfg_ok = -1, False
    db = SessionLocal()
    try:
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 1 · EP-10 · los gastos locales nacen PRECARGADOS desde MonzaConfig
        # ══════════════════════════════════════════════════════════════════════
        cfg = db.query(mm.MonzaConfig).order_by(mm.MonzaConfig.id.asc()).first()
        creada = False
        if cfg is None:
            cfg = mm.MonzaConfig(id=1)
            db.add(cfg)
            db.commit()
            db.refresh(cfg)
            creada = True
        _cfg_foto.update({"id": cfg.id, "creada": creada,
                          "desconsolidado_clp": cfg.desconsolidado_clp,
                          "bodegaje_clp": cfg.bodegaje_clp,
                          "costo_agencia_minimo_clp": cfg.costo_agencia_minimo_clp})
        cfg.desconsolidado_clp = 120000
        cfg.bodegaje_clp = 85000
        cfg.costo_agencia_minimo_clp = 200000
        db.commit()
        iva_rate = integ.iva_rate_de_config(cfg)
        print(f"[cfg] iva_rate de MonzaConfig = {iva_rate}")

        emb_a = _embarque(db, "A")
        r = client.get(f"/api/monza/embarques-pricing/{emb_a}")
        check("1a detalle de un embarque nuevo → 200 (auto-crea pricing)",
              r.status_code == 200, r.text[:200])
        det = r.json()
        esperado = {"desconsolidacion": 120000.0, "almacenaje": 85000.0, "agencia": 200000.0}
        for tipo, neto in esperado.items():
            g = _gasto(det, tipo)
            check(f"1b {tipo} nace PRECARGADO en {neto:,.0f} (antes nacía en 0)",
                  g is not None and abs(g["monto_neto"] - neto) < 1, g)
            check(f"1b {tipo} nace con su IVA ({iva_rate:.0%})",
                  g is not None and abs(g["iva"] - round(neto * iva_rate, 0)) < 1, g)
        for tipo in ("arancel", "otros", "iva_importacion"):
            g = _gasto(det, tipo)
            check(f"1c {tipo} sigue naciendo en 0 (lo carga el operador con el papel)",
                  g is not None and g["monto_neto"] == 0 and g["iva"] == 0, g)
        check("1c las 6 líneas canónicas siguen estando", len(det["gastos"]) == 6,
              len(det["gastos"]))
        check("1c total que CAPITALIZA = 405.000 (los 3 netos afectos)",
              abs(det["totales_gastos"]["total_capitaliza"] - 405000) < 1,
              det["totales_gastos"])
        check("1c el landed ya incluye los gastos de internación (gastos_clp > 0)",
              det["totales"]["gastos_clp"] > 0, det["totales"])

        # ── SONDA 1d: con el seed VIEJO los mismos checks se caen ──────────────
        _limpiar(db)
        emb_v = _embarque(db, "V")
        try:
            integ.seed_gastos = _seed_gastos_viejo
            det_v = client.get(f"/api/monza/embarques-pricing/{emb_v}").json()
        finally:
            integ.seed_gastos = _seed_gastos_orig
        malos = [t for t in esperado
                 if (_gasto(det_v, t) or {}).get("monto_neto", 0) != 0]
        check("1d SONDA: con el seed VIEJO las 3 líneas afectas nacen en 0 "
              "(el predicado de 1b da ROJO → el check discrimina de verdad)",
              malos == [] and (det_v["totales_gastos"]["total_capitaliza"] == 0),
              det_v["totales_gastos"])
        check("1d SONDA: y el landed viejo se calcula SIN gastos (gastos_clp == 0)",
              det_v["totales"]["gastos_clp"] == 0, det_v["totales"])
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 2 · EP-16 · dos POST /cerrar simultáneos: uno cierra, el otro 409
        # ══════════════════════════════════════════════════════════════════════
        emb_l = _embarque(db, "L1")
        r = client.put(f"/api/monza/embarques-pricing/{emb_l}",
                       json={"tc_valor": 1000, "tc_tipo": "manual", "shipping_clp": 50000})
        check("2a guardar (PUT) bajo lock sigue funcionando → 200 y estado calculado",
              r.status_code == 200 and r.json()["pricing"]["estado"] == "calculado",
              r.text[:200])
        try:
            pr_mod._compute_detail = _detail_con_compuerta
            estados = _cerrar_en_paralelo(emb_l)
        finally:
            pr_mod._compute_detail = _detail_orig
        codigos = sorted(s for s, _ in estados)
        check("2b dos cierres concurrentes → exactamente un 200 y un 409",
              codigos == [200, 409], estados)
        db.rollback()
        pr = db.query(MonzaEmbPricing).filter(MonzaEmbPricing.embarque_id == emb_l).first()
        check("2b el pricing quedó cerrado UNA vez", pr is not None and pr.estado == "cerrado",
              pr.estado if pr else None)
        n_snap = db.query(MonzaEmbPricingItem).filter(
            MonzaEmbPricingItem.pricing_id == pr.id).count() if pr else -1
        check("2b y con UN solo snapshot por ítem (1 ítem = 1 fila, sin duplicados)",
              n_snap == 1, n_snap)
        _limpiar(db)

        # ── SONDA 2c: sin el lock, los DOS cierres pasan (la carrera de antes) ─
        emb_s = _embarque(db, "L2")
        client.put(f"/api/monza/embarques-pricing/{emb_s}",
                   json={"tc_valor": 1000, "tc_tipo": "manual", "shipping_clp": 50000})
        _get_orig = pr_mod._get_or_create_pricing
        try:
            pr_mod._compute_detail = _detail_con_compuerta
            # El código de ANTES: _get_or_create_pricing SIN with_for_update.
            pr_mod._get_or_create_pricing = (
                lambda db_, emb_, bloquear=False: _get_orig(db_, emb_, bloquear=False))
            estados_sin = _cerrar_en_paralelo(emb_s)
        finally:
            pr_mod._get_or_create_pricing = _get_orig
            pr_mod._compute_detail = _detail_orig
        check("2c SONDA: sin el lock los DOS cierres responden 200 — dos costos "
              "congelados para el mismo embarque (el 409 de 2b lo prueba el lock)",
              sorted(s for s, _ in estados_sin) == [200, 200], estados_sin)
        _limpiar(db)

        # ── SONDA 2d: el lock SIN populate_existing() tampoco sirve ────────────
        # SQLAlchemy devuelve el objeto del identity map (lo metió la lectura plana de
        # ensure_pricing_for_embarque) y DESCARTA la fila fresca que trajo el FOR
        # UPDATE: el 2º cierre vuelve a leer 'calculado' y pisa el snapshot igual.
        emb_p = _embarque(db, "L3")
        client.put(f"/api/monza/embarques-pricing/{emb_p}",
                   json={"tc_valor": 1000, "tc_tipo": "manual", "shipping_clp": 50000})

        def _lock_sin_populate(db_, emb_, bloquear=False):
            p = integ.ensure_pricing_for_embarque(db_, emb_, commit=True)
            if not bloquear:
                return p
            return (db_.query(MonzaEmbPricing).filter(MonzaEmbPricing.id == p.id)
                    .with_for_update().first())

        try:
            pr_mod._compute_detail = _detail_con_compuerta
            pr_mod._get_or_create_pricing = _lock_sin_populate
            estados_sp = _cerrar_en_paralelo(emb_p)
        finally:
            pr_mod._get_or_create_pricing = _get_orig
            pr_mod._compute_detail = _detail_orig
        check("2d SONDA: con with_for_update() pero SIN populate_existing() los DOS "
              "cierres responden 200 (el identity map pisa la fila fresca)",
              sorted(s for s, _ in estados_sp) == [200, 200], estados_sp)
        _limpiar(db)

    finally:
        # Restaurar MonzaConfig ANTES de la verificación (columnas nuevas, foto exacta).
        db.rollback()
        if _cfg_foto:
            cfg = db.query(mm.MonzaConfig).filter(
                mm.MonzaConfig.id == _cfg_foto["id"]).first()
            if cfg is not None:
                if _cfg_foto["creada"]:
                    db.delete(cfg)
                else:
                    cfg.desconsolidado_clp = _cfg_foto["desconsolidado_clp"]
                    cfg.bodegaje_clp = _cfg_foto["bodegaje_clp"]
                    cfg.costo_agencia_minimo_clp = _cfg_foto["costo_agencia_minimo_clp"]
            db.commit()
        _limpiar(db)
        db.close()
        # Verificación con SESIÓN NUEVA: la del test arrastra su read view.
        db2 = SessionLocal()
        try:
            resto = db2.execute(
                text("SELECT COUNT(*) FROM monza_embarques WHERE numero LIKE :m"),
                {"m": f"{MARK}%"}).scalar()
            resto += db2.execute(
                text("SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE :m"),
                {"m": f"{MARK}%"}).scalar()
            cfg2 = (db2.query(mm.MonzaConfig).filter(mm.MonzaConfig.id == _cfg_foto["id"]).first()
                    if _cfg_foto else None)
            cfg_ok = True
            if _cfg_foto and not _cfg_foto["creada"]:
                cfg_ok = (cfg2 is not None
                          and cfg2.desconsolidado_clp == _cfg_foto["desconsolidado_clp"]
                          and cfg2.bodegaje_clp == _cfg_foto["bodegaje_clp"]
                          and cfg2.costo_agencia_minimo_clp == _cfg_foto["costo_agencia_minimo_clp"])
            elif _cfg_foto:
                cfg_ok = cfg2 is None
            print(f"[cleanup] filas MARCADAS que sobreviven: {resto} · "
                  f"MonzaConfig restaurada: {cfg_ok}")
        finally:
            db2.close()

    assert not _fails and resto == 0 and cfg_ok, \
        f"fallas={_fails} residuos={resto} config_restaurada={cfg_ok}"
    print("\n=== TODO OK ===")


def test_monza_pricing_paridad_seed_y_lock():
    """Wrapper de una línea: sin esto pytest no descubre run() (patrón de la casa; ya
    hubo DOS suites invisibles por olvidarlo)."""
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
