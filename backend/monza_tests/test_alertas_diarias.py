"""Barrido diario de alertas de MonzaParts (`scheduler.py` + `monza_notif.py`).

El job de las 06:00 (America/Santiago) solo consultaba tablas de Grupo AM: un proveedor de
MonzaParts atrasado NO avisaba NUNCA, aunque `monza_oc_proveedor.plazo_dias` estuviera
cargado, y "venta lista para despacho" / "plazo crítico" solo se disparaban en el INSTANTE
de cerrar una recepción en Bodega. Esta suite cubre las reglas nuevas y —sobre todo— los
dos riesgos que las vuelven peligrosas si están mal hechas:

  1. SPAM. Sin idempotencia, cada corrida de las 06:00 repite el mismo aviso hasta que el
     dueño deja de mirar la campana. Secciones 3, 4 y 5.
  2. AISLAMIENTO ENTRE MARCAS. Un error del barrido de Monza no puede dejar a Grupo AM sin
     alertas, ni al revés (el job es el único disparo del día). Secciones 6, 7 y 8.

Secciones:
  1. Aritmética de días hábiles de Chile con fechas FIJAS (fin de semana + feriado + signo).
  2. Las 3 reglas nuevas notifican, con el texto/rol/severidad/regla/link exactos.
  2b. Los casos que NO deben avisar (OC al día, OC cancelada, proveedor que ya entregó,
      `plazo_dias` imposible, venta parcial con plazo holgado) y el que SÍ pese a verse
      raro (OC legada con `estado` NULL: en SQL `NULL NOT IN (...)` da NULL y la fila se
      caería del filtro en silencio).
  3. IDEMPOTENCIA: dos corridas seguidas no duplican nada.
  4. La notificación LEÍDA sí vuelve a avisar (reconocer ≠ silenciar para siempre).
  5. Cruce con el aviso INSTANTÁNEO de Bodega (que se crea sin `regla`): el barrido no lo
     duplica, porque la ventana mira regla O título.
  6. Error en UNA regla de Monza → la otra regla de Monza sigue notificando.
  7. Error en TODA la rama Monza → Grupo AM sigue corriendo Y notificando.
  8. Error en la rama Grupo AM → Monza sigue corriendo Y notificando (la dirección dura:
     Grupo AM corre primero).

Datos MARCADOS + limpieza total en try/finally, verificada con SESIÓN NUEVA. Las
notificaciones se limpian por `id > snapshot` tomado al empezar: `run_daily_checks()`
evalúa TODA la base, así que puede generar avisos de filas que no son del test y esos
también hay que barrer.

Requiere las 3 columnas nuevas en monza_notificaciones:
    python -m migrations.monza_notif_alertas

Corre con:  ./venv/bin/python -m pytest monza_tests/test_alertas_diarias.py -q
(también:   ./venv/bin/python monza_tests/test_alertas_diarias.py)
"""
import os
import sys
import uuid
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

import scheduler  # noqa: E402  (se monkeypatchea a propósito en las secciones 6-8)
from database import SessionLocal  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, Notificacion, OcCliente,
)
from monza_fechas import add_business_days  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaNotificacion,
    MonzaOcProveedor,
)
from monza_notif import crear_notif  # noqa: E402

MARK = "test-mza-alertas"
SUF = uuid.uuid4().hex[:6].upper()

# id máximo de las dos tablas de notificaciones ANTES de tocar nada: todo lo que aparezca
# arriba de esta marca lo creó el test y hay que borrarlo (incluso lo que no es del MARK).
_SNAP = {"monza": None, "ga": None}


# ── helpers ───────────────────────────────────────────────────────────────────

def _snapshot_notifs():
    db = SessionLocal()
    try:
        _SNAP["monza"] = db.query(MonzaNotificacion.id).order_by(
            MonzaNotificacion.id.desc()).limit(1).scalar() or 0
        _SNAP["ga"] = db.query(Notificacion.id).order_by(
            Notificacion.id.desc()).limit(1).scalar() or 0
    finally:
        db.close()


def _limpiar_notifs():
    """Borra TODA notificación creada desde el snapshot (las dos marcas).

    Se llama entre secciones: la idempotencia de 24 h suprimiría los avisos de la sección
    siguiente si los de la anterior quedaran vivos, y entonces la sección siguiente pasaría
    por la razón equivocada.
    """
    if _SNAP["monza"] is None:
        return
    db = SessionLocal()
    try:
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.id > _SNAP["monza"]).delete(synchronize_session="fetch")
        db.query(Notificacion).filter(
            Notificacion.id > _SNAP["ga"]).delete(synchronize_session="fetch")
        db.commit()
    finally:
        db.close()


def _notifs_monza(entidad=None, entidad_id=None):
    """Notificaciones Monza creadas por el test, leídas con SESIÓN NUEVA."""
    db = SessionLocal()
    try:
        q = db.query(MonzaNotificacion).filter(MonzaNotificacion.id > _SNAP["monza"])
        if entidad:
            q = q.filter(MonzaNotificacion.entidad == entidad)
        if entidad_id:
            q = q.filter(MonzaNotificacion.entidad_id == entidad_id)
        return q.order_by(MonzaNotificacion.id).all()
    finally:
        db.close()


def _notifs_ga(entidad_id=None):
    db = SessionLocal()
    try:
        q = db.query(Notificacion).filter(Notificacion.id > _SNAP["ga"])
        if entidad_id:
            q = q.filter(Notificacion.entidad_id == entidad_id,
                         Notificacion.entidad_tipo == "oc_cliente")
        return q.order_by(Notificacion.id).all()
    finally:
        db.close()


def _cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        S = "fetch"
        # ── MonzaParts ──
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre == MARK).all()]
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
        db.query(MonzaOcProveedor).filter(
            MonzaOcProveedor.numero.like(f"OCP-{SUF}-%")).delete(synchronize_session=S)
        # ── Grupo AM (la venta sembrada para el test de aislamiento) ──
        ga_cot_ids = [r[0] for r in db.query(Cotizacion.id)
                      .filter(Cotizacion.cliente == MARK).all()]
        db.query(OcCliente).filter(
            OcCliente.cotizacion_id.in_(ga_cot_ids or [0])).delete(synchronize_session=S)
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id.in_(ga_cot_ids or [0])).delete(synchronize_session=S)
        db.query(Cotizacion).filter(
            Cotizacion.id.in_(ga_cot_ids or [0])).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()
    _limpiar_notifs()


def _setup(db, hoy):
    """Siembra los 4 escenarios que deben avisar y los 4 que NO deben."""
    cli = MonzaCliente(nombre=MARK)
    db.add(cli); db.flush()

    hace_30 = datetime.utcnow() - timedelta(days=30)

    # ── (a) OCs de proveedor ────────────────────────────────────────────────────
    ocp_venc = MonzaOcProveedor(numero=f"OCP-{SUF}-VENC", proveedor_nombre=f"{MARK} Motors",
                                estado="emitida", plazo_dias=5, created_at=hace_30)
    # AL DÍA: creada hoy con 30 días hábiles de plazo → no vence
    ocp_alDia = MonzaOcProveedor(numero=f"OCP-{SUF}-ALDIA", proveedor_nombre=f"{MARK} AlDia",
                                 estado="emitida", plazo_dias=30, created_at=datetime.utcnow())
    # CANCELADA y vencida → la OC muerta no debe avisar todos los días para siempre
    ocp_cancel = MonzaOcProveedor(numero=f"OCP-{SUF}-CANC", proveedor_nombre=f"{MARK} Cancel",
                                  estado="cancelada", plazo_dias=5, created_at=hace_30)
    # VENCIDA pero el proveedor YA entregó (ítem 'preparado') → falso positivo si avisara
    ocp_entreg = MonzaOcProveedor(numero=f"OCP-{SUF}-ENTREG", proveedor_nombre=f"{MARK} Entrego",
                                  estado="emitida", plazo_dias=5, created_at=hace_30)
    # LEGADA con estado NULL y vencida → SÍ debe avisar (NULL no es 'cancelada'). El
    # estado se pone a NULL con SQL crudo más abajo: el default del modelo lo taparía.
    ocp_nulo = MonzaOcProveedor(numero=f"OCP-{SUF}-NULO", proveedor_nombre=f"{MARK} Legada",
                                estado="emitida", plazo_dias=5, created_at=hace_30)
    # plazo_dias IMPOSIBLE (el API lo acepta: `Optional[int]` sin cota) → no debe avisar
    # NI colgar el barrido recorriendo día por día.
    ocp_absurdo = MonzaOcProveedor(numero=f"OCP-{SUF}-ABSURDO", proveedor_nombre=f"{MARK} Absurdo",
                                   estado="emitida", plazo_dias=99999999, created_at=hace_30)
    db.add_all([ocp_venc, ocp_alDia, ocp_cancel, ocp_entreg, ocp_nulo, ocp_absurdo])
    db.flush()

    cot_prov = MonzaCotizacion(numero=f"CT-{SUF}-PROV", cliente_id=cli.id, estado="vendida")
    db.add(cot_prov); db.flush()
    db.add_all([
        MonzaCotizacionItem(cotizacion_id=cot_prov.id, descripcion=f"{MARK} filtro",
                            numero_parte=f"{SUF}-FILTRO", cantidad=2,
                            estado_linea="comprado", oc_proveedor_id=ocp_venc.id),
        MonzaCotizacionItem(cotizacion_id=cot_prov.id, descripcion=f"{MARK} bomba",
                            numero_parte=f"{SUF}-BOMBA", cantidad=1,
                            estado_linea="comprado", oc_proveedor_id=ocp_venc.id),
        MonzaCotizacionItem(cotizacion_id=cot_prov.id, descripcion=f"{MARK} al dia",
                            numero_parte=f"{SUF}-ALDIA", cantidad=1,
                            estado_linea="comprado", oc_proveedor_id=ocp_alDia.id),
        MonzaCotizacionItem(cotizacion_id=cot_prov.id, descripcion=f"{MARK} cancelada",
                            numero_parte=f"{SUF}-CANC", cantidad=1,
                            estado_linea="comprado", oc_proveedor_id=ocp_cancel.id),
        MonzaCotizacionItem(cotizacion_id=cot_prov.id, descripcion=f"{MARK} entregada",
                            numero_parte=f"{SUF}-ENTREG", cantidad=1,
                            estado_linea="preparado", oc_proveedor_id=ocp_entreg.id),
        MonzaCotizacionItem(cotizacion_id=cot_prov.id, descripcion=f"{MARK} legada",
                            numero_parte=f"{SUF}-NULO", cantidad=1,
                            estado_linea="comprado", oc_proveedor_id=ocp_nulo.id),
        MonzaCotizacionItem(cotizacion_id=cot_prov.id, descripcion=f"{MARK} absurda",
                            numero_parte=f"{SUF}-ABSURDO", cantidad=1,
                            estado_linea="comprado", oc_proveedor_id=ocp_absurdo.id),
    ])

    # ── (b) Ventas ──────────────────────────────────────────────────────────────
    # LISTA: los 2 ítems en bodega
    cot_lista = MonzaCotizacion(numero=f"CT-{SUF}-LISTA", cliente_id=cli.id, estado="vendida",
                                fecha_entrega_est=hoy + timedelta(days=30))
    # CRÍTICA: parcial (1 de 3 en bodega) y el plazo a 1 día hábil
    cot_crit = MonzaCotizacion(numero=f"CT-{SUF}-CRIT", cliente_id=cli.id, estado="vendida",
                               fecha_entrega_est=add_business_days(hoy, 1))
    # ATRASADA: parcial y el plazo YA VENCIÓ (rama del mensaje "venció hace N días")
    cot_atras = MonzaCotizacion(numero=f"CT-{SUF}-ATRAS", cliente_id=cli.id, estado="vendida",
                                fecha_entrega_est=hoy - timedelta(days=7))
    # HOLGADA: parcial pero con el plazo lejos → no debe avisar
    cot_holg = MonzaCotizacion(numero=f"CT-{SUF}-HOLG", cliente_id=cli.id, estado="vendida",
                               fecha_entrega_est=hoy + timedelta(days=60))
    db.add_all([cot_lista, cot_crit, cot_atras, cot_holg]); db.flush()
    db.add_all([
        MonzaCotizacionItem(cotizacion_id=cot_lista.id, descripcion=f"{MARK} L1",
                            numero_parte=f"{SUF}-L1", cantidad=1, estado_linea="en_bodega"),
        MonzaCotizacionItem(cotizacion_id=cot_lista.id, descripcion=f"{MARK} L2",
                            numero_parte=f"{SUF}-L2", cantidad=1, estado_linea="en_bodega"),
        MonzaCotizacionItem(cotizacion_id=cot_crit.id, descripcion=f"{MARK} C1",
                            numero_parte=f"{SUF}-C1", cantidad=1, estado_linea="en_bodega"),
        MonzaCotizacionItem(cotizacion_id=cot_crit.id, descripcion=f"{MARK} C2",
                            numero_parte=f"{SUF}-C2", cantidad=1, estado_linea="comprado"),
        MonzaCotizacionItem(cotizacion_id=cot_crit.id, descripcion=f"{MARK} C3",
                            numero_parte=f"{SUF}-C3", cantidad=1, estado_linea="comprado"),
        MonzaCotizacionItem(cotizacion_id=cot_atras.id, descripcion=f"{MARK} A1",
                            numero_parte=f"{SUF}-A1", cantidad=1, estado_linea="en_bodega"),
        MonzaCotizacionItem(cotizacion_id=cot_atras.id, descripcion=f"{MARK} A2",
                            numero_parte=f"{SUF}-A2", cantidad=1, estado_linea="comprado"),
        MonzaCotizacionItem(cotizacion_id=cot_holg.id, descripcion=f"{MARK} H1",
                            numero_parte=f"{SUF}-H1", cantidad=1, estado_linea="en_bodega"),
        MonzaCotizacionItem(cotizacion_id=cot_holg.id, descripcion=f"{MARK} H2",
                            numero_parte=f"{SUF}-H2", cantidad=1, estado_linea="comprado"),
    ])

    # ── Grupo AM: venta lista (regla B6) para probar el aislamiento entre marcas ──
    ga_cot = Cotizacion(numero=f"COT-{SUF}-GA", cliente=MARK, estado="completado")
    db.add(ga_cot); db.flush()
    db.add(ItemCotizacion(cotizacion_id=ga_cot.id, descripcion=f"{MARK} GA item",
                          numero_parte=f"{SUF}-GA", cantidad=1, estado_item="en_bodega"))
    ga_oc = OcCliente(cotizacion_id=ga_cot.id, numero_oc=f"OC-{SUF}-GA")
    db.add(ga_oc); db.flush()

    db.commit()

    # Fila LEGADA: el `default="emitida"` del modelo dispara cuando el atributo vale None,
    # así que la única forma de simular el NULL real de una fila antigua es con SQL crudo.
    db.execute(
        text("UPDATE monza_oc_proveedor SET estado = NULL WHERE id = :i"),
        {"i": ocp_nulo.id},
    )
    db.commit()

    return {
        "ocp_venc": ocp_venc.id, "ocp_alDia": ocp_alDia.id,
        "ocp_cancel": ocp_cancel.id, "ocp_entreg": ocp_entreg.id,
        "ocp_nulo": ocp_nulo.id, "ocp_absurdo": ocp_absurdo.id,
        "ocp_venc_creada": hace_30,
        "cot_lista": cot_lista.id, "cot_crit": cot_crit.id,
        "cot_atras": cot_atras.id, "cot_holg": cot_holg.id,
        "cot_crit_fecha": cot_crit.fecha_entrega_est,
        "cot_atras_fecha": cot_atras.fecha_entrega_est,
        "ga_oc": ga_oc.id, "ga_oc_numero": ga_oc.numero_oc,
    }


def _boom(*a, **kw):
    """Falla inyectada para probar el aislamiento."""
    raise RuntimeError("falla inyectada por el test de aislamiento")


# ── suite ─────────────────────────────────────────────────────────────────────

def test_alertas_diarias_monza():
    _snapshot_notifs()
    db = SessionLocal()
    try:
        _cleanup()
        _snapshot_notifs()
        hoy = scheduler._hoy_chile()
        ids = _setup(db, hoy)

        # ══ 1) Días hábiles de Chile con fechas FIJAS ═════════════════════════
        dh = scheduler._dias_habiles_chile
        assert dh(date(2026, 7, 6), date(2026, 7, 10)) == 4, "Lun→Vie = 4 hábiles"
        assert dh(date(2026, 7, 10), date(2026, 7, 13)) == 1, \
            "Vie→Lun = 1 hábil (el fin de semana no cuenta)"
        # 2026-07-16 es Virgen del Carmen (feriado): Mié→Vie cuenta solo el miércoles
        assert dh(date(2026, 7, 15), date(2026, 7, 17)) == 1, \
            "el feriado no puede contar como día hábil"
        assert dh(date(2026, 7, 10), date(2026, 7, 6)) == -4, "hacia atrás va con signo"
        assert dh(date(2026, 7, 10), date(2026, 7, 10)) == 0, "mismo día = 0"
        print("1) días hábiles Chile (fin de semana + feriado + signo) OK")

        # ══ 2) Las 3 reglas nuevas notifican ══════════════════════════════════
        assert _notifs_monza() == [], "arrancamos sin avisos Monza del test"
        scheduler.run_daily_checks()

        # (a) proveedor atrasado
        n = _notifs_monza("oc_proveedor", ids["ocp_venc"])
        assert len(n) == 1, f"la OC vencida debía generar 1 aviso, generó {len(n)}"
        a = n[0]
        f_esp = add_business_days(ids["ocp_venc_creada"], 5)
        atraso = dh(f_esp, hoy)
        assert a.titulo == f"Proveedor atrasado · OCP-{SUF}-VENC", a.titulo
        assert a.mensaje == (
            f"{MARK} Motors se comprometió para el {f_esp.strftime('%d-%m-%Y')} y lleva "
            f"{atraso} día(s) hábil(es) de atraso. Faltan 2 ítem(s): "
            f"{SUF}-FILTRO, {SUF}-BOMBA. Hay que apurar al proveedor y avisarle al "
            f"cliente si la entrega se corre."
        ), a.mensaje
        assert (a.tipo, a.severidad, a.destinatario_rol) == ("warning", "warning", "abastecimiento"), \
            (a.tipo, a.severidad, a.destinatario_rol)
        assert a.regla == "monza_plazo_proveedor_vencido", a.regla
        assert a.link == "/monzaparts/seguimiento", a.link
        assert a.leida == 0, "un aviso nuevo nace sin leer"

        # (b1) venta lista para despacho
        n = _notifs_monza("cotizacion", ids["cot_lista"])
        assert len(n) == 1, f"la venta lista debía generar 1 aviso, generó {len(n)}"
        b = n[0]
        assert b.titulo == f"Venta CT-{SUF}-LISTA lista para despacho", b.titulo
        assert b.mensaje == (
            f"Todos los ítems (2) están en bodega · {MARK}. "
            f"Ya se puede preparar el despacho y emitir la guía."
        ), b.mensaje
        assert (b.tipo, b.severidad, b.destinatario_rol) == ("success", "info", "ventas"), \
            (b.tipo, b.severidad, b.destinatario_rol)
        assert b.regla == "monza_venta_lista_despacho", b.regla
        assert b.link == "/monzaparts/despachos", b.link

        # (b2) plazo crítico (queda plazo)
        n = _notifs_monza("cotizacion", ids["cot_crit"])
        assert len(n) == 1, f"el plazo crítico debía generar 1 aviso, generó {len(n)}"
        c = n[0]
        dias = dh(hoy, ids["cot_crit_fecha"])
        assert c.titulo == f"Plazo crítico · CT-{SUF}-CRIT", c.titulo
        assert c.mensaje == (
            f"Quedan {dias} día(s) hábil(es) para la entrega y la venta está PARCIAL en "
            f"bodega (1 de 3 ítems) · {MARK}. "
            f"Conviene despachar lo que ya llegó o avisarle al cliente."
        ), c.mensaje
        assert (c.tipo, c.severidad, c.destinatario_rol) == ("warning", "critical", "ventas"), \
            (c.tipo, c.severidad, c.destinatario_rol)
        assert c.regla == "monza_plazo_critico_3d", c.regla

        # (b3) plazo YA vencido → la otra rama del mensaje
        n = _notifs_monza("cotizacion", ids["cot_atras"])
        assert len(n) == 1, f"la venta atrasada debía generar 1 aviso, generó {len(n)}"
        d = n[0]
        vencido = abs(dh(hoy, ids["cot_atras_fecha"]))
        assert d.mensaje == (
            f"El plazo de entrega venció hace {vencido} día(s) hábil(es) y la venta está "
            f"PARCIAL en bodega (1 de 2 ítems) · {MARK}. "
            f"Conviene despachar lo que ya llegó o avisarle al cliente."
        ), d.mensaje
        print("2) las 3 reglas nuevas notifican con el texto exacto OK")
        print(f"   · {a.titulo}\n     {a.mensaje}")
        print(f"   · {b.titulo}\n     {b.mensaje}")
        print(f"   · {c.titulo}\n     {c.mensaje}")
        print(f"   · {d.titulo}\n     {d.mensaje}")

        # ══ 2b) Los que NO deben avisar ═══════════════════════════════════════
        for clave, motivo in (
            ("ocp_alDia", "OC dentro del plazo"),
            ("ocp_cancel", "OC cancelada (aunque el plazo esté vencido)"),
            ("ocp_entreg", "el proveedor ya entregó (ítem 'preparado')"),
            ("ocp_absurdo", "plazo_dias imposible (99999999): se salta, no cuelga el job"),
        ):
            assert _notifs_monza("oc_proveedor", ids[clave]) == [], \
                f"no debía avisar: {motivo}"
        assert _notifs_monza("cotizacion", ids["cot_holg"]) == [], \
            "venta parcial con el plazo lejos no debía avisar"
        # La fila LEGADA con estado NULL sí tiene que avisar: en SQL `NULL NOT IN (...)` da
        # NULL y se caería del filtro en silencio.
        assert len(_notifs_monza("oc_proveedor", ids["ocp_nulo"])) == 1, \
            "una OC con estado NULL está viva y debía avisar"
        print("2b) los 5 casos que NO deben avisar quedaron callados, y la OC legada "
              "con estado NULL sí avisó OK")

        # ══ 3) IDEMPOTENCIA: segunda corrida no duplica ═══════════════════════
        antes = [x.id for x in _notifs_monza()]
        assert len(antes) == 5, f"esperábamos 5 avisos Monza del test, hay {len(antes)}"
        scheduler.run_daily_checks()
        despues = [x.id for x in _notifs_monza()]
        assert despues == antes, \
            f"la 2ª corrida DUPLICÓ avisos: antes={antes} después={despues}"
        # y una tercera tampoco (el bug clásico aparece al acumular corridas)
        scheduler.run_daily_checks()
        assert [x.id for x in _notifs_monza()] == antes, "la 3ª corrida duplicó avisos"
        print("3) idempotencia: 3 corridas seguidas → los mismos 5 avisos OK")

        # ══ 4) La notificación LEÍDA vuelve a avisar ══════════════════════════
        # Reconocer un aviso no puede silenciar el problema para siempre: si mañana la OC
        # sigue atrasada, tiene que volver a sonar (mismo criterio que Grupo AM).
        db2 = SessionLocal()
        try:
            db2.query(MonzaNotificacion).filter(
                MonzaNotificacion.id == a.id).update({"leida": 1})
            db2.commit()
        finally:
            db2.close()
        scheduler.run_daily_checks()
        n = _notifs_monza("oc_proveedor", ids["ocp_venc"])
        assert len(n) == 2, f"leída → debía volver a avisar, hay {len(n)} aviso(s)"
        assert n[1].leida == 0 and n[1].regla == "monza_plazo_proveedor_vencido"
        print("4) un aviso LEÍDO vuelve a sonar en la corrida siguiente OK")

        # ══ 5) Cruce con el aviso INSTANTÁNEO de Bodega (sin `regla`) ═════════
        # Bodega crea "Venta X lista para despacho" sin `regla`; el barrido no puede
        # dejar DOS filas del mismo evento en la campana.
        _limpiar_notifs()
        db3 = SessionLocal()
        try:
            inst = crear_notif(
                db3, f"Venta CT-{SUF}-LISTA lista para despacho",
                "Todos los ítems están en bodega · (aviso instantáneo de Bodega)",
                "success", "/monzaparts/despachos", "cotizacion", ids["cot_lista"])
            assert inst is not None, "el aviso instantáneo (sin regla) debe crearse igual"
            assert inst.regla is None and inst.severidad == "info", \
                "compat: sin regla, severidad derivada del tipo 'success'"
        finally:
            db3.close()
        scheduler.run_daily_checks()
        n = _notifs_monza("cotizacion", ids["cot_lista"])
        assert len(n) == 1, \
            f"el barrido duplicó el aviso instantáneo de Bodega: {[x.titulo for x in n]}"
        assert n[0].regla is None, "el que sobrevive es el instantáneo (llegó primero)"
        print("5) el barrido NO duplica el aviso instantáneo de Bodega OK")

        # ══ 6) Error en UNA regla de Monza → la otra sigue ════════════════════
        _limpiar_notifs()
        original_regla_a = scheduler._monza_ocp_plazo_vencido
        try:
            scheduler._monza_ocp_plazo_vencido = _boom
            scheduler.run_daily_checks()
        finally:
            scheduler._monza_ocp_plazo_vencido = original_regla_a
        assert _notifs_monza("oc_proveedor", ids["ocp_venc"]) == [], \
            "la regla reventada no puede haber notificado"
        assert len(_notifs_monza("cotizacion", ids["cot_lista"])) == 1, \
            "la OTRA regla de Monza tenía que seguir notificando"
        assert len(_notifs_ga(ids["ga_oc"])) == 1, \
            "Grupo AM tenía que seguir notificando"
        print("6) error en 1 regla de Monza → la otra regla y Grupo AM siguen OK")

        # ══ 7) Error en TODA la rama Monza → Grupo AM sigue ═══════════════════
        _limpiar_notifs()
        original_monza = scheduler._checks_monza
        try:
            scheduler._checks_monza = _boom
            scheduler.run_daily_checks()
        finally:
            scheduler._checks_monza = original_monza
        assert _notifs_monza() == [], "la rama Monza reventada no pudo notificar"
        ga = _notifs_ga(ids["ga_oc"])
        assert len(ga) == 1, f"Grupo AM tenía que notificar igual, hay {len(ga)}"
        assert ga[0].titulo == f"OC {ids['ga_oc_numero']} lista para despacho", ga[0].titulo
        assert ga[0].regla == "oc_lista_despacho" and ga[0].destinatario_rol == "ventas"
        print("7) error en TODA la rama Monza → Grupo AM notifica igual OK")

        # ══ 8) Error en la rama Grupo AM → Monza sigue ════════════════════════
        # La dirección dura: Grupo AM corre PRIMERO, así que si su fallo se propagara,
        # Monza no llegaría a evaluarse nunca.
        _limpiar_notifs()
        original_ga = scheduler._checks_grupo_am
        try:
            scheduler._checks_grupo_am = _boom
            scheduler.run_daily_checks()
        finally:
            scheduler._checks_grupo_am = original_ga
        assert _notifs_ga(ids["ga_oc"]) == [], "la rama Grupo AM reventada no pudo notificar"
        assert len(_notifs_monza()) == 5, \
            f"Monza tenía que notificar igual, hay {len(_notifs_monza())}"
        print("8) error en la rama Grupo AM → Monza notifica igual OK")

        print("\n=== TODO OK ===")
    finally:
        db.close()
        _cleanup()
        # Limpieza verificada con SESIÓN NUEVA
        db4 = SessionLocal()
        try:
            assert db4.query(MonzaNotificacion).filter(
                MonzaNotificacion.id > (_SNAP["monza"] or 0)).count() == 0
            assert db4.query(Notificacion).filter(
                Notificacion.id > (_SNAP["ga"] or 0)).count() == 0
            assert db4.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).count() == 0
            assert db4.query(MonzaOcProveedor).filter(
                MonzaOcProveedor.numero.like(f"OCP-{SUF}-%")).count() == 0
            assert db4.query(Cotizacion).filter(Cotizacion.cliente == MARK).count() == 0
        finally:
            db4.close()
        print("Cleanup OK (verificado con sesión nueva)")


if __name__ == "__main__":
    test_alertas_diarias_monza()
