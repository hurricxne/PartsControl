"""Teléfono normalizado para BUSCAR y para IDENTIFICAR (MonzaParts).

Hermano de `monza_rut.py`, y nace de la misma enfermedad en el otro campo del dedupe.
Dos hallazgos del equipo de testing (2026-08-27), ambos reproducidos:

  1. CRÍTICO — el dedupe por teléfono PISABA al RUT. Dos clientes distintos («EMPRESA A
     SpA» 76.111.222-3 y «EMPRESA B Ltda» 77.333.444-5) que comparten un número —la
     recepción del taller, el celular del gestor, o un '-' de relleno tecleado en las dos
     fichas— terminaban en la MISMA ficha, y el RUT recién tecleado se descartaba en
     silencio. De ahí en adelante la cotización, el cierre y la FACTURA 33 colgaban de la
     ficha equivocada: el DTE salía al RUT de otro contribuyente.
  2. ALTO — el buscador comparaba el teléfono literal, así que el vendedor que teclea el
     número como lo tiene en WhatsApp («+56 9 6229 3336») no encontraba a la ficha
     guardada como «962293336».

CÓMO DECIDE QUIÉN ES QUIÉN
    `telefono_identidad` se queda con los ÚLTIMOS 9 dígitos, que es el número nacional
    chileno (9 6229 3336 / 2 2345 6789): así «+56962293336», «56962293336» y «962293336»
    son el MISMO abonado sin tener que adivinar el formato con que se guardó. Por debajo
    de 8 dígitos devuelve "" —«esto no identifica a nadie»—, la misma regla con la que
    `rut_identidad` descarta un '-' de relleno. Sin ella, la ficha con teléfono '2342'
    que existe hoy en la base se habría comido a todo cliente nuevo con ese número.
"""
import re

from sqlalchemy import func

# Un teléfono chileno útil tiene 8 dígitos (fijo antiguo) o 9 (móvil y fijo actual).
# Por debajo de eso es relleno ('-', '0', '2342'): no identifica a un abonado y por lo
# tanto NO puede fusionar dos fichas de clientes.
TELEFONO_MIN_DIGITOS = 8
# El número nacional chileno tiene 9 dígitos; lo que venga delante es código de país o
# prefijo de salida, y no distingue a un abonado de otro.
_LARGO_NACIONAL = 9

_SOLO_DIGITOS = re.compile(r"\D+")


def telefono_norm_py(valor) -> str:
    """Solo los dígitos de lo que el operador tecleó."""
    if not valor:
        return ""
    return _SOLO_DIGITOS.sub("", str(valor))


def telefono_norm_sql(col):
    """Quita los separadores habituales de la columna dentro del SELECT.

    No es tan completo como la versión Python (SQL portable no tiene «borrar todo lo que
    no sea dígito»), pero cubre lo que un humano tipea: '+', espacios, guiones,
    paréntesis y puntos. Se usa solo como PREFILTRO barato — la decisión final siempre la
    toma `telefono_identidad` en Python, así que un formato exótico que se le escape al
    SQL no puede provocar un match incorrecto, a lo sumo uno que no se encuentra.
    `REPLACE` existe igual en MySQL (producción) y en SQLite (algunas suites).
    """
    limpio = col
    for sep in ("+", " ", "-", "(", ")", "."):
        limpio = func.replace(limpio, sep, "")
    return limpio


def telefono_identidad(valor) -> str:
    """Llave de IDENTIDAD del teléfono: los últimos 9 dígitos, o "" si no identifica.

    Espejo exacto de `rut_identidad`: BUSCAR tolera pedazos, IDENTIFICAR no. Cadena
    vacía significa «no hay teléfono útil» y quien decide identidad debe tratarla como
    ausencia, JAMÁS como una coincidencia con las otras fichas que también la tienen
    vacía — ese fue el bug que fusionó clientes distintos.
    """
    d = telefono_norm_py(valor)
    if len(d) < TELEFONO_MIN_DIGITOS:
        return ""
    return d[-_LARGO_NACIONAL:] if len(d) >= _LARGO_NACIONAL else d


def buscar_ficha_por_telefono(db, modelo, telefono, rut_tecleado=None):
    """La ficha de ese teléfono, o None. ÚNICA puerta del dedupe por teléfono.

    LA REGLA QUE CIERRA EL CRÍTICO: el teléfono NUNCA puede fusionar dos fichas cuyos
    RUT se contradicen. Un número compartido (recepción, gestor, taller) es evidencia
    DÉBIL de identidad; el RUT es evidencia FUERTE. Así que una ficha candidata se
    descarta si ya tiene un RUT que identifica y es DISTINTO del que el operador acaba
    de teclear.

    Lo que sí sigue funcionando —y es el caso legítimo por el que este dedupe existe—:
    la ficha vieja sin RUT que comparte teléfono se reconoce como el mismo cliente, y el
    RUT recién tecleado la COMPLETA en vez de crear una ficha duplicada.
    """
    from monza_rut import rut_identidad

    llave = telefono_identidad(telefono)
    if not llave:
        return None
    # Prefiltro barato en SQL (superset por sufijo); la decisión final, en Python.
    candidatos = (db.query(modelo)
                  .filter(telefono_norm_sql(modelo.telefono).like(f"%{llave}"))
                  .all())
    llave_rut = rut_identidad(rut_tecleado)
    for c in candidatos:
        if telefono_identidad(c.telefono) != llave:
            continue
        rut_ficha = rut_identidad(getattr(c, "rut", None))
        if rut_ficha and llave_rut and rut_ficha != llave_rut:
            # Mismo número, contribuyentes distintos: son DOS clientes.
            continue
        return c
    return None
