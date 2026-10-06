"""Tope de frecuencia por cuenta para las rutas del Portal de Clientes.

POR QUÉ
    Las rutas del portal las llama una cuenta EXTERNA. Sin tope, un guion (o un botón
    apretado en bucle) puede pedir el tablero cientos de veces por minuto —es la ruta
    más cara: rearma todas las órdenes del cliente—, llenar la bitácora de descargas y
    gastar llamadas a Wasabil con el token de la empresa. Todo en el mismo proceso que
    atiende al personal.

TRES CUPOS, todos por cuenta y por minuto (MAXIMOS):
    general    toda ruta de datos del cliente. Alto: una pantalla normal pide un JSON y
               las fotos del ítem; el tope solo corta un bucle.
    tablero    el resumen. Es la única ruta cuyo costo crece con el tamaño del cliente.
    descargas  cada archivo que se entrega (se cobra junto a la fila de la bitácora, así
               un 404 no gasta cupo).

MODOS DE FALLA, los mismos de backend/rate_limit_login.py
    Vive en la memoria del proceso: supone UN worker (con dos, cada uno cuenta aparte y
    el tope efectivo se duplica) y `pm2 restart` lo vacía. No es una defensa contra un
    ataque distribuido: para eso está nginx. Se apaga con PORTAL_CUPO_ENABLED=false.
"""
import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple

from fastapi import HTTPException

from config import settings

VENTANA_SEGUNDOS = 60
GENERAL, TABLERO, DESCARGAS = "general", "tablero", "descargas"
MAXIMOS = {GENERAL: 300, TABLERO: 30, DESCARGAS: 30}
DETALLE = "Demasiadas solicitudes seguidas. Espera un momento y vuelve a intentar."
# Sobre esta cantidad de llaves se barren las que quedaron vacías (cuentas que dejaron de
# pedir), para que el diccionario no crezca para siempre.
_LLAVES_ANTES_DE_BARRER = 2000

_marcas: Dict[Tuple[int, str], Deque[float]] = {}
_candado = threading.Lock()


def cobrar(usuario_id: int, cupo: str) -> None:
    """Anota una petición de la cuenta en ese cupo. Responde 429 (con Retry-After) si ya
    usó las que le tocan en el último minuto; esa petición no se anota."""
    if not settings.PORTAL_CUPO_ENABLED:
        return
    ahora = time.monotonic()
    with _candado:
        cola = _marcas.setdefault((usuario_id, cupo), deque())
        while cola and ahora - cola[0] >= VENTANA_SEGUNDOS:
            cola.popleft()
        if len(cola) >= MAXIMOS[cupo]:
            espera = int(VENTANA_SEGUNDOS - (ahora - cola[0])) + 1
            raise HTTPException(429, DETALLE, headers={"Retry-After": str(espera)})
        cola.append(ahora)
        if len(_marcas) > _LLAVES_ANTES_DE_BARRER:
            for llave in [k for k, v in _marcas.items() if not v or ahora - v[-1] >= VENTANA_SEGUNDOS]:
                del _marcas[llave]


def reset() -> None:
    """Vacía todos los contadores. Para las pruebas."""
    with _candado:
        _marcas.clear()
