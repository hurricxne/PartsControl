"""Alerta diaria «plazo crítico» de Grupo AM (regla B7 de `scheduler.py`).

POR QUÉ EXISTE ESTA SUITE. El bloque B7 del barrido de las 06:00 estaba MUERTO desde
siempre: `scheduler.py` preguntaba `oc.fecha_entrega if hasattr(oc.fecha_entrega,
"days")`, pero `OcCliente.fecha_entrega` es `Column(Date)` → un `date` de Python, y
`hasattr(date, "days")` es **False** (`days` es atributo de `timedelta`). Así que `fe`
era siempre None, el `if fe:` nunca entraba y Grupo AM JAMÁS recibió el aviso de que a
una venta le quedaban ≤3 días de plazo con mercadería parcial en bodega. Un bug de una
palabra, invisible: no hay excepción, no hay log, simplemente no pasa nada.

SONDA DE PODER DISCRIMINANTE: con el `hasattr` la sección 2 da 0 avisos y esta suite se
pone ROJA; con `isinstance(..., date)` da 1. Verificado quitando el arreglo.

Cubre también los 4 casos que NO deben avisar (para que el arreglo no se convierta en
spam) y la idempotencia de 24 h.

Datos MARCADOS + limpieza total en `finally`. Las notificaciones se borran por
`id > snapshot` tomado al empezar: el barrido evalúa TODA la base, así que también crea
avisos de filas que no son del test y esos también hay que barrer para dejar la campana
del dueño como estaba.

Corre con:  ./venv/bin/python -m pytest recepcion_nacional/tests/test_alertas_plazo_critico.py -q
(también:   ./venv/bin/python recepcion_nacional/tests/test_alertas_plazo_critico.py)
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import scheduler  # noqa: E402
from database import SessionLocal  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, Notificacion, OcCliente,
)

MARK = "__TEST_B7__"

# id máximo de notificaciones ANTES de tocar nada: todo lo que aparezca arriba lo creó
# esta corrida (incluso lo que no es del MARK) y hay que borrarlo.
_SNAP = {"id": None}

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _snapshot():
    db = SessionLocal()
    try:
        _SNAP["id"] = db.query(Notificacion.id).order_by(
            Notificacion.id.desc()).limit(1).scalar() or 0
    finally:
        db.close()


def _borrar_notifs():
    if _SNAP["id"] is None:
        return
    db = SessionLocal()
    try:
        db.query(Notificacion).filter(
            Notificacion.id > _SNAP["id"]).delete(synchronize_session="fetch")
        db.commit()
    finally:
        db.close()


def _avisos(oc_id, regla=None):
    """Avisos de esta OC creados por el test, leídos con SESIÓN NUEVA."""
    db = SessionLocal()
    try:
        q = db.query(Notificacion).filter(
            Notificacion.id > _SNAP["id"],
            Notificacion.entidad_tipo == "oc_cliente",
            Notificacion.entidad_id == oc_id,
        )
        if regla:
            q = q.filter(Notificacion.regla == regla)
        return q.order_by(Notificacion.id).all()
    finally:
        db.close()


def _venta(db, sufijo, estados, fecha_entrega):
    """Cotización + ítems con los `estados` pedidos + OC-Cliente con esa fecha."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} CLIENTE",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    for n, est in enumerate(estados, start=1):
        db.add(ItemCotizacion(cotizacion_id=cot.id, item_num=n,
                              numero_parte=f"P-{MARK}-{sufijo}-{n}",
                              descripcion=f"Parte {n}", cantidad=1, estado_item=est))
    occ = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OCC-{sufijo}",
                    fecha_oc="2026-07-18", fecha_entrega=fecha_entrega)
    db.add(occ)
    db.commit()
    db.refresh(occ)
    return occ


def _limpiar(db):
    db.rollback()
    cot_ids = [c.id for c in db.query(Cotizacion)
               .filter(Cotizacion.numero.like(f"{MARK}%")).all()]
    if cot_ids:
        db.query(OcCliente).filter(
            OcCliente.cotizacion_id.in_(cot_ids)).delete(synchronize_session="fetch")
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session="fetch")
        db.query(Cotizacion).filter(
            Cotizacion.id.in_(cot_ids)).delete(synchronize_session="fetch")
    db.commit()
    _borrar_notifs()


def run():
    _snapshot()
    db = SessionLocal()
    try:
        _limpiar(db)
        _snapshot()
        hoy = date.today()   # la misma fecha que usa _checks_grupo_am

        # ═══ 1. Escenarios ═══════════════════════════════════════════════════
        # CRÍTICA: parcial en bodega (1 de 2) y el plazo a 1 día → DEBE avisar
        oc_crit = _venta(db, "CRIT", ["en_bodega", "comprado"], hoy + timedelta(days=1))
        # VENCIDA: parcial y el plazo ya pasó → DEBE avisar (dias negativo ≤ 3)
        oc_venc = _venta(db, "VENC", ["en_bodega", "embarcado"], hoy - timedelta(days=5))
        # HOLGADA: parcial pero el plazo lejos → NO debe avisar
        oc_holg = _venta(db, "HOLG", ["en_bodega", "comprado"], hoy + timedelta(days=30))
        # SIN NADA EN BODEGA: nada listo para salir → no hay decisión que tomar hoy
        oc_nada = _venta(db, "NADA", ["comprado", "comprado"], hoy + timedelta(days=1))
        # COMPLETA: todo en bodega → avisa B6 (lista para despacho), NO B7
        oc_full = _venta(db, "FULL", ["en_bodega", "en_bodega"], hoy + timedelta(days=1))
        # SIN FECHA: la OC no tiene plazo comprometido → no hay nada que vencer
        oc_sinf = _venta(db, "SINF", ["en_bodega", "comprado"], None)

        check("arrancamos sin avisos de la OC crítica", _avisos(oc_crit.id) == [])

        # ═══ 2. El barrido diario AVISA (con el bug daba 0) ══════════════════
        scheduler._checks_grupo_am()

        n = _avisos(oc_crit.id, "plazo_critico_3d")
        check("B7 avisa: 1 aviso de plazo crítico para la venta parcial", len(n) == 1,
              f"{len(n)} aviso(s) — con el bug del hasattr esto da 0")
        if n:
            a = n[0]
            check("B7: título con el N° de OC",
                  a.titulo == f"Plazo crítico: OC {oc_crit.numero_oc}", a.titulo)
            check("B7: mensaje con los días restantes y los ítems disponibles",
                  a.mensaje == ("Quedan 1 días y 1 ítems están disponibles en bodega."),
                  a.mensaje)
            check("B7: rol ventas, severidad critical, link a Cierre de Venta",
                  (a.destinatario_rol, a.severidad, a.link)
                  == ("ventas", "critical", "/cierre-venta"),
                  (a.destinatario_rol, a.severidad, a.link))
            check("B7: nace sin leer", not a.leida)

        n = _avisos(oc_venc.id, "plazo_critico_3d")
        check("B7 avisa también con el plazo YA vencido (días negativos)", len(n) == 1,
              f"{len(n)} aviso(s)")
        if n:
            check("B7 vencida: el mensaje muestra los días en negativo",
                  "Quedan -5 días" in n[0].mensaje, n[0].mensaje)

        # ═══ 3. Los que NO deben avisar (que el arreglo no sea spam) ═════════
        for oc, motivo in (
            (oc_holg, "plazo lejos (30 días)"),
            (oc_nada, "nada en bodega todavía"),
            (oc_full, "venta completa: eso es B6, no B7"),
            (oc_sinf, "OC sin fecha de entrega comprometida"),
        ):
            check(f"no avisa plazo crítico: {motivo}",
                  _avisos(oc.id, "plazo_critico_3d") == [],
                  [x.titulo for x in _avisos(oc.id, "plazo_critico_3d")])
        check("la venta COMPLETA sí avisa B6 (lista para despacho)",
              len(_avisos(oc_full.id, "oc_lista_despacho")) == 1,
              [x.titulo for x in _avisos(oc_full.id)])

        # ═══ 4. Idempotencia: 3 corridas seguidas no duplican ════════════════
        antes = [x.id for x in _avisos(oc_crit.id)]
        scheduler._checks_grupo_am()
        scheduler._checks_grupo_am()
        check("3 corridas seguidas → el mismo aviso (idempotencia de 24 h)",
              [x.id for x in _avisos(oc_crit.id)] == antes,
              f"antes={antes} después={[x.id for x in _avisos(oc_crit.id)]}")

        # ═══ 5. Un aviso LEÍDO vuelve a sonar mañana ═════════════════════════
        # Reconocer un aviso no puede silenciar el problema para siempre.
        db2 = SessionLocal()
        try:
            db2.query(Notificacion).filter(
                Notificacion.id.in_(antes or [0])).update({"leida": True},
                                                          synchronize_session="fetch")
            db2.commit()
        finally:
            db2.close()
        scheduler._checks_grupo_am()
        check("un aviso LEÍDO vuelve a sonar en la corrida siguiente",
              len(_avisos(oc_crit.id, "plazo_critico_3d")) == 2,
              len(_avisos(oc_crit.id, "plazo_critico_3d")))

    finally:
        _limpiar(db)
        db.close()
        # Limpieza verificada con SESIÓN NUEVA
        db3 = SessionLocal()
        try:
            assert db3.query(Notificacion).filter(
                Notificacion.id > (_SNAP["id"] or 0)).count() == 0
            assert db3.query(Cotizacion).filter(
                Cotizacion.numero.like(f"{MARK}%")).count() == 0
        finally:
            db3.close()
        print("Cleanup OK (verificado con sesión nueva)")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_alerta_plazo_critico_grupo_am():
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
