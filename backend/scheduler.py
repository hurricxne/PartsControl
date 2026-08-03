"""Barrido diario de alertas — CÓDIGO COMPARTIDO por Grupo AM y MonzaParts.

Un solo job cron a las 06:00 de Santiago (`start_scheduler`, llamado una vez desde
`main.py:145`) evalúa las reglas de las DOS marcas.

HASTA 2026-07-30 EL BARRIDO ERA SOLO DE GRUPO AM. Consecuencia real: un proveedor de
MonzaParts atrasado NO avisaba NUNCA, aunque `monza_oc_proveedor.plazo_dias` existiera y
estuviera cargado; y las alertas "venta lista para despacho" / "plazo crítico" de Monza
solo se disparaban en el INSTANTE de cerrar una recepción en Bodega, nunca por el simple
paso del tiempo. Las reglas de MonzaParts que siguen son 100% ADITIVAS: las de Grupo AM
quedaron tal cual, movidas sin un cambio de lógica a `_checks_grupo_am()`.

DOS INVARIANTES QUE NO SE NEGOCIAN
1. AISLAMIENTO ENTRE MARCAS. Cada marca corre con su propia sesión y su propio
   try/except (`_correr_marca`). Este job es el ÚNICO disparo del día: si un error de
   Monza se propagara, Grupo AM se quedaría sin alertas hasta mañana (y al revés).
2. IDEMPOTENCIA. Cada regla declara su `regla=` y `notificaciones.py` / `monza_notif.py`
   suprimen el aviso repetido dentro de 24 h. Sin eso, cada corrida de las 06:00 vuelve a
   notificar lo mismo, el dueño deja de mirar la campana y las alertas se vuelven ruido —
   peor que no tenerlas.
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import joinedload, selectinload

from database import SessionLocal
from models.models import (
    OcCliente, OcProveedor, OcProveedorItem, ItemCotizacion,
)
from notificaciones import crear_notificacion
# MonzaParts: modelos y helpers PROPIOS de la marca automotriz (nada compartido con los
# de arriba, misma separación que el resto de los módulos monza_*).
from monza_models import MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor
from monza_notif import crear_notif
from monza_fechas import add_business_days, _is_chile_holiday

TZ_CHILE = ZoneInfo("America/Santiago")


def _hoy_chile() -> date:
    """Fecha de HOY en Chile — la fecha del negocio.

    El cron dispara a las 06:00 de Santiago, hora en la que la fecha UTC del servidor
    coincide; pero un reintento manual de noche (21:00 en Chile = 01:00 UTC del día
    siguiente) daría un día de más y reportaría como "vencido ayer" un plazo que vence
    hoy. Por eso nada de `date.today()` crudo para fechas de negocio.
    """
    return datetime.now(TZ_CHILE).date()


def _business_days_between(start: date, end: date) -> int:
    """Count business days (Mon-Fri) between two dates, not including end."""
    if start >= end:
        return 0
    count = 0
    current = start
    while current < end:
        if current.weekday() < 5:  # Monday=0 … Friday=4
            count += 1
        current += timedelta(days=1)
    return count


# ══════════════════════════════════════════════════════════════════════════════
#  Entrada del job — orquesta las dos marcas AISLADAS entre sí
# ══════════════════════════════════════════════════════════════════════════════

def run_daily_checks():
    """Evalúa las reglas de alerta de las dos marcas y crea las notificaciones."""
    _correr_marca("grupo-am", _checks_grupo_am)
    _correr_marca("monzaparts", _checks_monza)


def _correr_marca(marca: str, fn) -> None:
    """Corre el barrido de UNA marca sin que su fallo contagie a la otra.

    No re-lanza a propósito: un `raise` acá dejaría a la otra marca sin barrido hasta la
    corrida del día siguiente. La marca va en el log para poder ubicar el fallo (sin el
    nombre, un stack de MonzaParts se confundiría con uno de Grupo AM).
    """
    try:
        fn()
    except Exception as e:
        print(f"[scheduler][{marca}] ERROR en el barrido diario: "
              f"{type(e).__name__}: {e}", flush=True)


def _correr_regla(marca: str, nombre: str, fn, db, hoy) -> int:
    """Corre UNA regla aislada del resto de las reglas de su marca.

    Segundo anillo del aislamiento: sin él, un error en la primera regla de Monza dejaría
    sin correr a la segunda. El `rollback` deja la sesión limpia para la regla siguiente.
    """
    try:
        return fn(db, hoy)
    except Exception as e:
        db.rollback()
        print(f"[scheduler][{marca}] ERROR en la regla '{nombre}': "
              f"{type(e).__name__}: {e}", flush=True)
        return 0


# ══════════════════════════════════════════════════════════════════════════════
#  GRUPO AM — reglas originales (sin cambios de lógica)
# ══════════════════════════════════════════════════════════════════════════════

def _checks_grupo_am():
    """Reglas de alerta de Grupo AM / MachParts."""
    db = SessionLocal()
    try:
        hoy = date.today()

        # ── Rule 2.1: OC Proveedor con plazo vencido ──────────────────────────
        asig_list = db.query(OcProveedorItem).filter(
            OcProveedorItem.plazo_dias_prov.isnot(None),
            OcProveedorItem.fecha_asignacion.isnot(None),
        ).all()

        for asig in asig_list:
            item = db.query(ItemCotizacion).filter(
                ItemCotizacion.id == asig.item_cotizacion_id
            ).first()
            if not item or item.estado_item not in ("comprado",):
                continue

            fecha_asig = (
                asig.fecha_asignacion.date()
                if hasattr(asig.fecha_asignacion, "date")
                else asig.fecha_asignacion
            )
            fecha_esperada = fecha_asig + timedelta(days=asig.plazo_dias_prov)

            if hoy > fecha_esperada:
                ocp = db.query(OcProveedor).filter(
                    OcProveedor.id == asig.oc_proveedor_id
                ).first()
                atraso = _business_days_between(fecha_esperada, hoy)
                for rol in ("abastecimiento", "logistica"):
                    crear_notificacion(
                        db=db,
                        rol=rol,
                        severidad="warning",
                        titulo=f"Plazo vencido: OCP {ocp.numero if ocp else asig.oc_proveedor_id}",
                        mensaje=(
                            f"Ítem {item.numero_parte} lleva {atraso} días de atraso"
                        ),
                        entidad_tipo="oc_proveedor",
                        entidad_id=asig.oc_proveedor_id,
                        link="/seguimiento",
                        regla="plazo_proveedor_rojo",
                    )

        # ── Rule B6/B7: OCs-Cliente listas / plazo crítico ────────────────────
        oc_list = db.query(OcCliente).all()
        for oc in oc_list:
            items = db.query(ItemCotizacion).filter(
                ItemCotizacion.cotizacion_id == oc.cotizacion_id,
                ItemCotizacion.estado_item.in_([
                    "cerrado", "comprado", "preparado", "pre_embarcado",
                    "embarcado", "en_bodega", "reclamo_proveedor",
                ])
            ).all()
            if not items:
                continue

            todos = all(i.estado_item == "en_bodega" for i in items)

            # B6: all items in bodega → notify Ventas
            if todos:
                crear_notificacion(
                    db=db,
                    rol="ventas",
                    severidad="info",
                    titulo=f"OC {oc.numero_oc} lista para despacho",
                    mensaje="Todos los ítems están en bodega.",
                    entidad_tipo="oc_cliente",
                    entidad_id=oc.id,
                    link="/cierre-venta",
                    regla="oc_lista_despacho",
                )

            # B7: deadline approaching with mixed items
            if oc.fecha_entrega:
                try:
                    # `OcCliente.fecha_entrega` es Column(Date) → un `date` de Python, y
                    # `hasattr(date, "days")` es FALSE (`days` es atributo de timedelta,
                    # no de date). Con el `hasattr` original `fe` era SIEMPRE None y este
                    # bloque B7 completo NUNCA se ejecutó: Grupo AM jamás recibió la
                    # alerta diaria de "plazo crítico". Se usa `isinstance(..., date)`,
                    # que es lo que sí hace la vía instantánea de Bodega
                    # (routers/bodega.py::_notificar_ocs_cliente).
                    fe = oc.fecha_entrega if isinstance(oc.fecha_entrega, date) else None
                    if isinstance(fe, datetime):
                        # Cinturón: un DateTime también es instancia de date, y restarle
                        # un date daría TypeError que el except de abajo se tragaría
                        # (dejando la alerta muerta otra vez, ahora en silencio).
                        fe = fe.date()
                    if fe:
                        dias = (fe - hoy).days
                        en_bodega = [i for i in items if i.estado_item == "en_bodega"]
                        otros = [i for i in items if i.estado_item != "en_bodega"]
                        if dias <= 3 and en_bodega and otros:
                            crear_notificacion(
                                db=db,
                                rol="ventas",
                                severidad="critical",
                                titulo=f"Plazo crítico: OC {oc.numero_oc}",
                                mensaje=(
                                    f"Quedan {dias} días y {len(en_bodega)} ítems "
                                    f"están disponibles en bodega."
                                ),
                                entidad_tipo="oc_cliente",
                                entidad_id=oc.id,
                                link="/cierre-venta",
                                regla="plazo_critico_3d",
                            )
                except Exception:
                    pass

        print(f"[scheduler] daily checks done: {hoy}")
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
#  MONZAPARTS — reglas equivalentes (aditivas)
# ══════════════════════════════════════════════════════════════════════════════

# Un rol por regla: el panel de Monza NO filtra por rol, así que dos filas por evento se
# verían DOS VECES en la campana (ver la cabecera de monza_notif.py).
ROL_ABASTECIMIENTO = "abastecimiento"
ROL_VENTAS = "ventas"

# Identificadores ESTABLES de cada regla: son la llave de la idempotencia de 24 h.
# Cambiarlos re-notifica todo lo que ya estaba avisado.
REGLA_OCP_VENCIDA = "monza_plazo_proveedor_vencido"
REGLA_VENTA_LISTA = "monza_venta_lista_despacho"
REGLA_PLAZO_CRITICO = "monza_plazo_critico_3d"

# Estado de la línea que significa "el proveedor todavía no entregó". El resto del
# pipeline de Monza (preparado → embarcado → en_bodega → despachado) ya implica entrega,
# así que reclamarle el plazo sería un falso positivo. Mismo criterio que la regla 2.1 de
# Grupo AM, que también mira solo 'comprado'.
ESTADOS_ESPERANDO_PROVEEDOR = ("comprado",)

# Estados de la OC de proveedor en los que el reclamo ya no corresponde. Grupo AM no
# filtra por estado de la OC porque le basta el estado del ítem; en Monza una OC
# 'cancelada' puede quedar con líneas 'comprado' colgando, y sin este filtro esa OC
# muerta avisaría todos los días para siempre.
ESTADOS_OCP_CERRADOS = ("recibida", "cancelada")

# Ventana del "plazo crítico" (días hábiles). Igual que la B7 de Grupo AM y que el aviso
# instantáneo de Bodega (monza_router_bodega.py:442) — los tres tienen que coincidir.
DIAS_PLAZO_CRITICO = 3

# Tope de cordura del plazo del proveedor (10 años, el mismo que valida el schema de
# Contabilidad: monza_contabilidad/schemas.py:40 `le=3650`). NO es cosmético:
# `ComprarBody.plazo_dias` es un `Optional[int]` SIN cota, así que un dedazo tipo
# 99999999 queda guardado, y `add_business_days` avanza día por día — el barrido se
# colgaría y NINGUNA alerta del día saldría, de ninguna de las dos marcas.
MAX_PLAZO_DIAS = 3650

# Topes de las columnas: monza_notificaciones.titulo VARCHAR(200) / mensaje VARCHAR(400).
# Un texto más largo hace fallar el INSERT y el aviso se pierde en silencio.
MAX_TITULO = 200
MAX_MENSAJE = 400
# Cuántos números de parte se nombran en el mensaje antes de resumir con "…".
MAX_PARTES_EN_MENSAJE = 3


def _recortar(texto: str, tope: int) -> str:
    """Recorta al tope de la columna. Un nombre de proveedor largo no puede tumbar el aviso."""
    if texto is None:
        return texto
    return texto if len(texto) <= tope else texto[: tope - 1] + "…"


def _dias_habiles_chile(desde: date, hasta: date) -> int:
    """Días hábiles de Chile entre dos fechas EXPLÍCITAS, con signo.

    Positivo si `hasta` está en el futuro respecto de `desde`, negativo si ya pasó, 0 si
    son el mismo día. Reusa el calendario de feriados de `monza_fechas` (fuente única de
    los feriados chilenos) en vez de copiarlo.

    `monza_fechas.business_days_remaining` no sirve acá porque toma "hoy" por su cuenta
    con `date.today()`, y el barrido tiene que comparar contra la fecha de SANTIAGO.
    """
    if desde == hasta:
        return 0
    signo, a, b = (1, desde, hasta) if hasta > desde else (-1, hasta, desde)
    count = 0
    cursor = a
    while cursor < b:
        if cursor.weekday() < 5 and not _is_chile_holiday(cursor):
            count += 1
        cursor += timedelta(days=1)
    return signo * count


def _checks_monza():
    """Reglas de alerta de MonzaParts (marca automotriz)."""
    db = SessionLocal()
    try:
        hoy = _hoy_chile()
        nuevas = 0
        nuevas += _correr_regla("monzaparts", "plazo de proveedor vencido",
                                _monza_ocp_plazo_vencido, db, hoy)
        nuevas += _correr_regla("monzaparts", "venta lista / plazo crítico",
                                _monza_ventas_listas_y_plazo_critico, db, hoy)
        # El contador va al log a propósito: si un día sale 0 con OCs atrasadas vivas, el
        # problema está en el barrido y no en los datos.
        print(f"[scheduler][monzaparts] barrido diario {hoy}: {nuevas} aviso(s) nuevo(s)",
              flush=True)
    finally:
        db.close()


def _monza_ocp_plazo_vencido(db, hoy: date) -> int:
    """(a) OC de proveedor de Monza con `plazo_dias` vencido.

    Esta regla NO EXISTÍA: `monza_oc_proveedor.plazo_dias` se cargaba al comprar y nadie
    lo evaluaba nunca. Equivalente de la regla 2.1 de Grupo AM, adaptada al modelo de
    Monza, que NO tiene tabla OcProveedorItem: el plazo vive en la CABECERA de la OC y el
    vínculo con los ítems es directo (`MonzaCotizacionItem.oc_proveedor_id`).

    La fecha comprometida se calcula con DÍAS HÁBILES (`monza_fechas.add_business_days`),
    no con días corridos: es lo que hace la pantalla de Seguimiento de Grupo AM
    (`routers/compras.py:421`) y la regla de la casa manda usar el helper de días hábiles.
    El scheduler de Grupo AM usa días corridos y es el que quedó fuera de norma; copiarlo
    haría que Monza reclame ANTES de la fecha que el sistema promete en pantalla.
    """
    ocs = db.query(MonzaOcProveedor).filter(
        MonzaOcProveedor.plazo_dias.isnot(None),
        MonzaOcProveedor.created_at.isnot(None),
        # `estado` es nullable: en SQL, `NULL NOT IN (...)` da NULL y la fila se caería del
        # filtro en silencio. Un estado NULL no es 'cancelada' — es una OC viva que hay
        # que evaluar, así que se la deja pasar explícitamente.
        (MonzaOcProveedor.estado.is_(None))
        | (MonzaOcProveedor.estado.notin_(ESTADOS_OCP_CERRADOS)),
    ).all()
    if not ocs:
        return 0

    # Ítems que siguen esperando al proveedor, en UNA sola query: la regla de Grupo AM
    # hace un SELECT por asignación (N+1) y acá no hace falta repetir ese vicio.
    esperando = {}
    for it in db.query(MonzaCotizacionItem).filter(
        MonzaCotizacionItem.oc_proveedor_id.in_([o.id for o in ocs]),
        MonzaCotizacionItem.estado_linea.in_(ESTADOS_ESPERANDO_PROVEEDOR),
    ).all():
        esperando.setdefault(it.oc_proveedor_id, []).append(it)

    creadas = 0
    for ocp in ocs:
        items = esperando.get(ocp.id)
        if not items:
            continue  # nada esperando: el proveedor entregó (o la OC quedó sin líneas)

        plazo = int(ocp.plazo_dias)
        if plazo < 0 or plazo > MAX_PLAZO_DIAS:
            # Dato imposible: no se inventa un plazo, se deja constancia y se sigue. Sin
            # este corte `add_business_days` colgaría el job completo (ver MAX_PLAZO_DIAS).
            print(f"[scheduler][monzaparts] OC {ocp.numero or ocp.id}: plazo_dias={plazo} "
                  f"fuera de rango (0..{MAX_PLAZO_DIAS}), no se evalúa", flush=True)
            continue

        fecha_esperada = add_business_days(ocp.created_at, plazo)
        if hoy <= fecha_esperada:
            continue

        atraso = _dias_habiles_chile(fecha_esperada, hoy)
        partes = [(it.numero_parte or it.descripcion or f"ítem {it.id}") for it in items]
        listado = ", ".join(partes[:MAX_PARTES_EN_MENSAJE])
        if len(partes) > MAX_PARTES_EN_MENSAJE:
            listado += f" y {len(partes) - MAX_PARTES_EN_MENSAJE} más"

        titulo = f"Proveedor atrasado · {ocp.numero or f'OC #{ocp.id}'}"
        mensaje = (
            f"{ocp.proveedor_nombre or 'El proveedor'} se comprometió para el "
            f"{fecha_esperada.strftime('%d-%m-%Y')} y lleva {atraso} día(s) hábil(es) de "
            f"atraso. Faltan {len(items)} ítem(s): {listado}. "
            f"Hay que apurar al proveedor y avisarle al cliente si la entrega se corre."
        )
        if crear_notif(
            db, _recortar(titulo, MAX_TITULO), _recortar(mensaje, MAX_MENSAJE),
            "warning", "/monzaparts/seguimiento", "oc_proveedor", ocp.id,
            rol=ROL_ABASTECIMIENTO, severidad="warning", regla=REGLA_OCP_VENCIDA,
        ):
            creadas += 1
    return creadas


def _monza_ventas_listas_y_plazo_critico(db, hoy: date) -> int:
    """(b) Re-evaluación DIARIA de "venta lista para despacho" y "plazo crítico".

    En Monza estas dos alertas ya existían, pero solo se disparaban en el INSTANTE de
    cerrar una recepción en Bodega (`monza_router_bodega.py:413 _notificar_ventas_listas`).
    Si la última recepción se cerró el lunes y el plazo se vuelve crítico el jueves, nadie
    avisaba: la condición dependía de que ALGUIEN cerrara una recepción ese día.

    Los TÍTULOS son los mismos del aviso instantáneo, a propósito: es el MISMO evento, y
    el título es el ancla que comparten las dos vías para no duplicarse (ver monza_notif).
    El mensaje sí es más explícito sobre qué hacer, porque lo lee el dueño.
    """
    ventas = (
        db.query(MonzaCotizacion)
        # selectinload para los ítems (colección) + joinedload para el cliente: evita
        # tanto el producto cartesiano como el N+1 por venta.
        .options(selectinload(MonzaCotizacion.items), joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacion.estado.in_(("vendida", "despachado")))
        .all()
    )

    creadas = 0
    for cot in ventas:
        if not cot.items:
            continue
        cliente = cot.cliente.nombre if cot.cliente else "sin cliente"
        en_bodega = [i for i in cot.items if i.estado_linea == "en_bodega"]
        todos_en_bodega = len(en_bodega) == len(cot.items)

        # ── Venta lista para despacho ───────────────────────────────────────────
        if todos_en_bodega:
            titulo = f"Venta {cot.numero} lista para despacho"
            mensaje = (
                f"Todos los ítems ({len(cot.items)}) están en bodega · {cliente}. "
                f"Ya se puede preparar el despacho y emitir la guía."
            )
            if crear_notif(
                db, _recortar(titulo, MAX_TITULO), _recortar(mensaje, MAX_MENSAJE),
                "success", "/monzaparts/despachos", "cotizacion", cot.id,
                rol=ROL_VENTAS, severidad="info", regla=REGLA_VENTA_LISTA,
            ):
                creadas += 1
            continue  # ya está completa: el plazo crítico (que es para las PARCIALES) no aplica

        # ── Plazo crítico con la venta PARCIAL en bodega ────────────────────────
        # Misma condición que Bodega y que la B7 de Grupo AM: hay algo listo para salir y
        # algo que todavía no llega. Sin nada en bodega no hay decisión que tomar hoy.
        if not en_bodega or not cot.fecha_entrega_est:
            continue
        dias = _dias_habiles_chile(hoy, cot.fecha_entrega_est)
        if dias > DIAS_PLAZO_CRITICO:
            continue

        titulo = f"Plazo crítico · {cot.numero}"
        cuando = (
            f"Quedan {dias} día(s) hábil(es) para la entrega"
            if dias >= 0 else
            f"El plazo de entrega venció hace {abs(dias)} día(s) hábil(es)"
        )
        mensaje = (
            f"{cuando} y la venta está PARCIAL en bodega "
            f"({len(en_bodega)} de {len(cot.items)} ítems) · {cliente}. "
            f"Conviene despachar lo que ya llegó o avisarle al cliente."
        )
        if crear_notif(
            db, _recortar(titulo, MAX_TITULO), _recortar(mensaje, MAX_MENSAJE),
            "warning", "/monzaparts/despachos", "cotizacion", cot.id,
            rol=ROL_VENTAS, severidad="critical", regla=REGLA_PLAZO_CRITICO,
        ):
            creadas += 1
    return creadas


# ══════════════════════════════════════════════════════════════════════════════

def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    tz = pytz.timezone("America/Santiago")
    scheduler.add_job(
        run_daily_checks,
        CronTrigger(hour=6, minute=0, timezone=tz),
        id="daily_checks",
        replace_existing=True,
    )
    scheduler.start()
    print("[scheduler] APScheduler started — daily checks at 06:00 Santiago")
    return scheduler
