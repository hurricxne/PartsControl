"""FECHA PROMETIDA CONGELADA al Cierre de Venta (Fase 2 del Portal de Clientes, GA).

LO QUE ESTA SUITE PROTEGE (docs/plan-portal-clientes-2026-08-20.md §7 + §15 de la Fase 0):
  §A  El cierre (POST /compras/oc-cliente) congela items_cotizacion.fecha_promesa_cliente
      = día del cierre (hoy de Chile) + el plazo que la cotización formal le informó al
      cliente (plazo_prometido: plazo_entrega_max o, si el ítem no tiene, el plazo máximo
      por defecto del cotizador), en días hábiles. La cotización se RETRO-DATA 30 días
      antes de cerrar: si el congelado anclara en created_at (lo que hace el cálculo
      legado de _item_deadline), la fecha saldría un mes antes y la sonda cae — así se
      discrimina el ancla, no solo la existencia del dato. Ítem sin plazo propio → se
      congela con el plazo por defecto (a2); ítem con SOLO plazo mínimo → también con el
      por defecto, no con el mínimo (a2b); plazo fuera del tope de cordura
      (MAX_PLAZO_DIAS_PROMESA) → NULL y el request responde (sin el tope, un dedazo
      colgaría el Cierre completo).
  §C  «No se mueve al comprar»: asignar_items PISA plazo_entrega_max (la puerta real:
      su update de plazo_entrega_map) y la sonda exige DOS cosas — que el pisado ocurrió
      de verdad (si no, la sonda no probaría nada) y que la fecha congelada quedó intacta.
  §D  «No se mueve al partir la línea»: _clone_item_cotizacion es el ÚNICO dueño de la
      copia (lo usan los 3 splits) y enumera campos A MANO — el plan asumía herencia
      automática y era falso. El clon debe traer la MISMA fecha, no NULL.
  §E  _item_deadline (Despachos) PREFIERE la congelada cuando existe; con NULL sigue el
      cálculo legado (created_at + plazo hábiles) — freeze-forward: las ventas viejas no
      cambian ni un día — y con un plazo que no sirve para calcular (ninguno, absurdo o
      negativo) cae a oc.fecha_entrega: ni una fecha de 2041 ni el día de creación.
  §B  Reintento idempotente del cierre: se planta un centinela en la fecha y se
      reintenta. La producción tiene TRES capas que protegen esto (el early-return corre
      antes del congelado, la guarda «solo si es None», y ese camino no hace commit),
      así que b1 NO vigila la posición del bloque ni la guarda sueltas — quitar una capa
      sola deja las otras dos en pie y la suite verde, a propósito. Lo que b1 discrimina
      es la regresión realista que derrota a las tres: un re-sellado ACTIVO agregado al
      camino idempotente (esa es la mutación verificada).
  §F  Dos cierres SIMULTÁNEOS de la misma venta dejan UNA sola OC: la cotización se lee
      bajo lock (with_for_update) y el segundo cierre espera y cae en el early-return.
      La carrera es DETERMINISTA: routers.compras.hoy_chile se reemplaza por un punto de
      encuentro (Barrier) que queda entre el chequeo de oc_existente y el commit; sin el
      lock los dos hilos pasan el chequeo, se encuentran ahí y cada uno inserta su OC.
  §G  Los días restantes (business_days_remaining: semáforo de Despachos y de los
      listados de Compras) se cuentan desde el día de CHILE, no desde el del servidor
      en UTC (que pasadas las 20-21 h de Chile ya está en mañana).
  §H  _item_to_dict (los 5 listados de Compras) no calcula con un plazo que no sirve:
      dias_restantes queda None en vez de un número gigante o un 500 por un solo ítem.
  §I  Tablas de verdad de plazo_creible y plazo_prometido, y paridad del respaldo (45)
      con lo que imprime la cotización formal (_config_to_dict, routers/cotizador.py).

MUTACIONES: las verifica el arnés del repo (python -m portal.tests.mutaciones
fecha_promesa; la lista vive en portal/tests/mutaciones/cierre.py): congelar solo con el
plazo propio (→ a2, a2b), el mínimo como respaldo del máximo (→ a2b), herencia en el clon
(→ §D), preferencia y respaldo en _item_deadline (→ §E), plazo sin filtrar en
_item_deadline (→ e4, e5) y en _item_to_dict (→ h1), cotas de plazo_creible (→ §I, a3),
respaldo 45 y precedencia de plazo_prometido (→ §I), lock del cierre (→ f1), import
local de hoy_chile (→ f0), reloj del servidor (→ §G) y re-sellado activo en el camino
idempotente (→ §B).

Datos MARCADOS (sin guiones bajos: en LIKE el _ es comodín) con barrido por MARK ANTES
de sembrar y en finally (una corrida muerta no rompe la siguiente). Requiere la BD local.
El plazo por defecto del cotizador se LEE de configuracion_cotizador (id=1) y no se toca.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_fecha_promesa.py -q
(también:   ./venv/bin/python routers/tests/test_fecha_promesa.py)
"""
import os
import sys
import threading
from datetime import datetime, timedelta, date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, OcProveedor, OcProveedorItem, Notificacion,
    ConfiguracionCotizador,
)
# PIN del módulo raíz `notificaciones` (mismo patrón que test_firma_parcial.py): con
# routers/ en sys.path, el router homónimo puede sombrear al módulo raíz que trae
# crear_notificacion (crear_oc_cliente lo usa best-effort).
import notificaciones as _notif_raiz  # noqa: E402
assert hasattr(_notif_raiz, "crear_notificacion"), (
    f"el módulo notificaciones resolvió a {_notif_raiz.__file__} (sombra del router)")

# El reloj del cierre y de los días restantes se parchea en routers.compras.hoy_chile:
# §F y §G lo reemplazan y lo restauran en finally.
import routers.compras as compras_mod  # noqa: E402
from routers.compras import (  # noqa: E402
    router as compras_router, add_business_days, business_days_remaining,
    _clone_item_cotizacion, _item_to_dict, plazo_creible, plazo_prometido,
    MAX_PLAZO_DIAS_PROMESA, PLAZO_MAX_SIN_CONFIG,
)
from routers.cotizador import _config_to_dict  # noqa: E402
from routers.despachos import _item_deadline  # noqa: E402
from wasabil_dte.service import hoy_chile  # noqa: E402

MARK = "ZZFECHAPROM"  # SIN guiones bajos: en LIKE el _ es comodín
CURRENT = {"empresa": "mineria", "id": None}
# §F: cuánto espera el primer cierre en el punto de encuentro al segundo. Con el lock el
# segundo nunca llega (está esperando la fila), así que esto es lo que dura la sonda.
SEGUNDOS_PUNTO_DE_ENCUENTRO = 2

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(compras_router, prefix="/api")


# Auth REALISTA (patrón test_firma_parcial.py): una lectura en la MISMA sesión del
# request abre el read view de MySQL antes de cualquier lock, igual que en producción.
def _current_user_realista(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _current_user_realista
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Limpieza por MARK (no por ids en memoria: una corrida muerta se auto-cura) ────────

def _limpiar():
    db = SessionLocal()
    try:
        cot_ids = [c.id for c in db.query(Cotizacion)
                   .filter(Cotizacion.numero.like(f"{MARK}%")).all()]
        item_ids = [i.id for i in db.query(ItemCotizacion)
                    .filter(ItemCotizacion.cotizacion_id.in_(cot_ids or [0])).all()]
        db.query(OcProveedorItem).filter(
            OcProveedorItem.item_cotizacion_id.in_(item_ids or [0])
        ).delete(synchronize_session=False)
        db.query(OcProveedor).filter(
            OcProveedor.numero.like(f"{MARK}%")).delete(synchronize_session=False)
        db.query(Notificacion).filter(
            Notificacion.titulo.like(f"%{MARK}%")).delete(synchronize_session=False)
        db.query(OcCliente).filter(
            OcCliente.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=False)
        db.query(ItemCotizacion).filter(
            ItemCotizacion.id.in_(item_ids or [0])).delete(synchronize_session=False)
        db.query(Cotizacion).filter(
            Cotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ── Fábrica de datos MARCADOS ─────────────────────────────────────────────────────────

def _plazo_por_defecto_del_cotizador(db) -> int:
    """El plazo máximo por defecto que la cotización formal imprime para un ítem sin
    plazo propio: la fila global de configuración (id=1) o, si no trae, el respaldo del
    cotizador. Solo se LEE: la fila la comparten las demás suites y la aplicación."""
    cfg = db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    return (cfg.plazo_max_default if cfg is not None else None) or PLAZO_MAX_SIN_CONFIG


def _sembrar(db, minimo_solo: int):
    """Cotización RETRO-DATADA 30 días con 4 ítems: plazo normal / sin plazo / absurdo /
    solo plazo mínimo (`minimo_solo`, distinto del plazo por defecto para que a2b
    discrimine)."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} Cliente",
                     rut_cliente="78.279.030-7")
    db.add(cot)
    db.flush()
    # Retro-datar: si el congelado anclara en created_at, §A caería un mes corto.
    db.query(Cotizacion).filter(Cotizacion.id == cot.id).update(
        {"created_at": datetime.utcnow() - timedelta(days=30)})
    it_normal = ItemCotizacion(cotizacion_id=cot.id, descripcion=f"{MARK} normal",
                               numero_parte="FP-1", cantidad=3.0,
                               plazo_entrega_max=10, estado_item="ingresado")
    it_sin = ItemCotizacion(cotizacion_id=cot.id, descripcion=f"{MARK} sin plazo",
                            numero_parte="FP-2", cantidad=1.0,
                            plazo_entrega_max=None, estado_item="ingresado")
    it_absurdo = ItemCotizacion(cotizacion_id=cot.id, descripcion=f"{MARK} absurdo",
                                numero_parte="FP-3", cantidad=1.0,
                                plazo_entrega_max=MAX_PLAZO_DIAS_PROMESA + 1,
                                estado_item="ingresado")
    it_min = ItemCotizacion(cotizacion_id=cot.id, descripcion=f"{MARK} solo minimo",
                            numero_parte="FP-4", cantidad=1.0,
                            plazo_entrega_min=minimo_solo, plazo_entrega_max=None,
                            estado_item="ingresado")
    db.add_all([it_normal, it_sin, it_absurdo, it_min])
    db.commit()
    return cot.id, it_normal.id, it_sin.id, it_absurdo.id, it_min.id


def _sembrar_para_la_carrera(db) -> int:
    """Segunda cotización marcada, con un ítem de plazo 10, para los dos cierres de §F."""
    cot = Cotizacion(numero=f"{MARK}-COT2", cliente=f"{MARK} Cliente 2",
                     rut_cliente="78.279.030-7")
    db.add(cot)
    db.flush()
    db.add(ItemCotizacion(cotizacion_id=cot.id, descripcion=f"{MARK} carrera",
                          numero_parte="FP-5", cantidad=1.0,
                          plazo_entrega_max=10, estado_item="ingresado"))
    db.commit()
    return cot.id


def run():
    _limpiar()  # barrido POR MARK antes de sembrar: se auto-cura de corridas muertas
    db = SessionLocal()
    try:
        por_defecto = _plazo_por_defecto_del_cotizador(db)
        minimo_solo = 20 if por_defecto != 20 else 7
        cot_id, id_normal, id_sin, id_absurdo, id_min = _sembrar(db, minimo_solo)

        # ══ §A — El cierre congela ═══════════════════════════════════════════════════
        r = client.post("/api/compras/oc-cliente",
                        json={"cotizacion_id": cot_id, "numero_oc": f"{MARK}-OC1"})
        check("a0 cierre responde 201", r.status_code == 201, r.text)
        oc_id = r.json().get("id")

        db.expire_all()
        it_n = db.query(ItemCotizacion).get(id_normal)
        it_s = db.query(ItemCotizacion).get(id_sin)
        it_a = db.query(ItemCotizacion).get(id_absurdo)
        it_m = db.query(ItemCotizacion).get(id_min)
        esperada = add_business_days(hoy_chile(), 10)
        esperada_defecto = add_business_days(hoy_chile(), por_defecto)
        check("a1 congela hoy Chile + 10 hábiles (ancla = cierre, no created_at)",
              it_n.fecha_promesa_cliente == esperada,
              f"{it_n.fecha_promesa_cliente} != {esperada}")
        check("a2 ítem sin plazo propio se congela con el plazo por defecto del cotizador",
              it_s.fecha_promesa_cliente == esperada_defecto,
              f"{it_s.fecha_promesa_cliente} != {esperada_defecto} (por defecto {por_defecto})")
        check("a2b ítem con SOLO plazo mínimo también usa el por defecto (no hoy + mínimo)",
              it_m.fecha_promesa_cliente == esperada_defecto,
              f"{it_m.fecha_promesa_cliente} != {esperada_defecto} "
              f"(hoy + {minimo_solo} = {add_business_days(hoy_chile(), minimo_solo)})")
        check("a3 plazo fuera del tope de cordura queda NULL y el request respondió",
              it_a.fecha_promesa_cliente is None, it_a.fecha_promesa_cliente)
        check("a4 el flip ingresado→cerrado sigue intacto",
              it_n.estado_item == "cerrado", it_n.estado_item)

        # ══ §C — No se mueve al comprar (la puerta real: asignar_items pisa el plazo) ═
        ocp = OcProveedor(numero=f"{MARK}-OCP", proveedor=f"{MARK} Prov")
        db.add(ocp)
        db.commit()
        r = client.post(f"/api/compras/oc-proveedor/{ocp.id}/items",
                        json={"item_ids": [id_normal], "oc_cliente_id": oc_id,
                              "item_plazos": [{"id": id_normal, "plazo_dias_prov": 30,
                                               "plazo_entrega_max": 99}]})
        check("c0 asignar_items responde", r.status_code == 200, r.text)
        db.expire_all()
        it_n = db.query(ItemCotizacion).get(id_normal)
        check("c1 la puerta se ejercitó: plazo_entrega_max quedó pisado a 99",
              it_n.plazo_entrega_max == 99, it_n.plazo_entrega_max)
        check("c2 la fecha congelada NO se movió con la compra",
              it_n.fecha_promesa_cliente == esperada,
              f"{it_n.fecha_promesa_cliente} != {esperada}")

        # ══ §D — No se mueve al partir la línea (el único dueño de la copia) ═════════
        clon = _clone_item_cotizacion(it_n, 1.0, "cerrado", db, clonar_vinculo=False)
        db.commit()
        check("d1 el clon del split hereda la fecha congelada (no nace NULL)",
              clon.fecha_promesa_cliente == esperada,
              f"{clon.fecha_promesa_cliente} != {esperada}")

        # ══ §E — _item_deadline prefiere la congelada; con NULL, todo sigue igual ════
        cot = db.query(Cotizacion).get(cot_id)
        oc = db.query(OcCliente).get(oc_id)
        # e1: plazo ya pisado a 99 — el cálculo vivo daría OTRA fecha; manda la congelada
        check("e1 _item_deadline devuelve la congelada, no el cálculo vivo",
              _item_deadline(it_n, cot, oc) == esperada,
              _item_deadline(it_n, cot, oc))
        # e2: ítem NULL con plazo → cálculo legado created_at + plazo hábiles, intacto
        it_legacy = ItemCotizacion(cotizacion_id=cot_id, descripcion=f"{MARK} legado",
                                   cantidad=1.0, plazo_entrega_max=5,
                                   estado_item="cerrado")
        db.add(it_legacy)
        db.commit()
        db.refresh(cot)
        legado_esperado = add_business_days(cot.created_at.date(), 5)
        check("e2 con NULL el cálculo legado no cambia ni un día (freeze-forward)",
              _item_deadline(it_legacy, cot, oc) == legado_esperado,
              f"{_item_deadline(it_legacy, cot, oc)} != {legado_esperado}")
        # e3-e5: ítems TRANSITORIOS (no se guardan) sin fecha congelada: el respaldo
        # oc.fecha_entrega de las ventas cerradas sin congelar y de los plazos que no
        # sirven para calcular. El ítem sin plazo del cierre ya no sirve acá: nace con
        # fecha (a2).
        db.query(OcCliente).filter(OcCliente.id == oc_id).update(
            {"fecha_entrega": date(2030, 1, 15)})
        db.commit()
        db.refresh(oc)
        it_sin_nada = ItemCotizacion(plazo_entrega_max=None)
        check("e3 con NULL y sin plazo manda el respaldo oc.fecha_entrega",
              _item_deadline(it_sin_nada, cot, oc) == date(2030, 1, 15),
              _item_deadline(it_sin_nada, cot, oc))
        it_absurdo_t = ItemCotizacion(plazo_entrega_max=MAX_PLAZO_DIAS_PROMESA + 1)
        check("e4 plazo fuera del tope → la fecha de entrega de la orden, no una de 2041",
              _item_deadline(it_absurdo_t, cot, oc) == date(2030, 1, 15),
              _item_deadline(it_absurdo_t, cot, oc))
        it_negativo_t = ItemCotizacion(plazo_entrega_max=-5)
        check("e5 plazo negativo → la fecha de entrega de la orden, no el día de creación",
              _item_deadline(it_negativo_t, cot, oc) == date(2030, 1, 15),
              _item_deadline(it_negativo_t, cot, oc))

        # ══ §B — El reintento idempotente NO re-sella ════════════════════════════════
        centinela = date(2031, 12, 31)
        db.query(ItemCotizacion).filter(ItemCotizacion.id == id_normal).update(
            {"fecha_promesa_cliente": centinela})
        db.commit()
        r = client.post("/api/compras/oc-cliente",
                        json={"cotizacion_id": cot_id, "numero_oc": f"{MARK}-OC1"})
        check("b0 reintento devuelve la OC existente", r.status_code in (200, 201)
              and r.json().get("id") == oc_id, r.text)
        db.expire_all()
        it_n = db.query(ItemCotizacion).get(id_normal)
        check("b1 el reintento no re-sella la fecha (early-return antes del congelado)",
              it_n.fecha_promesa_cliente == centinela, it_n.fecha_promesa_cliente)

        # ══ §F — Dos cierres simultáneos de la misma venta: una sola OC ═════════════
        cot2_id = _sembrar_para_la_carrera(db)
        encuentro = threading.Barrier(2, timeout=SEGUNDOS_PUNTO_DE_ENCUENTRO)
        pisadas = []
        hoy_real = compras_mod.hoy_chile

        def hoy_con_punto_de_encuentro():
            # El único hoy_chile() del cierre está entre el chequeo de oc_existente y el
            # commit: quien llega acá ya decidió crear la OC.
            pisadas.append(threading.get_ident())
            try:
                encuentro.wait()
            except threading.BrokenBarrierError:
                pass  # el otro hilo no llegó (espera el lock): se sigue solo
            return hoy_real()

        respuestas = []

        def cerrar():
            try:
                r2 = client.post("/api/compras/oc-cliente",
                                 json={"cotizacion_id": cot2_id, "numero_oc": f"{MARK}-OC2"})
                respuestas.append((r2.status_code,
                                   r2.json().get("id") if r2.status_code < 300 else r2.text))
            except Exception as e:  # noqa: BLE001 — se reporta en f2, no se pierde
                respuestas.append(("EXC", repr(e)))

        compras_mod.hoy_chile = hoy_con_punto_de_encuentro
        try:
            hilos = [threading.Thread(target=cerrar, name=f"cierre-{i}") for i in range(2)]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join(timeout=90)
        finally:
            compras_mod.hoy_chile = hoy_real
        # Se lee con una sesión NUEVA: lo confirmado, no la foto de `db`.
        db_lector = SessionLocal()
        try:
            n_oc = db_lector.query(OcCliente).filter(
                OcCliente.cotizacion_id == cot2_id).count()
            n_notif = db_lector.query(Notificacion).filter(
                Notificacion.titulo.like(f"%{MARK}-OC2%")).count()
        finally:
            db_lector.close()
        check("f0 el punto de encuentro se pisó al menos una vez (si no, la sonda no prueba nada)",
              len(pisadas) >= 1, pisadas)
        check("f1 una sola OC para la cotización", n_oc == 1, n_oc)
        check("f2 las dos respuestas son 201 con el mismo id",
              len(respuestas) == 2 and all(st == 201 for st, _ in respuestas)
              and len({cuerpo for _, cuerpo in respuestas}) == 1, respuestas)
        check("f3 una sola notificación «Nueva OC Cliente»", n_notif == 1, n_notif)

        print()
        if _fails:
            print(f"FALLARON {len(_fails)}: {_fails}")
        else:
            print("SUITE VERDE: fecha prometida congelada, con las puertas vigiladas")
        assert not _fails, _fails
    finally:
        db.close()
        _limpiar()


def test_fecha_promesa():
    run()


# ══ §G — El reloj de los días restantes es el de Chile (sin base) ════════════════════

def test_g_los_dias_restantes_se_cuentan_desde_el_dia_de_chile():
    """business_days_remaining toma «hoy» de routers.compras.hoy_chile. Se parchea con una
    fecha PASADA a propósito (un miércoles de 2024) para que nunca coincida con el día
    real: si la función volviera a date.today(), las tres cuentas darían números del
    orden de -600 en vez de 0, 1 y -1."""
    hoy_real = compras_mod.hoy_chile
    compras_mod.hoy_chile = lambda: date(2024, 3, 13)
    try:
        assert business_days_remaining(date(2024, 3, 13)) == 0
        assert business_days_remaining(date(2024, 3, 14)) == 1
        assert business_days_remaining(date(2024, 3, 12)) == -1
    finally:
        compras_mod.hoy_chile = hoy_real


# ══ §H — _item_to_dict no calcula con un plazo que no sirve (sin base) ═══════════════

def test_h_item_to_dict_no_calcula_con_un_plazo_que_no_sirve():
    fecha_ref = datetime(2026, 9, 1)
    absurdo = _item_to_dict(
        ItemCotizacion(id=1, cantidad=1, plazo_entrega_max=MAX_PLAZO_DIAS_PROMESA + 1),
        None, None, None, 0.0, fecha_ref)
    assert absurdo["dias_restantes"] is None, absurdo["dias_restantes"]
    # El dato se devuelve tal cual: el filtro es del cálculo, no del contrato.
    assert absurdo["plazo_entrega_max"] == MAX_PLAZO_DIAS_PROMESA + 1
    normal = _item_to_dict(ItemCotizacion(id=2, cantidad=1, plazo_entrega_max=10),
                           None, None, None, 0.0, fecha_ref)
    assert isinstance(normal["dias_restantes"], int), normal["dias_restantes"]


# ══ §I — Tablas de verdad del plazo (sin base) ═══════════════════════════════════════

def test_i_plazo_creible_tabla_de_verdad():
    assert plazo_creible(None) is None
    assert plazo_creible(0) is None
    assert plazo_creible(1) == 1
    assert plazo_creible(3650) == 3650
    assert plazo_creible(3651) is None
    assert plazo_creible(-5) is None
    assert plazo_creible("abc") is None


def test_i_plazo_prometido_tabla_de_verdad_y_paridad_con_la_cotizacion_formal():
    assert plazo_prometido(None, None) == 45
    assert plazo_prometido(None, 60) == 60
    assert plazo_prometido(10, 60) == 10
    assert plazo_prometido(0, 60) == 60
    # Un plazo propio que no sirve NO cae al default: no se inventa una promesa sobre
    # un dato basura (queda NULL y el ítem sigue la fecha de la orden).
    assert plazo_prometido(3651, 60) is None
    assert plazo_prometido(-5, 60) is None
    assert plazo_prometido(None, 99999) is None
    # El respaldo es el MISMO que imprime la cotización formal cuando la configuración
    # no trae plazo máximo por defecto (no el 15 de _item_to_dict).
    assert PLAZO_MAX_SIN_CONFIG == _config_to_dict(ConfiguracionCotizador())["plazo_max_default"]


if __name__ == "__main__":
    run()
    test_g_los_dias_restantes_se_cuentan_desde_el_dia_de_chile()
    test_h_item_to_dict_no_calcula_con_un_plazo_que_no_sirve()
    test_i_plazo_creible_tabla_de_verdad()
    test_i_plazo_prometido_tabla_de_verdad_y_paridad_con_la_cotizacion_formal()
    print("SONDAS SIN BASE (§G, §H, §I): verdes")
