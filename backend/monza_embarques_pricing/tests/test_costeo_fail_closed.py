"""Costeo de embarques que falla CERRADO: negativos, moneda divergente y TC sin parametrizar.

Espejo de `embarques_pricing/tests/test_costeo_fail_closed.py` (Grupo AM), adaptado a Monza:
acá la moneda del embarque se siembra con la del PRIMER ÍTEM de cotización (Monza no tiene OC
de proveedor en el camino del pricing), y los parámetros viven en `monza_config`.

LOS 4 AGUJEROS QUE ESTA SUITE CIERRA (todos son error de COSTO silencioso y congelado)
--------------------------------------------------------------------------------------
A) NEGATIVOS POR LA SEGUNDA PUERTA. El `ge=0` del PUT cubre el payload, pero los montos de
   Desconsolidación / Almacenaje / Agencia entran TAMBIÉN por `integration.seed_gastos`,
   copiados de MonzaConfig, cuyo editor no valida signo. `total_gastos_que_capitalizan` SUMA
   los netos, así que un negativo RESTA del pozo que se prorratea a TODOS los ítems; un PUT
   solo-encabezado no pasa por el schema y `cerrar` solo exigía `costo_total > 0`, así que el
   costo negativo quedaba CONGELADO. Ahora: piso 0 al sembrar + `_validar_gastos_no_negativos`
   en el PUT y en el cierre.

B) EL AVISO DE MONEDA MIRABA AL CASO EQUIVOCADO. Solo avisaba si los ítems traían monedas
   distintas ENTRE ELLOS — el caso que el dueño dice que no pasa — y era CIEGO al que sí pasa:
   `monza_emb_pricing.moneda` se siembra UNA vez y nunca se re-sincroniza, así que si el ítem
   se corrige a EUR después de abrir el pricing, el costo se sigue calculando con el TC del
   dólar sin un solo aviso, con UN SOLO ítem.

C) MONEDA SIN TC PARAMETRIZADO (pesos, o un typo): el FOB se multiplicaba por el TC del dólar.

D) `tc_de_config` prometía "fail closed" y hacía lo contrario: cualquier moneda que no fuera
   EXACTAMENTE 'EUR' recibía el TC del DÓLAR, etiquetado `tc_tipo='config'`. Y los 3 gastos de
   internación por defecto de MonzaConfig **no tenían NINGÚN endpoint que los escribiera**
   (`monza_router_config.ConfigIn` no los declara), así que se quedaban en 0 para siempre y la
   precarga del pricing producía exactamente lo mismo que antes. Ahora hay endpoint propio
   (`/config/parametros`) con `ge=0`.

SONDAS: todo por HTTP contra el router REAL, o llamando a la función REAL. Cero introspección
de código.

Datos MARCADOS + limpieza en `finally`. Los 4 parámetros de `monza_config` se FOTOGRAFÍAN al
empezar y se RESTAURAN al terminar, y la restauración se VERIFICA con conexión nueva.
No emite ni toca ningún documento tributario.

Corre con:  cd backend && ./venv/bin/python -m pytest monza_embarques_pricing/tests/test_costeo_fail_closed.py -q
(también:   ./venv/bin/python monza_embarques_pricing/tests/test_costeo_fail_closed.py)
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
from monza_embarques_pricing.router import router  # noqa: E402
from monza_embarques_pricing.integration import tc_de_config, MONEDAS_CON_TC  # noqa: E402

MARK = "__T_MEP_FC__"        # corto: monza_cotizaciones.numero es VARCHAR(20)
PARAMS_CFG = ("tc_eur_clp", "desconsolidado_clp", "bodegaje_clp", "costo_agencia_minimo_clp")


def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=None, email=f"{MARK}@test.invalid", empresa="automotriz")


app = FastAPI()
app.include_router(router)
app.dependency_overrides[get_current_user] = _cu
cli = TestClient(app)

_fails: list = []
_cfg_foto: dict = {}
_cfg_id = {"id": None}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def approx(a, b, tol=1.0):
    return abs(float(a) - float(b)) <= tol


def _url(emb_id: int) -> str:
    return f"/api/monza/embarques-pricing/{emb_id}"


# ─── Config: foto / escritura directa / restauración ──────────────────────────
def _cfg_leer() -> dict:
    """Los parámetros de MonzaConfig, con conexión NUEVA. `get_cfg` toma la fila de menor id,
    así que se lee la MISMA (no se asume id=1)."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT id, tc_usd_clp, tc_eur_clp, desconsolidado_clp, bodegaje_clp, "
            "costo_agencia_minimo_clp FROM monza_config ORDER BY id ASC LIMIT 1")).fetchone()
    if row is None:
        return {}
    _cfg_id["id"] = int(row[0])
    return {
        "tc_usd_clp": float(row[1] or 0), "tc_eur_clp": float(row[2] or 0),
        "desconsolidado_clp": float(row[3] or 0), "bodegaje_clp": float(row[4] or 0),
        "costo_agencia_minimo_clp": float(row[5] or 0),
    }


def _cfg_escribir(campo: str, valor: float) -> None:
    """Escribe DIRECTO en la BD: así es como entra el dato malo (el editor de Configuración no
    valida signo, y no es código de este módulo)."""
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE monza_config SET {campo} = :v WHERE id = :i"),
                     {"v": valor, "i": _cfg_id["id"]})


def _item_moneda(parte: str, moneda: str) -> None:
    """Cambia la moneda del ítem DESPUÉS de que el pricing nació (es lo que pasa en la vida
    real: la cotización se edita y el pricing se queda con la moneda vieja)."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE monza_cotizacion_items SET moneda = :m WHERE numero_parte = :p"),
                     {"m": moneda, "p": parte})


def _gasto_negativo_en_bd(emb_id: int, tipo: str, neto: float) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE monza_emb_pricing_gasto g JOIN monza_emb_pricing p ON p.id = g.pricing_id "
            "SET g.monto_neto = :v WHERE p.embarque_id = :e AND g.tipo = :t"),
            {"v": neto, "e": emb_id, "t": tipo})


def _estado_y_costo(emb_id: int):
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT MAX(p.estado), COALESCE(SUM(i.costo_total_clp), 0) FROM monza_emb_pricing p "
            "LEFT JOIN monza_emb_pricing_item i ON i.pricing_id = p.id "
            "WHERE p.embarque_id = :e"), {"e": emb_id}).fetchone()
    return (row[0] if row else None), float(row[1] or 0) if row else 0.0


# ─── Seed / limpieza ──────────────────────────────────────────────────────────
def _purge(db: Session) -> None:
    for emb in db.query(mm.MonzaEmbarque).filter(
            mm.MonzaEmbarque.numero.like(f"{MARK}%")).all():
        pr = db.query(MonzaEmbPricing).filter(MonzaEmbPricing.embarque_id == emb.id).first()
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
    for c in db.query(mm.MonzaCliente).filter(mm.MonzaCliente.nombre.like(f"{MARK}%")).all():
        db.delete(c)
        db.flush()
    db.commit()


def _embarque(db, sufijo: str, cot, moneda: str, forwarder: str) -> tuple:
    """1 embarque marcado con 2 ítems en `moneda`. Devuelve (embarque_id, parte_del_1er_item)."""
    emb = mm.MonzaEmbarque(numero=f"{MARK}-E-{sufijo}", estado="en_transito",
                           forwarder=forwarder)
    db.add(emb)
    db.flush()
    partes = []
    for n, peso in ((1, 2.0), (2, 5.0)):
        parte = f"{MARK}-{sufijo}-{n}"
        it = mm.MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"Pieza {parte}",
                                    numero_parte=parte, cantidad=1, costo=100,
                                    moneda=moneda, peso_kg=peso, estado_linea="en_transito")
        db.add(it)
        db.flush()
        db.add(mm.MonzaEmbarqueItem(embarque_id=emb.id, item_id=it.id))
        partes.append(parte)
    db.flush()
    return emb.id, partes[0]


def seed():
    _cfg_foto.update(_cfg_leer())
    db = SessionLocal()
    try:
        _purge(db)
        cliente = mm.MonzaCliente(nombre=f"{MARK} Cli")
        db.add(cliente)
        db.flush()
        cot = mm.MonzaCotizacion(numero=f"{MARK}-COT", cliente_id=cliente.id,
                                 estado="vendida", iva_pct=19)
        db.add(cot)
        db.flush()
        ids = {}
        ids["neg"], _ = _embarque(db, "NEG", cot, "USD", "Fastmark")
        ids["deriva"], ids["parte_deriva"] = _embarque(db, "DER", cot, "USD", "Fastmark")
        ids["clp"], _ = _embarque(db, "CLP", cot, "CLP", "Fastmark")
        ids["eur"], _ = _embarque(db, "EUR", cot, "EUR", "BAUKAT")
        db.commit()
        print(f"[seed] {ids}  ·  cfg foto: {_cfg_foto}")
        return ids
    finally:
        db.close()


def _residuos():
    with engine.connect() as conn:
        n = 0
        for sql in (
            "SELECT COUNT(*) FROM monza_embarques WHERE numero LIKE :m",
            "SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE :m",
            "SELECT COUNT(*) FROM monza_clientes WHERE nombre LIKE :m",
        ):
            n += int(conn.execute(text(sql), {"m": f"{MARK}%"}).scalar() or 0)
    ahora = _cfg_leer()
    for k, v in _cfg_foto.items():
        if not approx(ahora.get(k, -1), v, tol=0.0001):
            print(f"⚠️  MonzaConfig NO restaurada: {k} foto={v} ahora={ahora.get(k)}")
            n += 1
    return n


# ─── Checks ───────────────────────────────────────────────────────────────────
def run(ids):
    tc_usd = _cfg_foto.get("tc_usd_clp", 0)
    tc_eur = _cfg_foto.get("tc_eur_clp", 0)

    # ══ A · NEGATIVOS ═════════════════════════════════════════════════════════
    _cfg_escribir("desconsolidado_clp", -500_000)
    db = SessionLocal()
    try:
        cot = db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.numero == f"{MARK}-COT").first()
        emb_seed_id, _ = _embarque(db, "SEE", cot, "USD", "Fastmark")
        db.commit()
    finally:
        db.close()
    r = cli.get(_url(emb_seed_id))          # este GET es el que SIEMBRA las 6 líneas
    gastos = {g["tipo"]: g for g in (r.json().get("gastos") or [])} if r.status_code == 200 else {}
    check("A.1 con el parámetro de MonzaConfig en −500.000, la línea sembrada nace en 0 "
          "(NO se copia el negativo al costeo)",
          r.status_code == 200
          and approx(gastos.get("desconsolidacion", {}).get("monto_neto", -1), 0),
          gastos.get("desconsolidacion"))
    check("A.1 y el total que capitaliza NO es negativo",
          (r.json().get("totales_gastos", {}).get("total_capitaliza", -1)) >= 0,
          r.json().get("totales_gastos"))
    _cfg_escribir("desconsolidado_clp", _cfg_foto.get("desconsolidado_clp", 0))

    emb_neg = ids["neg"]
    r = cli.put(_url(emb_neg), json={"tc_tipo": "manual", "tc_valor": 962,
                                     "flete_en_me": False, "shipping_clp": 40_000,
                                     "gastos": [{"tipo": "agencia", "monto_neto": 340_000,
                                                 "iva": 64_600}]})
    check("A.2 el guardado sano deja el pricing calculado", r.status_code == 200, r.text[:200])
    estado_sano, costo_sano = _estado_y_costo(emb_neg)
    check("A.2 y el costo congelado del guardado sano es > 0",
          estado_sano == "calculado" and costo_sano > 0, (estado_sano, costo_sano))

    _gasto_negativo_en_bd(emb_neg, "otros", -500_000)
    r = cli.put(_url(emb_neg), json={"observaciones": f"{MARK} solo encabezado"})
    check("A.2 PUT SOLO-ENCABEZADO con un gasto negativo en BD → 400 "
          "(antes: 200, y el negativo restaba del costo de todos los ítems)",
          r.status_code == 400, (r.status_code, r.text[:220]))
    check("A.2 el 400 NOMBRA la línea culpable",
          r.status_code == 400 and "Otros" in r.text, r.text[:250])
    r = cli.post(f"{_url(emb_neg)}/cerrar")
    check("A.2 POST /cerrar con un gasto negativo → 400 "
          "(antes: 200 y el costo NEGATIVO quedaba CONGELADO)",
          r.status_code == 400, (r.status_code, r.text[:220]))
    estado2, costo2 = _estado_y_costo(emb_neg)
    check("A.2 y el snapshot NO se pisó: sigue el costo sano y sin cerrar",
          estado2 != "cerrado" and approx(costo2, costo_sano, tol=2.0),
          (estado2, costo2, costo_sano))
    r = cli.put(_url(emb_neg), json={"tc_valor": 962, "flete_en_me": False,
                                     "shipping_clp": 40_000,
                                     "gastos": [{"tipo": "agencia", "monto_neto": 340_000,
                                                 "iva": 64_600},
                                                {"tipo": "otros", "monto_neto": 0}]})
    check("A.2 corregir la línea con un PUT normal la desbloquea (200)",
          r.status_code == 200, r.text[:200])
    r = cli.post(f"{_url(emb_neg)}/cerrar")
    check("A.2 y ahora sí cierra (el guard no rompe la operación legítima)",
          r.status_code == 200, r.text[:200])

    # ══ B · MONEDA DEL COSTEO ≠ MONEDA DEL ÍTEM (el caso que SÍ pasa) ═════════
    emb_d = ids["deriva"]
    r = cli.get(_url(emb_d))
    p = r.json().get("pricing", {})
    check("B el pricing nace en USD con el TC USD de MonzaConfig",
          p.get("moneda") == "USD" and approx(p.get("tc_valor", 0), tc_usd),
          (p.get("moneda"), p.get("tc_valor"), tc_usd))
    check("B y sin advertencias mientras el ítem siga en USD",
          (r.json().get("advertencias") or []) == [], r.json().get("advertencias"))
    _item_moneda(ids["parte_deriva"], "EUR")   # ← la cotización se edita DESPUÉS
    r = cli.get(_url(emb_d))
    advs = r.json().get("advertencias") or []
    check("B con UN SOLO ítem corregido a EUR y el pricing todavía en USD → AVISA "
          "(antes: lista vacía, porque solo miraba embarques con 2+ monedas)",
          any("están en EUR" in a for a in advs), advs)
    check("B el aviso dice en qué moneda se está calculando y en cuál está el ítem",
          any("USD" in a and "EUR" in a for a in advs), advs)
    r = cli.put(_url(emb_d), json={"tc_valor": 940, "flete_en_me": False,
                                   "shipping_clp": 10_000})
    check("B el aviso NO bloquea el guardado (la mercadería ya llegó)",
          r.status_code == 200, r.text[:200])
    check("B y sigue visible después de guardar",
          any("están en EUR" in a for a in (r.json().get("advertencias") or [])),
          r.json().get("advertencias"))
    # …con los DOS ítems en EUR y el embarque en EUR, ya no hay divergencia.
    _item_moneda(f"{MARK}-DER-2", "EUR")
    r = cli.put(_url(emb_d), json={"moneda": "EUR", "tc_valor": tc_eur or 1100})
    check("B alineando la moneda del embarque con la de los ítems, el aviso DESAPARECE "
          "(no es un aviso que grite siempre)",
          r.status_code == 200
          and not any("están en" in a for a in (r.json().get("advertencias") or [])),
          (r.status_code, r.json().get("advertencias")))

    # ══ C · ÍTEM EN PESOS: sin TC parametrizado, no se inventa uno ════════════
    emb_clp = ids["clp"]
    r = cli.get(_url(emb_clp))
    p = r.json().get("pricing", {})
    advs = r.json().get("advertencias") or []
    check("C el embarque del ítem en pesos NO recibe el TC del dólar: nace en 0 y 'manual' "
          "(antes: tc_valor 950 con etiqueta 'config')",
          approx(p.get("tc_valor", -1), 0) and p.get("tc_tipo") == "manual",
          (p.get("moneda"), p.get("tc_valor"), p.get("tc_tipo")))
    check("C tampoco se le SUGIERE el TC del dólar (tc_config = 0)",
          approx(p.get("tc_config", -1), 0), p.get("tc_config"))
    check("C y avisa que el módulo solo sabe convertir USD y EUR",
          any("solo sabe convertir" in a for a in advs), advs)
    r = cli.post(f"{_url(emb_clp)}/cerrar")
    check("C con TC 0 el cierre se niega (400): nada se congela a ciegas",
          r.status_code == 400, (r.status_code, r.text[:180]))

    # ══ D · TC DEL DÓLAR EN UN EMBARQUE EUR ══════════════════════════════════
    emb_eur = ids["eur"]
    r = cli.get(_url(emb_eur))
    p = r.json().get("pricing", {})
    check("D el embarque EUR nace con el TC EUR de MonzaConfig y tc_tipo='config'",
          p.get("moneda") == "EUR" and approx(p.get("tc_valor", 0), tc_eur)
          and p.get("tc_tipo") == "config" and tc_eur > 0,
          (p.get("tc_valor"), p.get("tc_tipo"), tc_eur))
    r = cli.put(_url(emb_eur), json={"tc_valor": tc_usd})
    advs = r.json().get("advertencias") or [] if r.status_code == 200 else []
    hay_aviso_tc = any("es exactamente el del DÓLAR" in a for a in advs)
    check("D si en un embarque EUR se carga el TC del DÓLAR → AVISA "
          f"(TC USD={tc_usd} vs TC EUR={tc_eur})",
          hay_aviso_tc if abs(tc_usd - tc_eur) >= 0.01 else True, advs)
    r = cli.put(_url(emb_eur), json={"tc_valor": tc_eur})
    check("D y con el TC correcto el aviso desaparece",
          r.status_code == 200
          and not any("del DÓLAR" in a for a in (r.json().get("advertencias") or [])),
          r.json().get("advertencias"))

    # ══ E · tc_de_config falla CERRADO (función real, no introspección) ═══════
    fake = SimpleNamespace(tc_usd_clp=950, tc_eur_clp=1100)
    check("E tc_de_config('USD') = el TC USD", approx(tc_de_config(fake, "USD"), 950))
    check("E tc_de_config('EUR') = el TC EUR (nunca el del dólar)",
          approx(tc_de_config(fake, "EUR"), 1100))
    for mala in ("CLP", "EURO", "", "GBP", None):
        val = tc_de_config(fake, mala)
        check(f"E tc_de_config({mala!r}) NO devuelve el TC del dólar por descarte (dio {val})",
              approx(val, 0), val)
    check("E el módulo declara EXACTAMENTE las monedas que sabe convertir",
          set(MONEDAS_CON_TC) == {"USD", "EUR"}, MONEDAS_CON_TC)

    # ══ F · los 3 gastos por defecto AHORA se pueden cargar (EP-10 estaba INERTE) ══
    r = cli.get("/api/monza/embarques-pricing/config/parametros")
    check("F GET /config/parametros devuelve los parámetros de costeo",
          r.status_code == 200 and all(k in r.json() for k in ("tc_usd_clp", *PARAMS_CFG)),
          (r.status_code, r.text[:200]))
    r = cli.put("/api/monza/embarques-pricing/config/parametros",
                json={"desconsolidado_clp": -1})
    check("F un gasto por defecto negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put("/api/monza/embarques-pricing/config/parametros", json={"tc_eur_clp": -1})
    check("F un TC EUR negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put("/api/monza/embarques-pricing/config/parametros", json={})
    check("F un PUT vacío → 400 (no se 'guarda' nada en silencio)",
          r.status_code == 400, r.status_code)

    r = cli.put("/api/monza/embarques-pricing/config/parametros",
                json={"desconsolidado_clp": 111_000, "bodegaje_clp": 22_000,
                      "costo_agencia_minimo_clp": 33_000, "tc_eur_clp": 1_234})
    check("F los 3 gastos de internación por defecto SE PUEDEN CARGAR "
          "(antes NINGÚN endpoint los escribía: se quedaban en 0 para siempre)",
          r.status_code == 200 and approx(r.json().get("desconsolidado_clp", 0), 111_000)
          and approx(r.json().get("bodegaje_clp", 0), 22_000)
          and approx(r.json().get("costo_agencia_minimo_clp", 0), 33_000),
          (r.status_code, r.text[:220]))
    guardado = _cfg_leer()
    check("F y quedan escritos en la BD, no solo en la respuesta",
          approx(guardado.get("desconsolidado_clp", 0), 111_000)
          and approx(guardado.get("tc_eur_clp", 0), 1_234), guardado)

    # …y GOBIERNAN de verdad: un embarque NUEVO nace con esos montos y ese TC.
    db = SessionLocal()
    try:
        cot = db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.numero == f"{MARK}-COT").first()
        emb_eur2, _ = _embarque(db, "EU2", cot, "EUR", "BAUKAT")
        db.commit()
    finally:
        db.close()
    r = cli.get(_url(emb_eur2))
    p = r.json().get("pricing", {})
    gastos = {g["tipo"]: g for g in (r.json().get("gastos") or [])}
    check("F un embarque EUR NUEVO nace con el TC recién editado",
          approx(p.get("tc_valor", 0), 1_234) and approx(p.get("tc_config", 0), 1_234),
          (p.get("tc_valor"), p.get("tc_config")))
    check("F y sus 3 líneas afectas nacen con los montos recién editados "
          "(EP-10 deja de ser inerte: el costo landed ya no se congela sin internación)",
          approx(gastos.get("desconsolidacion", {}).get("monto_neto", 0), 111_000)
          and approx(gastos.get("almacenaje", {}).get("monto_neto", 0), 22_000)
          and approx(gastos.get("agencia", {}).get("monto_neto", 0), 33_000),
          {k: gastos.get(k, {}).get("monto_neto")
           for k in ("desconsolidacion", "almacenaje", "agencia")})
    check("F y llevan su IVA calculado (no nacen afectas sin impuesto)",
          gastos.get("desconsolidacion", {}).get("iva", 0) > 0,
          gastos.get("desconsolidacion"))

    for campo in PARAMS_CFG:
        _cfg_escribir(campo, _cfg_foto.get(campo, 0))
    check("F MonzaConfig quedó restaurada",
          all(approx(_cfg_leer().get(k, -1), v, tol=0.0001) for k, v in _cfg_foto.items()),
          (_cfg_leer(), _cfg_foto))


def cleanup():
    for campo in PARAMS_CFG:
        if campo in _cfg_foto:
            try:
                _cfg_escribir(campo, _cfg_foto[campo])
            except Exception as e:                           # noqa: BLE001
                print(f"⚠️  no se pudo restaurar {campo}: {e}")
    db = SessionLocal()
    try:
        db.rollback()
        _purge(db)
    except Exception as e:                                   # noqa: BLE001
        db.rollback()
        print(f"⚠️  cleanup falló: {e}")
    finally:
        db.close()


def test_costeo_fail_closed_monza():
    """Wrapper para pytest: llama a run() DIRECTAMENTE (el candado
    tests_infra/test_suites_visibles.py exige la llamada literal)."""
    ids = seed()
    try:
        run(ids)
    finally:
        cleanup()
    resto = _residuos()
    print(f"[cleanup] residuos (filas marcadas + Config sin restaurar): {resto}")
    assert not _fails and resto == 0, f"fallas={_fails} residuos={resto}"


if __name__ == "__main__":
    _ids = seed()
    try:
        run(_ids)
    finally:
        cleanup()
    _resto = _residuos()
    print(f"[cleanup] residuos: {_resto}")
    print()
    if _fails or _resto:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails} · residuos={_resto}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
