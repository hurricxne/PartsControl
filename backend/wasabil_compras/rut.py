"""RUT canónico — la llave de cruce entre el libro del SII y el ERP.

POR QUÉ EXISTE (hallazgo del reconocimiento, 4 de 8 lentes): el ERP no tenía RUT de
proveedor en NINGUNA parte — la tabla `proveedores` ni siquiera tenía la columna, y el único
RUT del sistema (`cont_compra.proveedor_rut`) es texto libre tecleado a mano. Wasabil entrega
'76.513.680-6'; un humano teclea '76513680-6' o '76.513.680-6 ' con espacio. Sin UNA forma
canónica, el informe «existe en el SII y no en el ERP» acusa como faltantes facturas que ya
están cargadas, y el operador las carga de nuevo: el remedio ES el doble conteo.

LA FORMA CANÓNICA elegida: cuerpo sin puntos + guión + DV en mayúscula → '76513680-6'.
  - Sin puntos: los puntos son formato de pantalla, no dato.
  - CON guión: separa el DV sin ambigüedad ('761234567' ¿es 76123456-7 o 7612345-67?).
  - Se guarda canónico y se FORMATEA al mostrar (rut_formateado).

Módulo PURO: sin base de datos, sin red, sin imports del resto del backend — así lo pueden
usar el cliente del libro, la migración, los routers y los tests sin arrastrar nada.
"""
from typing import Optional


def rut_canonico(rut) -> Optional[str]:
    """Texto libre → RUT canónico 'CUERPO-DV', o None si no tiene forma de RUT.

    None NO es un error: es «esto no es un RUT» (celda vacía, un nombre, basura). El
    llamador decide si eso bloquea (una CxP nueva) o se tolera (dato legado).
    Deliberadamente NO valida el dígito verificador: canónico ≠ válido. Un RUT mal
    digitado pero con forma de RUT se canoniza igual — así el cruce lo ENCUENTRA y el
    operador ve la discrepancia, en vez de que el sistema lo esconda como "no-RUT".

    Dónde está el DV según lo que se recibió (la estructura del texto manda):
      · con guión → lo de después del guión es el DV ('76.513.680-6', '76513680-6');
      · sin guión y sin puntos → el último carácter es el DV ('765136806' pegado);
      · con puntos pero SIN guión → None: el formato chileno con puntos SIEMPRE lleva el
        guión antes del DV, así que '76.513.680' es un RUT al que le FALTA el dígito —
        adivinar que el último dígito del cuerpo es el DV lo convertiría en OTRO RUT
        ('7651368-0'), que es peor que rechazarlo.
    """
    if rut is None:
        return None
    texto = str(rut).strip().upper().replace(" ", "")
    if not texto:
        return None
    if "-" in texto:
        cuerpo_txt, _, dv = texto.rpartition("-")
        cuerpo_txt = cuerpo_txt.replace(".", "")
    elif "." in texto:
        return None  # puntos sin guión = le falta el DV (ver docstring)
    else:
        cuerpo_txt, dv = texto[:-1], texto[-1]
    if not cuerpo_txt.isdigit() or not (dv.isdigit() or dv == "K") or len(dv) != 1:
        return None
    cuerpo = int(cuerpo_txt)  # bota ceros a la izquierda: '076…' == '76…'
    # Un RUT real tiene cuerpo de 6 a 8 dígitos significativos; '12-3' es un tipeo y un
    # cuerpo de 9+ no existe en el registro chileno.
    if not (6 <= len(str(cuerpo)) <= 8):
        return None
    return f"{cuerpo}-{dv}"


def rut_valido(rut) -> bool:
    """Módulo 11 del SII sobre CUALQUIER formato de entrada. Para exigir en datos NUEVOS.

    Se calcula sobre la forma canónica: si ni siquiera tiene forma de RUT, es inválido.
    """
    canon = rut_canonico(rut)
    if canon is None:
        return False
    cuerpo, dv = canon.split("-")
    suma, factor = 0, 2
    for d in reversed(cuerpo):
        suma += int(d) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    dv_ok = "0" if resto == 11 else "K" if resto == 10 else str(resto)
    return dv == dv_ok


def rut_formateado(rut) -> str:
    """Canónico (o texto libre) → '76.513.680-6' para pantallas. Si no es RUT, lo devuelve
    tal cual: mostrar algo raro es mejor que esconderlo detrás de un string vacío."""
    canon = rut_canonico(rut)
    if canon is None:
        return "" if rut is None else str(rut)
    cuerpo, dv = canon.split("-")
    con_puntos = ""
    for i, d in enumerate(reversed(cuerpo)):
        if i and i % 3 == 0:
            con_puntos = "." + con_puntos
        con_puntos = d + con_puntos
    return f"{con_puntos}-{dv}"
