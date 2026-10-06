"""El login tiene tope de intentos.

QUÉ PROTEGE
    `POST /api/auth/login` está abierto a internet y a usuarios EXTERNOS (los contactos de
    clientes del portal), que reutilizan contraseñas de otros sitios. Sin tope es fuerza
    bruta ilimitada contra cuentas reales y un DoS barato de CPU (cada intento gasta un
    bcrypt completo). La política vive en `rate_limit_login.py`; acá se prueba por HTTP y,
    lo que no necesita bcrypt, llamando al módulo directo.

LAS PROPIEDADES QUE SE PRUEBAN
    1. Tope por LLAVE: al 6º intento fallido responde 429 con Retry-After.
    2. Tope por IP: 10 llaves DISTINTAS fallidas desde una IP también dan 429, aunque
       ninguna cuenta individual haya llegado a 5. Es la firma del rociado de claves.
    3. El NAT no se castiga: varias personas detrás de la misma IP, cada una fallando
       sobre su propio correo pocas veces, NO se bloquean entre sí. Esta es la propiedad
       que hace usable la regla en una minera, donde toda la empresa sale por una IP.
    4. El 429 se levanta al pasar la ventana, y un login correcto borra los fallos.
    5. Los éxitos también topan: 30 intentos por llave cada 15 minutos, hayan salido bien
       o mal (`_paso_11`). Frena a quien SÍ sabe la clave y la usa en bucle.
    6. Una IP que no identifica al cliente (loopback, ausente) no activa la regla por IP:
       con ella aplicada, un nginx sin X-Forwarded-For sería un cerrojo global que
       cualquiera activa con 10 peticiones (`_paso_10`). Las reglas por llave siguen
       vivas sin IP.
    7. Las llaves son disjuntas: el texto «uid:N» tecleado como correo no gasta el cupo
       de la cuenta N, y el diagnóstico mira la misma llave que el login (`_paso_9`).
    8. El 401 no delata ni revienta (`_paso_12`): un correo imposible (surrogate suelto,
       más de 254 caracteres) recibe el 401 de siempre sin tocar la base ni el contador;
       un correo sin cuenta gasta el mismo bcrypt que uno con cuenta (contra un hash de
       relleno, con techo); una clave con un surrogate da 401 haya cuenta o no.

SONDAS DE PODER DISCRIMINANTE — qué línea hay que romper para que cada etapa falle
    · `_paso_1` / `_paso_2` ← borrar `rl.reservar_intento(...)` de routers/auth.py.
    · `_paso_3` ← que el contador de IP cuente FALLOS en vez de LLAVES DISTINTAS; o que
                  `login` arme la llave del correo sin cuenta por otro camino que
                  `rl.llave_de_login` (la sonda lee la llave que el login escribe).
    · `_paso_4` ← no llamar a `limpiar_exito`, o volver `_segundos_restantes` a `min()`.
    · `_paso_6` ← separar la comprobación de la anotación en `reservar_intento`, o
                  anotar también los rechazos en `_INTENTOS`.
    · `_paso_7` ← quitar el plegado de acentos de `normalizar_email` (correos sin cuenta).
    · `_paso_8` ← volver `_segundos_restantes` a `int()` (trunca el último segundo).
    · `_paso_9` ← que el login NO llavee por `uid:{user.id}`: con una cuenta real, la
                  variante con un dígito de otro alfabeto vuelve a dar cupo nuevo, porque
                  el plegado de Python no la cubre pero la base sí. También: quitar el
                  prefijo `mail:` de `llave_de_login`; que `estado_rate_limit` arme su
                  propia llave; no pasar `correo=` al limitador desde `login`.
    · `_paso_10` ← que `_ip_identifica_cliente` devuelva siempre True; anotar en
                   `_SPRAY_IP` sin mirar `regla_ip`; avisar en cada intento, o nunca;
                   saltarse `reservar_intento` entero cuando no hay IP.
    · `_paso_11` ← que `limpiar_exito` borre también `_INTENTOS`; que `reset()` no lo
                   vacíe; dejar «fallidos» en el texto del 429.
    · `_paso_12` ← quitar `rl.correo_imposible` del login, o el tope de largo de esa
                   función; volver al cortocircuito `not user or not verify_password`;
                   verificar el relleno sin `cupo_de_relleno`; quitar el try/except de
                   `verify_password`; generar `_HASH_SIN_CUENTA` con otro costo.
  Se repiten con `python -m portal.tests.mutaciones login` (portal/tests/mutaciones/login.py).

DOS CAMINOS DE LA LLAVE DEL CONTADOR (la arma `rate_limit_login.py`, nadie más)
    · Cuenta que EXISTE  -> `uid:{user.id}`: la EQUIVALENCIA la define la base, así que
      ningún truco de Unicode da cupo nuevo. Es la protección de verdad, y `_paso_9` la
      ejercita con una cuenta real (por eso ese paso SÍ toca la base y la limpia con MARK).
    · Correo SIN cuenta  -> `mail:` + el correo plegado (`normalizar_email`), que `_paso_7`
      cubre. El prefijo separa los dos espacios: un texto tecleado como correo no puede
      imitar la llave `uid:N` de una cuenta real.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_login_rate_limit.py -q
"""
import contextlib
import io
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import rate_limit_login as rl  # noqa: E402
import routers.auth as modulo_login  # noqa: E402
from auth import create_access_token, get_password_hash  # noqa: E402
from database import SessionLocal  # noqa: E402
from models.models import User  # noqa: E402
from routers.auth import router as auth_router  # noqa: E402

# `_paso_9` y `_paso_12` crean una cuenta real; el resto usa correos inexistentes. Prefijo
# sin guiones bajos por lo del comodín de LIKE (ver test_bodega_fotos_auth.py).
MARK = "ZZTESTRATELIMITZZ"
CLAVE_REAL = "clave-buena-larga-2026"
DETALLE_401 = "Email o contraseña incorrectos"

app = FastAPI()
app.include_router(auth_router, prefix="/api")
client = TestClient(app)
# Para los pasos que esperan un 500 como RESPUESTA (y no como excepción que aborta la corrida).
cliente_crudo = TestClient(app, raise_server_exceptions=False)

# Las etapas se llaman `_paso_*` y NO `test_*` a proposito: `check()` acumula en
# `_fails` en vez de lanzar, asi que si pytest las recolectara por separado cada una
# seria un verde incondicional. La UNICA funcion recolectable es la de mas abajo,
# que corre todo y despues revisa `_fails`. Es la convencion del resto del repo.
_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _login(email, ip="200.10.10.10", clave="clave-mala-a-proposito"):
    """Intento de login, fallido salvo que se pase la clave de una cuenta real: ejercita el
    limitador. Con la reserva atómica, el cupo se toma al ENTRAR, no al fallar."""
    return client.post(
        "/api/auth/login",
        json={"email": email, "password": clave},
        headers={"X-Forwarded-For": ip},
    )


def _crudo(cuerpo: bytes, ip="12.12.12.12"):
    """POST al login con el cuerpo ESCRITO A MANO: `json=` del cliente no puede codificar un
    surrogate suelto, que es justo lo que estos pasos mandan."""
    return cliente_crudo.post(
        "/api/auth/login", content=cuerpo,
        headers={"Content-Type": "application/json", "X-Forwarded-For": ip},
    )


def _crear_cuenta_real(db, sufijo: str) -> tuple:
    """Una cuenta interna de `mineria` marcada con MARK: (id, correo). Para los pasos que
    necesitan una cuenta que EXISTE; se borra con `_borrar_cuentas`."""
    email = f"{sufijo}-{MARK}@test.invalid".lower()
    cuenta = User(email=email, nombre=MARK, hashed_password=get_password_hash(CLAVE_REAL),
                  is_active=1, empresa="mineria")
    db.add(cuenta)
    db.commit()
    db.refresh(cuenta)
    return cuenta.id, email


def _borrar_cuentas(db) -> None:
    db.execute(text("DELETE FROM users WHERE email LIKE :m"), {"m": f"%{MARK}%"})
    db.commit()


def _paso_1_tope_por_llave():
    rl.reset()
    email = "no-existe-rate-limit@test.invalid"
    for i in range(rl.MAX_FALLOS_POR_LLAVE):
        r = _login(email)
        check(f"intento {i + 1} responde 401 (aún no bloquea)", r.status_code == 401, r.status_code)
    r = _login(email)
    check("al superar el tope responde 429", r.status_code == 429, r.status_code)
    check("el 429 trae Retry-After", "retry-after" in {k.lower() for k in r.headers}, dict(r.headers))
    check("el 429 explica en español", "Reintenta" in r.json().get("detail", ""), r.text)


def _paso_2_tope_por_ip_rociado():
    rl.reset()
    ip = "45.45.45.45"
    for i in range(rl.MAX_LLAVES_POR_IP):
        r = _login(f"victima{i}@test.invalid", ip=ip)
        check(f"correo distinto {i + 1} responde 401", r.status_code == 401, r.status_code)
    # Un correo NUEVO desde la misma IP: ninguna cuenta llegó a 5 fallos, pero la IP ya
    # probó demasiadas cuentas distintas.
    r = _login("otra-mas@test.invalid", ip=ip)
    check("el rociado desde una IP se corta con 429", r.status_code == 429, r.status_code)


def _paso_3_el_nat_no_se_castiga():
    """La propiedad que hace usable esto en una minera: 8 personas tras el mismo NAT,
    cada una equivocándose 3 veces con SU correo, no se bloquean entre sí."""
    rl.reset()
    ip = "190.1.1.1"   # la IP de salida de toda la faena
    for persona in range(8):
        for _ in range(3):     # 3 < MAX_FALLOS_POR_LLAVE
            r = _login(f"persona{persona}@minera.invalid", ip=ip)
            check(
                f"persona {persona} sigue recibiendo 401, no 429",
                r.status_code == 401,
                r.status_code,
            )
    # 8 correos distintos < MAX_LLAVES_POR_IP (10): la IP tampoco se bloqueó. Se lee la
    # llave que el login ESCRIBE (`rl.llave_de_login`), no el correo crudo: así la sonda
    # mira el contador de verdad y no solo que «nadie quedó bloqueado».
    est = rl.estado(rl.llave_de_login(None, "persona0@minera.invalid"), ip)
    check("nadie quedó bloqueado tras 24 fallos legítimos en el mismo NAT",
          est["bloqueado"] is False, est)
    check("y los 3 fallos de persona0 están anotados en la llave que el login escribe",
          est["intentos_fallidos"] == 3, est)


def _paso_4_se_levanta_y_el_exito_limpia():
    rl.reset()
    email = "ventana@test.invalid"
    ip = "8.8.8.8"
    llave = rl.llave_de_login(None, email)   # la llave que el login escribe para ese correo
    for _ in range(rl.MAX_FALLOS_POR_LLAVE):
        _login(email, ip=ip)
    check("bloqueado tras el tope", rl.estado(llave, ip)["bloqueado"] is True)

    # La ventana se levanta: en vez de esperar 15 minutos, se envejecen las marcas.
    # Desde 2026-10-06 las reglas 1 y 2 cuentan por (llave, IP): ver rl._contador.
    contador = rl._contador(llave, ip)
    with rl._LOCK:
        rl._FALLOS[contador] = [t - rl.VENTANA_SEGUNDOS - 1 for t in rl._FALLOS[contador]]
        rl._SPRAY_IP[ip] = {e: t - rl.VENTANA_SEGUNDOS - 1 for e, t in rl._SPRAY_IP[ip].items()}
    check("se levanta solo al pasar la ventana", rl.estado(llave, ip)["bloqueado"] is False,
          rl.estado(llave, ip))

    # Un login correcto borra los fallos de la llave.
    rl.reset()
    for _ in range(rl.MAX_FALLOS_POR_LLAVE - 1):
        _login(email, ip=ip)
    check("quedan fallos anotados antes del éxito",
          rl.estado(llave, ip)["intentos_fallidos"] == rl.MAX_FALLOS_POR_LLAVE - 1,
          rl.estado(llave, ip))
    rl.limpiar_exito(llave, ip)
    check("el login correcto borra los fallos",
          rl.estado(llave, ip)["intentos_fallidos"] == 0, rl.estado(llave, ip))


def _paso_5_ip_desde_x_forwarded_for_toma_el_ultimo_salto():
    """El PRIMER salto de X-Forwarded-For lo escribe el cliente y es falsificable: con
    él, un atacante evade la regla de IP y de paso incrimina a un tercero. Se toma el
    ÚLTIMO, que lo escribe nuestro proxy."""
    class _Req:
        headers = {"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"}
        client = None
    check("toma el último salto, no el primero", rl.ip_cliente(_Req()) == "3.3.3.3",
          rl.ip_cliente(_Req()))


def _paso_6_la_reserva_es_atomica():
    """La propiedad más cara de todas.

    Si comprobar el tope y anotar el intento fueran dos pasos con un SELECT y un bcrypt
    (~200 ms) en medio, las 40 peticiones simultáneas que caben en el threadpool de anyio
    (el de los endpoints `def` de FastAPI) pasarían el control ANTES de que ninguna
    alcanzara a anotar: el tope real sería 5 más el tamaño del threadpool, y 40 bcrypt
    concurrentes saturarían el pool que comparte TODA la aplicación.

    Esta etapa lo ejercita con hilos de verdad y una barrera, para que todos ataquen en
    el mismo instante: con dos operaciones y una espera en medio pasan 40 de 40 en vez
    de 5. Y los 35 rechazados no dejan marca en ninguno de los dos contadores: un 429
    no alarga el bloqueo.
    """
    rl.reset()
    llave, ip = "carrera@test.invalid", "77.77.77.77"
    N = 40
    barrera = threading.Barrier(N)
    pasaron = []
    cerrojo = threading.Lock()

    def intento():
        barrera.wait()          # todos arrancan a la vez
        try:
            rl.reservar_intento(llave, ip)
            with cerrojo:
                pasaron.append(1)
        except Exception:
            pass                # 429: es lo esperado para la mayoría

    hilos = [threading.Thread(target=intento) for _ in range(N)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    check(f"con {N} hilos simultáneos pasan EXACTAMENTE {rl.MAX_FALLOS_POR_LLAVE}",
          len(pasaron) == rl.MAX_FALLOS_POR_LLAVE, len(pasaron))
    contador = rl._contador(llave, ip)   # reglas 1 y 2 por (llave, IP) desde 2026-10-06
    check("y el contador de fallos quedó consistente",
          len(rl._FALLOS.get(contador, [])) == rl.MAX_FALLOS_POR_LLAVE,
          len(rl._FALLOS.get(contador, [])))
    check("y los rechazados no dejaron marca en el contador de intentos",
          len(rl._INTENTOS.get(contador, [])) == rl.MAX_FALLOS_POR_LLAVE,
          len(rl._INTENTOS.get(contador, [])))


def _paso_7_las_tildes_no_dan_cupo_nuevo():
    """La llave de un correo SIN cuenta usa la misma equivalencia que la base, hasta
    donde el plegado de Python alcanza.

    `users.email` es `utf8mb4_unicode_ci`: para MySQL 'ádmin@x.cl' y 'admin@x.cl' son
    la MISMA fila. Si el limitador las contara por separado, escribir el correo con una
    tilde regalaría un cupo nuevo de 5 intentos contra la misma cuenta. El criterio del
    plegado es la CATEGORÍA Unicode y no la clase de combinación: `unicodedata.combining`
    vale 0 para muchas marcas reales y las dejaría pasar todas.
    """
    rl.reset()
    base = "victima@test.invalid"
    ip = "33.33.33.33"
    for _ in range(rl.MAX_FALLOS_POR_LLAVE):
        _login(base, ip=ip)
    r = _login(base, ip=ip)
    check("la cuenta base queda bloqueada", r.status_code == 429, r.status_code)

    # Variantes que la base trata como la MISMA fila. Tres familias, y las TRES importan:
    #   · acentos                    -> la collation les da peso cero
    #   · INVISIBLES (Cf/Cc)         -> la collation también
    #   · MARCAS DE CLASE 0 (Mn/Me)  -> solo las atrapa un filtro por categoría
    variantes = [
        "víctima@test.invalid",          # tilde
        "VÍCTIMA@test.invalid",          # tilde + mayúsculas
        "vïctima@test.invalid",          # diéresis
        "vic​tima@test.invalid",    # ancho cero (Cf)
        "⁠victima@test.invalid",    # word joiner (Cf)
        "victima@test.invalid‎",    # marca de dirección (Cf)
        "vic‍tima@test.invalid",    # zero-width joiner (Cf)
        # ── Mn/Me con CLASE DE COMBINACIÓN 0 ────────────────────────────────────────
        # `unicodedata.combining(c)` devuelve un NÚMERO (la clase canónica) y vale 0
        # para muchísimas marcas reales: un filtro que pregunte por él las deja pasar
        # todas, y con la clave correcta U+FE0F entra como el mismo usuario. Hay que
        # preguntar por la CATEGORÍA, no por la clase.
        "vicंtima@test.invalid",    # anusvara devanagari (Mn, clase 0)
        "vic͏tima@test.invalid",    # juntador de grafemas (Mn, clase 0)
        "victima️@test.invalid",    # selector de variación (Mn, clase 0)
        "vic҈tima@test.invalid",    # marca envolvente (Me, clase 0)
    ]
    for variante in variantes:
        r = _login(variante, ip=ip)
        check(f"la variante {variante!r} NO recibe cupo nuevo", r.status_code == 429,
              r.status_code)

    check("y todas colapsan a la misma llave",
          len({rl.normalizar_email(x) for x in [base] + variantes}) == 1,
          {rl.normalizar_email(x) for x in [base] + variantes})

    # Las descomposiciones que PRODUCEN mayúsculas (№ -> No, ™ -> TM) solo se pliegan si
    # `casefold()` corre DESPUÉS de NFKD. Con el orden invertido se escapan.
    check("el orden NFKD-antes-de-casefold pliega № y ™",
          rl.normalizar_email("№@x.cl") == "no@x.cl"
          and rl.normalizar_email("a™@x.cl") == "atm@x.cl",
          (rl.normalizar_email("№@x.cl"), rl.normalizar_email("a™@x.cl")))

    # Y NO debe colapsar cuentas legítimamente distintas.
    check("no colapsa cuentas distintas",
          rl.normalizar_email("otro@test.invalid") != rl.normalizar_email(base))

    # Propiedad directa: el criterio es la CATEGORÍA, no la clase de combinación. Se
    # comprueba con caracteres cuya clase es 0 pero cuya categoría sí está en la lista.
    import unicodedata
    for cp in (0x0902, 0x034F, 0xFE0F, 0x0488, 0x200B):
        c = chr(cp)
        check(f"U+{cp:04X} (categoria {unicodedata.category(c)}, clase "
              f"{unicodedata.combining(c)}) se descarta de la llave",
              rl.normalizar_email(f"a{c}b@x.cl") == "ab@x.cl",
              rl.normalizar_email(f"a{c}b@x.cl"))


def _paso_8_el_diagnostico_coincide_con_el_limitador():
    """`estado()` y `reservar_intento()` no pueden contradecirse.

    Con más marcas que el tope —posible en una ráfaga— `_segundos_restantes` tiene que
    mirar la marca que ocupa la posición `tope` desde el final: con `min()` daría «no
    bloqueado» mientras el login sigue respondiendo 429, EXACTAMENTE el síntoma que el
    encabezado del módulo enseña a leer como «hay más de un proceso». Y redondea hacia
    ARRIBA: una marca a la que le queda medio segundo sigue viva, así que todavía no hay
    cupo; con `int()` el último segundo se truncaría y entraría un intento de más.
    """
    rl.reset()
    llave, ip = "desfase@test.invalid", "22.22.22.22"
    # Más marcas que el tope, con la más antigua a punto de salir de la ventana.
    ahora = time.time()
    with rl._LOCK:
        rl._FALLOS[rl._contador(llave, ip)] = [ahora - rl.VENTANA_SEGUNDOS + 1 + i for i in range(8)]
        rl._SPRAY_IP[ip] = {llave: ahora}

    est = rl.estado(llave, ip)
    bloquea = False
    try:
        rl.reservar_intento(llave, ip)
    except Exception:
        bloquea = True
    check("el diagnóstico y el limitador dicen lo mismo",
          est["bloqueado"] == bloquea, (est["bloqueado"], bloquea))
    check("y el diagnóstico dice que está bloqueado", est["bloqueado"] is True, est)

    # Función pura, determinista, sin reloj: el último segundo no se trunca.
    t0 = 1_000_000.0
    marcas = [t0 + i * 0.01 for i in range(5)]
    check("a medio segundo de vencer la marca crítica sigue bloqueado (redondea hacia arriba)",
          rl._segundos_restantes(marcas, 5, t0 + rl.VENTANA_SEGUNDOS - 0.5) == 1,
          rl._segundos_restantes(marcas, 5, t0 + rl.VENTANA_SEGUNDOS - 0.5))
    check("vencida la marca crítica, hay cupo (0)",
          rl._segundos_restantes(marcas, 5, t0 + rl.VENTANA_SEGUNDOS + 0.001) == 0,
          rl._segundos_restantes(marcas, 5, t0 + rl.VENTANA_SEGUNDOS + 0.001))


def _paso_9_cuenta_real_la_variante_con_digito_exotico_no_evade():
    """Contra una cuenta que EXISTE, la llave es `uid:{id}`.

    La base funde 'admin1@x.cl' con 'admin١@x.cl' (dígito árabe-índico ≡ '1' bajo la
    collation). El plegado de Python NO cubre esa familia —los dígitos son categoría
    Nd— así que si el contador se llaveara por el correo normalizado, la variante daría
    cupo nuevo. Llaveando por `uid:{user.id}` la equivalencia la pone la base: todas las
    variantes que ella considera la misma fila caen en la misma llave.

    Se prueba con una cuenta REAL (por eso este paso toca la base y limpia con MARK), y
    con la misma cuenta se prueban las otras caras de la llave: que el texto «uid:N»
    tecleado como correo NO gasta el cupo de la cuenta N (son espacios distintos, `mail:`
    y `uid:`), que la línea de log de una cuenta real trae también el correo tecleado, y
    que el diagnóstico mira exactamente la llave que el login escribe.
    """
    rl.reset()
    db = SessionLocal()
    ip = "55.55.55.55"
    try:
        _borrar_cuentas(db)
        uid, email_base = _crear_cuenta_real(db, "robusto1")
        variante = f"robusto١-{MARK}@test.invalid".lower()   # ١ = U+0661, dígito árabe

        # La base tiene que considerar las dos la MISMA fila (si no, no hay nada que probar).
        igual = db.execute(text("SELECT :a = :b COLLATE utf8mb4_unicode_ci"),
                           {"a": email_base, "b": variante}).scalar()
        check("la base funde el dígito árabe con el latino", bool(igual), igual)

        # Llaves disjuntas: «uid:N» tecleado como correo (en cualquiera de sus formas) cae
        # al espacio `mail:`, no al de la cuenta N.
        for texto in (f"uid:{uid}", f"UID:{uid}", "ｕｉｄ：" + str(uid),
                      f" uid:{uid} ", f"uid:{uid}"):
            r = _login(texto, ip="66.66.66.66")
            check(f"el texto {texto!r} tecleado como correo responde 401",
                  r.status_code == 401, r.status_code)
        r = _login(email_base, ip=ip, clave=CLAVE_REAL)
        check("y la cuenta real entra con su clave: los cinco «uid:N» no le gastaron cupo",
              r.status_code == 200, r.status_code)

        # Se agota el tope con el correo base (clave mala). El primer intento, con la
        # salida capturada: la línea de log de una cuenta real trae su llave Y el correo,
        # para que buscar a una persona por su correo en `pm2 logs` encuentre la línea.
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            _login(email_base, ip=ip, clave="mala")
        linea = next((l for l in salida.getvalue().splitlines() if "[login] intento" in l), "")
        check("la línea de log de una cuenta real trae su llave Y el correo tecleado",
              f"llave='uid:{uid}'" in linea and f"correo='{email_base}'" in linea, linea)
        for _ in range(rl.MAX_FALLOS_POR_LLAVE - 1):
            _login(email_base, ip=ip, clave="mala")
        r = _login(email_base, ip=ip, clave="mala")
        check("la cuenta real queda bloqueada", r.status_code == 429, r.status_code)

        # La variante con el dígito árabe NO recibe cupo nuevo: misma fila, misma llave.
        r = _login(variante, ip=ip, clave="mala")
        check("la variante con dígito árabe NO evade el tope", r.status_code == 429,
              r.status_code)

        # Y con la CLAVE CORRECTA la variante tampoco entra: sigue bloqueada.
        r = _login(variante, ip=ip, clave=CLAVE_REAL)
        check("ni siquiera con la clave correcta entra la variante bloqueada",
              r.status_code == 429, r.status_code)

        # El diagnóstico mira la MISMA llave que el login. La cuenta es interna y de
        # `mineria`, así que su token pasa `require_empresa` (y no lleva huella).
        cab = {"Authorization": f"Bearer {create_access_token({'sub': str(uid)})}"}
        for consulta in (email_base, variante):
            r = client.get("/api/auth/rate-limit/estado",
                           params={"email": consulta, "ip": ip}, headers=cab)
            d = r.json() if r.status_code == 200 else {}
            check(f"el diagnóstico de {consulta!r} dice bloqueado por fallos, en la llave de la cuenta",
                  r.status_code == 200 and d.get("bloqueado") is True and d.get("motivo") == "fallos"
                  and d.get("llave") == rl.llave_de_cuenta(uid) and d.get("usuario_existe") is True,
                  (r.status_code, d))
            check("y `email` es el correo consultado, no la llave",
                  d.get("email") == consulta, d.get("email"))

        # El lado sin cuenta: el diagnóstico lee la misma llave `mail:` que escribe el login.
        correo = "diag-sin-cuenta@test.invalid"
        for _ in range(rl.MAX_FALLOS_POR_LLAVE):
            _login(correo, ip="44.44.44.44")
        r = client.get("/api/auth/rate-limit/estado",
                       params={"email": correo, "ip": "44.44.44.44"}, headers=cab)
        d = r.json() if r.status_code == 200 else {}
        check("el diagnóstico de un correo sin cuenta lo ve bloqueado en la llave `mail:`",
              r.status_code == 200 and d.get("bloqueado") is True
              and d.get("llave") == rl.llave_de_login(None, correo)
              and d.get("usuario_existe") is False,
              (r.status_code, d))
        check("y `email` es el correo consultado", d.get("email") == correo, d.get("email"))
    finally:
        _borrar_cuentas(db)
        db.close()
        rl.reset()


def _paso_10_sin_ip_de_cliente_no_hay_cerrojo_global():
    """La regla por IP solo tiene sentido si la IP distingue a un cliente de otro.

    Detrás de nginx sin `proxy_set_header X-Forwarded-For`, TODOS los clientes llegan
    como 127.0.0.1: con la regla aplicada, 10 peticiones de cualquiera dejarían sin
    login a toda la empresa. Con una IP así la regla se OMITE (y se avisa una vez); las
    reglas por llave no dependen de la IP y siguen igual. `_paso_2` vigila el otro lado:
    una IP pública conserva la regla de rociado.
    """
    for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "desconocida"):
        rl.reset()
        salida = io.StringIO()
        rechazos = 0
        with contextlib.redirect_stdout(salida):
            for i in range(rl.MAX_LLAVES_POR_IP + 2):
                try:
                    rl.reservar_intento(rl.llave_de_login(None, f"sin-ip-{i}@test.invalid"), ip)
                except HTTPException:
                    rechazos += 1
        check(f"ip {ip!r}: 12 llaves distintas pasan todas (la regla por IP se omite)",
              rechazos == 0, rechazos)
        check(f"ip {ip!r}: no deja rastro en el contador de rociado",
              ip not in rl._SPRAY_IP, list(rl._SPRAY_IP))
        texto = salida.getvalue()
        check(f"ip {ip!r}: avisa UNA sola vez y marca cada intento con regla_ip=omitida",
              texto.count("[login] AVISO") == 1 and texto.count("regla_ip=omitida") == 12,
              (texto.count("[login] AVISO"), texto.count("regla_ip=omitida")))

    # Sin IP de cliente, la regla por cuenta sigue viva: 5 pasan, la 6ª no.
    rl.reset()
    llave = rl.llave_de_cuenta(999998)
    paso = []
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(rl.MAX_FALLOS_POR_LLAVE + 1):
            try:
                rl.reservar_intento(llave, "127.0.0.1")
                paso.append(True)
            except HTTPException:
                paso.append(False)
    check("sin IP de cliente la regla por cuenta sigue viva (5 pasan, la 6ª recibe 429)",
          paso == [True] * rl.MAX_FALLOS_POR_LLAVE + [False], paso)
    rl.reset()


def _paso_11_los_exitos_tambien_topan():
    """Quien SÍ sabe la clave tampoco puede usar el login en bucle.

    Un intento correcto borra los FALLOS de la llave (si no, cinco ingresos correctos
    bloquearían al usuario), pero no los INTENTOS: sin ese segundo contador cada éxito
    borraría el rastro, y una sola cuenta —un cliente del portal, o una clave robada—
    gastaría bcrypt sin tope y ocuparía los hilos que comparte todo el ERP. Se prueba sin
    HTTP ni bcrypt: la regla vive entera en el módulo.
    """
    rl.reset()
    llave, ip = rl.llave_de_cuenta(999999), "88.88.88.88"
    codigo, detalle = None, ""
    with contextlib.redirect_stdout(io.StringIO()):
        for _ in range(rl.MAX_INTENTOS_POR_LLAVE):
            rl.reservar_intento(llave, ip)
            rl.limpiar_exito(llave, ip)
        try:
            rl.reservar_intento(llave, ip)
        except HTTPException as e:
            codigo, detalle = e.status_code, str(e.detail)
    check(f"{rl.MAX_INTENTOS_POR_LLAVE} éxitos seguidos pasan y el siguiente intento recibe 429",
          codigo == 429, codigo)
    check("y el 429 no habla de intentos «fallidos» (pueden haber sido éxitos)",
          codigo == 429 and "fallidos" not in detalle, detalle)
    est = rl.estado(llave, ip)
    check("el diagnóstico lo dice con su motivo: 'intentos', sin fallos, 30 intentos totales",
          est["bloqueado"] is True and est["motivo"] == "intentos"
          and est["intentos_fallidos"] == 0 and est["intentos_totales"] == rl.MAX_INTENTOS_POR_LLAVE,
          est)
    rl.reset()
    check("reset() también vacía el contador de intentos totales",
          rl.estado(llave, ip)["intentos_totales"] == 0, rl.estado(llave, ip))


def _paso_12_el_401_no_delata_ni_revienta():
    """El 401 es el mismo para un correo sin cuenta, una clave mala y un correo imposible,
    y no dice qué correos tienen cuenta: ni por el texto ni por el tiempo.

    · Un surrogate suelto en el JSON ("\\ud800") pasa pydantic, pero el driver de MySQL
      no puede codificarlo: sin `rl.correo_imposible` antes del SELECT el login
      respondería 500 (y en la clave, 500 solo cuando la cuenta existe: un oráculo).
    · Un correo de más de 254 caracteres no puede ser de nadie: 401 sin tocar la base
      ni el contador. El estado solo no discrimina (sin el corte también sale 401): lo
      que discrimina es el contador.
    · Un correo sin cuenta gasta un bcrypt contra `_HASH_SIN_CUENTA`, del mismo costo
      que los reales, para que el tiempo de respuesta no diga qué correos existen. Ese
      bcrypt tiene su propio techo (`rl.cupo_de_relleno`): agotado, el 401 sale sin
      bcrypt. Se mide con un espía sobre `verify_password` (determinista), no con un
      cronómetro.
    """
    rl.reset()
    r = _crudo(b'{"email":"a\\ud800@x.cl","password":"x"}')
    check("un surrogate suelto en el correo recibe el 401 de siempre, no un 500",
          r.status_code == 401 and r.json().get("detail") == DETALLE_401,
          (r.status_code, r.text[:120]))

    rl.reset()
    r = _crudo(('{"email":"' + "a" * 250 + '@x.cl","password":"x"}').encode())
    check("un correo de 255 caracteres recibe 401", r.status_code == 401, r.status_code)
    check("y no movió ningún contador",
          rl._FALLOS == {} and rl._INTENTOS == {} and rl._SPRAY_IP == {},
          (dict(rl._FALLOS), dict(rl._INTENTOS), dict(rl._SPRAY_IP)))
    r = _crudo(('{"email":"' + "a" * 249 + '@x.cl","password":"x"}').encode())
    check("uno de 254 (el borde) sí se atiende: 401 y una llave en el contador",
          r.status_code == 401 and len(rl._FALLOS) == 1, (r.status_code, list(rl._FALLOS)))

    # El espía: anota qué hash recibe cada bcrypt y delega en el original.
    rl.reset()
    vistos = []
    original = modulo_login.verify_password

    def espia(clave, hash_guardado):
        vistos.append(hash_guardado)
        return original(clave, hash_guardado)

    modulo_login.verify_password = espia
    try:
        r = _login("sin-cuenta-relleno@test.invalid")
        check("un correo sin cuenta responde 401 y gasta EXACTAMENTE un bcrypt, contra el hash de relleno",
              r.status_code == 401 and vistos == [modulo_login._HASH_SIN_CUENTA],
              (r.status_code, len(vistos)))

        del vistos[:]
        with rl._LOCK:
            rl._RELLENOS.extend([time.time()] * (rl.MAX_RELLENOS_POR_VENTANA - len(rl._RELLENOS)))
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            r1 = _login("sin-cuenta-2@test.invalid")
            r2 = _login("sin-cuenta-3@test.invalid")
        check("con el techo de rellenos agotado, el 401 sale SIN bcrypt",
              r1.status_code == 401 and r2.status_code == 401 and vistos == [],
              (r1.status_code, r2.status_code, len(vistos)))
        check("y lo avisa una sola vez", salida.getvalue().count("[login] AVISO") == 1,
              salida.getvalue()[-300:])
        with rl._LOCK:
            viejas = [t - rl.VENTANA_SEGUNDOS - 1 for t in rl._RELLENOS]
            rl._RELLENOS.clear()
            rl._RELLENOS.extend(viejas)
        check("al pasar la ventana vuelve el cupo de relleno", rl.cupo_de_relleno() is True)
    finally:
        modulo_login.verify_password = original

    check("el hash de relleno cuesta lo mismo que uno real (mismo prefijo de costo)",
          modulo_login._HASH_SIN_CUENTA[:7] == get_password_hash("x")[:7],
          (modulo_login._HASH_SIN_CUENTA[:7], get_password_hash("x")[:7]))

    # La clave con un surrogate: 401 haya cuenta o no, con el mismo detalle.
    rl.reset()
    db = SessionLocal()
    try:
        _borrar_cuentas(db)
        _, email_real = _crear_cuenta_real(db, "relleno")
        con = _crudo(('{"email":"' + email_real + '","password":"\\ud800"}').encode())
        sin = _crudo(('{"email":"nadie-' + MARK.lower() + '@test.invalid","password":"\\ud800"}').encode())
        check("una clave con un surrogate recibe 401 con cuenta y sin cuenta, con el mismo detalle",
              con.status_code == 401 and sin.status_code == 401 and con.json() == sin.json(),
              (con.status_code, sin.status_code, con.text[:80], sin.text[:80]))
    finally:
        _borrar_cuentas(db)
        db.close()
        rl.reset()


def run():
    try:
        _paso_1_tope_por_llave()
        _paso_2_tope_por_ip_rociado()
        _paso_3_el_nat_no_se_castiga()
        _paso_4_se_levanta_y_el_exito_limpia()
        _paso_5_ip_desde_x_forwarded_for_toma_el_ultimo_salto()
        _paso_6_la_reserva_es_atomica()
        _paso_7_las_tildes_no_dan_cupo_nuevo()
        _paso_8_el_diagnostico_coincide_con_el_limitador()
        _paso_9_cuenta_real_la_variante_con_digito_exotico_no_evade()
        _paso_10_sin_ip_de_cliente_no_hay_cerrojo_global()
        _paso_11_los_exitos_tambien_topan()
        _paso_12_el_401_no_delata_ni_revienta()
    finally:
        rl.reset()
        print("Cleanup OK (contadores en memoria vaciados)")
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_login_rate_limit():
    run()


if __name__ == "__main__":
    run()
