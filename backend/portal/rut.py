"""Helpers de RUT del Portal de Clientes: sanear, canonizar, validar y generar.

Nacen de los de routers/contabilidad.py (_rut_saneado, _rut_canonico, _rut_valido) y NO
se importan de ahí: un router de 3.500 líneas arrastra medio ERP y, peor, un helper
compartido cambia de significado según quién lo llame. Coinciden con los de Contabilidad
en todo RUT escrito normalmente (lo vigila portal/tests/test_vinculacion.py) y se apartan
A PROPÓSITO en tres bordes, porque acá el RUT decide a quién se le muestra una venta:
  · solo valen dígitos ASCII (ver rut_valido);
  · los ceros a la izquierda no cambian de quién es el RUT (ver rut_canonico);
  · se tolera la tipografía de un RUT pegado desde Word, un PDF o una página web: guion
    largo, espacio no separable, caracteres invisibles (ver rut_saneado). Sin eso la
    venta cerrada queda fuera del portal en silencio, con el RUT viéndose correcto en
    pantalla. Facturación lo seguirá rechazando hasta que se corrija en la cotización.

El generador (rut_con_dv) vive acá y no en el script del piloto porque lo usan también
las fábricas de las suites. Comparte digito_verificador con rut_valido: por eso el
oráculo del módulo 11 en las pruebas son RUT fijos, no generados.

Sin imports del backend: se prueba sin levantar nada. service.py los re-exporta, así que
routers/clientes.py, portal/dtes.py y las suites siguen importándolos de ahí.
"""
import unicodedata
from typing import Optional

# Guiones que Word, un PDF o una página web ponen en lugar del '-' del teclado (U+2010 a
# U+2015 y el signo menos U+2212). Van como escapes: escritos tal cual no se distinguen de
# un guion común en el editor.
_GUIONES_TIPOGRAFICOS = "‐‑‒–—―−"
_A_GUION = dict.fromkeys(map(ord, _GUIONES_TIPOGRAFICOS), "-")


def digito_verificador(cuerpo: str) -> str:
    """Dígito verificador (módulo 11) de un cuerpo de RUT de solo dígitos ASCII. Es el
    ÚNICO cálculo del módulo 11 del portal: lo usan el validador y el generador."""
    suma, factor = 0, 2
    for d in reversed(cuerpo):
        suma += int(d) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    return "0" if resto == 11 else "K" if resto == 10 else str(resto)


def rut_saneado(rut: Optional[str]) -> str:
    """Sin puntos, sin espacios de ningún tipo (tampoco el no separable ni el tabulador),
    sin caracteres invisibles de formato, con el guion del teclado y en mayúsculas."""
    texto = (rut or "").translate(_A_GUION)
    limpio = "".join(c for c in texto if not c.isspace() and unicodedata.category(c) != "Cf")
    return limpio.replace(".", "").upper()


def rut_canonico(rut: Optional[str]) -> str:
    """Forma para COMPARAR (cuerpo+DV sin puntos, guion ni ceros a la izquierda):
    '76.874.971-K', '76874971k' y '076.874.971-K' son el mismo RUT.

    Los ceros a la izquierda se quitan acá y no en Contabilidad: con ellos, una venta
    digitada '076…' no calzaría con su ficha, y dos fichas del mismo RUT no se
    detectarían como ambiguas."""
    return rut_saneado(rut).replace("-", "").lstrip("0")


def rut_valido(rut: Optional[str]) -> bool:
    """RUT chileno bien formado (módulo 11). Acepta con o sin puntos/guion."""
    r = rut_saneado(rut)
    if "-" in r:
        cuerpo, _, dv = r.partition("-")
    else:
        cuerpo, dv = r[:-1], r[-1:]
    # isascii además de isdigit: '²' o '٣' son "dígitos" para str.isdigit, y con ellos
    # int() revienta (el superíndice) o el canónico no calza con nada (otros alfabetos).
    # Un RUT así en UNA sola ficha tumbaría el cálculo del mapa completo.
    if not (cuerpo.isascii() and cuerpo.isdigit()) or len(cuerpo) < 7 or not dv:
        return False
    return dv == digito_verificador(cuerpo)


def rut_con_dv(cuerpo: str) -> str:
    """Cuerpo → RUT con su dígito verificador y el formato de la casa ('76.874.971-K').
    Para sembrar datos: el piloto y las pruebas."""
    return f"{int(cuerpo):,}".replace(",", ".") + f"-{digito_verificador(cuerpo)}"
