"""Textos de MonzaParts más largos que su columna: 422 con un mensaje que el usuario
entiende, en vez de un 500 crudo ("Data too long for column ...") y el registro perdido.

El ANCHO se lee de la columna del modelo, no se copia acá, para que no se desincronicen.
Cuenta CARACTERES, igual que MariaDB en utf8mb4: una tilde o una ñ valen uno.

POR QUÉ (PROD):
  · 2026-09-25: el N° de parte de un ítem de lead con la descripción pegada (100 máx.).
  · 2026-08-20 y 2026-10-09: el VIN escrito en el campo Año (10 máx.) al crear una
    cotización y un lead — tres reintentos cada vez y el registro nunca se guardó.
  · 2026-10-27: un enlace del proveedor en el N° de parte, desde la calculadora.
"""
import re

from fastapi import HTTPException

NOMBRES = {
    "descripcion": "La descripción",
    "numero_parte": "El N° de parte",
    "marca": "La marca",
    "procedencia": "La procedencia",
    "calidad": "La calidad",
    "plazo_entrega": "El plazo de entrega",
    "vehiculo": "El vehículo",
    "modelo": "El modelo",
    "anio": "El año",
    "vin": "El VIN",
    "linea": "La línea",
    "tipo_cotizacion": "El tipo de cotización",
    "forma_pago": "La forma de pago",
    "oc_cliente": "La OC del cliente",
}

_PISTAS = {
    "numero_parte": (" Si pegaste la descripción del repuesto, va en el campo Descripción;"
                     " si es un enlace del proveedor, pégalo en el comentario del lead."),
}

# 17 caracteres sin I, O ni Q: la forma de un VIN (ISO 3779).
_VIN = re.compile(r"[A-HJ-NPR-Z0-9]{17}", re.IGNORECASE)


def limpio(valor):
    """Sin espacios ni tabulaciones a los lados ("2015 " → "2015"); lo demás tal cual."""
    return valor.strip() if isinstance(valor, str) else valor


def validar_largos(datos: dict, tabla, prefijo: str = "") -> None:
    """422 si algún texto de `datos` no cabe en su columna de `tabla` (un `__table__`).
    Revisa solo los campos con nombre en NOMBRES que existan en la tabla."""
    for campo, nombre in NOMBRES.items():
        valor = datos.get(campo)
        if not isinstance(valor, str) or campo not in tabla.c:
            continue
        maximo = getattr(tabla.c[campo].type, "length", None)
        if not maximo or len(valor) <= maximo:
            continue
        if campo == "anio" and _VIN.fullmatch(valor.strip()):
            detalle = f"{prefijo}Eso parece un VIN: va en el campo VIN, no en Año."
        else:
            detalle = (f"{prefijo}{nombre} admite hasta {maximo} caracteres y tiene "
                       f"{len(valor)}.{_PISTAS.get(campo, '')}")
        raise HTTPException(status_code=422, detail=detalle)
