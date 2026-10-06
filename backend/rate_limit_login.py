"""Límite de intentos del inicio de sesión y del cambio de clave del Portal de Clientes.

POR QUÉ EXISTE
    Cada intento de login cuesta un bcrypt completo, y el login está abierto a internet y
    a usuarios EXTERNOS (los contactos de clientes del portal), que reutilizan contraseñas
    de otros sitios. Sin tope es fuerza bruta ilimitada contra cuentas reales y una forma
    barata de gastar la CPU que comparte todo el ERP.

LA LLAVE DEL CONTADOR — leerla primero: el resto habla de «llaves», no de correos
    Este módulo es el ÚNICO que decide el formato de la llave (`llave_de_login`,
    `llave_de_cuenta`); los llamadores no la arman a mano.
      · Cuenta que EXISTE   -> `uid:{id}`. Qué correos son «el mismo» lo decide la base
        (`utf8mb4_unicode_ci` funde mayúsculas, acentos, invisibles y hasta dígitos de
        otros alfabetos), así que ninguna forma de escribir el correo da cupo nuevo. Si
        la llave usara una equivalencia MÁS ESTRICTA que la base, el tope se evadiría
        escribiendo el mismo correo de otra manera. Por eso el login resuelve la cuenta
        con un SELECT antes de llamar acá.
      · Correo SIN cuenta   -> `mail:` + el correo plegado (`normalizar_email`). El
        prefijo separa los dos espacios: un texto tecleado como correo nunca puede
        imitar la llave `uid:N` de una cuenta real.

QUÉ HACE — cuatro reglas, cada una en ventanas de 15 minutos
    Las reglas 1 y 2 cuentan por (llave, IP) — el «contador» (`_contador`) — y no por la
    llave sola (cambio del 2026-10-06, hallazgo MEDIO de la revisión de la entrega): con
    la llave sola, quien conociera el correo de una cuenta interna la dejaba sin poder
    entrar 15 minutos con 5 claves malas desde CUALQUIER IP, renovable sin fin. Contando
    por (llave, IP), esos 5 fallos bloquean solo la IP de quien los hizo; el dueño entra
    desde la suya. Si la IP no distingue al cliente (sin X-Forwarded-For), el contador
    vuelve a ser la llave sola: el comportamiento anterior.
    1) FALLOS por (llave, IP): 5 -> 429. Frena adivinar la clave de una cuenta desde una
       conexión. Un intento correcto borra estas marcas (`limpiar_exito`).
    1b) FALLOS por CUENTA, sumando todas las IPs: 50 -> 429 (`MAX_FALLOS_POR_CUENTA`).
       Frena la fuerza bruta REPARTIDA en muchas IPs, que la regla 1 por sí sola dejaría
       en 5 intentos por IP. El éxito NO lo borra: si lo borrara, los ingresos del dueño
       le renovarían el cupo al atacante.
    2) INTENTOS por (llave, IP): 30, hayan salido bien o mal -> 429. El éxito NO los
       borra. Frena a quien SÍ sabe la clave (un cliente, o quien se la robó) y la usa
       para gastar bcrypt en bucle: sin esta regla, cada éxito borraría el rastro y una
       sola cuenta ocuparía todos los hilos y las conexiones que comparte el ERP.
    3) LLAVES distintas por IP: 10 -> 429. Frena el rociado de contraseñas (una clave
       común contra muchas cuentas).

    La tercera NO cuenta fallos, cuenta LLAVES DISTINTAS. Es deliberado: una minera
    entera sale por un NAT con una sola IP pública. Si se contaran fallos por IP, 30
    personas equivocándose el lunes en la mañana bloquearían a toda la empresa. En
    cambio cada una falla sobre SU propia llave (regla 1) y la regla 3 solo se dispara
    cuando UNA IP prueba muchas cuentas diferentes, que es la firma del ataque.

    La regla 3 solo tiene sentido si la IP distingue a un cliente de otro. Cuando no lo
    hace (ver «MODOS DE FALLA») se OMITE y se avisa: aplicarla sobre una IP que
    comparten todos sería un cerrojo global que cualquiera activa con 10 peticiones.

CÓMO CUENTA
    El intento se reserva ANTES del bcrypt, en la misma operación que comprueba los
    topes (`reservar_intento`): ver el docstring de esa función.

EL BCRYPT DE RELLENO (`cupo_de_relleno`)
    El login gasta un bcrypt también cuando el correo no tiene cuenta, para que el
    tiempo de respuesta no diga qué correos existen. Ese gasto no depende de ninguna
    cuenta, así que lleva su propio techo global: 300 por ventana. Pasado el techo, el
    401 de un correo sin cuenta sale sin bcrypt (se pierde el disimulo, no la CPU).

COSTO ACEPTADO
    Bloquear el login de OTRO ahora exige la regla 1b: 50 fallos en 15 minutos, que con
    5 por IP son al menos 10 IPs distintas (la regla 1 corta antes a cada una). Sigue
    siendo posible para quien controle muchas IPs; bloquea el login y no la cuenta (quien
    ya está dentro sigue trabajando) y un humano lo limpia con `pm2 restart machparts-api`.
    El precio de contar por (llave, IP): quien adivina tiene 5 intentos por IP en vez de
    5 en total, con el techo de 50 por cuenta de la regla 1b.
    El cambio de clave del portal (POST /portal/yo/clave) usa la misma llave `uid:{id}`:
    durante el bloqueo tampoco se puede cambiar la clave. A cambio no hay un segundo
    contador que regale 5 intentos más contra la misma cuenta.
    Una cuenta que choca con la regla 2 se reconoce en `pm2 logs` por su `llave='uid:N'`
    repetida, y se corta desactivándola: sin cuenta activa sus intentos caen a la llave
    `mail:`, que nunca tiene éxito y topa en 5.
    El propio contador deja una forma CARA de averiguar si un correo tiene cuenta: agotar
    sus 5 intentos y probar después una variante que la base funde y `normalizar_email`
    no (429 = tiene cuenta). Cuesta 6 peticiones por correo y deja la cuenta bloqueada y
    anotada como TOPE ALCANZADO, así que no pasa inadvertida.

MODOS DE FALLA — leerlos antes de confiar en esto
    · Vive en MEMORIA del proceso: un `pm2 restart` borra todos los contadores.
      Aceptable, porque un atacante externo no puede provocar el restart.
    · SUPONE UN SOLO PROCESO (uvicorn sin `--workers`). Con N procesos cada uno lleva su
      propio contador y los topes reales se multiplican por N. `pm2 describe` no lo
      delata (pm2 ve un único proceso padre aunque uvicorn abra varios). Lo delatan: más
      de una línea "[scheduler] APScheduler started" al arrancar, o dos `pid` distintos
      en llamadas seguidas a GET /api/auth/rate-limit/estado.
      SÍNTOMA: el usuario ve 429 pero ese diagnóstico responde "bloqueado": false.
      OJO: desde el 2026-10-06 el diagnóstico hay que pedirlo CON la ip de la persona
      (`&ip=`); sin ella mira el contador sin IP y también diría "false" con un solo
      proceso. Descartar eso antes de concluir que hay varios procesos.
    · La IP del cliente sale del ÚLTIMO salto de X-Forwarded-For, que escribe nginx solo
      si tiene `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`. Sin esa
      línea todos los clientes llegan como 127.0.0.1: la regla 3 se omite y cada intento
      queda en el log con `regla_ip=omitida` (más un AVISO la primera vez). Las reglas 1
      y 2 no dependen de la IP y siguen igual. Se comprueba mirando que
      `[login] intento ... ip=` muestre la IP pública de quien entró.
    · Las llaves `mail:` las elige el atacante (correos arbitrarios) -> los diccionarios
      podrían crecer sin tope. Por eso se purga en CADA escritura y hay un techo duro de
      llaves (`_MAX_LLAVES`), descartando las más viejas.

CÓMO SE APAGA
    settings.LOGIN_RATE_LIMIT_ENABLED = False  (backend/.env + restart, sin deploy).
    Lo consultan los llamadores (`login`, `cambiar_clave`), no este módulo.

CÓMO SE DEPURA
    · Cada intento admitido deja una línea en `pm2 logs machparts-api`:
          [login] intento llave='uid:42' correo='juan@grupoam.cl' ip='200.1.2.3' ...
      Para seguir a una persona se busca por su correo; para ver todo lo que le pasó a
      una cuenta (también los cambios de clave, que no traen correo), por su llave. Los
      intentos del cambio de clave se distinguen porque llevan `origen=cambio-clave` al
      final de la línea; los del login no llevan marca.
    · GET /api/auth/rate-limit/estado?email=...&ip=... (exige sesión y marca `mineria`)
      devuelve la llave, el estado exacto de sus contadores y el PID del proceso.
"""
from __future__ import annotations

import ipaddress
import math
import threading
import time
import unicodedata
from collections import deque
from typing import Deque, Dict, List, Optional, Sequence

from fastapi import HTTPException

# ── Política ──────────────────────────────────────────────────────────────────
VENTANA_SEGUNDOS = 15 * 60      # 15 min: corta para el usuario, larga para el ataque
MAX_FALLOS_POR_LLAVE = 5        # regla 1, por (llave, IP): muy por encima del ritmo humano
MAX_FALLOS_POR_CUENTA = 50      # regla 1b, por llave sumando IPs: fuerza bruta repartida
MAX_INTENTOS_POR_LLAVE = 30     # regla 2, por (llave, IP): nadie entra 30 veces en 15 min
MAX_LLAVES_POR_IP = 10          # regla 3: firma del rociado de contraseñas
MAX_RELLENOS_POR_VENTANA = 300  # bcrypt de relleno (correos sin cuenta), en total
_MAX_LLAVES = 10_000            # techo anti-crecimiento (las llaves `mail:` las elige el atacante)

# Nombres de las reglas: salen en el log del rechazo y en `estado()["motivo"]`.
MOTIVO_FALLOS, MOTIVO_CUENTA, MOTIVO_INTENTOS, MOTIVO_IP = "fallos", "cuenta", "intentos", "ip"

# ── Estado (en memoria del proceso) ───────────────────────────────────────────
# contador (llave+IP, ver `_contador`) -> marcas que ningún éxito ha borrado (regla 1)
_FALLOS: Dict[str, List[float]] = {}
# llave -> marcas de fallos de la cuenta desde TODAS las IPs; el éxito no las borra (1b)
_FALLOS_CUENTA: Dict[str, List[float]] = {}
# contador -> marcas de TODOS los intentos admitidos; el éxito no las borra (regla 2)
_INTENTOS: Dict[str, List[float]] = {}
# ip -> {llave: marca del último intento sin éxito de esa llave desde esa ip} (regla 3)
_SPRAY_IP: Dict[str, Dict[str, float]] = {}
# marcas de los bcrypt de relleno gastados (ver `cupo_de_relleno`)
_RELLENOS: Deque[float] = deque()
# Avisos que se imprimen una vez y no en cada intento.
_aviso_sin_ip_dado = False
_aviso_relleno_dado = False
# Los endpoints `def` (no async) de FastAPI corren en un threadpool: hay concurrencia
# real sobre este estado. Un lock simple basta; las secciones son de microsegundos.
_LOCK = threading.Lock()


# ── La llave ──────────────────────────────────────────────────────────────────
_PREFIJO_CUENTA = "uid:"
_PREFIJO_CORREO = "mail:"

# Largo máximo de una dirección de correo según el RFC 5321. El campo `email` del login
# es un `str` sin tope: `correo_imposible` rechaza lo que pase de acá antes de trabajar
# con él, y `normalizar_email` corta la llave al mismo largo, para que su tamaño no lo
# elija el atacante (`_MAX_LLAVES` acota la CANTIDAD de llaves, no su tamaño).
_MAX_LARGO_EMAIL = 254

# Categorías Unicode que la collation `utf8mb4_unicode_ci` ignora al comparar, y que por
# lo tanto conviene descartar de la llave de un correo sin cuenta: Mn/Mc/Me (acentos y
# marcas) y Cf/Cc (formato y control, los invisibles).
_CATEGORIAS_IGNORADAS = frozenset(("Mn", "Mc", "Me", "Cf", "Cc"))


def normalizar_email(email: Optional[str]) -> str:
    """Correo de entrada -> su forma plegada (minúsculas, sin acentos ni invisibles, acotada).

    OJO CON EL ALCANCE: solo arma la llave de correos que NO tienen cuenta
    (`llave_de_login`). Para una cuenta real la llave es `uid:{id}`, que deja que la
    EQUIVALENCIA la ponga la base y cierra la clase entera (dígitos de otros alfabetos
    incluidos). Perseguir esas equivalencias en Python nunca es completo: la collation
    `utf8mb4_unicode_ci` funde mayúsculas, acentos, invisibles Y dígitos árabes/devanagari
    ('admin1@x.cl' == 'admin١@x.cl'), y esto último no lo cubre ningún filtro de categoría.
    Por eso el plegado de acá es una segunda línea, no la principal.

    Aun así se pliega lo que sí se puede, porque frena las variantes triviales del rociado
    contra correos sin cuenta:
      · NFKD y descarte de `_CATEGORIAS_IGNORADAS`: acentos e invisibles.
      · `casefold()` en vez de `lower()`: cubre casos como la ß alemana.
      · Corte de largo: ver `_MAX_LARGO_EMAIL`.
    """
    # ORDEN: NFKD primero, casefold DESPUÉS. Al revés se escapan las descomposiciones que
    # PRODUCEN mayúsculas: '№' -> 'No', '™' -> 'TM', '℡' -> 'TEL'; con casefold antes,
    # esas mayúsculas nacen después del plegado y sobreviven.
    descompuesto = unicodedata.normalize("NFKD", (email or "").strip())
    limpio = "".join(c for c in descompuesto
                     if unicodedata.category(c) not in _CATEGORIAS_IGNORADAS)
    return limpio.casefold()[:_MAX_LARGO_EMAIL]


def llave_de_cuenta(user_id: int) -> str:
    """Llave del contador de una cuenta que existe. La usa directamente quien ya tiene la
    cuenta resuelta (el cambio de clave del portal); el login pasa por `llave_de_login`."""
    return f"{_PREFIJO_CUENTA}{int(user_id)}"


def llave_de_login(user_id: Optional[int], email: Optional[str]) -> str:
    """La llave del contador para un intento de login: `user_id` es el de la cuenta que la
    base encontró para ese correo, o None si no hay ninguna.

    Es el único lugar que arma la llave de un correo sin cuenta. El login y el
    diagnóstico (`estado_rate_limit`) llaman las dos a esta función: si cada una armara
    la llave por su lado y una cambiara, el diagnóstico miraría una llave que el login
    no escribe y diría «no bloqueado» con el usuario viendo 429.
    """
    if user_id is not None:
        return llave_de_cuenta(user_id)
    return _PREFIJO_CORREO + normalizar_email(email)


def correo_imposible(email: Optional[str]) -> bool:
    """¿Este texto NO puede ser el correo de ninguna cuenta?

    Lo es si mide más que una dirección de correo o si no es Unicode válido (un
    surrogate suelto, "\\ud800" en el JSON: pydantic lo acepta y el driver de MySQL no
    puede codificarlo, así que el SELECT del login terminaría en un 500).

    `login` lo pregunta ANTES de tocar la base y responde su 401 de siempre, sin gastar
    cupo. No se valida en el schema: el 422 de FastAPI devuelve el valor rechazado y
    revienta al serializarlo con el mismo carácter.
    """
    email = email or ""
    if len(email) > _MAX_LARGO_EMAIL:
        return True  # primero el largo: el encode de abajo trabaja con 254 caracteres como máximo
    try:
        email.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


# ── La IP ─────────────────────────────────────────────────────────────────────
_IP_DESCONOCIDA = "desconocida"


def _seguro_para_log(valor: str) -> str:
    """Valor de entrada -> texto de UNA línea, seguro para el log.

    El correo viaja sin validar desde el cuerpo del POST. Sin esto, un correo con un
    salto de línea fabrica entradas falsas en `pm2 logs`:
        "a\\n[login] TOPE ALCANZADO llave='uid:1'"
    y a las 3 AM el operador persigue un ataque que nunca ocurrió.
    `repr()` escapa los saltos y deja el valor entre comillas, visible tal cual llegó.
    """
    return repr(valor)[:120]


def ip_cliente(request) -> str:
    """IP del cliente detrás del proxy inverso: el ÚLTIMO salto de X-Forwarded-For.

    OJO — divergencia deliberada del precedente: `monza_router_leads.py` toma el PRIMER
    salto. Acá se toma el último, porque el primero lo escribe el propio cliente y es
    falsificable: con el primero, un atacante evade la regla de IP mandando una cabecera
    distinta en cada intento, y de paso puede incriminar a un tercero.

    El último salto lo escribe nginx SIEMPRE QUE tenga la directiva
    `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`. Sin cabecera se
    devuelve la IP del par, que detrás de nginx es 127.0.0.1 para todos: ese caso lo
    reconoce `_ip_identifica_cliente`.
    """
    xff = ""
    try:
        xff = request.headers.get("x-forwarded-for", "") or ""
    except Exception:                                   # pragma: no cover
        xff = ""
    if xff:
        partes = [p.strip() for p in xff.split(",") if p.strip()]
        if partes:
            return partes[-1]
    try:
        return (request.client.host if request.client else "") or _IP_DESCONOCIDA
    except Exception:                                   # pragma: no cover
        return _IP_DESCONOCIDA


def _ip_identifica_cliente(ip: str) -> bool:
    """¿Esta IP distingue a un cliente de otro? Si no, la regla 3 no se le aplica.

    No lo hace la IP de loopback ni la ausente: es la MISMA para todos cuando nginx no
    reenvía X-Forwarded-For. Un texto que no es una IP sí cuenta como identificador:
    solo se descarta lo que se SABE compartido (el cliente de pruebas llega como
    'testclient').
    """
    if not ip or ip == _IP_DESCONOCIDA:
        return False
    try:
        direccion = ipaddress.ip_address(ip)
    except ValueError:
        return True
    # '::ffff:127.0.0.1' es el mismo loopback escrito como IPv6.
    direccion = getattr(direccion, "ipv4_mapped", None) or direccion
    return not direccion.is_loopback


# ── Los contadores ────────────────────────────────────────────────────────────
def _contador(llave: str, ip: str) -> str:
    """La clave de las reglas 1 y 2: la llave más la IP, si la IP distingue al cliente.

    Sin IP útil (detrás de nginx sin X-Forwarded-For todos son 127.0.0.1) es la llave
    sola, como antes del 2026-10-06: contar por una IP que comparten todos repartiría el
    tope entre nadie. La IP se acota en largo: la escribe nginx, pero el contador no debe
    crecer con un valor que no se controla.
    """
    if _ip_identifica_cliente(ip):
        return f"{llave}|ip={ip[:64]}"
    return llave


def _vencer(marcas_por_llave: Dict[str, List[float]], corte: float) -> None:
    """Saca de un diccionario llave -> marcas todo lo anterior a `corte`."""
    for llave in list(marcas_por_llave):
        vivas = [t for t in marcas_por_llave[llave] if t > corte]
        if vivas:
            marcas_por_llave[llave] = vivas
        else:
            del marcas_por_llave[llave]


def _purgar(ahora: float, aplicar_techo: bool = True) -> None:
    """Saca lo vencido y, si se pide, aplica el techo de llaves. Corre DENTRO del lock.

    `aplicar_techo=False` lo usa `estado()`: una consulta de diagnóstico no debe
    descartar contadores vivos como efecto secundario de mirarlos.
    """
    corte = ahora - VENTANA_SEGUNDOS
    _vencer(_FALLOS, corte)
    _vencer(_FALLOS_CUENTA, corte)
    _vencer(_INTENTOS, corte)
    for ip in list(_SPRAY_IP):
        vivas = {llave: t for llave, t in _SPRAY_IP[ip].items() if t > corte}
        if vivas:
            _SPRAY_IP[ip] = vivas
        else:
            del _SPRAY_IP[ip]

    if not aplicar_techo:
        return

    # Techo duro: si aun así hay demasiadas llaves, se descartan las MÁS VIEJAS.
    # Perder contadores viejos es preferible a que el proceso crezca sin límite.
    for marcas_por_llave in (_FALLOS, _FALLOS_CUENTA, _INTENTOS):
        while len(marcas_por_llave) > _MAX_LLAVES:
            del marcas_por_llave[min(marcas_por_llave, key=lambda k: max(marcas_por_llave[k]))]
    while len(_SPRAY_IP) > _MAX_LLAVES:
        del _SPRAY_IP[min(_SPRAY_IP, key=lambda k: max(_SPRAY_IP[k].values()))]


def _segundos_restantes(marcas: Sequence[float], tope: int, ahora: float) -> int:
    """Segundos hasta que estas marcas bajen de `tope` (0 = hay cupo).

    Se mira la marca que ocupa la posición `tope` contando desde el final: cuando ESA
    salga de la ventana quedarán `tope - 1` y se puede pasar. Con exactamente `tope`
    marcas es la más antigua, pero la fórmula no lo supone (las pruebas inyectan más):
    con `min(marcas)` y más marcas que el tope daría 0 con `tope` marcas todavía vivas,
    y el diagnóstico diría «no bloqueado» mientras el login responde 429 — el síntoma que
    el encabezado enseña a leer como «hay más de un proceso».

    Redondea hacia ARRIBA: una marca a la que le queda medio segundo sigue viva
    (`_purgar` no la saca), así que todavía no hay cupo.
    """
    if len(marcas) < tope:
        return 0
    critica = sorted(marcas)[len(marcas) - tope]
    return max(0, math.ceil(critica + VENTANA_SEGUNDOS - ahora))


def _esperas(llave: str, ip: str, ahora: float) -> Dict[str, int]:
    """Segundos de espera que impone cada regla (0 = esa regla deja pasar). DENTRO del lock.

    La usan `reservar_intento` Y `estado`: un solo cálculo, para que el limitador y el
    diagnóstico no puedan contradecirse.
    """
    por_ip = 0
    if _ip_identifica_cliente(ip):
        por_ip = _segundos_restantes(list(_SPRAY_IP.get(ip, {}).values()), MAX_LLAVES_POR_IP, ahora)
    contador = _contador(llave, ip)
    return {
        MOTIVO_FALLOS: _segundos_restantes(_FALLOS.get(contador, ()), MAX_FALLOS_POR_LLAVE, ahora),
        MOTIVO_CUENTA: _segundos_restantes(_FALLOS_CUENTA.get(llave, ()), MAX_FALLOS_POR_CUENTA, ahora),
        MOTIVO_INTENTOS: _segundos_restantes(_INTENTOS.get(contador, ()), MAX_INTENTOS_POR_LLAVE, ahora),
        MOTIVO_IP: por_ip,
    }


def reservar_intento(llave: str, ip: str, correo: str = "", origen: str = "login") -> None:
    """Comprueba los topes y ANOTA el intento, todo dentro del MISMO lock. 429 si no hay cupo.

    `correo` es el texto que tecleó la persona y solo va al log: con la llave `uid:N`
    sola, buscar a alguien por su correo en `pm2 logs` no encontraría nada.
    `origen` es un literal del código, no un dato de entrada: de dónde viene el intento
    ('login' o 'cambio-clave'). Las líneas del login quedan idénticas; las del cambio de
    clave llevan ` origen=cambio-clave` al final, para que quien lee el log distinga un
    cambio de clave de un intento de entrar. El prefijo «[login]» se conserva: el cupo ES
    el del login y los grep de siempre siguen sirviendo.

    POR QUÉ EN UNA SOLA OPERACIÓN
        Si comprobar y anotar fueran dos pasos con el bcrypt (décimas de segundo) en
        medio, las 40 peticiones simultáneas que caben en el threadpool de FastAPI
        pasarían el control antes de que ninguna alcanzara a anotar: el tope real sería
        5 + el tamaño del threadpool, y 40 bcrypt a la vez ocuparían el threadpool que
        comparte TODA la aplicación — justo lo que este módulo existe para frenar.
        Reservando el cupo en el mismo lock que lo comprueba, la carrera no existe: el
        intento nº 6 ya está anotado antes de que el nº 1 termine su bcrypt.

    CONSECUENCIA A TENER PRESENTE
        El intento se cuenta ANTES de saber si la contraseña era correcta. Por eso
        `limpiar_exito()` es obligatorio en el camino feliz: sin esa llamada, un usuario
        que entra bien 5 veces en 15 minutos se bloquearía solo.
        Un 429 no se anota: quien insiste mientras está bloqueado no alarga su bloqueo.
    """
    global _aviso_sin_ip_dado
    ahora = time.time()
    regla_ip = _ip_identifica_cliente(ip)
    avisar_sin_ip = False
    n_fallos = n_cuenta = n_intentos = n_ip = 0
    contador = _contador(llave, ip)
    with _LOCK:
        _purgar(ahora)
        esperas = _esperas(llave, ip, ahora)
        motivo = max(esperas, key=esperas.get)
        faltan = esperas[motivo]
        if faltan <= 0:
            # Cupo disponible: se toma AHORA, dentro del mismo lock.
            _FALLOS.setdefault(contador, []).append(ahora)
            _FALLOS_CUENTA.setdefault(llave, []).append(ahora)
            _INTENTOS.setdefault(contador, []).append(ahora)
            if regla_ip:
                # Una IP que no identifica al cliente no deja rastro acá: sin la regla
                # que lo acota en 10, su conjunto de llaves crecería sin límite.
                _SPRAY_IP.setdefault(ip, {})[llave] = ahora
            elif not _aviso_sin_ip_dado:
                _aviso_sin_ip_dado = avisar_sin_ip = True
            n_fallos, n_intentos = len(_FALLOS[contador]), len(_INTENTOS[contador])
            n_cuenta = len(_FALLOS_CUENTA[llave])
            n_ip = len(_SPRAY_IP.get(ip, ()))

    quien = (f"llave={_seguro_para_log(llave)} correo={_seguro_para_log(correo)} "
             f"ip={_seguro_para_log(ip)}")
    marca = "" if origen == "login" else f" origen={origen}"
    if faltan > 0:
        print(f"[login] RECHAZADO por limite ({motivo}) {quien} faltan={faltan}s{marca}", flush=True)
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos. Reintenta en {faltan} segundos.",
            headers={"Retry-After": str(faltan)},
        )

    if avisar_sin_ip:
        print(
            f"[login] AVISO: llega un intento sin IP de cliente (ip={_seguro_para_log(ip)}). "
            "Detras de nginx significa que falta `proxy_set_header X-Forwarded-For "
            "$proxy_add_x_forwarded_for;`. La regla por IP queda SIN EFECTO hasta corregirlo.",
            flush=True,
        )
    de_la_ip = f"llaves_ip={n_ip}/{MAX_LLAVES_POR_IP}" if regla_ip else "regla_ip=omitida"
    print(
        f"[login] intento {quien} fallos={n_fallos}/{MAX_FALLOS_POR_LLAVE} "
        f"cuenta={n_cuenta}/{MAX_FALLOS_POR_CUENTA} "
        f"intentos={n_intentos}/{MAX_INTENTOS_POR_LLAVE} {de_la_ip}{marca}",
        flush=True,
    )
    if n_fallos >= MAX_FALLOS_POR_LLAVE:
        print(f"[login] TOPE ALCANZADO {quien}{marca}", flush=True)
    if n_cuenta >= MAX_FALLOS_POR_CUENTA:
        print(f"[login] TOPE DE CUENTA ALCANZADO (muchas IPs) {quien}{marca}", flush=True)
    if n_ip >= MAX_LLAVES_POR_IP:
        print(f"[login] SPRAY ip={_seguro_para_log(ip)} llaves_distintas={n_ip}", flush=True)


def limpiar_exito(llave: str, ip: str) -> None:
    """Un intento correcto borra los FALLOS de la llave DESDE ESTA IP: dos tipeos y entrar
    no deja nada.

    También saca la llave del conjunto de la IP, para que la persona que se equivocó y
    después entró no siga sumando al contador de rociado de su propia oficina.
    NO toca los intentos totales (regla 2): si los borrara, quien sabe la clave no
    tendría ningún tope. Tampoco el tope de la cuenta (regla 1b): si lo borrara, cada
    ingreso del dueño le renovaría el cupo a quien adivina desde muchas IPs.
    """
    with _LOCK:
        _FALLOS.pop(_contador(llave, ip), None)
        llaves = _SPRAY_IP.get(ip)
        if llaves:
            llaves.pop(llave, None)
            if not llaves:
                del _SPRAY_IP[ip]


def cupo_de_relleno() -> bool:
    """¿Queda cupo para gastar un bcrypt en un correo SIN cuenta? Si queda, lo toma.

    El login verifica la clave contra un hash de relleno cuando el correo no tiene
    cuenta, para que ese 401 tarde lo mismo que el de una clave mala. Ese bcrypt no
    cuelga de ninguna cuenta: lo único que lo acota por fuera es la regla por IP, que
    se omite si la IP no identifica al cliente y no existe con el limitador apagado. Por
    eso tiene un techo propio, global, que no depende de la IP ni de
    LOGIN_RATE_LIMIT_ENABLED. Con el techo agotado el login responde su 401 sin bcrypt:
    lo que se pierde es el disimulo (vuelve a notarse qué correos no tienen cuenta), no
    la CPU del ERP.
    """
    global _aviso_relleno_dado
    ahora = time.time()
    avisar = False
    with _LOCK:
        corte = ahora - VENTANA_SEGUNDOS
        while _RELLENOS and _RELLENOS[0] <= corte:
            _RELLENOS.popleft()
        hay = len(_RELLENOS) < MAX_RELLENOS_POR_VENTANA
        if hay:
            _RELLENOS.append(ahora)
            _aviso_relleno_dado = False
        elif not _aviso_relleno_dado:
            _aviso_relleno_dado = avisar = True
    if avisar:
        print(
            f"[login] AVISO: {MAX_RELLENOS_POR_VENTANA} intentos contra correos SIN cuenta en "
            f"{VENTANA_SEGUNDOS // 60} min. Hasta que bajen, esos 401 salen sin bcrypt.",
            flush=True,
        )
    return hay


def estado(llave: str, ip: str = "") -> dict:
    """Estado legible de una llave para el endpoint de diagnóstico.

    Calcula las esperas con la misma función que `reservar_intento` (`_esperas`) y, como
    ella, purga antes lo vencido. Lo que NO hace es aplicar el techo de llaves, para que
    una consulta de diagnóstico no descarte contadores vivos.
    `ip` es opcional: sin ella no se informa la regla por IP, y los fallos e intentos que
    se ven son los del contador sin IP (desde 2026-10-06 las reglas 1 y 2 cuentan por
    llave + IP: para ver los de una persona, pasar SU ip).
    """
    ahora = time.time()
    contador = _contador(llave, ip)
    with _LOCK:
        _purgar(ahora, aplicar_techo=False)
        esperas = _esperas(llave, ip, ahora)
        fallos = len(_FALLOS.get(contador, ()))
        fallos_cuenta = len(_FALLOS_CUENTA.get(llave, ()))
        intentos = len(_INTENTOS.get(contador, ()))
        llaves_ip = len(_SPRAY_IP.get(ip, ()))
    motivo = max(esperas, key=esperas.get)
    faltan = esperas[motivo]
    return {
        "llave": llave,
        "bloqueado": faltan > 0,
        "motivo": motivo if faltan > 0 else None,
        "faltan_segundos": faltan,
        "intentos_fallidos": fallos,
        "max_intentos": MAX_FALLOS_POR_LLAVE,
        "fallos_cuenta": fallos_cuenta,
        "max_fallos_cuenta": MAX_FALLOS_POR_CUENTA,
        "intentos_totales": intentos,
        "max_intentos_totales": MAX_INTENTOS_POR_LLAVE,
        "ip": ip or None,
        "llaves_distintas_desde_ip": llaves_ip,
        "max_llaves_por_ip": MAX_LLAVES_POR_IP,
        "ventana_segundos": VENTANA_SEGUNDOS,
    }


def reset() -> None:
    """Vacía todos los contadores. Lo usan las pruebas; en el servidor, el equivalente es
    `pm2 restart machparts-api`."""
    global _aviso_sin_ip_dado, _aviso_relleno_dado
    with _LOCK:
        _FALLOS.clear()
        _FALLOS_CUENTA.clear()
        _INTENTOS.clear()
        _SPRAY_IP.clear()
        _RELLENOS.clear()
        _aviso_sin_ip_dado = False
        _aviso_relleno_dado = False
