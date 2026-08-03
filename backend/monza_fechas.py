"""Días hábiles Chile para los módulos MonzaParts (Fase 4 del espejo Grupo AM).

COPIA deliberada de routers/compras.py:69-141 (Grupo AM) y no un import: compras.py es
código 100% Grupo AM con candado de minería — importarlo desde Monza acoplaría las dos
empresas por un helper de calendario. La vara es ÚNICA: los días hábiles se calculan en
el BACKEND y el frontend solo pinta el número.
"""
from datetime import date, datetime, timedelta


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
    """Días hábiles (Chile) hasta deadline. Negativo si ya pasó."""
    today = date.today()
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
