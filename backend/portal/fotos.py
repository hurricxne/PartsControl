"""Las fotos de los repuestos que ve el cliente: cómo se guardan y de dónde se leen.

QUÉ ES UNA FOTO DEL PORTAL
    La que el personal saca al despachar para que el cliente reconozca lo suyo. NO es la
    foto de recepción en bodega (routers/bodega.py), que es evidencia interna y vive en
    otra tabla a propósito — ver portal/models.py::FotoArticulo.

SE GUARDA SOLO UN DERIVADO
    La imagen que sube el teléfono se abre, se endereza, se reduce y se vuelve a escribir
    como JPEG. Dos razones, las dos de fondo:
      · Los metadatos del original traen el GPS de la bodega y una hora que puede
        contradecir el hito declarado. El derivado se escribe solo con los píxeles: ni
        EXIF, ni comentario, ni perfil de color. El original no se guarda en ninguna
        parte.
      · Una foto de teléfono pesa 3-8 MB y en la red de una faena no carga. El derivado
        queda en un lado largo de 1600 px.
    Re-codificar además garantiza que lo guardado ES una imagen: un archivo que no se
    puede abrir como JPEG/PNG/WebP se rechaza, sea cual sea su extensión.

EL NOMBRE Y LA RUTA los decide el servidor (uuid4 + '.jpg' dentro de DIRECTORIO). Nada
que venga del cliente participa de la ruta, ni al escribir ni al leer.
"""
import io
import os
import re
import threading
import uuid
from typing import Optional

from PIL import Image, ImageOps, UnidentifiedImageError

DIRECTORIO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads", "portal_fotos"))

LADO_MAXIMO_PX = 1600
CALIDAD_JPEG = 82
PESO_MAXIMO_SUBIDA = 15 * 1024 * 1024   # lo que acepta la subida, antes de reducir
FORMATOS_ACEPTADOS = ("JPEG", "PNG", "WEBP", "MPO")  # MPO: el JPEG de algunos teléfonos
FORMATOS_JPEG = ("JPEG", "MPO")
# Tope contra una "bomba de descompresión": pocos KB en disco que al abrirse son cientos
# de MB. Un JPEG se le pide al decodificador ya reducido (`draft`), así que aguanta la
# foto más grande de un teléfono; un PNG o un WebP se decodifican enteros (un PNG de
# 60 MP, que en disco pesa 230 KB, ocupa más de 500 MB): su tope es más bajo.
PIXELES_MAXIMOS_JPEG = 60_000_000
PIXELES_MAXIMOS_OTROS = 25_000_000
FOTOS_MAXIMAS_POR_ITEM = 6
# El servidor es un solo proceso y cada derivado puede ocupar más de 100 MB mientras se
# calcula: a lo más dos a la vez, para que varias subidas juntas no lo tumben.
_turno = threading.BoundedSemaphore(2)

_NOMBRE_VALIDO = re.compile(r"^[0-9a-f]{32}\.jpg$")


class FotoError(Exception):
    """La foto no se puede aceptar. El mensaje va tal cual al operador (HTTP 400)."""


def derivado(contenido: bytes) -> tuple:
    """bytes del archivo subido → (bytes del JPEG derivado, ancho, alto).

    Lanza FotoError con un mensaje para el operador si el archivo no es una imagen
    aceptada, está dañado o es desmesurado.
    """
    if not contenido:
        raise FotoError("El archivo está vacío.")
    if len(contenido) > PESO_MAXIMO_SUBIDA:
        raise FotoError(f"La foto pesa más de {PESO_MAXIMO_SUBIDA // (1024 * 1024)} MB.")
    with _turno:
        return _derivar(contenido)


def _derivar(contenido: bytes) -> tuple:
    try:
        imagen = Image.open(io.BytesIO(contenido))
        if imagen.format not in FORMATOS_ACEPTADOS:
            raise FotoError("Formato no soportado: sube una foto JPG, PNG o WebP.")
        tope = PIXELES_MAXIMOS_JPEG if imagen.format in FORMATOS_JPEG else PIXELES_MAXIMOS_OTROS
        if imagen.width * imagen.height > tope:
            raise FotoError("La imagen es demasiado grande.")
        if imagen.format in FORMATOS_JPEG:
            # Le pide al decodificador una versión ya reducida: una foto de 48 MP baja de
            # ~380 MB de memoria a ~110 MB. No cambia el resultado, solo el pico.
            imagen.draft("RGB", (LADO_MAXIMO_PX, LADO_MAXIMO_PX))
        imagen = ImageOps.exif_transpose(imagen)  # endereza según la orientación del teléfono
        imagen = imagen.convert("RGB")            # sin canal alfa ni paleta: JPEG plano
        imagen.thumbnail((LADO_MAXIMO_PX, LADO_MAXIMO_PX))
        # Pillow copia al archivo nuevo lo que encuentre en `info`: el comentario JPEG del
        # original (donde algunas cámaras y apps escriben texto libre), el perfil de
        # color, etc. Se vacía para que el derivado lleve SOLO los píxeles.
        imagen.info = {}
        salida = io.BytesIO()
        imagen.save(salida, format="JPEG", quality=CALIDAD_JPEG, optimize=True)
    except FotoError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise FotoError("El archivo no es una imagen válida o está dañado.")
    return salida.getvalue(), imagen.width, imagen.height


def guardar(datos_jpeg: bytes) -> str:
    """Escribe el derivado en disco y devuelve su nombre de archivo."""
    os.makedirs(DIRECTORIO, exist_ok=True)
    nombre = f"{uuid.uuid4().hex}.jpg"
    with open(os.path.join(DIRECTORIO, nombre), "wb") as destino:
        destino.write(datos_jpeg)
    return nombre


def ruta_de(archivo: Optional[str]) -> Optional[str]:
    """Ruta absoluta de una foto guardada, o None si el nombre no es de los que escribe
    `guardar` o el archivo ya no está. El nombre viene de la base, no del cliente, pero
    igual se valida: una fila alterada a mano no puede sacar la lectura del directorio."""
    if not archivo or not _NOMBRE_VALIDO.match(archivo):
        return None
    ruta = os.path.join(DIRECTORIO, archivo)
    return ruta if os.path.isfile(ruta) else None


def borrar_archivo(archivo: Optional[str]) -> None:
    ruta = ruta_de(archivo)
    if ruta:
        os.remove(ruta)

