"""Fechas de negocio de MonzaParts: días hábiles Chile + el «hoy» de Chile.

COPIA deliberada de routers/compras.py:69-141 (Grupo AM) y no un import: compras.py es
código 100% Grupo AM con candado de minería — importarlo desde Monza acoplaría las dos
empresas por un helper de calendario. La vara es ÚNICA: los días hábiles se calculan en
el BACKEND y el frontend solo pinta el número.

SECCIÓN «HORA DE CHILE» (2026-08-22): el mismo criterio aplicado a los FILTROS. Las
columnas de fecha se escriben con `datetime.utcnow()` (UTC naive) pero el operador digita
DÍAS DE CHILE, así que comparar la fecha cruda contra la columna corría el borde 3-4
horas y —peor— el `hasta` se interpretaba como medianoche, dejando fuera el día entero
que el operador acababa de pedir. Acá vive la conversión, una sola vez, para que las
pestañas no puedan divergir (precedente: «el hoy del negocio es el de Chile»,
logística 2026-08-11).
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException


def _easter_date(year: int) -> date:
    """Algoritmo de Meeus/Jones/Butcher para calcular Pascua."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _is_chile_holiday(d: date) -> bool:
    """Retorna True si la fecha es feriado en Chile (fijos + Viernes Santo)."""
    fixed = {
        (1, 1),   # Año Nuevo
        (5, 1),   # Día del Trabajo
        (5, 21),  # Glorias Navales
        (6, 20),  # Día de los Pueblos Indígenas (tercer lunes junio — aproximado fijo)
        (6, 29),  # San Pedro y San Pablo
        (7, 16),  # Virgen del Carmen
        (8, 15),  # Asunción de la Virgen
        (9, 18),  # Independencia
        (9, 19),  # Glorias del Ejército
        (10, 12), # Encuentro de Dos Mundos
        (10, 31), # Día de las Iglesias Evangélicas
        (11, 1),  # Todos los Santos
        (12, 8),  # Inmaculada Concepción
        (12, 25), # Navidad
    }
    if (d.month, d.day) in fixed:
        return True
    # Viernes Santo (2 días antes de Pascua)
    easter = _easter_date(d.year)
    good_friday = easter - timedelta(days=2)
    return d == good_friday


def add_business_days(start_date, days: int) -> date:
    """Retorna la fecha que resulta de agregar *days* días hábiles (Chile)."""
    if isinstance(start_date, datetime):
        current = start_date.date()
    else:
        current = start_date
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5 and not _is_chile_holiday(current):
            added += 1
    return current


def business_days_remaining(deadline: date) -> int:
    """Días hábiles (Chile) hasta deadline. Negativo si ya pasó.

    El «hoy» es el de CHILE, no el del servidor: en el VPS (que corre en UTC) el
    semáforo de los plazos se adelantaba un día durante la tarde-noche chilena.
    """
    today = hoy_chile()
    if deadline == today:
        return 0
    step = 1 if deadline > today else -1
    count = 0
    cursor = today
    while cursor != deadline:
        cursor += timedelta(days=step)
        if cursor.weekday() < 5 and not _is_chile_holiday(cursor):
            count += step
    return count


# ═══════════════════════ HORA DE CHILE (filtros y «hoy») ═══════════════════════
#
# POR QUÉ EXISTE ESTA SECCIÓN
#   Las columnas de fecha del sistema se escriben con `datetime.utcnow()` (UTC naive),
#   pero el operador digita DÍAS DE CHILE en un <input type="date">. Comparar la fecha
#   cruda contra la columna tiene DOS defectos que se sumaban:
#     1. El borde se corría 3-4 horas: un lead creado a las 21:30 del día 30 (Chile)
#        quedaba registrado el 31 UTC y no aparecía al filtrar «hasta el 30».
#     2. El `hasta` se comparaba con `<=` contra la MEDIANOCHE del día elegido, así que
#        filtrar «hasta hoy» escondía TODO lo de hoy — el operador concluía que el
#        filtro estaba roto (y tenía razón).
#   La conversión vive acá una sola vez: si cada router la resolviera por su cuenta,
#   dos pestañas terminarían diciendo cosas distintas con el mismo filtro.

TZ_CHILE = ZoneInfo("America/Santiago")


def ahora_chile() -> datetime:
    """«Ahora» en hora de Chile (aware).

    ÚNICA fuente de «ahora» de este módulo: las suites parchan ESTA función en vez de
    tocar el reloj del sistema, así que las sondas de borde de mes son deterministas.
    """
    return datetime.now(TZ_CHILE)


def hoy_chile() -> date:
    """El «hoy» del negocio: el día de Chile, no el del servidor en UTC.

    Precedente de la casa (logística 2026-08-11): un despacho cerrado a las 21:30 de
    Chile es de HOY, aunque en UTC ya sea mañana.
    """
    return ahora_chile().date()


def _dia_chile_a_utc_naive(d: date) -> datetime:
    """00:00 de ese día EN CHILE, expresado como UTC naive.

    UTC naive a propósito: las columnas se escriben con `datetime.utcnow()` (naive), y
    mezclar aware con naive en una comparación de SQLAlchemy revienta. El offset lo
    resuelve zoneinfo, así que el verano (-03) y el invierno (-04) chilenos salen
    correctos sin un solo número mágico.

    DST: en la madrugada del arranque del horario de verano las 00:00 locales no
    existen; zoneinfo NO levanta excepción — resuelve con el offset posterior al salto y
    entrega un instante válido y monótono. Documentado porque es comportamiento
    deliberado, no casualidad (sonda en test_monza_rango_fechas).
    """
    return (datetime.combine(d, time.min, tzinfo=TZ_CHILE)
            .astimezone(timezone.utc)
            .replace(tzinfo=None))


def dia_chile_utc(d: date) -> datetime:
    """00:00 de ese día EN CHILE, como UTC naive. Versión pública de la conversión.

    La necesitan las TARJETAS de resumen, que cuentan «lo de hoy» sin pasar por
    `rango_utc` (que parte de lo que digitó el operador). Contar con `func.date(col) ==
    hoy` NO sirve: compara el día en UTC contra el día de Chile, así que la tarjeta y el
    filtro de la misma pantalla se contradicen 3-4 horas cada noche. Con esta función,
    tarjeta y filtro usan LA MISMA conversión — que es la razón de existir del módulo.
    """
    return _dia_chile_a_utc_naive(d)


def parse_fecha_op(valor: str, campo: str) -> date:
    """Fecha que digitó el OPERADOR (`YYYY-MM-DD`) → date. Inválida ⇒ 422.

    FALLA CERRADO a propósito: un filtro que el operador pidió y el backend ignora en
    silencio (el `try/except pass` que había en Logs y Despachos) es peor que un error —
    el operador cree que está viendo el universo filtrado y está viendo otro. Solo se
    acepta fecha pura, que es lo único que emite un <input type="date">: un datetime ISO
    completo se rechaza en vez de descartarle la hora a medias.
    """
    texto = (valor or "").strip()
    # `date.fromisoformat` acepta desde Python 3.11 formatos que un <input type="date">
    # jamás manda y que el operador no reconocería: la semana ISO ('2026-W01-1', que
    # resuelve al 29-dic-2025 — ¡otro año!) y el compacto sin guiones ('20260315').
    # Aceptarlos en silencio sería filtrar por una fecha que el operador no pidió, así
    # que la forma se valida ANTES: exactamente AAAA-MM-DD.
    if len(texto) != 10 or texto[4] != "-" or texto[7] != "-":
        raise HTTPException(
            status_code=422,
            detail=f"Parámetro '{campo}' inválido: '{valor}'. Usa el formato AAAA-MM-DD.",
        )
    try:
        return date.fromisoformat(texto)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=422,
            detail=f"Parámetro '{campo}' inválido: '{valor}'. Usa el formato AAAA-MM-DD.",
        )


def rango_utc(desde: Optional[str], hasta: Optional[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Rango SEMIABIERTO [desde, hasta) en UTC naive, a partir de días de Chile.

    Para columnas DateTime escritas con `datetime.utcnow()` (fecha_creacion, fecha_venta,
    MonzaLog.fecha…). USO OBLIGATORIO en el filtro:

        col >= desde_utc     y     col <  hasta_utc      ← SIEMPRE `<`, nunca `<=`

    El `hasta_utc` es la medianoche de Chile del día SIGUIENTE: así el día que el
    operador escribió entra COMPLETO, que es lo que cualquiera espera al pedir «hasta
    el 25». Vacío o None ⇒ None en ese extremo (sin filtro). `desde` posterior a `hasta`
    no es un error: devuelve un rango vacío honesto (0 filas), no un 422 — el operador
    ve que no hay nada y corrige, en vez de recibir un mensaje técnico.

    Para columnas Date CIVILES (fecha_despacho, que ya guarda un día sin hora) NO se usa
    esta función: ahí el `<=` inclusivo ya es correcto — ver `rango_dias`.
    """
    desde_utc = _dia_chile_a_utc_naive(parse_fecha_op(desde, "desde")) if desde else None
    hasta_utc = None
    if hasta:
        dia = parse_fecha_op(hasta, "hasta")
        try:
            dia_siguiente = dia + timedelta(days=1)
        except OverflowError:
            # 9999-12-31 es una fecha VÁLIDA que el parser acepta, pero sumarle el día
            # del borde semiabierto se sale del calendario de Python. Falla CERRADO como
            # el resto del módulo: un 422 que el operador entiende, no un 500 con
            # traceback (y es alcanzable desde la pantalla: los <input type="date"> no
            # llevan `max`).
            raise HTTPException(
                status_code=422,
                detail=f"Parámetro 'hasta' fuera de rango: '{hasta}'. Usa una fecha real "
                       "(AAAA-MM-DD).",
            )
        hasta_utc = _dia_chile_a_utc_naive(dia_siguiente)
    return desde_utc, hasta_utc


def rango_dias(desde: Optional[str], hasta: Optional[str]) -> Tuple[Optional[date], Optional[date]]:
    """Rango INCLUSIVO de días civiles, con el mismo 422 fail-closed que `rango_utc`.

    Para columnas Date que ya guardan un día sin hora (MonzaCotizacion.fecha_despacho):
    ahí no hay zona horaria que convertir y el `<=` inclusivo ES la semántica correcta.
    Existe como función aparte —y no como una bandera de `rango_utc`— para que nadie
    aplique el semiabierto UTC a una columna civil y rompa un filtro que estaba sano.

        col >= desde_d      y      col <= hasta_d        ← acá SÍ es `<=`
    """
    return (parse_fecha_op(desde, "desde") if desde else None,
            parse_fecha_op(hasta, "hasta") if hasta else None)


def inicio_mes_utc() -> datetime:
    """Primer día del mes EN CURSO DE CHILE, 00:00 Chile, como UTC naive.

    Los KPIs del mes se cortaban con `datetime(utcnow().year, utcnow().month, 1)`: entre
    las 21:00 y la medianoche de Chile del último día del mes, UTC ya estaba en el mes
    siguiente y las ventas de esas horas caían en el mes equivocado.
    """
    hoy = hoy_chile()
    return _dia_chile_a_utc_naive(date(hoy.year, hoy.month, 1))
