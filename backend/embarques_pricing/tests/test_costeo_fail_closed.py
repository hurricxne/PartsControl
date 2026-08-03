"""Costeo de embarques que falla CERRADO: negativos, moneda divergente y TC sin parametrizar.

LOS 4 AGUJEROS QUE ESTA SUITE CIERRA (todos son error de COSTO silencioso y congelado)
--------------------------------------------------------------------------------------
A) NEGATIVOS POR LA SEGUNDA PUERTA. El `ge=0` del PUT cubre el payload, pero los montos de
   Desconsolidación / Almacenaje / Agencia entran TAMBIÉN por `integration.seed_gastos`,
   copiados de `ConfiguracionCotizador`, y ese `ConfigUpdate` (routers/cotizador.py) no tiene
   un solo `ge=0`. `total_gastos_que_capitalizan` SUMA los netos, así que un negativo RESTA
   del pozo que se prorratea a TODOS los ítems (340.000 → −30.000 con un −500.000); un PUT
   solo-encabezado no pasa por el schema y `cerrar` solo exigía `costo_total > 0`, así que el
   costo negativo quedaba CONGELADO. Ahora: piso 0 al sembrar + `_validar_gastos_no_negativos`
   en el PUT y en el cierre (fail closed en el punto donde el costo se calcula y se persiste).

B) EL AVISO DE MONEDA MIRABA AL CASO EQUIVOCADO. Solo avisaba si un embarque consolidaba OC
   en monedas distintas ENTRE ELLAS — el caso que el dueño dice que no pasa — y era CIEGO al
   que sí pasa: `emb_pricing.moneda` se siembra UNA vez y nunca se re-sincroniza, mientras
   `OcProveedorCreate.moneda` nace 'USD' y el PATCH de la OC la deja editar. Secuencia normal:
   OC nace USD → se abre el pricing (USD/940) → se corrige la OC a EUR → el costo sigue
   calculándose a 940 en vez de ~1100 (−14,5 % de FOB) sin un solo aviso, con UNA sola OC.

C) OC EN PESOS. En la BD real hay una OC en 'CLP': el FOB en pesos se multiplicaba por 940.

D) `tc_de_config` prometía "fail closed" y hacía lo contrario: cualquier moneda que no fuera
   EXACTAMENTE 'EUR' recibía el TC del DÓLAR ('CLP' → 940, 'EURO' → 940, '' → 940, 'GBP' →
   940), y encima etiquetado `tc_tipo='config'`. Y el TC del EURO era un DEFAULT 1100 cableado
   en la migración que NADIE podía cambiar: no estaba en `_config_to_dict` ni en `ConfigUpdate`
   ni en ninguna pantalla, así que en cuanto el euro se movía la "sugerencia" era un número
   viejo. Ahora hay endpoint propio (`/config/parametros`) con `ge=0`.

SONDAS: todo por HTTP contra el router REAL, o llamando a la función REAL. Cero introspección
de código (ni `inspect.getsource`, ni contar strings, ni buscar nombres de función).

Datos MARCADOS + limpieza en `finally`. Los 4 parámetros de `configuracion_cotizador` se
FOTOGRAFÍAN al empezar y se RESTAURAN al terminar (es la fila real del dueño), y la
restauración se VERIFICA con conexión nueva. No emite ni toca ningún documento tributario.

Corre con:  cd backend && ./venv/bin/python -m pytest embarques_pricing/tests/test_costeo_fail_closed.py -q
(también:   ./venv/bin/python embarques_pricing/tests/test_costeo_fail_closed.py)
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
from models.models import (  # noqa: E402
    User, Cotizacion, ItemCotizacion, OcProveedor, OcProveedorItem, Embarque, EmbarqueItem,
    ConfiguracionCotizador,
)
from embarques_pricing.models import (  # noqa: E402
    EmbarquePricing, EmbarquePricingGasto, EmbarquePricingItem,
)
from embarques_pricing.router import router  # noqa: E402
from embarques_pricing.integration import tc_de_config, MONEDAS_CON_TC  # noqa: E402

MARK = "__TEST_EP_FAILCLOSED__"
USER_EMAIL = f"{MARK}@test.local"
PARAMS_CFG = ("tipo_cambio_eur", "desconsolidado_clp", "bodegaje_clp",
              "costo_agencia_minimo_clp")


def _cu(db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == USER_EMAIL).first()
    if u is None:
        return SimpleNamespace(id=None, empresa="mineria")
    return u


app = FastAPI()
app.include_router(router, prefix="/api")
app.dependency_overrides[get_current_user] = _cu
cli = TestClient(app)

_fails: list = []
_cfg_foto: dict = {}


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def approx(a, b, tol=1.0):
    return abs(float(a) - float(b)) <= tol


def _url(emb_id: int) -> str:
    return f"/api/embarques-pricing/{emb_id}"


# ─── Config: foto / escritura directa / restauración ──────────────────────────
def _cfg_leer() -> dict:
    """Los 4 parámetros, con conexión NUEVA (nada de identity map)."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT tipo_cambio_usd, tipo_cambio_eur, desconsolidado_clp, bodegaje_clp, "
            "costo_agencia_minimo_clp FROM configuracion_cotizador WHERE id = 1")).fetchone()
    if row is None:
        return {}
    return {
        "tipo_cambio_usd": float(row[0] or 0), "tipo_cambio_eur": float(row[1] or 0),
        "desconsolidado_clp": float(row[2] or 0), "bodegaje_clp": float(row[3] or 0),
        "costo_agencia_minimo_clp": float(row[4] or 0),
    }


def _cfg_escribir(campo: str, valor: float) -> None:
    """Escribe DIRECTO en la BD: así es como entra el dato malo en producción (el editor del
    cotizador no tiene `ge=0`, y no es código de este módulo)."""
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE configuracion_cotizador SET {campo} = :v WHERE id = 1"),
                     {"v": valor})


def _oc_moneda(numero: str, moneda: str) -> None:
    """Cambia la moneda de la OC DESPUÉS de que el pricing nació — el PATCH de la OC que ya
    existe en producción hace exactamente esto."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE oc_proveedor SET moneda = :m WHERE numero = :n"),
                     {"m": moneda, "n": numero})


def _gasto_negativo_en_bd(emb_id: int, tipo: str, neto: float) -> None:
    """Deja un gasto NEGATIVO ya guardado (fila legada, o sembrada antes del piso)."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE emb_pricing_gasto g JOIN emb_pricing p ON p.id = g.pricing_id "
            "SET g.monto_neto = :v WHERE p.embarque_id = :e AND g.tipo = :t"),
            {"v": neto, "e": emb_id, "t": tipo})


def _estado_y_costo(emb_id: int):
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT MAX(p.estado), COALESCE(SUM(i.costo_total_clp), 0) FROM emb_pricing p "
            "LEFT JOIN emb_pricing_item i ON i.pricing_id = p.id WHERE p.embarque_id = :e"),
            {"e": emb_id}).fetchone()
    return (row[0] if row else None), float(row[1] or 0) if row else 0.0


# ─── Seed / limpieza ──────────────────────────────────────────────────────────
def _purge(db: Session) -> None:
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


def _embarque(db, sufijo: str, cot, moneda: str, forwarder: str) -> tuple:
    """1 OC + 1 embarque con 2 ítems. Devuelve (embarque_id, numero_oc)."""
    ocp = OcProveedor(numero=f"{MARK}-OCP-{sufijo}", proveedor=f"{MARK} PROV", moneda=moneda)
    db.add(ocp)
    db.flush()
    emb = Embarque(numero=f"{MARK}-EMB-{sufijo}", estado="en_bodega", forwarder=forwarder)
    db.add(emb)
    db.flush()
    for parte, peso in ((f"{sufijo}-1", 2.0), (f"{sufijo}-2", 5.0)):
        it = ItemCotizacion(cotizacion_id=cot.id, numero_parte=parte,
                            descripcion=f"DESC {parte}", cantidad=1, peso_unit_lbs=peso,
                            precio_unit_cotizacion=100)
        db.add(it)
        db.flush()
        db.add(OcProveedorItem(oc_proveedor_id=ocp.id, item_cotizacion_id=it.id))
        db.add(EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=it.id,
                            oc_proveedor_id=ocp.id))
    db.flush()
    return emb.id, ocp.numero


def seed():
    _cfg_foto.update(_cfg_leer())
    db = SessionLocal()
    try:
        _purge(db)
        db.add(User(email=USER_EMAIL, nombre=MARK, hashed_password="x", empresa="mineria"))
        cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} Cliente")
        db.add(cot)
        db.flush()
        ids = {}
        # A: negativos (se costea normal, en USD)
        ids["neg"], _ = _embarque(db, "NEG", cot, "USD", "LATAM Cargo")
        # B: la OC arranca en USD y DESPUÉS se corrige a EUR (el caso que sí pasa)
        ids["deriva"], ids["oc_deriva"] = _embarque(db, "DERIVA", cot, "USD", "LATAM Cargo")
        # C: OC en pesos
        ids["clp"], _ = _embarque(db, "CLP", cot, "CLP", "LATAM Cargo")
        # D: embarque EUR para el TC del dólar mal puesto
        ids["eur"], _ = _embarque(db, "EUR", cot, "EUR", "BAUKAT")
        db.commit()
        print(f"[seed] {ids}  ·  cfg foto: {_cfg_foto}")
        return ids
    finally:
        db.close()


def _residuos():
    with engine.connect() as conn:
        n = 0
        for sql, par in (
            ("SELECT COUNT(*) FROM embarques WHERE numero LIKE :m", {"m": f"{MARK}%"}),
            ("SELECT COUNT(*) FROM oc_proveedor WHERE numero LIKE :m", {"m": f"{MARK}%"}),
            ("SELECT COUNT(*) FROM cotizaciones WHERE numero LIKE :m", {"m": f"{MARK}%"}),
            ("SELECT COUNT(*) FROM users WHERE email = :e", {"e": USER_EMAIL}),
        ):
            n += int(conn.execute(text(sql), par).scalar() or 0)
    # …y la Config del dueño tiene que haber vuelto EXACTA
    ahora = _cfg_leer()
    for k, v in _cfg_foto.items():
        if not approx(ahora.get(k, -1), v, tol=0.0001):
            print(f"⚠️  Config NO restaurada: {k} foto={v} ahora={ahora.get(k)}")
            n += 1
    return n


# ─── Checks ───────────────────────────────────────────────────────────────────
def run(ids):
    tc_usd = _cfg_foto.get("tipo_cambio_usd", 0)
    tc_eur = _cfg_foto.get("tipo_cambio_eur", 0)

    # ══ A · NEGATIVOS ═════════════════════════════════════════════════════════
    # A.1 · el parámetro negativo de Config NO puede llegar al gasto sembrado.
    _cfg_escribir("desconsolidado_clp", -500_000)
    db = SessionLocal()
    try:
        cot = db.query(Cotizacion).filter(Cotizacion.numero == f"{MARK}-COT").first()
        emb_seed_id, _ = _embarque(db, "SEED", cot, "USD", "LATAM Cargo")
        db.commit()
    finally:
        db.close()
    r = cli.get(_url(emb_seed_id))          # este GET es el que SIEMBRA las 6 líneas
    gastos = {g["tipo"]: g for g in (r.json().get("gastos") or [])} if r.status_code == 200 else {}
    check("A.1 con el parámetro de Config en −500.000, la línea sembrada nace en 0 "
          "(NO se copia el negativo al costeo)",
          r.status_code == 200 and approx(gastos.get("desconsolidacion", {}).get("monto_neto", -1), 0),
          gastos.get("desconsolidacion"))
    check("A.1 y el total que capitaliza NO es negativo",
          (r.json().get("totales_gastos", {}).get("total_capitaliza", -1)) >= 0,
          r.json().get("totales_gastos"))
    _cfg_escribir("desconsolidado_clp", _cfg_foto.get("desconsolidado_clp", 0))

    # A.2 · una fila que YA quedó negativa en la BD no puede calcularse ni congelarse.
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
    check("A.2 el 400 NOMBRA la línea culpable (el contador sabe qué corregir)",
          r.status_code == 400 and "Otros" in r.text, r.text[:250])
    r = cli.post(f"{_url(emb_neg)}/cerrar")
    check("A.2 POST /cerrar con un gasto negativo → 400 "
          "(antes: 200 y el costo NEGATIVO quedaba CONGELADO)",
          r.status_code == 400, (r.status_code, r.text[:220]))
    estado2, costo2 = _estado_y_costo(emb_neg)
    check("A.2 y el snapshot NO se pisó: sigue el costo sano y sin cerrar",
          estado2 != "cerrado" and approx(costo2, costo_sano, tol=2.0),
          (estado2, costo2, costo_sano))
    # …y el camino de salida existe: un PUT con los gastos corregidos arregla la fila.
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

    # ══ B · MONEDA DEL COSTEO ≠ MONEDA DE LA ORDEN (el caso que SÍ pasa) ══════
    emb_d = ids["deriva"]
    r = cli.get(_url(emb_d))
    p = r.json().get("pricing", {})
    check("B el pricing nace en USD con el TC USD de Config",
          p.get("moneda") == "USD" and approx(p.get("tc_valor", 0), tc_usd),
          (p.get("moneda"), p.get("tc_valor"), tc_usd))
    check("B y sin advertencias mientras la OC siga en USD",
          (r.json().get("advertencias") or []) == [], r.json().get("advertencias"))
    _oc_moneda(ids["oc_deriva"], "EUR")     # ← el PATCH de la OC, DESPUÉS de abrir el pricing
    r = cli.get(_url(emb_d))
    advs = r.json().get("advertencias") or []
    check("B con UNA SOLA OC corregida a EUR y el pricing todavía en USD → AVISA "
          "(antes: lista vacía, porque solo miraba embarques con 2+ OC)",
          any("están en EUR" in a for a in advs), advs)
    check("B el aviso dice en qué moneda se está calculando y en cuál está la orden",
          any("USD" in a and "EUR" in a for a in advs), advs)
    r = cli.put(_url(emb_d), json={"tc_valor": 940, "flete_en_me": False,
                                   "shipping_clp": 10_000})
    check("B el aviso NO bloquea el guardado (la mercadería ya llegó)",
          r.status_code == 200, r.text[:200])
    check("B y sigue visible después de guardar",
          any("están en EUR" in a for a in (r.json().get("advertencias") or [])),
          r.json().get("advertencias"))
    # …y desaparece cuando el contador alinea la moneda del embarque con la de la orden.
    r = cli.put(_url(emb_d), json={"moneda": "EUR", "tc_valor": tc_eur or 1100})
    check("B alineando la moneda del embarque con la de la OC, el aviso DESAPARECE "
          "(no es un aviso que grite siempre)",
          r.status_code == 200
          and not any("están en" in a for a in (r.json().get("advertencias") or [])),
          (r.status_code, r.json().get("advertencias")))

    # ══ C · OC EN PESOS: sin TC parametrizado, no se inventa uno ══════════════
    emb_clp = ids["clp"]
    r = cli.get(_url(emb_clp))
    p = r.json().get("pricing", {})
    advs = r.json().get("advertencias") or []
    check("C el embarque de la OC en pesos NO recibe el TC del dólar: nace en 0 y 'manual' "
          "(antes: tc_valor 940 con etiqueta 'config')",
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
    check("D el embarque EUR nace con el TC EUR de Config y tc_tipo='config'",
          p.get("moneda") == "EUR" and approx(p.get("tc_valor", 0), tc_eur)
          and p.get("tc_tipo") == "config" and tc_eur > 0,
          (p.get("tc_valor"), p.get("tc_tipo"), tc_eur))
    r = cli.put(_url(emb_eur), json={"tc_valor": tc_usd})   # el operador deja el TC del dólar
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
    fake = SimpleNamespace(tipo_cambio_usd=940, tipo_cambio_eur=1100)
    check("E tc_de_config('USD') = el TC USD", approx(tc_de_config(fake, "USD"), 940))
    check("E tc_de_config('EUR') = el TC EUR (nunca el del dólar)",
          approx(tc_de_config(fake, "EUR"), 1100))
    for mala in ("CLP", "EURO", "", "GBP", "usd ", None):
        val = tc_de_config(fake, mala)
        esperado_ok = approx(val, 940) if (mala or "").strip().upper() == "USD" else approx(val, 0)
        check(f"E tc_de_config({mala!r}) NO devuelve el TC del dólar por descarte "
              f"(dio {val})", esperado_ok, val)
    check("E el módulo declara EXACTAMENTE las monedas que sabe convertir",
          set(MONEDAS_CON_TC) == {"USD", "EUR"}, MONEDAS_CON_TC)

    # ══ F · los parámetros de costeo son EDITABLES (el TC EUR no tenía dueño) ═
    r = cli.get("/api/embarques-pricing/config/parametros")
    check("F GET /config/parametros devuelve los 4 parámetros de costeo",
          r.status_code == 200 and all(k in r.json() for k in
                                       ("tipo_cambio_usd", *PARAMS_CFG)),
          (r.status_code, r.text[:200]))
    r = cli.put("/api/embarques-pricing/config/parametros", json={"tipo_cambio_eur": -1})
    check("F un TC EUR negativo → 422", r.status_code == 422, r.status_code)
    r = cli.put("/api/embarques-pricing/config/parametros", json={"desconsolidado_clp": -1})
    check("F un gasto por defecto negativo → 422 (la puerta del cotizador no lo valida)",
          r.status_code == 422, r.status_code)
    r = cli.put("/api/embarques-pricing/config/parametros", json={})
    check("F un PUT vacío → 400 (no se 'guarda' nada en silencio)",
          r.status_code == 400, r.status_code)

    nuevo_eur = 1_234.0
    r = cli.put("/api/embarques-pricing/config/parametros",
                json={"tipo_cambio_eur": nuevo_eur, "desconsolidado_clp": 111_000})
    check("F el TC del EURO se puede ACTUALIZAR (antes era un DEFAULT 1100 cableado en la "
          "migración, sin un solo camino de escritura en toda la app)",
          r.status_code == 200 and approx(r.json().get("tipo_cambio_eur", 0), nuevo_eur),
          (r.status_code, r.text[:200]))
    check("F y queda escrito en la BD, no solo en la respuesta",
          approx(_cfg_leer().get("tipo_cambio_eur", 0), nuevo_eur), _cfg_leer())
    # …y el valor nuevo GOBIERNA de verdad: un embarque EUR nuevo nace con él.
    db = SessionLocal()
    try:
        cot = db.query(Cotizacion).filter(Cotizacion.numero == f"{MARK}-COT").first()
        emb_eur2, _ = _embarque(db, "EUR2", cot, "EUR", "BAUKAT")
        db.commit()
    finally:
        db.close()
    r = cli.get(_url(emb_eur2))
    p = r.json().get("pricing", {})
    check("F un embarque EUR NUEVO nace con el TC recién editado (el parámetro gobierna, "
          "no es un adorno de pantalla)",
          approx(p.get("tc_valor", 0), nuevo_eur) and approx(p.get("tc_config", 0), nuevo_eur),
          (p.get("tc_valor"), p.get("tc_config"), nuevo_eur))
    gastos = {g["tipo"]: g for g in (r.json().get("gastos") or [])}
    check("F y sus gastos locales nacen con el monto recién editado (111.000)",
          approx(gastos.get("desconsolidacion", {}).get("monto_neto", 0), 111_000),
          gastos.get("desconsolidacion"))
    # Restaurar la Config del dueño (el `finally` la revalida).
    for campo in PARAMS_CFG:
        _cfg_escribir(campo, _cfg_foto.get(campo, 0))
    check("F la Config del dueño quedó restaurada",
          all(approx(_cfg_leer().get(k, -1), v, tol=0.0001) for k, v in _cfg_foto.items()),
          (_cfg_leer(), _cfg_foto))


def cleanup():
    # La Config se restaura SIEMPRE, incluso si run() se cortó a la mitad.
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


def test_costeo_fail_closed_ga():
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
