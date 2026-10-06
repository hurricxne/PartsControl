"""Fechas del portal: el "hoy" y los días que ve el cliente son los de Chile.

El servidor corre en UTC. Pasadas las 20-21 h de Chile, `date.today()` ya es "mañana": un
ítem comprometido para hoy aparecería vencido la misma tarde, y un despacho cerrado a
las 21:00 saldría fechado al día siguiente. Todo lo que el portal muestra o compara como
DÍA pasa por acá.

TRES CLASES DE FECHA EN LA BASE, y cada una tiene su función:
  · un INSTANTE que escribió el reloj del servidor (created_at, fecha_despacho,
    fecha_cierre): está en UTC → `dia_chile`;
  · un DÍA que alguien tecleó y quedó en una columna de fecha y hora, a medianoche
    (despachos.fecha_firma): ya es el día correcto → `dia_tecleado`. Pasarlo por
    `dia_chile` lo corre al día anterior;
  · un TEXTO libre (oc_cliente.fecha_oc) → `fecha_de_texto`.
Una columna DATE (fecha_guia, fecha_emision, fecha_promesa_cliente) ya es un día: se usa
su `.isoformat()` sin más.
"""
from datetime import date, datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

TZ_CHILE = ZoneInfo("America/Santiago")


def hoy_chile() -> date:
    return datetime.now(TZ_CHILE).date()


def dia_chile(momento: Union[datetime, date, None]) -> Optional[str]:
    """Un INSTANTE guardado por el servidor → el día de Chile en que ocurrió (AAAA-MM-DD).

    Si llega sin zona (naive) se le declara UTC antes de convertir. Un `date` puro ya es
    un día: se devuelve tal cual.
    """
    if momento is None:
        return None
    if isinstance(momento, datetime):
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)
        return momento.astimezone(TZ_CHILE).date().isoformat()
    return momento.isoformat()


def dia_tecleado(momento: Union[datetime, date, None]) -> Optional[str]:
    """Un DÍA tecleado que el ERP guardó en una columna de fecha y hora → AAAA-MM-DD.

    Es el caso de la firma de la guía: routers/despachos.py guarda el día que eligió el
    operador a medianoche exacta, sin zona. Ese valor ya es el día de Chile. Si la
    columna trae hora es porque no se tecleó nada y quedó el reloj del servidor: ahí sí
    es un instante y se convierte.
    """
    if isinstance(momento, datetime) and momento.time() == datetime.min.time():
        return momento.date().isoformat()
    return dia_chile(momento)


# Cómo teclea una fecha la gente del ERP (día primero, que es el uso en Chile).
_FORMATOS_ANIO_COMPLETO = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d")
# Con el año en dos dígitos el texto es ambiguo («26-06-10» puede ser 26 de junio de 2010
# o 10 de junio de 2026). Solo se acepta si el resultado cae cerca de una fecha conocida.
_FORMATOS_ANIO_CORTO = ("%d/%m/%y", "%d-%m-%y")
_ANIOS_CREIBLES = range(2000, 2101)
DIAS_DE_HOLGURA_ANIO_CORTO = 2 * 366


def fecha_de_texto(texto: Optional[str], referencia: Optional[date] = None) -> Optional[str]:
    """Una fecha escrita a mano («10/06/2026», «2026-06-10», «10-06-26») → AAAA-MM-DD, o
    None si no se entiende.

    `oc_cliente.fecha_oc` es un campo de texto libre: hay fechas en varios formatos y
    también cosas que no son fechas. Al cliente se le entrega una fecha normalizada o
    nada; nunca el texto crudo (la pantalla lo mostraría como «Invalid Date» o, peor, lo
    leería con mes y día al revés).

    `referencia` es una fecha cercana y conocida (el día en que se ingresó la orden): un
    texto con el año en dos dígitos solo vale si el resultado queda a menos de dos años
    de ella. Sin referencia, esos textos no se aceptan.
    """
    limpio = (texto or "").strip()
    if not limpio:
        return None
    solo_dia = limpio.replace("T", " ").split(" ")[0]
    for formato in _FORMATOS_ANIO_COMPLETO:
        try:
            dia = datetime.strptime(solo_dia, formato).date()
        except ValueError:
            continue
        return dia.isoformat() if dia.year in _ANIOS_CREIBLES else None
    if referencia is None:
        return None
    for formato in _FORMATOS_ANIO_CORTO:
        try:
            dia = datetime.strptime(solo_dia, formato).date()
        except ValueError:
            continue
        cerca = abs((dia - referencia).days) <= DIAS_DE_HOLGURA_ANIO_CORTO
        return dia.isoformat() if cerca else None
    return None
