"""Agrupación por proveedor en Logística y Seguimiento Monza — contrato 2026-08-17.

Los DOS endpoints (GET /api/monza/logistica/preparados y
GET /api/monza/abastecimiento/seguimiento) son ADITIVOS: ninguna clave existente
cambia ni desaparece (tienen consumidores vivos) y cada ítem GANA las claves nuevas
(costo/moneda/peso_kg/peso_total_kg/fob_total + el objeto `ocp` normalizado con
semáforo en días HÁBILES y completitud de la OC). La agrupación la hace el frontend
sobre la lista plana.

Lo que esta suite certifica, en orden de importancia:

  1. SHAPE EXACTO del contrato (§1-2): claves nuevas con la forma exigida (objeto
     `ocp`, `semaforo`, `completitud` — igualdad de CONJUNTO de claves, no subset) y
     las claves VIEJAS byte-iguales contra un snapshot literal de HEAD (incluida la
     AUSENCIA de las ocp_* planas cuando la línea no tiene OC).
  2. SEMÁFORO en días HÁBILES Chile (§3-4): la MISMA regla de la alerta de las 06:00
     (scheduler._monza_ocp_plazo_vencido) — ancla created_at, add_business_days,
     vencido solo si hoy PASÓ la fecha comprometida. Casos verde/amarillo/rojo/
     sin_plazo/sin_fecha con fechas CONTROLADAS (created_at sembrado a mano por SQL) y
     sondas unitarias con `hoy` fijo que distinguen HÁBILES de CORRIDOS (viernes+finde
     y feriado 18-09): bajo la mutación a corridos estas sondas CAEN (§9.1 lo
     demuestra inyectando la mutación y exigiendo el rojo falso).
  3. NADA DE N+1 (§8): sonda de conteo de queries con event listener — más OCs y más
     ítems NO agregan queries (la carga es UNA query IN + UNA GROUP BY, molde
     scheduler.py:537-541). §9.2 inyecta el caché-por-OC en loop y exige que la misma
     sonda vea crecer las queries.
  4. LA PLATA Y EL PESO (§6): moneda del ÍTEM (puede diferir de la OC), costo NULL →
     fob_total null (JAMÁS 0 mudo), peso 0/null se preserva, totales calculados en el
     BACKEND. Ítem sin OC → ocp: null (§7, grupo defensivo del frontend).

ARNÉS (molde de la casa, ver test_asignacion_parcial_compra.py):
· auth realista: el override lee la BD en la MISMA sesión del request.
· verificaciones con CONEXIÓN NUEVA y SQL crudo; limpieza verificada al final.
· created_at controlado por UPDATE crudo (el default utcnow del ORM pisaría la fecha).

Corre con:  ./venv/bin/python -m pytest monza_tests/test_logistica_seguimiento_agrupado.py -q
       o:   ./venv/bin/python monza_tests/test_logistica_seguimiento_agrupado.py
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, func, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, Base, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
)
from monza_fechas import _is_chile_holiday, add_business_days  # noqa: E402

import monza_router_abastecimiento as abast  # noqa: E402
from monza_router_abastecimiento import router as abastecimiento_router  # noqa: E402
from monza_router_logistica import router as logistica_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "T-AGRUP-QX"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(abastecimiento_router)
app.include_router(logistica_router)


def _cu(db: Session = Depends(get_db)):
    # Auth realista (lección G13): lectura en la MISMA sesión del request.
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=None, email=EMAIL, empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


# ══════════════ Lecturas de VERIFICACIÓN: conexión nueva, SQL crudo ══════════════

def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _exec(q, **p):
    with engine.begin() as c:
        c.execute(text(q), p)


# ═══════════ Calendario del test: mismo origen de feriados, walker propio ═════════
# Reusa _is_chile_holiday (fuente ÚNICA de feriados) pero el conteo es un walker
# PROPIO del test: si el backend muta a días corridos, este walker NO muta con él
# (verificado por la sonda §9.1 y por la mutación de archivo del protocolo).

def _habiles(desde: date, hasta: date) -> int:
    n, cur = 0, desde
    while cur < hasta:
        cur += timedelta(days=1)
        if cur.weekday() < 5 and not _is_chile_holiday(cur):
            n += 1
    return n


def _fecha_hace_habiles(n: int, tope=90) -> date:
    """Una fecha D del pasado con exactamente n días hábiles hasta hoy."""
    hoy = date.today()
    for k in range(1, tope):
        d = hoy - timedelta(days=k)
        if _habiles(d, hoy) == n:
            return d
    raise AssertionError(f"no encontré fecha a {n} hábiles en {tope} días")


# ═══════════════════════════════ Siembra marcada ═════════════════════════════════

def _ocp(db, prov="BAUKAT", plazo=30, numero_oc=None, moneda="EUR",
         pais="Alemania", estado="emitida", awb=None, tracking=None):
    suf = uuid.uuid4().hex[:6].upper()
    o = MonzaOcProveedor(
        numero=f"OCP-AGR-{suf}", proveedor_nombre=f"{MARK} {prov}", pais=pais,
        moneda=moneda, plazo_dias=plazo, numero_oc=numero_oc, estado=estado,
        awb=awb, tracking=tracking,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _set_created(ocp_id, dt):
    """created_at CONTROLADO por SQL crudo (el default utcnow del ORM lo pisaría).
    dt=None deja NULL (el histórico legado)."""
    _exec("UPDATE monza_oc_proveedor SET created_at=:d WHERE id=:i", d=dt, i=ocp_id)


def _venta(db, lineas):
    """lineas: dicts con estado, y opcionales ocp/cantidad/costo/moneda/peso/parte."""
    suf = uuid.uuid4().hex[:6].upper()
    cli = MonzaCliente(nombre=MARK, rut="22.222.222-2")
    db.add(cli)
    db.flush()
    cot = MonzaCotizacion(numero=f"CT-AGR-{suf}", cliente_id=cli.id, estado="vendida",
                          total_neto=0, iva_monto=0, total_bruto=0,
                          pct_adelanto=0, adelanto_verificado=0)
    db.add(cot)
    db.flush()
    items = []
    for n, l in enumerate(lineas, start=1):
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion=f"{MARK} {l.get('desc', f'Parte {n}')}",
            numero_parte=l.get("parte", f"P-AGR-{n}"), marca="MONZA",
            procedencia="Alemania", calidad="original",
            cantidad=l.get("cantidad", 1), costo=l.get("costo", 99.5),
            moneda=l.get("moneda", "EUR"), peso_kg=l.get("peso", 2.75),
            precio_unitario_clp=1000.0,
            subtotal_clp=1000.0 * (l.get("cantidad", 1) or 0),
            plazo_entrega="15 dias", estado_linea=l["estado"],
            oc_proveedor_id=l.get("ocp"),
        )
        db.add(it)
        items.append(it)
    db.commit()
    for obj in [cot] + items:
        db.refresh(obj)
    return cot, items


def _seguimiento():
    r = client.get("/api/monza/abastecimiento/seguimiento", params={"q": MARK})
    assert r.status_code == 200, r.text[:300]
    return {d["id"]: d for d in r.json()}


def _preparados():
    r = client.get("/api/monza/logistica/preparados", params={"q": MARK})
    assert r.status_code == 200, r.text[:300]
    return {d["id"]: d for d in r.json()}


def _contar_selects(fn):
    """Sonda de N+1: cuenta los SELECT que dispara `fn` en el engine."""
    vistos = []

    def _cb(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            vistos.append(statement)

    event.listen(engine, "before_cursor_execute", _cb)
    try:
        r = fn()
    finally:
        event.remove(engine, "before_cursor_execute", _cb)
    return r, len(vistos)


# ══════════════════════════════ Limpieza marcada ═════════════════════════════════

def _limpiar():
    _exec("DELETE FROM monza_cotizacion_items WHERE descripcion LIKE :m", m=f"{MARK}%")
    _exec("DELETE FROM monza_cotizaciones WHERE cliente_id IN "
          "(SELECT id FROM monza_clientes WHERE nombre = :m)", m=MARK)
    _exec("DELETE FROM monza_clientes WHERE nombre = :m", m=MARK)
    _exec("DELETE FROM monza_oc_proveedor WHERE proveedor_nombre LIKE :m", m=f"{MARK}%")


def _residuos():
    return {
        "items": _sql("SELECT COUNT(*) FROM monza_cotizacion_items WHERE descripcion LIKE :m", m=f"{MARK}%")[0][0],
        "cotizaciones": _sql("SELECT COUNT(*) FROM monza_cotizaciones WHERE cliente_id IN "
                             "(SELECT id FROM monza_clientes WHERE nombre = :m)", m=MARK)[0][0],
        "clientes": _sql("SELECT COUNT(*) FROM monza_clientes WHERE nombre = :m", m=MARK)[0][0],
        "ocps": _sql("SELECT COUNT(*) FROM monza_oc_proveedor WHERE proveedor_nombre LIKE :m", m=f"{MARK}%")[0][0],
    }


# ═════════════════ El shape del contrato (igualdad de conjuntos) ═════════════════

OCP_KEYS = {"id", "numero", "numero_oc", "proveedor_nombre", "pais", "moneda",
            "tipo_origen", "plazo_dias", "awb", "tracking", "fecha_emision",
            "semaforo", "completitud"}
SEM_KEYS = {"dias_habiles", "plazo_dias", "estado", "dias_habiles_restantes"}
COMP_KEYS = {"total", "por_estado"}

# Snapshot de HEAD (antes de este cambio): las claves que cada endpoint YA emitía.
SEG_VIEJAS_BASE = {"id", "cotizacion_id", "cot_numero", "cliente", "vehiculo",
                   "descripcion", "numero_parte", "marca", "procedencia", "calidad",
                   "cantidad", "costo", "moneda", "precio_unitario_clp",
                   "subtotal_clp", "plazo_entrega", "estado_linea",
                   "oc_proveedor_id", "fecha_venta", "pct_adelanto",
                   "requiere_adelanto", "pago_verificado"}
SEG_VIEJAS_OCP = {"ocp_numero", "ocp_proveedor", "ocp_estado", "ocp_awb",
                  "ocp_tracking", "ocp_numero_oc", "ocp_plazo_dias", "tipo_origen"}
SEG_NUEVAS = {"peso_kg", "peso_total_kg", "fob_total", "ocp"}

PREP_VIEJAS = {"id", "cot_numero", "cliente", "descripcion", "numero_parte",
               "marca", "calidad", "cantidad", "estado_linea", "oc_proveedor_id"}
PREP_NUEVAS = {"costo", "moneda", "peso_kg", "peso_total_kg", "fob_total", "ocp"}


def _shape_ocp_ok(o):
    return (isinstance(o, dict) and set(o.keys()) == OCP_KEYS
            and set(o["semaforo"].keys()) == SEM_KEYS
            and set(o["completitud"].keys()) == COMP_KEYS)


def run():
    db = SessionLocal()
    _limpiar()
    try:
        hoy = date.today()

        # ══ §1 · Siembra base: 1 OC con líneas en 4 estados + 1 línea sin OC ══════
        oc1 = _ocp(db, prov="BAUKAT", plazo=30, numero_oc="4521")
        cot, (a, b, c, d_sin, e) = _venta(db, [
            {"estado": "comprado", "ocp": oc1.id, "cantidad": 1, "parte": "P-AGR-A"},
            # B: moneda del ÍTEM distinta de la OC (EUR) — panel #9e, OC mixta.
            {"estado": "preparado", "ocp": oc1.id, "cantidad": 2, "costo": 99.5,
             "moneda": "USD", "peso": 2.75, "parte": "P-AGR-B"},
            # C: costo NULL → fob_total null; peso NULL (UPDATE crudo abajo: la
            # columna tiene default=0 y el ORM lo aplicaría) → peso_total null.
            {"estado": "embarcado", "ocp": oc1.id, "cantidad": 3, "costo": None,
             "parte": "P-AGR-C"},
            # D: sin OC y peso 0 (0 se preserva, no se vuelve null ni desaparece).
            {"estado": "preparado", "cantidad": 4, "peso": 0.0, "parte": "P-AGR-D"},
            # E: 'despachado' NO sale en los listados, pero SÍ cuenta en la
            # completitud de la OC (el conteo es de TODA la OC).
            {"estado": "despachado", "ocp": oc1.id, "cantidad": 1, "parte": "P-AGR-E"},
        ])
        _exec("UPDATE monza_cotizacion_items SET peso_kg=NULL WHERE id=:i", i=c.id)
        oc1_created = _sql("SELECT created_at FROM monza_oc_proveedor WHERE id=:i", i=oc1.id)[0][0]

        seg = _seguimiento()
        prep = _preparados()

        check("1.1 seguimiento lista A, B, C y D (E despachado no)",
              set(seg.keys()) == {a.id, b.id, c.id, d_sin.id}, sorted(seg.keys()))
        check("1.2 preparados lista B y D",
              set(prep.keys()) == {b.id, d_sin.id}, sorted(prep.keys()))

        # ══ §2 · SHAPE estricto + claves viejas byte-iguales (snapshot de HEAD) ══
        sb, pb = seg[b.id], prep[b.id]
        check("2.1 seguimiento con OC: conjunto de claves EXACTO (viejas ∪ nuevas)",
              set(sb.keys()) == SEG_VIEJAS_BASE | SEG_VIEJAS_OCP | SEG_NUEVAS,
              sorted(set(sb.keys()) ^ (SEG_VIEJAS_BASE | SEG_VIEJAS_OCP | SEG_NUEVAS)))
        check("2.2 seguimiento sin OC: sin las ocp_* planas (ausencia preservada)",
              set(seg[d_sin.id].keys()) == SEG_VIEJAS_BASE | SEG_NUEVAS,
              sorted(set(seg[d_sin.id].keys()) ^ (SEG_VIEJAS_BASE | SEG_NUEVAS)))
        check("2.3 preparados: conjunto de claves EXACTO",
              set(pb.keys()) == PREP_VIEJAS | PREP_NUEVAS,
              sorted(set(pb.keys()) ^ (PREP_VIEJAS | PREP_NUEVAS)))
        check("2.4 objeto ocp con la forma EXACTA del contrato (ocp+semaforo+completitud)",
              _shape_ocp_ok(sb["ocp"]) and _shape_ocp_ok(pb["ocp"]),
              (sb.get("ocp"), pb.get("ocp")))

        exp_seg_b = {  # byte-igual a lo que HEAD emitía para esta línea
            "id": b.id, "cotizacion_id": cot.id, "cot_numero": cot.numero,
            "cliente": MARK, "vehiculo": None, "descripcion": f"{MARK} Parte 2",
            "numero_parte": "P-AGR-B", "marca": "MONZA", "procedencia": "Alemania",
            "calidad": "original", "cantidad": 2, "costo": 99.5, "moneda": "USD",
            "precio_unitario_clp": 1000.0, "subtotal_clp": 2000.0,
            "plazo_entrega": "15 dias", "estado_linea": "preparado",
            "oc_proveedor_id": oc1.id, "fecha_venta": None, "pct_adelanto": 0,
            "requiere_adelanto": False, "pago_verificado": False,
            "ocp_numero": oc1.numero, "ocp_proveedor": f"{MARK} BAUKAT",
            "ocp_estado": "emitida", "ocp_awb": None, "ocp_tracking": None,
            "ocp_numero_oc": "4521", "ocp_plazo_dias": 30,
            "tipo_origen": "internacional",
        }
        check("2.5 claves VIEJAS de seguimiento byte-iguales al snapshot",
              {k: sb[k] for k in exp_seg_b} == exp_seg_b,
              {k: (sb[k], v) for k, v in exp_seg_b.items() if sb[k] != v})

        exp_prep_b = {
            "id": b.id, "cot_numero": cot.numero, "cliente": MARK,
            "descripcion": f"{MARK} Parte 2", "numero_parte": "P-AGR-B",
            "marca": "MONZA", "calidad": "original", "cantidad": 2,
            "estado_linea": "preparado", "oc_proveedor_id": oc1.id,
        }
        check("2.6 claves VIEJAS de preparados byte-iguales al snapshot",
              {k: pb[k] for k in exp_prep_b} == exp_prep_b,
              {k: (pb[k], v) for k, v in exp_prep_b.items() if pb[k] != v})

        o = sb["ocp"]
        check("2.7 ocp normalizado: numero y numero_oc SEPARADOS + datos de la OC",
              o["id"] == oc1.id and o["numero"] == oc1.numero
              and o["numero_oc"] == "4521" and o["proveedor_nombre"] == f"{MARK} BAUKAT"
              and o["pais"] == "Alemania" and o["moneda"] == "EUR"
              and o["tipo_origen"] == "internacional" and o["plazo_dias"] == 30
              and o["awb"] is None and o["tracking"] is None, o)
        check("2.8 fecha_emision = created_at en ISO date",
              o["fecha_emision"] == oc1_created.date().isoformat(),
              (o["fecha_emision"], oc1_created))
        check("2.9 el MISMO objeto ocp en los dos endpoints (una sola forma)",
              pb["ocp"] == sb["ocp"])

        # ══ §5 · Completitud: TODA la OC en el conteo, por estado_linea exacto ════
        check("5.1 completitud exacta (incluye el 'despachado' que el listado no muestra)",
              o["completitud"] == {"total": 4, "por_estado":
                                   {"comprado": 1, "preparado": 1,
                                    "embarcado": 1, "despachado": 1}},
              o["completitud"])

        # ══ §6 · Plata y peso: backend calcula, null JAMÁS 0 mudo ════════════════
        check("6.1 moneda del ÍTEM (B es USD aunque la OC sea EUR) y fob_total = costo × cantidad",
              sb["moneda"] == "USD" and sb["costo"] == 99.5
              and sb["fob_total"] == 199.0 and sb["ocp"]["moneda"] == "EUR",
              (sb["moneda"], sb["costo"], sb["fob_total"]))
        check("6.2 peso_total_kg = peso × cantidad (backend)",
              sb["peso_kg"] == 2.75 and sb["peso_total_kg"] == 5.5,
              (sb["peso_kg"], sb["peso_total_kg"]))
        sc = seg[c.id]
        check("6.3 costo NULL → fob_total null (JAMÁS 0 mudo); peso NULL → peso_total null",
              sc["costo"] is None and sc["fob_total"] is None
              and sc["peso_kg"] is None and sc["peso_total_kg"] is None,
              {k: sc[k] for k in ("costo", "fob_total", "peso_kg", "peso_total_kg")})
        sd = prep[d_sin.id]
        check("6.4 peso 0 se PRESERVA (0.0, no null): 0 × 4 = 0",
              sd["peso_kg"] == 0.0 and sd["peso_total_kg"] == 0.0,
              (sd["peso_kg"], sd["peso_total_kg"]))

        # ══ §7 · Ítem sin OC → ocp null en los DOS endpoints ═════════════════════
        check("7.1 sin OC → ocp: null (seguimiento y preparados)",
              seg[d_sin.id]["ocp"] is None and prep[d_sin.id]["ocp"] is None)

        # ══ §3 · Semáforo por endpoint con created_at CONTROLADO ═════════════════
        casos = {}
        for nombre, plazo in [("verde", 30), ("amarillo", 5), ("rojo", 2),
                              ("sin_plazo", None), ("sin_fecha", 20), ("sonda7", 6)]:
            oc = _ocp(db, prov=f"SEM-{nombre.upper()}", plazo=plazo)
            _venta(db, [{"estado": "preparado", "ocp": oc.id, "parte": f"P-SEM-{nombre}"}])
            casos[nombre] = oc
        _set_created(casos["verde"].id, datetime.combine(hoy, datetime.min.time()))
        d_amar = _fecha_hace_habiles(4)      # 4 de 5 hábiles = 80% justo → amarillo
        _set_created(casos["amarillo"].id, datetime.combine(d_amar, datetime.min.time()))
        d_rojo = _fecha_hace_habiles(10)     # 10 hábiles con plazo 2 → vencida
        _set_created(casos["rojo"].id, datetime.combine(d_rojo, datetime.min.time()))
        _set_created(casos["sin_plazo"].id, datetime.combine(hoy, datetime.min.time()))
        _set_created(casos["sin_fecha"].id, None)
        # sonda7: 7 días CORRIDOS atrás (siempre cruza un finde) con plazo 6 — en
        # HÁBILES van ≤5 y NO está vencida; en CORRIDOS irían 7 > 6 y daría rojo.
        d7 = hoy - timedelta(days=7)
        _set_created(casos["sonda7"].id, datetime.combine(d7, datetime.min.time()))

        seg = _seguimiento()
        sems = {}
        for nombre, oc in casos.items():
            fila = next((x for x in seg.values()
                         if x.get("ocp") and x["ocp"]["id"] == oc.id), None)
            sems[nombre] = fila["ocp"]["semaforo"] if fila else None

        check("3.1 verde: recién emitida con plazo 30 → 0 transcurridos, 30 restantes",
              sems["verde"] == {"dias_habiles": 0, "plazo_dias": 30,
                                "estado": "verde", "dias_habiles_restantes": 30},
              sems["verde"])
        check("3.2 amarillo: 4 de 5 hábiles (80% justo) y aún no vencida",
              sems["amarillo"] == {"dias_habiles": 4, "plazo_dias": 5,
                                   "estado": "amarillo", "dias_habiles_restantes": 1},
              sems["amarillo"])
        # El atraso esperado se CALCULA con la convención de la campana ([a, b): el
        # día comprometido cuenta, el hoy no) en una implementación INDEPENDIENTE del
        # router — el número rígido (-8) solo valía consultado un día HÁBIL: mirado un
        # sábado, la campana dice un hábil MÁS (el viernes cuenta) y este check se
        # ponía rojo cada fin de semana sin que nada estuviera roto (visto el
        # 2026-08-22, sábado). La sonda de hábiles-vs-corridos vive en §9; acá se
        # afirma la exactitud contra la MISMA regla que la alerta de las 06:00.
        def _atraso_campana(desde, hasta):
            n, cursor = 0, desde
            while cursor < hasta:
                if cursor.weekday() < 5 and not _is_chile_holiday(cursor):
                    n += 1
                cursor += timedelta(days=1)
            return n

        fe_rojo = add_business_days(d_rojo, 2)
        esperado_rojo = -_atraso_campana(fe_rojo, hoy)
        check("3.3 rojo: 10 hábiles con plazo 2 → vencida, restantes exacto según la "
              "convención de la campana (válido también en fin de semana)",
              sems["rojo"] == {"dias_habiles": 10, "plazo_dias": 2,
                               "estado": "rojo",
                               "dias_habiles_restantes": esperado_rojo},
              (sems["rojo"], esperado_rojo))
        check("3.4 sin_plazo: plazo null → edad informada, sin contra qué vencer",
              sems["sin_plazo"] == {"dias_habiles": 0, "plazo_dias": None,
                                    "estado": "sin_plazo", "dias_habiles_restantes": None},
              sems["sin_plazo"])
        check("3.5 sin_fecha: created_at NULL (legado) → no se inventa ancla",
              sems["sin_fecha"] == {"dias_habiles": None, "plazo_dias": 20,
                                    "estado": "sin_fecha", "dias_habiles_restantes": None},
              sems["sin_fecha"])
        fila_sf = next(x for x in seg.values()
                       if x.get("ocp") and x["ocp"]["id"] == casos["sin_fecha"].id)
        check("3.6 sin_fecha: fecha_emision null", fila_sf["ocp"]["fecha_emision"] is None)
        habiles7 = _habiles(d7, hoy)
        check("3.7 ★ SONDA HÁBILES vs CORRIDOS (endpoint): 7 corridos son ≤5 hábiles "
              "y con plazo 6 NO está vencida (en corridos daría rojo)",
              sems["sonda7"] is not None and habiles7 <= 5
              and sems["sonda7"]["dias_habiles"] == habiles7
              and sems["sonda7"]["estado"] in ("verde", "amarillo"),
              (habiles7, sems["sonda7"]))
        check("3.8 el semáforo del mismo caso llega IGUAL por /preparados",
              next(x for x in _preparados().values()
                   if x.get("ocp") and x["ocp"]["id"] == casos["sonda7"].id
                   )["ocp"]["semaforo"] == sems["sonda7"])

        # ══ §4 · Sondas unitarias con `hoy` FIJO (deterministas, feriados reales) ═
        # Viernes 2026-01-09 + plazo 1: NO vence el sábado (la sonda del encargo).
        s = abast._semaforo_ocp(SimpleNamespace(plazo_dias=1, created_at=datetime(2026, 1, 9)),
                                hoy=date(2026, 1, 10))
        check("4.1 ★ OC de un viernes con plazo 1 NO vence el sábado (hábiles, no corridos)",
              s == {"dias_habiles": 0, "plazo_dias": 1, "estado": "verde",
                    "dias_habiles_restantes": 1}, s)
        # Feriado: mié 16-09-2026 + plazo 2 → jue 17 (1), vie 18 FERIADO, finde,
        # lun 21 (2). El domingo 20 van 4 corridos pero solo 1 hábil → verde.
        s = abast._semaforo_ocp(SimpleNamespace(plazo_dias=2, created_at=datetime(2026, 9, 16)),
                                hoy=date(2026, 9, 20))
        check("4.2 ★ el 18-09 (feriado) NO cuenta: 4 corridos = 1 hábil → verde",
              s == {"dias_habiles": 1, "plazo_dias": 2, "estado": "verde",
                    "dias_habiles_restantes": 1}, s)
        s = abast._semaforo_ocp(SimpleNamespace(plazo_dias=2, created_at=datetime(2026, 9, 16)),
                                hoy=date(2026, 9, 22))
        check("4.3 pasada la fecha comprometida (lun 21) → rojo con restantes -1",
              s == {"dias_habiles": 3, "plazo_dias": 2, "estado": "rojo",
                    "dias_habiles_restantes": -1}, s)
        s = abast._semaforo_ocp(SimpleNamespace(plazo_dias=5, created_at=datetime(2026, 3, 2)),
                                hoy=date(2026, 3, 6))
        check("4.4 amarillo en el umbral: 4 de 5 hábiles (80%) sin vencer",
              s == {"dias_habiles": 4, "plazo_dias": 5, "estado": "amarillo",
                    "dias_habiles_restantes": 1}, s)
        s = abast._semaforo_ocp(SimpleNamespace(plazo_dias=99999999, created_at=datetime(2026, 1, 9)),
                                hoy=date(2026, 1, 10))
        check("4.5 plazo basura (99999999) → sin_plazo, el request NO se cuelga",
              s["estado"] == "sin_plazo" and s["dias_habiles"] == 0, s)

        # 4.6 (revisión H2): el ATRASO de una vencida MIRADA EN FIN DE SEMANA dice el
        # MISMO número que la campana. OC comprometida un viernes (vie 09-ene-2026 +
        # plazo 0 no vale: usamos emitida lun 05 con plazo 4 → comprometida vie 09),
        # mirada el SÁBADO 10: la campana ([a,b)) cuenta el viernes → 1 hábil de
        # atraso. Con la convención (a,b] cruda diría 0 y la pantalla contradiría a
        # la alerta — la sonda cae si alguien "simplifica" el corrimiento.
        s = abast._semaforo_ocp(SimpleNamespace(plazo_dias=4, created_at=datetime(2026, 1, 5)),
                                hoy=date(2026, 1, 10))
        check("4.6 ★ vencida mirada el SÁBADO: atraso −1 (el número de la campana, "
              "no el 0 de la convención cruda)",
              s["estado"] == "rojo" and s["dias_habiles_restantes"] == -1, s)

        # 4.7 (revisión H1): el «hoy» del semáforo es el de CHILE, no date.today().
        # Sonda discriminante: se parchea _hoy_chile y el resultado DEBE moverse —
        # si el código usara date.today() crudo, el parche no cambiaría nada y esto
        # quedaría en rojo.
        _hoy_orig = abast._hoy_chile
        try:
            # mar 22-sep: la OC (emitida mié 16, plazo 2, feriados 18/19) venció el
            # lun 21 → rojo con 3 hábiles. Con date.today() crudo (agosto real), la
            # OC sería FUTURA → verde: el parche discrimina de verdad.
            abast._hoy_chile = lambda: date(2026, 9, 22)
            s = abast._semaforo_ocp(SimpleNamespace(plazo_dias=2,
                                                    created_at=datetime(2026, 9, 16)))
            check("4.7 ★ SONDA TZ: el semáforo obedece a _hoy_chile parcheado "
                  "(si usara date.today() crudo, esto no se movería)",
                  s["estado"] == "rojo" and s["dias_habiles"] == 3, s)
        finally:
            abast._hoy_chile = _hoy_orig
        check("4.8 _hoy_chile restaurado", abast._hoy_chile is _hoy_orig)

        # 6.5 (revisión H8): cantidad NULL → totales NULL, jamás 0 mudo.
        cp = abast._claves_plata_peso(SimpleNamespace(cantidad=None, costo=10.0,
                                                      moneda="EUR", peso_kg=2.0))
        check("6.5 cantidad NULL → fob_total y peso_total_kg NULL (no un 0 que la "
              "barra sumaría en silencio)",
              cp["fob_total"] is None and cp["peso_total_kg"] is None, cp)

        # ══ §8 · SONDA N+1: más OCs y más ítems NO agregan queries ═══════════════
        _, n_seg_1 = _contar_selects(_seguimiento)
        _, n_prep_1 = _contar_selects(_preparados)
        for k in range(3):
            oc = _ocp(db, prov=f"MAS-{k}", plazo=15)
            _venta(db, [{"estado": "preparado", "ocp": oc.id, "parte": f"P-MAS-{k}a"},
                        {"estado": "comprado", "ocp": oc.id, "parte": f"P-MAS-{k}b"}])
        _, n_seg_2 = _contar_selects(_seguimiento)
        _, n_prep_2 = _contar_selects(_preparados)
        check("8.1 seguimiento: +3 OCs y +6 ítems, MISMAS queries (batch IN, no N+1)",
              n_seg_2 == n_seg_1, (n_seg_1, n_seg_2))
        check("8.2 preparados: +3 OCs y +3 ítems preparados, MISMAS queries",
              n_prep_2 == n_prep_1, (n_prep_1, n_prep_2))
        check("8.3 el puñado fijo es chico de verdad (≤6 SELECT por request)",
              n_seg_2 <= 6 and n_prep_2 <= 6, (n_seg_2, n_prep_2))

        # ══ §9 · SONDAS DE PODER DISCRIMINANTE (inyectar el vicio → debe dar rojo) ═
        # 9.1 semáforo en CORRIDOS: las sondas 4.2 y 3.7 deben CAER.
        orig_add, orig_entre = abast.add_business_days, abast._dias_habiles_entre
        try:
            abast.add_business_days = lambda d, n: (
                (d.date() if isinstance(d, datetime) else d) + timedelta(days=n))
            abast._dias_habiles_entre = lambda a, b: (b - a).days
            mal_42 = abast._semaforo_ocp(
                SimpleNamespace(plazo_dias=2, created_at=datetime(2026, 9, 16)),
                hoy=date(2026, 9, 20))
            seg_mal = _seguimiento()
            fila7 = next((x for x in seg_mal.values()
                          if x.get("ocp") and x["ocp"]["id"] == casos["sonda7"].id), None)
        finally:
            abast.add_business_days, abast._dias_habiles_entre = orig_add, orig_entre
        check("9.1 ★ SONDA corridos: con la mutación, 4.2 daría ROJO (acá dio verde)",
              mal_42["estado"] == "rojo" and mal_42["dias_habiles"] == 4, mal_42)
        check("9.2 ★ SONDA corridos por el ENDPOINT: sonda7 pasa a rojo con 7 'hábiles'",
              fila7 is not None and fila7["ocp"]["semaforo"]["estado"] == "rojo"
              and fila7["ocp"]["semaforo"]["dias_habiles"] == 7,
              fila7 and fila7["ocp"]["semaforo"])

        # 9.3 batch IN reemplazado por caché-por-OC en loop: la sonda §8 debe CAER.
        orig_ctx = abast._ocp_contexto_agrupacion

        def _ctx_por_oc(db_, items_):
            ocps_, conteos_ = {}, {}
            for it_ in items_:
                oid = it_.oc_proveedor_id
                if not oid or oid in ocps_:
                    continue
                o_ = db_.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == oid).first()
                if not o_:
                    continue
                ocps_[oid] = o_
                for est_, n_ in (db_.query(MonzaCotizacionItem.estado_linea,
                                           func.count(MonzaCotizacionItem.id))
                                 .filter(MonzaCotizacionItem.oc_proveedor_id == oid)
                                 .group_by(MonzaCotizacionItem.estado_linea).all()):
                    conteos_.setdefault(oid, {})[est_ or "cotizado"] = int(n_)
            return ocps_, conteos_

        try:
            abast._ocp_contexto_agrupacion = _ctx_por_oc
            r_mal, n_seg_mal = _contar_selects(_seguimiento)
            _, n_prep_mal = _contar_selects(_preparados)
        finally:
            abast._ocp_contexto_agrupacion = orig_ctx
        check("9.4 ★ SONDA N+1: con el caché-por-OC las queries CRECEN (la §8 caería)",
              n_seg_mal > n_seg_2 and n_prep_mal > n_prep_2,
              (n_seg_2, n_seg_mal, n_prep_2, n_prep_mal))
        check("9.5 …y el resultado con N+1 es idéntico: la sonda mide QUERIES, no datos",
              r_mal == _seguimiento())
        _, n_rest = _contar_selects(_seguimiento)
        check("9.6 módulo restaurado tras las sondas (queries de vuelta al puñado fijo)",
              abast.add_business_days is orig_add
              and abast._dias_habiles_entre is orig_entre
              and abast._ocp_contexto_agrupacion is orig_ctx
              and n_rest == n_seg_2, (n_rest, n_seg_2))

    finally:
        _limpiar()
        res = _residuos()
        db.close()
        print(f"Residuos: {res}")
    check("10 CERO residuos en la BD con el MARK (conexión nueva, SQL crudo)",
          all(v == 0 for v in res.values()), res)

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_logistica_seguimiento_agrupado():
    """Wrapper pytest de una línea: sin él la suite sería INVISIBLE al gate."""
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
