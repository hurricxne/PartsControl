"""N° de BULTO del despacho (Grupo AM / MachParts) — suite de contrato.

LO QUE ESTA SUITE PROTEGE (columna despachos.bulto_numero, rotulado logístico puro):
  §A  CREAR (POST /despachos/): el bulto viaja desde el nacimiento del despacho
      («el operador empaca MIENTRAS crea despachos»); texto libre ≤50 («1», «B2»,
      «Cajas 2-3»); "" y puros espacios → NULL en BD (no string vacío); trim de
      bordes; >50 chars → 400 con mensaje claro (no el DataError críptico del
      commit) y SIN despacho fantasma.
  §B  EDITAR (PUT): cambia en en_preparacion Y en despachado (cabecera editable
      post-cierre, gemelo del N° de expedición); tri-estado exclude_unset (un PUT
      que no menciona bulto no lo toca); >50 → 400 y el valor anterior INTACTO;
      ""/espacios → NULL.
  §C  DETALLE DE OC (GET /despachos/oc-clientes/{id}): cada fila de despacho trae
      bulto_numero IGUAL al GET por id (sonda de igualdad detalle==porId — dos
      serializers, un solo dato).
  §D  ANULADO: el PUT rebota (comportamiento existente, sin duplicar el guard)
      pero el GET aún muestra su bulto — el rotulado histórico se conserva.
  §B+ (2026-08-26) b7: null EXPLÍCITO en el PUT borra el bulto (la 3ª pata del
      tri-estado; "no mandar" y "" ya estaban probados); b8: EXACTAMENTE 50 chars
      pasa y se relee íntegro (solo estaba probado el borde 51→400).
  §E  (2026-08-26) contrato del COLAPSADO (_colapsar de routers/despachos.py):
      mitad backend del espejo con frontend-src/src/picking/picking.ts — guiones
      y espacios fuera, upper, y los PUNTOS NO se eliminan.

MUTACIÓN verificada al construir la suite (quitar → rojo → restaurar → verde,
sha256 idéntico; evidencia en el reporte del encargo): el `or None` de
_normalizar_bulto (""→None) vuelto trim puro → caen las sondas de NULL de §A/§B
(a4, b5, b6): sin la normalización, la BD guarda "" y el chip pinta vacíos.

Datos MARCADOS con TBULTO (sin guiones bajos: en LIKE el _ es comodín) y limpieza
FK-safe verificada con SESIÓN NUEVA. Requiere la BD local (igual que las demás
suites GA).

Corre con:  ./venv/bin/python -m pytest routers/tests/test_bulto_despacho.py -q
(también:   ./venv/bin/python routers/tests/test_bulto_despacho.py)
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    Notificacion, User,
)
# PIN del módulo raíz `notificaciones` (mismo patrón que test_firma_parcial.py):
# con routers/ en sys.path, el ROUTER homónimo routers/notificaciones.py puede sombrear
# al módulo raíz que trae crear_notificacion. Importarlo acá, con backend/ al frente
# del path, deja la resolución correcta cacheada en sys.modules.
import notificaciones as _notif_raiz  # noqa: E402
assert hasattr(_notif_raiz, "crear_notificacion"), (
    f"el módulo notificaciones resolvió a {_notif_raiz.__file__} (sombra del router)")

from routers.despachos import router as despachos_router  # noqa: E402

MARK = "TBULTO"  # SIN guiones bajos: en LIKE el _ es comodín
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(despachos_router, prefix="/api")


# Auth REALISTA (patrón test_firma_parcial.py): una lectura en la MISMA sesión del
# request, igual que en producción.
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


# ── Fábricas de datos MARCADOS ────────────────────────────────────────────────────

def _venta(db, sufijo, qty=10.0):
    """Cotización + OC + 1 ítem EN BODEGA (sin recepción: el tope físico no acota
    ítems sin registro — flujo histórico — y el POST de crear despacho pasa)."""
    cot = Cotizacion(numero=f"{MARK}-{sufijo}", cliente=f"{MARK} Cliente",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1,
                        numero_parte=f"{MARK}-P{sufijo}",
                        descripcion=f"{MARK} pieza {sufijo}", cantidad=qty,
                        estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}",
                   fecha_oc="2026-08-01")
    db.add(oc); db.flush()
    db.commit()
    for obj in (cot, it, oc):
        db.refresh(obj)
    return SimpleNamespace(cot_id=cot.id, item_id=it.id, oc_id=oc.id, qty=qty)


def _crear(oc_id, item_id, qty=1.0, **extra):
    """POST real de creación (con observaciones marcadas: la fila de despachos
    queda rastreable por sí sola para la verificación de limpieza)."""
    body = {"oc_cliente_id": oc_id, "observaciones": f"{MARK} obs",
            "items": [{"item_cotizacion_id": item_id, "qty_despachada": qty}]}
    body.update(extra)
    return client.post("/api/despachos/", json=body)


def _bulto_en_bd(desp_id):
    """Relee el bulto con SESIÓN NUEVA (nada de identity map viejo): la sonda es
    contra lo COMMITEADO, no contra lo que el request creyó guardar."""
    db = SessionLocal()
    try:
        return db.query(Despacho.bulto_numero).filter(Despacho.id == desp_id).scalar()
    finally:
        db.close()


def _set_estado(desp_id, estado):
    """Flip directo del estado (sesión nueva): el ciclo de vida cerrar/anular tiene
    sus propias suites; aquí solo interesa el bulto en cada estado."""
    db = SessionLocal()
    try:
        db.query(Despacho).filter(Despacho.id == desp_id).update({"estado": estado})
        db.commit()
    finally:
        db.close()


# ── Limpieza ─────────────────────────────────────────────────────────────────────

def _limpiar():
    db = SessionLocal()
    try:
        db.rollback()
        cot_ids = [c.id for c in db.query(Cotizacion)
                   .filter(Cotizacion.numero.like(f"{MARK}%")).all()]
        oc_ids = [o.id for o in db.query(OcCliente)
                  .filter(OcCliente.cotizacion_id.in_(cot_ids or [0])).all()]
        if oc_ids:
            desp_ids = [d.id for d in db.query(Despacho)
                        .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
            if desp_ids:
                db.query(Notificacion).filter(
                    Notificacion.entidad_tipo == "despacho",
                    Notificacion.entidad_id.in_(desp_ids)).delete(synchronize_session=False)
                db.query(DespachoItem).filter(
                    DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
                db.query(Despacho).filter(
                    Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
        if cot_ids:
            db.query(ItemCotizacion).filter(
                ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.email.like(f"{MARK}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (
            db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
            + db.query(OcCliente).filter(OcCliente.numero_oc.like(f"{MARK}%")).count()
            # Los despachos llevan observaciones marcadas: contables por sí solos
            # aunque su numero_despacho sea el correlativo DSP genérico.
            + db.query(Despacho).filter(Despacho.observaciones.like(f"{MARK}%")).count()
            + db.query(User).filter(User.email.like(f"{MARK}%")).count()
        )
        assert restos == 0, f"limpieza incompleta: quedan {restos} filas {MARK}"
        print("Limpieza verificada con sesión nueva: 0 restos")
    finally:
        db.close()


# ── Suite ────────────────────────────────────────────────────────────────────────

def run():
    _limpiar()
    db = SessionLocal()
    try:
        CURRENT["empresa"] = "mineria"
        u = User(email=f"{MARK}@test.cl", nombre=f"{MARK} user",
                 hashed_password="x", empresa="mineria")
        db.add(u); db.commit(); db.refresh(u)
        CURRENT["id"] = u.id

        V = _venta(db, "A", qty=10.0)

        # ── §A · CREAR: el bulto viaja desde el nacimiento ───────────────────────
        r = _crear(V.oc_id, V.item_id, bulto_numero="B2")
        check("a1 crear CON bulto «B2» → 200", r.status_code == 200, r.text[:200])
        d1 = r.json()["id"]
        g = client.get(f"/api/despachos/{d1}").json()
        check("a2 GET por id devuelve el bulto «B2»",
              g.get("bulto_numero") == "B2", g.get("bulto_numero"))

        r = _crear(V.oc_id, V.item_id)
        check("a3 crear SIN bulto → 200 y GET null", r.status_code == 200
              and client.get(f"/api/despachos/{r.json()['id']}").json()
              .get("bulto_numero") is None, r.text[:200])
        d2 = r.json()["id"]

        r = _crear(V.oc_id, V.item_id, bulto_numero="   ")
        check("a4 ★ crear con «   » (espacios) → NULL en BD (sesión nueva, no \"\")",
              r.status_code == 200 and _bulto_en_bd(r.json()["id"]) is None,
              (r.status_code, repr(_bulto_en_bd(r.json()["id"])) if r.status_code == 200 else r.text[:200]))

        r = _crear(V.oc_id, V.item_id, bulto_numero="  Cajas 2-3  ")
        check("a5 trim de bordes: «  Cajas 2-3  » se guarda «Cajas 2-3»",
              r.status_code == 200 and _bulto_en_bd(r.json()["id"]) == "Cajas 2-3",
              (r.status_code, _bulto_en_bd(r.json()["id"]) if r.status_code == 200 else r.text[:200]))

        antes = db.query(Despacho).filter(Despacho.oc_cliente_id == V.oc_id).count()
        r = _crear(V.oc_id, V.item_id, bulto_numero="X" * 51)
        db.expire_all()
        despues = db.query(Despacho).filter(Despacho.oc_cliente_id == V.oc_id).count()
        check("a6 crear con 51 chars → 400 con mensaje claro (no DataError)",
              r.status_code == 400 and "50 caracteres" in r.text,
              (r.status_code, r.text[:200]))
        check("a7 …y NO quedó despacho fantasma (mismo conteo en la OC)",
              antes == despues, (antes, despues))

        # ── §B · EDITAR: cabecera viva en preparación Y despachado ───────────────
        r = client.put(f"/api/despachos/{d1}", json={"bulto_numero": "CAJA 3"})
        check("b1 PUT en en_preparacion cambia el bulto → 200 y GET refleja",
              r.status_code == 200
              and client.get(f"/api/despachos/{d1}").json().get("bulto_numero") == "CAJA 3",
              (r.status_code, r.text[:200]))

        r = client.put(f"/api/despachos/{d1}", json={"transportista": f"{MARK} Courier"})
        check("b2 tri-estado exclude_unset: un PUT que NO menciona bulto no lo toca",
              r.status_code == 200 and _bulto_en_bd(d1) == "CAJA 3",
              (r.status_code, _bulto_en_bd(d1)))

        _set_estado(d1, "despachado")
        r = client.put(f"/api/despachos/{d1}", json={"bulto_numero": "REEMPACADO 7"})
        check("b3 PUT en despachado también edita (cabecera post-cierre, gemelo de "
              "expedición)", r.status_code == 200 and _bulto_en_bd(d1) == "REEMPACADO 7",
              (r.status_code, r.text[:200]))

        r = client.put(f"/api/despachos/{d1}", json={"bulto_numero": "Y" * 51})
        check("b4 PUT con 51 chars → 400 «no puede superar 50 caracteres» y el valor "
              "anterior INTACTO",
              r.status_code == 400 and "El N° de bulto no puede superar 50 caracteres" in r.text
              and _bulto_en_bd(d1) == "REEMPACADO 7",
              (r.status_code, r.text[:200], _bulto_en_bd(d1)))

        r = client.put(f"/api/despachos/{d1}", json={"bulto_numero": ""})
        check("b5 ★ PUT con \"\" → NULL en BD (borrar el rótulo)",
              r.status_code == 200 and _bulto_en_bd(d1) is None,
              (r.status_code, repr(_bulto_en_bd(d1))))

        client.put(f"/api/despachos/{d1}", json={"bulto_numero": "B9"})
        r = client.put(f"/api/despachos/{d1}", json={"bulto_numero": "   "})
        check("b6 ★ PUT con «   » (espacios) → NULL también",
              r.status_code == 200 and _bulto_en_bd(d1) is None,
              (r.status_code, repr(_bulto_en_bd(d1))))

        client.put(f"/api/despachos/{d1}", json={"bulto_numero": "B7"})
        r = client.put(f"/api/despachos/{d1}", json={"bulto_numero": None})
        check("b7 ★ PUT con null EXPLÍCITO borra el bulto (3ª pata del tri-estado: "
              "no mandar=no tocar y \"\"→NULL ya estaban; null=borrar faltaba)",
              r.status_code == 200 and _bulto_en_bd(d1) is None,
              (r.status_code, repr(_bulto_en_bd(d1)) if r.status_code == 200 else r.text[:200]))

        exacto50 = f"{MARK}-" + "X" * (50 - len(MARK) - 1)  # marcado Y de largo 50 exacto
        r = client.put(f"/api/despachos/{d1}", json={"bulto_numero": exacto50})
        check("b8 ★ EXACTAMENTE 50 chars pasa y se relee ÍNTEGRO (el borde bueno del "
              "tope; solo estaba probado el 51→400)",
              r.status_code == 200 and len(exacto50) == 50 and _bulto_en_bd(d1) == exacto50,
              (r.status_code, _bulto_en_bd(d1) if r.status_code == 200 else r.text[:200]))

        client.put(f"/api/despachos/{d1}", json={"bulto_numero": "B-FINAL"})

        # ── §C · DETALLE DE OC == GET por id (dos serializers, un dato) ──────────
        det = client.get(f"/api/despachos/oc-clientes/{V.oc_id}").json()
        filas = {dd["id"]: dd for dd in det.get("despachos", [])}
        check("c1 el detalle de la OC trae bulto_numero en cada fila de despacho",
              filas and all("bulto_numero" in dd for dd in filas.values()),
              list(filas.values())[:1])
        iguales = all(
            dd.get("bulto_numero") == client.get(f"/api/despachos/{did}").json().get("bulto_numero")
            for did, dd in filas.items()
        )
        check("c2 ★ SONDA de igualdad detalle==porId en TODOS los despachos "
              "(incluye el null de a3)", iguales,
              {did: dd.get("bulto_numero") for did, dd in filas.items()})
        check("c2b …y la fila con bulto dice «B-FINAL» y la sin bulto dice None",
              filas.get(d1, {}).get("bulto_numero") == "B-FINAL"
              and filas.get(d2, {}).get("bulto_numero") is None,
              (filas.get(d1, {}).get("bulto_numero"), filas.get(d2, {}).get("bulto_numero")))

        # ── §D · ANULADO: PUT rebota, el histórico se conserva ───────────────────
        r = _crear(V.oc_id, V.item_id, bulto_numero="BULTO-ANULADO")
        d_anu = r.json()["id"]
        r = client.delete(f"/api/despachos/{d_anu}")
        check("d1 anular (DELETE) el despacho en preparación → 200",
              r.status_code == 200, (r.status_code, r.text[:200]))
        r = client.put(f"/api/despachos/{d_anu}", json={"bulto_numero": "NUEVO"})
        check("d2 PUT sobre anulado rebota 400 (guard existente, sin duplicar)",
              r.status_code == 400 and "anulado" in r.text, (r.status_code, r.text[:200]))
        g = client.get(f"/api/despachos/{d_anu}").json()
        check("d3 …pero el GET aún muestra su bulto (rotulado histórico conservado)",
              g.get("estado") == "anulado" and g.get("bulto_numero") == "BULTO-ANULADO",
              (g.get("estado"), g.get("bulto_numero")))

    finally:
        db.close()
        _limpiar()
        print("Cleanup OK")
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_bulto_despacho():
    run()


# ── §E · Contrato del COLAPSADO (mitad backend del espejo con picking.ts) ────────

def run_colapsado():
    """_colapsar (routers/despachos.py) es la mitad backend del contrato con
    frontend-src/src/picking/picking.ts: picking.ts:23-29 se declara su espejo
    EXACTO (`re.sub(r"[-\\s]", "", tok).upper()`) y documenta explícitamente que
    los PUNTOS NO se eliminan. El frontend no tiene runner de tests, así que esta
    sonda congela la regla al menos de este lado: si alguien «mejora» _colapsar
    botando puntos (o dejando de botar espacios), cae acá y obliga a mirar el
    espejo TS antes de tocar. Lógica pura: no necesita BD."""
    from routers.despachos import _colapsar
    fails = []

    def check2(name, cond, extra=""):
        print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
        if not cond:
            fails.append(name)

    check2("e1 guiones fuera: colapsar(7T-1997) == colapsar(7T1997) == «7T1997»",
           _colapsar("7T-1997") == _colapsar("7T1997") == "7T1997",
           (_colapsar("7T-1997"), _colapsar("7T1997")))
    check2("e2 espacios fuera (bordes E internos)",
           _colapsar(" 7T 1997 ") == "7T1997", _colapsar(" 7T 1997 "))
    check2("e3 upper: minúsculas suben", _colapsar("7t-1997x") == "7T1997X",
           _colapsar("7t-1997x"))
    check2("e4 ★ los PUNTOS NO se eliminan (1R.0716 ≠ 1R0716, tal cual lo "
           "documenta picking.ts)",
           _colapsar("1R.0716") != _colapsar("1R0716")
           and _colapsar("1R.0716") == "1R.0716",
           (_colapsar("1R.0716"), _colapsar("1R0716")))
    return fails


def test_colapsado_contrato_picking():
    assert run_colapsado() == [], run_colapsado()


if __name__ == "__main__":
    run()
    assert run_colapsado() == [], run_colapsado()
