from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://machparts_user:password@localhost:3306/machparts_db"
    SECRET_KEY: str = "changeme-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "https://machparts.bigcode.cl"]
    NEXOR_WEBHOOK_KEY: str = ""
    # Clave de la integración PartsFlow (POST /api/monza/integraciones/partsflow/leads,
    # 2026-10-21). Vacía = integración deshabilitada (responde 401). Una por ambiente.
    PARTSFLOW_API_KEY: str = ""
    # Contabilidad MonzaParts: en PROD va APAGADA (decision cliente 2026-07-15).
    # Con False, main.py ni importa esos modulos -> sus rutas responden 404 y create_all
    # no crea sus tablas. Son 6 modulos: contabilidad, tesoreria, compras/CxP, pricing,
    # DTE al SII y — desde 2026-08-08 — el Libro de compras del SII de Monza
    # (monza_wasabil_compras), que antes se montaba SIEMPRE: barria de noche y escribia
    # un espejo que nadie podia mirar, porque su pantalla obedece al mismo flag.
    MONZA_CONTAB_ENABLED: bool = True
    # Matcher banco<->libro SII: ¿puede correr DESATENDIDO en el job de las 05:30?
    # Nace APAGADO en las dos marcas, a proposito. El motor no cambia; esto es solo la
    # puerta. Con el matcher suelto, la PRIMERA noche despues del deploy corre sobre la
    # cartola historica completa (no tiene ventana de fecha) y puede dejar movimientos
    # del banco marcados conciliado=True por una decision automatica que nadie reviso.
    # El estreno se hace a mano, de dia y con el dueño del proceso al lado (boton
    # "Correr matcher" de Contabilidad -> Libro SII); recien despues se prende esto.
    # Mismo criterio que la señal RUT-en-glosa, que tambien nace apagada.
    SII_MATCHER_NOCTURNO: bool = False
    SII_MATCHER_NOCTURNO_MONZA: bool = False
    # Registro MANUAL de factura/boleta en Contabilidad MonzaParts (2026-09-29): con True
    # (default, PROD) todo folio tecleado se VERIFICA contra Wasabil o se declara emitido
    # fuera de Wasabil (monza_contabilidad/verificacion_folio.py). False = comportamiento
    # anterior (folio sin verificar). Existe para dos cosas: correr las suites antiguas
    # que registran facturas manuales sin simular Wasabil
    # (`MONZA_FOLIO_VERIFICACION=false pytest ...`) y como interruptor de emergencia si
    # el API de consulta de Wasabil cambiara. NO es para "saltarse" la verificación.
    MONZA_FOLIO_VERIFICACION: bool = True
    # create_all al arrancar: crea tablas que falten (NO agrega columnas a tablas
    # existentes; para eso estan backend/migrations/).
    AUTO_CREATE_TABLES: bool = True
    # Wasabil (facturador electrónico — emisión de DTE al SII). El token se genera en
    # https://app.wasabil.com/api-tokens y va SOLO en backend/.env (nunca en git).
    # Sin token, el módulo wasabil_dte permite previsualizar pero NO emitir.
    WASABIL_API_TOKEN: str = ""
    # Token de la cuenta Wasabil de MonzaParts (LOPEZ HERNANDEZ INVERSIONES SPA,
    # RUT 78.121.316-0) — módulo monza_wasabil_dte (Fase 5 del espejo). El valor va
    # SOLO en backend/.env: jamás leerlo/imprimirlo/commitearlo. OJO: sin este campo
    # declarado, pydantic rechaza la variable del .env (extra_forbidden) y el backend
    # COMPLETO no arranca.
    WASABIL_API_TOKEN_MONZA: str = ""
    WASABIL_API_BASE: str = "https://api.wasabil.com/api"
    # Correo saliente de la app (tickets/avisos). Casilla no-reply@bigcode.cl.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    MAIL_FROM: str = "no-reply@bigcode.cl"
    MAIL_SOPORTE: str = "soporte@bigcode.cl"
    # ── Fase 0 del Portal de Clientes (2026-08-20) ────────────────────────────────
    # Limite de intentos del login (politica y modos de falla: backend/rate_limit_login.py).
    # Nace ENCENDIDO. Apagarlo es un .env + restart, sin deploy de codigo, por si bloquea a
    # alguien un dia de operacion critica; apagado, las cuentas quedan sin tope de intentos.
    LOGIN_RATE_LIMIT_ENABLED: bool = True
    # Tope de peticiones por minuto de cada cuenta del Portal de Clientes (tablero,
    # descargas, todo lo demás). Nace ENCENDIDO. Apagarlo es un .env + restart. Vive en
    # memoria del proceso: ver backend/portal/cupo.py.
    PORTAL_CUPO_ENABLED: bool = True
    # Servir uploads/bodega como carpeta PUBLICA (el comportamiento historico).
    # Nace APAGADO: las fotos de recepcion se sirven ahora por
    # GET /api/bodega/fotos/{id}, que exige sesion y resuelve el archivo desde la BD.
    # La carpeta publica tenia nombres adivinables (recepcion_{id}_{timestamp}.jpg).
    # Es la PALANCA DE ROLLBACK: ponerlo en true reactiva el mount de main.py en 30 s.
    SERVIR_FOTOS_BODEGA_SIN_AUTH: bool = False

    class Config:
        env_file = ".env"


settings = Settings()

# Fail-fast: la app no arranca con una SECRET_KEY que no es una clave. Con cualquiera de
# estos valores, quien tenga el repo puede firmar un JWT con el `sub` de cualquier usuario
# y `auth.get_usuario_autenticado` lo acepta. Si cambia el placeholder de
# backend/.env.example, cambiarlo aca (lo vigila routers/tests/test_config_secret_key.py).
_SECRET_KEY_SIN_CONFIGURAR = frozenset({
    "",                                  # la variable esta, pero vacia
    "changeme-in-production",            # el default de Settings
    "CAMBIAR-POR-UNO-ALEATORIO-LARGO",   # el placeholder de .env.example
})
if settings.SECRET_KEY.strip() in _SECRET_KEY_SIN_CONFIGURAR:
    raise RuntimeError(
        "SECRET_KEY no esta configurada (backend/.env): esta vacia, o tiene el valor por "
        "defecto o el de la plantilla .env.example. Generar una con: "
        "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    )

# Una clave corta se puede adivinar por fuerza bruta a partir de un solo token. No corta
# el arranque: el largo de las claves ya desplegadas no se conoce desde el repo y un raise
# dejaria el backend entero caido en el siguiente restart. Se AVISA, igual que con CORS.
_LARGO_MINIMO_SECRET_KEY = 32
if len(settings.SECRET_KEY.strip()) < _LARGO_MINIMO_SECRET_KEY:
    print(
        f"[config] AVISO SECRET_KEY: mide menos de {_LARGO_MINIMO_SECRET_KEY} caracteres. "
        "Conviene generar una nueva (cambiarla cierra las sesiones abiertas: todos vuelven "
        "a entrar).",
        flush=True,
    )

# Aviso de CORS (Fase 0, 2026-08-20): el default de CORS_ORIGINS incluye
# http://localhost:5173 para que el `npm run dev` funcione sin configurar nada. En
# PRODUCCION eso deja un origen de desarrollo autorizado con allow_credentials=True.
# No se cambia el default (romperia el entorno de desarrollo de todos): se AVISA en el
# log de arranque, y se corrige poniendo CORS_ORIGINS en backend/.env (ver .env.example).
_CORS_DEV = [o for o in settings.CORS_ORIGINS if "localhost" in o or "127.0.0.1" in o]
if _CORS_DEV:
    print(
        "[config] AVISO CORS: hay origenes de desarrollo autorizados "
        f"{_CORS_DEV}. En produccion define CORS_ORIGINS en backend/.env.",
        flush=True,
    )
