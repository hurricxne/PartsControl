"""La entrega de archivos al cliente: el PDF de una guía o factura electrónica y la
bitácora de descargas. (Qué documentos existen y cuáles ofrecen archivo lo decide
portal/documentos.py; acá ya se sabe que el documento es del cliente.)

EL PDF NO SE ENTREGA COMO ENLACE
    Wasabil deja en `wasabil_dte.pdf_url` un enlace PÚBLICO y permanente (responde sin
    credenciales; por eso la pantalla interna lo abre con window.open). Si ese enlace
    llegara al navegador del cliente, serviría para siempre sin pasar por la sesión, el
    corte por inactividad ni la bitácora. Acá el PDF lo baja EL SERVIDOR con el token de
    la empresa, armando la dirección desde el uuid del DTE (pedir una URL leída de la
    base sería una puerta de SSRF), lo guarda una vez —un DTE emitido no cambia— en
    uploads/portal_docs/ y se lo entrega al cliente como bytes.

LA BAJADA NO PUEDE TENER TOMADA UNA CONEXIÓN DE LA BASE
    Wasabil puede tardar o no responder, y el pool de conexiones es de todo el ERP (un
    solo proceso atiende a las dos marcas). Por eso el orden de una descarga es fijo:
      1. la ruta copia a variables lo que necesita (uuid, folio, ids);
      2. `anotar_descarga` cobra el cupo, deja el rastro, hace commit y CIERRA la sesión;
      3. recién entonces `pdf_del_dte(uuid)` habla con Wasabil.
    Después del paso 2 la ruta no toca ningún objeto de la base (leer un atributo
    volvería a tomar una conexión, y quedaría tomada mientras dura la bajada).

CUANDO WASABIL FALLA O TARDA
    Cada petición que espera ocupa un hilo del servidor, y los hilos son de todo el ERP.
    Por eso nada espera sin tope:
      · el documento ya bajado se entrega desde la copia local, sin esperar a nadie;
      · hay a lo más BAJADAS_SIMULTANEAS pedidos a Wasabil a la vez (el que no cabe
        recibe el aviso de inmediato, no hace fila);
      · el mismo documento no se baja dos veces en paralelo: la segunda petición espera
        a la primera SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA y, si no terminó, desiste;
      · un fallo (de Wasabil o al guardar la copia) se recuerda SEGUNDOS_SIN_REINTENTAR,
        para que diez clics no sean diez esperas.
    El cliente recibe siempre el mismo texto (router_ordenes.DOCUMENTO_NO_DISPONIBLE); el
    motivo real queda en el log.

Todo esto vive en la memoria del proceso (un solo worker), como backend/portal/cupo.py.
"""
import logging
import os
import re
import tempfile
import threading
import time
import zlib
from datetime import datetime
from typing import Dict, Optional

import httpx
from sqlalchemy.orm import Session

from config import settings
from . import cupo
from .deps import PortalScope
from .models import PortalDescargaLog

logger = logging.getLogger("portal")

# Copias locales de los PDF de DTE ya bajados de Wasabil (<uuid>.pdf).
DIRECTORIO_PDF = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads", "portal_docs"))

# Cabeceras de todo archivo del cliente (PDF, guía firmada, foto): no se guarda en cachés
# compartidos ni en el disco del navegador (un computador de faena lo usa más de una
# persona), y el navegador no adivina el tipo de contenido.
CABECERAS_PRIVADAS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}

_UUID_DE_DTE = re.compile(r"^[0-9A-Za-z-]{8,64}$")
PESO_MAXIMO_PDF = 15 * 1024 * 1024
ESPERA_WASABIL = httpx.Timeout(15.0, connect=5.0)
# El tope de httpx es por fase (conectar, cada lectura): una respuesta que gotea no lo
# gasta nunca. Este es el tope del pedido completo.
SEGUNDOS_MAXIMOS_POR_BAJADA = 30
BAJADAS_SIMULTANEAS = 3
SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA = 2
SEGUNDOS_SIN_REINTENTAR = 60

_cupo_de_bajadas = threading.BoundedSemaphore(BAJADAS_SIMULTANEAS)
# Un candado por documento, repartido en un número fijo de casilleros (dos uuid distintos
# pueden compartir casillero; eso solo los hace esperarse, nunca los mezcla).
_CANDADOS = tuple(threading.Lock() for _ in range(32))
_fallos: Dict[str, float] = {}  # uuid → cuándo falló por última vez (time.monotonic)
_candado_de_fallos = threading.Lock()


class DocumentoNoDisponible(Exception):
    """El documento existe pero no se pudo obtener ahora. El mensaje es para el log."""


def reset() -> None:
    """Olvida los fallos recientes. Para las pruebas."""
    with _candado_de_fallos:
        _fallos.clear()


def _candado_de(uuid: str) -> threading.Lock:
    return _CANDADOS[zlib.crc32(uuid.encode("utf-8")) % len(_CANDADOS)]


def _fallo_hace_poco(uuid: str) -> bool:
    with _candado_de_fallos:
        cuando = _fallos.get(uuid)
    return cuando is not None and time.monotonic() - cuando < SEGUNDOS_SIN_REINTENTAR


def _recordar_fallo(uuid: str) -> None:
    """Anota el fallo y, de paso, olvida los que ya vencieron (para que el diccionario
    no junte para siempre los documentos que fallaron una vez)."""
    ahora = time.monotonic()
    with _candado_de_fallos:
        for vencido in [u for u, cuando in _fallos.items() if ahora - cuando >= SEGUNDOS_SIN_REINTENTAR]:
            del _fallos[vencido]
        _fallos[uuid] = ahora


def _bajar_de_wasabil(uuid: str) -> bytes:
    """El PDF del DTE, pedido a Wasabil con el token de la empresa. Función aparte para
    que las pruebas la reemplacen: una suite no llama a Wasabil."""
    token = (settings.WASABIL_API_TOKEN or "").strip()
    if not token:
        raise DocumentoNoDisponible("Wasabil no está configurado (falta WASABIL_API_TOKEN)")
    url = f"{settings.WASABIL_API_BASE.rstrip('/')}/documents/{uuid}/pdf"
    limite = time.monotonic() + SEGUNDOS_MAXIMOS_POR_BAJADA
    trozos, peso = [], 0
    try:
        with httpx.Client(timeout=ESPERA_WASABIL, follow_redirects=False) as http:
            with http.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as respuesta:
                if respuesta.status_code != 200:
                    raise DocumentoNoDisponible(f"Wasabil respondió {respuesta.status_code}")
                for trozo in respuesta.iter_bytes():
                    peso += len(trozo)
                    if peso > PESO_MAXIMO_PDF:
                        raise DocumentoNoDisponible("el PDF pesa más que el tope")
                    if time.monotonic() > limite:
                        raise DocumentoNoDisponible("la bajada tardó más que el tope")
                    trozos.append(trozo)
    except httpx.HTTPError as error:
        raise DocumentoNoDisponible(f"Wasabil no respondió ({type(error).__name__})")
    contenido = b"".join(trozos)
    if not contenido.startswith(b"%PDF"):
        raise DocumentoNoDisponible("lo que respondió Wasabil no es un PDF")
    return contenido


def _copia_valida(ruta: str) -> bool:
    """¿Hay una copia local servible? Solo mira: una copia vacía o que no es un PDF no
    sirve, y la pisa la bajada siguiente (`_guardar`)."""
    try:
        with open(ruta, "rb") as copia:
            return copia.read(4) == b"%PDF"
    except OSError:
        return False


def _guardar(ruta: str, contenido: bytes) -> None:
    """Escribe la copia local (pisando la que hubiera). El temporal es único por llamada
    y el cambio de nombre es atómico: nunca queda servible un PDF a medio escribir, y
    quien esté leyendo la copia anterior no se entera."""
    temporal = None
    try:
        os.makedirs(DIRECTORIO_PDF, exist_ok=True)
        descriptor, temporal = tempfile.mkstemp(dir=DIRECTORIO_PDF, suffix=".tmp")
        with os.fdopen(descriptor, "wb") as destino:
            destino.write(contenido)
        os.replace(temporal, ruta)
    except OSError as error:
        if temporal and os.path.exists(temporal):
            os.remove(temporal)
        raise DocumentoNoDisponible(f"no se pudo guardar la copia local ({error})")


def pdf_del_dte(uuid: Optional[str]) -> str:
    """Ruta local del PDF del DTE de ese uuid: la copia ya guardada, o lo baja y lo
    guarda. Lanza DocumentoNoDisponible si no se pudo obtener. NO usa la base.
    Ver «CUANDO WASABIL FALLA O TARDA» en el encabezado."""
    uuid = uuid or ""
    if not _UUID_DE_DTE.match(uuid):
        raise DocumentoNoDisponible("el DTE no tiene un uuid utilizable")
    ruta = os.path.join(DIRECTORIO_PDF, f"{uuid}.pdf")
    # Lo que se puede responder sin esperar a nadie, se responde antes de pedir el candado.
    if _copia_valida(ruta):
        return ruta
    if _fallo_hace_poco(uuid):
        raise DocumentoNoDisponible("falló hace menos de un minuto; no se reintenta todavía")
    candado = _candado_de(uuid)
    if not candado.acquire(timeout=SEGUNDOS_ESPERANDO_LA_MISMA_BAJADA):
        raise DocumentoNoDisponible("otra petición está bajando este mismo documento y no terminó")
    try:
        # Mientras se esperaba, la petición que tenía el candado pudo bajarlo o fallar.
        if _copia_valida(ruta):
            return ruta
        if _fallo_hace_poco(uuid):
            raise DocumentoNoDisponible("falló hace menos de un minuto; no se reintenta todavía")
        if not _cupo_de_bajadas.acquire(blocking=False):
            raise DocumentoNoDisponible("hay demasiadas bajadas desde Wasabil en curso")
        try:
            _guardar(ruta, _bajar_de_wasabil(uuid))
        except DocumentoNoDisponible:
            _recordar_fallo(uuid)
            raise
        finally:
            _cupo_de_bajadas.release()
        return ruta
    finally:
        candado.release()


def nombre_de_archivo(prefijo: str, folio: Optional[str], respaldo: int, extension: str) -> str:
    """El nombre con que el navegador guarda la descarga. El folio de una guía en papel
    es texto libre y va a una cabecera HTTP: se deja solo en letras, dígitos y guiones."""
    limpio = re.sub(r"[^A-Za-z0-9_-]", "", folio or "") or str(respaldo)
    return f"{prefijo}-{limpio}.{extension}"


def anotar_descarga(db: Session, scope: PortalScope, tipo: str, referencia_id: int,
                    ip: Optional[str]) -> None:
    """Cobra el cupo de descargas de la cuenta, deja el rastro, hace COMMIT y CIERRA la
    sesión. Se llama ANTES de entregar el archivo: si el cupo se acabó o la bitácora
    falla, no hay descarga. Registra el INTENTO: si después Wasabil no responde, la fila
    queda igual. Ver «LA BAJADA NO PUEDE TENER TOMADA UNA CONEXIÓN» en el encabezado.
    """
    cupo.cobrar(scope.usuario_id, cupo.DESCARGAS)
    db.add(PortalDescargaLog(
        usuario_id=scope.usuario_id, tipo=tipo, referencia_id=referencia_id,
        ip=(ip or "")[:64] or None, created_at=datetime.utcnow(),
    ))
    db.commit()
    db.close()
