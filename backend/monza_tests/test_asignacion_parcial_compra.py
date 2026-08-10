"""Asignación PARCIAL a OC de proveedor — espejo MonzaParts del commit GA 1d2a069.

EL ENCARGO DEL DUEÑO: de una línea 'por_comprar' de 3 unidades, comprar 1 o 2 a un
proveedor y que el remanente quede en el panel, asignable a otro. Es el MISMO split
de preparar-parcial (la regla de oro vive en _clonar_item_remanente) con una
diferencia deliberada: el remanente NO hereda `oc_proveedor_id` (copiar_oc=False) —
si lo heredara nacería "comprado a X" y el panel jamás lo ofrecería a otro proveedor.

Lo que esta suite certifica, en orden de importancia:

  1. EL INVARIANTE DE PLATA (secciones 1 y 9): Σ cantidad y Σ subtotal_clp de las
     hermanas == línea original, cabecera BYTE-idéntica. La sección 9 inyecta el
     vicio de GA (clon con subtotal completo) y exige que el MISMO predicado dé ROJO.
  2. EL REMANENTE VUELVE AL PANEL (1-3): 'por_comprar' y SIN OC, re-asignable a un
     SEGUNDO proveedor (3 → 1+1+1 en dos OCs distintas, probado e2e).
  3. EL CONTRATO SIN LOS 4 VICIOS del patrón viejo (sección 5): 0/negativo/exceso →
     400 explícito (jamás clamp ni falsy), no-entero → 422 (pydantic), id
     inexistente → 404, 'comprado' → 409, vínculo sucio → 409 fail-closed — y tras
     CADA rechazo la base queda intacta (ni línea movida ni OC nacida).
  4. CONCURRENCIA con hilos REALES y FOTO PLANA de ids (7): sin el lock, dos
     asignaciones simultáneas leen cantidad=3 las dos y SE INVENTAN UNIDADES
     (sonda 9.3 lo reproduce quitando el FOR UPDATE).

ADAPTACIONES AL MODELO MONZA (verificadas en el reconocimiento, 2026-08-10):
· Sin tabla de vínculo (OcProveedorItem no existe): la FK vive en la línea. No hay
  LOCK 2 de vínculos; el vínculo sucio se rechaza sobre la fila ya lockeada.
· El guard de restaurar_version de GA NO aplica: _registrar_reversion marca la
  versión REVERTIDA y ajusta LTV sin reponer cantidades por id, y el re-cierre
  recorre cot.items VIVOS (hermanas incluidas) moviendo solo estado_linea.
· El guard del editor de GA NO tiene equivalente: no existe endpoint que edite o
  borre una MonzaCotizacionItem post-venta (los editores de leads tocan
  MonzaLeadItem, pre-cotización).

ARNÉS (los detalles SON el punto, ver test_preparar_parcial_monza.py):
· auth realista (lección G13): el override lee la BD en la MISMA sesión del request.
· verificaciones con CONEXIÓN NUEVA y SQL crudo (el read view del test miente).
· foto PLANA de ids antes de los hilos: jamás objetos ORM cruzando threads.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_asignacion_parcial_compra.py -q
       o:   ./venv/bin/python monza_tests/test_asignacion_parcial_compra.py
"""
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from database import SessionLocal, engine, Base, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
    MonzaLog, MonzaNotificacion,
)

import monza_router_abastecimiento as abast  # noqa: E402
from monza_router_abastecimiento import (  # noqa: E402
    router as abastecimiento_router, _clonar_item_remanente, _partir_linea,
    _lockear_items_para_comprar, _comprar_parcial_tx,
)

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "__TEST_ASIGP__"
EMAIL = f"{MARK}@test.invalid"
PRECIO = 12345.0        # 1×p + 2×p == 3×p exacto en float
RONDAS = 5              # la carrera es probabilística

app = FastAPI()
app.include_router(abastecimiento_router)


def _cu(db: Session = Depends(get_db)):
    # Auth realista (lección G13): lectura en la MISMA sesión del request para que
    # el read view nazca ANTES del with_for_update, como en producción.
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=None, email=EMAIL, empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


# ═══════════════ Lecturas de VERIFICACIÓN: conexión nueva, SQL crudo ═════════════

def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _lineas(cot_id):
    return [
        {"id": r[0], "cantidad": r[1], "subtotal": r[2], "precio": r[3],
         "estado": r[4], "parte": r[5], "ocp": r[6]}
        for r in _sql(
            "SELECT id,cantidad,subtotal_clp,precio_unitario_clp,estado_linea,"
            "numero_parte,oc_proveedor_id FROM monza_cotizacion_items "
            "WHERE cotizacion_id=:c ORDER BY id", c=cot_id)
    ]


def _cabecera(cot_id):
    r = _sql("SELECT total_neto,iva_monto,total_bruto FROM monza_cotizaciones "
             "WHERE id=:i", i=cot_id)[0]
    return (r[0], r[1], r[2])


def _hermanas(cot_id, parte):
    return [l for l in _lineas(cot_id) if l["parte"] == parte]


def _invariante_plata(cot_id, parte, cant_original, sub_original, cabecera_antes):
    """EL PREDICADO MÁS IMPORTANTE. Lo usan el check real (1) y la sonda de poder
    discriminante (9), que inyecta el vicio de GA y exige que devuelva False."""
    hs = _hermanas(cot_id, parte)
    suma_cant = sum(h["cantidad"] or 0 for h in hs)
    suma_sub = sum(float(h["subtotal"] or 0) for h in hs)
    cab = _cabecera(cot_id)
    ok = (
        suma_cant == cant_original
        and suma_sub == float(sub_original)          # exacto, no "casi": es plata
        and cab == cabecera_antes
    )
    return ok, (f"Σcant={suma_cant}/{cant_original} Σsub={suma_sub}/{sub_original} "
                f"cabecera={cab}/{cabecera_antes} filas={hs}")


def _estado(item_id):
    r = _sql("SELECT estado_linea,cantidad,oc_proveedor_id "
             "FROM monza_cotizacion_items WHERE id=:i", i=item_id)
    return (r[0][0], r[0][1], r[0][2]) if r else (None, None, None)


def _n_ocps():
    """Cuántas OCs del test existen (ancla: proveedor_nombre con MARK). Se usa para
    verificar que un rechazo NO dejó una OC nacida a medias."""
    return _sql("SELECT COUNT(*) FROM monza_oc_proveedor WHERE proveedor_nombre "
                "LIKE :p", p=f"{MARK}%")[0][0]


# ═══════════════════════════════ Siembra marcada ════════════════════════════════

def _venta(db, cantidades=(3,), estado_linea="por_comprar", ocp_id=None,
           precio=PRECIO, subtotal_none=False, pct_adelanto=0, adelanto_ok=0):
    suf = uuid.uuid4().hex[:6].upper()
    cli = MonzaCliente(nombre=MARK, rut="11.111.111-1")
    db.add(cli); db.flush()
    neto = 0.0 if subtotal_none else sum(c * precio for c in cantidades)
    cot = MonzaCotizacion(
        numero=f"CT-AP-{suf}", cliente_id=cli.id, estado="vendida",
        total_neto=neto, iva_monto=round(neto * 0.19, 2),
        total_bruto=round(neto * 1.19, 2), iva_pct=19,
        pct_adelanto=pct_adelanto, adelanto_verificado=adelanto_ok,
    )
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion=f"{MARK} Parte {n}",
            numero_parte=f"P-AP-{n}", marca="MONZA", procedencia="Alemania",
            calidad="original", cantidad=cant, estado_linea=estado_linea,
            costo=99.5, moneda="EUR", peso_kg=2.75, tc_aplicado=1050.25,
            tarifa_aerea=7.5, markup_pct=0.28,
            precio_unitario_clp=precio,
            subtotal_clp=(None if subtotal_none else cant * precio),
            plazo_entrega="15 dias", oc_proveedor_id=ocp_id,
        )
        db.add(it); items.append(it)
    db.commit()
    for obj in [cot] + items:
        db.refresh(obj)
    return cot, items


def _comprar(item_ids, cantidades=None, **extra):
    """POST /comprar. `cantidades is None` = body legado byte-igual (SIN el campo)."""
    body = {"item_ids": item_ids, "proveedor_nombre": f"{MARK} PROV",
            "moneda": "EUR", **extra}
    if cantidades is not None:
        body["cantidades"] = cantidades
    return client.post("/api/monza/abastecimiento/comprar", json=body)


def _en_paralelo(fn, n=2):
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


# ═══════════════════════ Limpieza TOTAL en orden FK-seguro ══════════════════════

def _limpiar(db):
    db.rollback()
    S = "fetch"
    cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
               .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
               .filter(MonzaCliente.nombre == MARK).all()]
    item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
    # Las OCs del ENDPOINT llevan numero autogenerado (OCP-YYYY-NNNN): el ancla es
    # el proveedor_nombre del body, que siempre lleva el MARK.
    ocp_ids = [r[0] for r in db.query(MonzaOcProveedor.id).filter(
        (MonzaOcProveedor.proveedor_nombre.like(f"{MARK}%"))
        | (MonzaOcProveedor.numero.like(f"{MARK}%"))).all()]
    db.query(MonzaNotificacion).filter(
        MonzaNotificacion.entidad == "oc_proveedor",
        MonzaNotificacion.entidad_id.in_(ocp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
    db.query(MonzaCotizacionItem).filter(
        MonzaCotizacionItem.id.in_(item_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacion).filter(
        MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaOcProveedor).filter(
        MonzaOcProveedor.id.in_(ocp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
    db.commit()


def _residuos():
    def _n(q, **p):
        return _sql(q, **p)[0][0]
    return {
        "clientes": _n("SELECT COUNT(*) FROM monza_clientes WHERE nombre=:m", m=MARK),
        "cotizaciones": _n("SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE 'CT-AP-%'"),
        "ocs": _n("SELECT COUNT(*) FROM monza_oc_proveedor WHERE proveedor_nombre "
                  "LIKE :p OR numero LIKE :p", p=f"{MARK}%"),
        "logs": _n("SELECT COUNT(*) FROM monza_logs WHERE user_email=:e", e=EMAIL),
    }


# ═════════════════════════════════════ RUN ══════════════════════════════════════

def run():
    db = SessionLocal()
    try:
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 1 · EL CASO DEL DUEÑO: de 3, comprar 1 — y el INVARIANTE DE PLATA.
        # ══════════════════════════════════════════════════════════════════════
        cot, (it,) = _venta(db, (3,))
        iid = it.id
        cab_antes = _cabecera(cot.id)
        sub_antes = _lineas(cot.id)[0]["subtotal"]
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        check("1.1 comprar 1 de 3 → 200", r.status_code == 200, r.text)
        j = r.json() if r.status_code == 200 else {}
        check("1.1 contrato de respuesta: ok, ocp_id, numero, items=1, partidos=1",
              j.get("ok") is True and isinstance(j.get("ocp_id"), int)
              and j.get("items") == 1 and j.get("partidos") == 1, j)
        rem = (j.get("remanentes") or [{}])[0]
        check("1.1 remanentes[0] = {item_id, remanente_item_id, comprado 1, "
              "pendiente 2, original 3}",
              rem.get("item_id") == iid and rem.get("comprado") == 1
              and rem.get("pendiente") == 2 and rem.get("original") == 3
              and isinstance(rem.get("remanente_item_id"), int), rem)
        ocp_id, rem_id = j.get("ocp_id"), rem.get("remanente_item_id")

        ok, det = _invariante_plata(cot.id, "P-AP-1", 3, sub_antes, cab_antes)
        check("1.2 ★ INVARIANTE DE PLATA: Σcantidad==3, Σsubtotal==original, "
              "cabecera BYTE-IDÉNTICA", ok, det)
        check("1.3 el trozo asignado quedó 'comprado' con SU OC",
              _estado(iid) == ("comprado", 1, ocp_id), _estado(iid))
        check("1.4 ★ el remanente quedó 'por_comprar' y SIN OC (vuelve al panel, "
              "asignable a otro proveedor)",
              _estado(rem_id) == ("por_comprar", 2, None), _estado(rem_id))
        hs = _hermanas(cot.id, "P-AP-1")
        check("1.5 precio_unitario_clp IDÉNTICO en las dos hermanas (no se prorratea)",
              len(hs) == 2 and all(h["precio"] == PRECIO for h in hs), hs)
        check("1.6 el remanente aparece en el panel /por-comprar y el asignado NO",
              (lambda ids: rem_id in ids and iid not in ids)(
                  {x["id"] for x in client.get(
                      "/api/monza/abastecimiento/por-comprar").json()}),
              )
        # Bitácora del split: «asignadas 1 de 3, remanente 2» con los ids.
        logs = _sql("SELECT detalle FROM monza_logs WHERE user_email=:e "
                    "AND entidad='oc_proveedor' ORDER BY id DESC", e=EMAIL)
        check("1.7 bitácora: «asignadas 1 de 3, remanente 2» y los dos ids",
              logs and "asignadas 1 de 3, remanente 2" in logs[0][0]
              and str(iid) in logs[0][0] and str(rem_id) in logs[0][0],
              logs[0][0] if logs else "sin log")
        _limpiar(db)

        # De 3, comprar 2 (la otra mitad del encargo literal).
        cot, (it,) = _venta(db, (3,))
        iid = it.id
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 2}])
        j = r.json()
        check("1.8 comprar 2 de 3 → comprado(2) + remanente por_comprar(1) sin OC",
              r.status_code == 200
              and _estado(iid) == ("comprado", 2, j["ocp_id"])
              and _estado(j["remanentes"][0]["remanente_item_id"])
              == ("por_comprar", 1, None), r.text[:200])
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 2 · DOBLE SPLIT: 3 → 1+1+1 en DOS OCs distintas; la tercera unidad
        #     sigue en el panel. (El remanente a una SEGUNDA OC, e2e.)
        # ══════════════════════════════════════════════════════════════════════
        cot, (it,) = _venta(db, (3,))
        iid = it.id
        cab_antes = _cabecera(cot.id)
        sub_antes = _lineas(cot.id)[0]["subtotal"]
        r1 = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        rem1 = r1.json()["remanentes"][0]["remanente_item_id"]
        r2 = _comprar([rem1], cantidades=[{"item_id": rem1, "cantidad": 1}],
                      proveedor_nombre=f"{MARK} PROV 2")
        check("2.1 el remanente se puede asignar a un SEGUNDO proveedor",
              r2.status_code == 200, r2.text[:200])
        rem2 = r2.json()["remanentes"][0]["remanente_item_id"]
        ocp1, ocp2 = r1.json()["ocp_id"], r2.json()["ocp_id"]
        check("2.2 las dos OCs son DISTINTAS y cada trozo cuelga de la suya",
              ocp1 != ocp2 and _estado(iid) == ("comprado", 1, ocp1)
              and _estado(rem1) == ("comprado", 1, ocp2),
              f"{_estado(iid)} {_estado(rem1)}")
        check("2.3 la tercera unidad sigue en el panel, sin OC",
              _estado(rem2) == ("por_comprar", 1, None), _estado(rem2))
        ok, det = _invariante_plata(cot.id, "P-AP-1", 3, sub_antes, cab_antes)
        check("2.4 ★ tras el DOBLE split la plata sigue cuadrada (3 hermanas)",
              ok and len(_hermanas(cot.id, "P-AP-1")) == 3, det)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 3 · LÍNEAS MIXTAS en un solo pedido: una parcial + una entera (sin
        #     entrada en `cantidades` = entera, mismo sentinela que None).
        # ══════════════════════════════════════════════════════════════════════
        cot, (a, b) = _venta(db, (3, 5))
        aid, bid = a.id, b.id
        r = _comprar([aid, bid], cantidades=[{"item_id": aid, "cantidad": 2}])
        j = r.json()
        check("3.1 mixto: items=2, partidos=1 (solo la línea con cantidad se parte)",
              r.status_code == 200 and j.get("items") == 2 and j.get("partidos") == 1,
              r.text[:200])
        check("3.2 la entera se asignó completa (5, sin clon) a la MISMA OC",
              _estado(bid) == ("comprado", 5, j["ocp_id"])
              and len(_hermanas(cot.id, "P-AP-2")) == 1, _estado(bid))
        # (el caso «None explícito = entera» lo cubre 3.4 con el campo en el body:
        #  una check(…, True) acá era una tautología que inflaba el conteo — H1 de la
        #  revisión del espejo)
        _limpiar(db)
        cot, (a,) = _venta(db, (3,))
        aid = a.id
        r = _comprar([aid], cantidades=[{"item_id": aid, "cantidad": None}])
        check("3.4 cantidad=None → línea entera, partidos=0, sin clon",
              r.status_code == 200 and r.json()["partidos"] == 0
              and _estado(aid)[0:2] == ("comprado", 3)
              and len(_lineas(cot.id)) == 1, r.text[:200])
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 4 · LOS DOS CAMINOS INVISIBLES: sin `cantidades` el body viejo corre el
        #     código legado byte-igual; con cantidades completas el resultado es
        #     EQUIVALENTE (sin clon).
        # ══════════════════════════════════════════════════════════════════════
        cot, (a, b) = _venta(db, (3, 5))
        aid, bid = a.id, b.id
        r = _comprar([aid, bid])   # SIN el campo `cantidades`: vía legada
        j = r.json()
        check("4.1 vía legada intacta: {ok, ocp_id, numero, items} sin claves nuevas",
              r.status_code == 200 and set(j) == {"ok", "ocp_id", "numero", "items"}
              and j["items"] == 2, j)
        check("4.2 líneas enteras compradas, cero clones",
              _estado(aid)[0:2] == ("comprado", 3) and _estado(bid)[0:2] == ("comprado", 5)
              and len(_lineas(cot.id)) == 2, _lineas(cot.id))
        _limpiar(db)
        cot, (a,) = _venta(db, (3,))
        aid = a.id
        r = _comprar([aid], cantidades=[{"item_id": aid, "cantidad": 3}])
        check("4.3 cantidad == total → equivalente a la vía legada (partidos=0, "
              "sin clon, comprado 3)",
              r.status_code == 200 and r.json()["partidos"] == 0
              and _estado(aid)[0:2] == ("comprado", 3) and len(_lineas(cot.id)) == 1,
              r.text[:200])
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 5 · EL CONTRATO: cada vicio del patrón viejo es un rechazo EXPLÍCITO y
        #     tras CADA rechazo la base queda intacta (línea sin mover, CERO OCs).
        # ══════════════════════════════════════════════════════════════════════
        cot, (it,) = _venta(db, (3,))
        iid = it.id

        def _rechazo(nombre, resp, codigo, frase=""):
            check(f"5.{nombre} → {codigo}",
                  resp.status_code == codigo
                  and (frase.lower() in resp.text.lower() if frase else True),
                  f"{resp.status_code} {resp.text[:200]}")
            check(f"5.{nombre} y NADA se movió (línea intacta, cero OCs nacidas)",
                  _estado(iid) == ("por_comprar", 3, None) and _n_ocps() == 0,
                  f"{_estado(iid)} ocps={_n_ocps()}")

        _rechazo("1 cantidad 0 (jamás el falsy que asignaba todo)",
                 _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 0}]),
                 400, "mayor que 0")
        _rechazo("2 cantidad negativa",
                 _comprar([iid], cantidades=[{"item_id": iid, "cantidad": -1}]),
                 400, "mayor que 0")
        _rechazo("3 exceso (4 de 3, jamás clamp silencioso)",
                 _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 4}]),
                 400, "excede")
        _rechazo("4 no-entero (1.5) → 422 de pydantic (la columna es Integer)",
                 _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1.5}]),
                 422)
        _rechazo("5 id inexistente → 404 explícito (jamás continue mudo)",
                 _comprar([iid, 99999999],
                          cantidades=[{"item_id": iid, "cantidad": 1}]),
                 404, "inexistente")
        _rechazo("6 id repetido en item_ids",
                 _comprar([iid, iid], cantidades=[{"item_id": iid, "cantidad": 1}]),
                 400, "repetidos")
        _rechazo("7 id repetido en cantidades",
                 _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1},
                                             {"item_id": iid, "cantidad": 1}]),
                 400, "repetidos")
        _rechazo("8 cantidades con id fuera de la selección",
                 _comprar([iid], cantidades=[{"item_id": 424242, "cantidad": 1}]),
                 400, "fuera de la selección")
        _rechazo("9 item_ids vacío",
                 _comprar([], cantidades=[]), 400, "Sin items")
        _rechazo("10 tipo_origen inválido",
                 _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}],
                          tipo_origen="nacionel"),
                 400, "tipo_origen")
        _limpiar(db)

        # 'comprado' → 409 (la reasignación parcial no existe todavía).
        cot, (it,) = _venta(db, (3,), estado_linea="comprado")
        iid = it.id
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        check("5.11 línea 'comprado' → 409 y el mensaje lo dice",
              r.status_code == 409 and "reasignación parcial" in r.text,
              f"{r.status_code} {r.text[:200]}")
        check("5.11 y nada se movió", _estado(iid)[0:2] == ("comprado", 3)
              and _n_ocps() == 0)
        _limpiar(db)

        # Otro estado ('preparado') → 400 con el texto del split genérico.
        cot, (it,) = _venta(db, (3,), estado_linea="preparado")
        iid = it.id
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        check("5.12 línea 'preparado' → 400 «no están en estado 'por_comprar'»",
              r.status_code == 400 and "por_comprar" in r.text, r.text[:200])
        _limpiar(db)

        # Vínculo SUCIO ('por_comprar' con oc_proveedor_id) → 409 fail-closed.
        ocp_sucia = MonzaOcProveedor(numero=f"{MARK}-OCP-SUCIA",
                                     proveedor_nombre=f"{MARK} PROV", moneda="EUR",
                                     tipo_origen="internacional", estado="emitida")
        db.add(ocp_sucia); db.commit(); db.refresh(ocp_sucia)
        cot, (it,) = _venta(db, (3,), ocp_id=ocp_sucia.id)
        iid = it.id
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        check("5.13 'por_comprar' con vínculo sucio → 409 fail-closed (dato "
              "inconsistente no puede duplicar plata)",
              r.status_code == 409 and "vínculo" in r.text, r.text[:200])
        check("5.13 y la línea no se tocó", _estado(iid) == ("por_comprar", 3, ocp_sucia.id))
        _limpiar(db)

        # Cortafuego de ADELANTO también en la vía parcial.
        cot, (it,) = _venta(db, (3,), pct_adelanto=50, adelanto_ok=0)
        iid = it.id
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        check("5.14 adelanto sin verificar → 409 también en la vía parcial",
              r.status_code == 409 and "Adelanto no verificado" in r.text,
              f"{r.status_code} {r.text[:200]}")
        check("5.14 y nada se movió", _estado(iid) == ("por_comprar", 3, None)
              and _n_ocps() == 0)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 6 · GUARD DE COHERENCIA de la foto: un subtotal que no cuadra con
        #     cantidad × precio MOVERÍA plata al partirse → 400. Asignar la línea
        #     ENTERA con esa deuda legada sigue permitido (no recalcula nada).
        # ══════════════════════════════════════════════════════════════════════
        cot, (it,) = _venta(db, (3,))
        iid = it.id
        db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == iid).update(
            {"subtotal_clp": 999999.0})
        db.commit()
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        check("6.1 subtotal divergente → 400 (partirla movería el monto de la venta)",
              r.status_code == 400 and "no" in r.text.lower()
              and "cuadra" in r.text.lower(), r.text[:200])
        check("6.1 y NADA se movió (sigue 1 línea de 3, cero OCs)",
              _estado(iid)[0:2] == ("por_comprar", 3) and len(_lineas(cot.id)) == 1
              and _n_ocps() == 0, _lineas(cot.id))
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 3}])
        check("6.2 la MISMA línea divergente asignada ENTERA sí pasa (no se "
              "recalcula plata; la deuda legada no bloquea el flujo)",
              r.status_code == 200 and r.json()["partidos"] == 0, r.text[:200])
        _limpiar(db)

        # Subtotal NULL: el None se preserva en las DOS mitades (no se inventa monto).
        cot, (it,) = _venta(db, (3,), subtotal_none=True)
        iid = it.id
        cab_antes = _cabecera(cot.id)
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        hs = _hermanas(cot.id, "P-AP-1")
        check("6.3 subtotal NULL se parte y el None se preserva en ambas mitades",
              r.status_code == 200 and len(hs) == 2
              and all(h["subtotal"] is None for h in hs)
              and sum(h["cantidad"] for h in hs) == 3
              and _cabecera(cot.id) == cab_antes, hs)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 7 · CONCURRENCIA con hilos REALES y FOTO PLANA de ids. Sin el lock, dos
        #     asignaciones simultáneas leen cantidad=3 las dos, cada una clona su
        #     remanente y SE INVENTAN UNIDADES (la sonda 9.3 lo demuestra).
        # ══════════════════════════════════════════════════════════════════════
        for r_i in range(RONDAS):
            _limpiar(db)
            cot, (it,) = _venta(db, (3,))
            iid = it.id                       # FOTO PLANA: jamás ORM en hilos
            cot_id = cot.id
            cab_antes = _cabecera(cot_id)
            sub_antes = _lineas(cot_id)[0]["subtotal"]
            res = _en_paralelo(lambda i: _comprar(
                [iid], cantidades=[{"item_id": iid, "cantidad": 2}]).status_code, n=2)
            ok, det = _invariante_plata(cot_id, "P-AP-1", 3, sub_antes, cab_antes)
            check(f"7.{r_i} ★ dos asignaciones simultáneas: Σ cantidad y Σ subtotal "
                  f"CONSERVADOS (no se inventan unidades)", ok, f"http={res} {det}")
            check(f"7.{r_i} exactamente UNA entra; la otra rebota explícito "
                  f"(409 del guard o 400)",
                  res.count(200) == 1 and all(s in (200, 400, 409) for s in res), res)
            check(f"7.{r_i} y nació UNA sola OC", _n_ocps() == 1, _n_ocps())
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 8 · EL CONTRATO DE copiar_oc, ejercido DIRECTO. En el endpoint es
        #     cinturón de segunda capa (el 409 del vínculo sucio corre antes y el
        #     vínculo se escribe DESPUÉS del split), así que su superficie real
        #     son los callers directos del split: se prueba ahí, con contraste.
        # ══════════════════════════════════════════════════════════════════════
        ocp_x = MonzaOcProveedor(numero=f"{MARK}-OCP-X", proveedor_nombre=f"{MARK} PROV",
                                 moneda="EUR", tipo_origen="internacional",
                                 estado="emitida")
        db.add(ocp_x); db.commit(); db.refresh(ocp_x)
        cot, (it,) = _venta(db, (3,), estado_linea="comprado", ocp_id=ocp_x.id)
        db3 = SessionLocal()
        try:
            it3 = db3.query(MonzaCotizacionItem).filter(
                MonzaCotizacionItem.id == it.id).first()
            clon = _partir_linea(db3, it3, 1, "por_comprar", copiar_oc=False)
            check("8.1 copiar_oc=False: el clon nace SIN oc_proveedor_id aunque la "
                  "original lo tenga", clon is not None and clon.oc_proveedor_id is None
                  and it3.oc_proveedor_id == ocp_x.id,
                  f"clon.ocp={clon.oc_proveedor_id if clon else '?'}")
            db3.rollback()
            it3 = db3.query(MonzaCotizacionItem).filter(
                MonzaCotizacionItem.id == it.id).populate_existing().first()
            clon = _partir_linea(db3, it3, 1, "comprado")
            check("8.2 CONTRASTE: por la vía posicional histórica (default True) el "
                  "clon SÍ hereda la OC — los 3 callers viejos quedan intactos",
                  clon is not None and clon.oc_proveedor_id == ocp_x.id,
                  f"clon.ocp={clon.oc_proveedor_id if clon else '?'}")
            db3.rollback()
        finally:
            db3.close()
        check("8.3 _clonar_item_remanente sigue siendo el ÚNICO dueño de la copia",
              abast._clonar_item_remanente is _clonar_item_remanente)
        check("8.4 _lockear_items_para_comprar y _comprar_parcial_tx importables "
              "(contrato público del espejo)",
              callable(_lockear_items_para_comprar) and callable(_comprar_parcial_tx))
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 9 · PODER DISCRIMINANTE: cada sonda inyecta el vicio y exige que el
        #     MISMO predicado que da verde con el código real dé ROJO.
        # ══════════════════════════════════════════════════════════════════════

        # 9.1-9.2 · Vicio de GA: el clon hereda el subtotal COMPLETO.
        original_clonar = abast._clonar_item_remanente

        def _clonar_vicioso(db_, it_, remanente, estado, copiar_oc=True):
            clon = original_clonar(db_, it_, remanente, estado, copiar_oc)
            clon.subtotal_clp = it_.subtotal_clp
            db_.flush()
            return clon

        abast._clonar_item_remanente = _clonar_vicioso
        try:
            cot, (it,) = _venta(db, (3,))
            iid = it.id
            cab_antes = _cabecera(cot.id)
            sub_antes = _lineas(cot.id)[0]["subtotal"]
            r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
            ok, det = _invariante_plata(cot.id, "P-AP-1", 3, sub_antes, cab_antes)
            check("9.1 ★ SONDA subtotal: con el vicio inyectado el predicado del "
                  "check 1.2 da ROJO (la suite no es complaciente)",
                  r.status_code == 200 and not ok,
                  f"http={r.status_code}; el invariante NO detectó la plata duplicada: {det}")
        finally:
            abast._clonar_item_remanente = original_clonar
            _limpiar(db)
        cot, (it,) = _venta(db, (3,))
        iid = it.id
        cab_antes = _cabecera(cot.id)
        sub_antes = _lineas(cot.id)[0]["subtotal"]
        r = _comprar([iid], cantidades=[{"item_id": iid, "cantidad": 1}])
        ok, det = _invariante_plata(cot.id, "P-AP-1", 3, sub_antes, cab_antes)
        check("9.2 restaurado el código real, el mismo predicado vuelve a VERDE",
              r.status_code == 200 and ok, det)
        _limpiar(db)

        # 9.3 · Vicio del LOCK: el mismo lockeo SIN with_for_update. Con él quitado,
        #      las dos asignaciones entran y se inventan unidades.
        from fastapi import HTTPException as _HTTPExc
        original_lock = abast._lockear_items_para_comprar

        def _lock_sin_for_update(db_, ids):
            items = (
                db_.query(MonzaCotizacionItem)
                .filter(MonzaCotizacionItem.id.in_(ids))
                .order_by(MonzaCotizacionItem.id.asc())
                .populate_existing()
                .all()
            )
            if len(items) != len(set(ids)):
                raise _HTTPExc(404, "Ítem(s) inexistente(s)")
            if [i for i in items if (i.estado_linea or "") == "comprado"]:
                raise _HTTPExc(409, "ya comprados")
            if [i for i in items if (i.estado_linea or "") != "por_comprar"]:
                raise _HTTPExc(400, "no están en estado 'por_comprar'")
            if [i for i in items if i.oc_proveedor_id]:
                raise _HTTPExc(409, "vínculo")
            return {i.id: i for i in items}

        abast._lockear_items_para_comprar = _lock_sin_for_update
        inventadas = 0
        try:
            for r_i in range(6):
                _limpiar(db)
                cot, (it,) = _venta(db, (3,))
                iid = it.id
                cot_id = cot.id
                cab_antes = _cabecera(cot_id)
                sub_antes = _lineas(cot_id)[0]["subtotal"]
                _en_paralelo(lambda i: _comprar(
                    [iid], cantidades=[{"item_id": iid, "cantidad": 2}]).status_code,
                    n=2)
                ok, _d = _invariante_plata(cot_id, "P-AP-1", 3, sub_antes, cab_antes)
                if not ok:
                    inventadas += 1
        finally:
            abast._lockear_items_para_comprar = original_lock
            _limpiar(db)
        check(f"9.3 ★ SONDA del LOCK: sin FOR UPDATE se inventan unidades en "
              f"{inventadas}/6 rondas (con el lock real: 0, sección 7)",
              inventadas > 0,
              "el arnés no logró reproducir la carrera: la sección 7 podría ser "
              "complaciente")
        check("9.4 el módulo quedó restaurado tras las dos sondas",
              abast._lockear_items_para_comprar is original_lock
              and abast._clonar_item_remanente is original_clonar)

        # ═══ §11 · SPLIT de una compra NACIONAL (H2 de la revisión del espejo) ═══
        # El carácter nacional vive en la OC, no en la línea: la hermana COMPRADA lo
        # hereda por su oc_proveedor_id (y el guard anti-embarque debe reconocerla),
        # y el remanente nace NEUTRO — sin OC — reasignable a un flujo internacional.
        # Sin esta sección, un refactor del JOIN de _rechazar_items_nacionales podía
        # colar un ítem nacional al pipeline de embarque sin que nada se pusiera rojo.
        _limpiar(db)
        cot, (a,) = _venta(db, (3,))
        aid = a.id
        r = _comprar([aid], cantidades=[{"item_id": aid, "cantidad": 1}],
                     tipo_origen="nacional")
        check("11.1 split 1 de 3 en una OC NACIONAL → 200", r.status_code == 200,
              r.text[:200])
        rem11 = (r.json().get("remanentes") or [{}])[0].get("remanente_item_id")
        r = client.post("/api/monza/abastecimiento/preparar",
                        json={"item_ids": [aid]})
        check("11.2 la hermana COMPRADA es nacional: preparar/embarcar la rechaza "
              "(el JOIN por oc_proveedor_id la reconoce)", r.status_code == 400,
              (r.status_code, r.text[:160]))
        r = _comprar([rem11], cantidades=None, tipo_origen="internacional")
        check("11.3 el remanente nació NEUTRO: se compra entero en una OC "
              "INTERNACIONAL → 200", r.status_code == 200, r.text[:200])
        r = client.post("/api/monza/abastecimiento/preparar",
                        json={"item_ids": [rem11]})
        check("11.4 …y esa hermana internacional SÍ entra al pipeline de embarque",
              r.status_code == 200, (r.status_code, r.text[:160]))
        est11 = _estado(rem11)
        check("11.5 estados finales: comprada nacional intacta, remanente preparado",
              _estado(aid)[0] == "comprado" and est11[0] == "preparado",
              (_estado(aid), est11))

    finally:
        _limpiar(db)
        res = _residuos()
        db.close()
        print(f"Residuos: {res}")
    check("10 CERO residuos en la BD con el MARK (sesión nueva, SQL crudo)",
          all(v == 0 for v in res.values()), res)

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_asignacion_parcial_compra():
    """Wrapper pytest de una línea: sin él la suite sería INVISIBLE al gate."""
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
