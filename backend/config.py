from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://machparts_user:password@localhost:3306/machparts_db"
    SECRET_KEY: str = "changeme-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "https://machparts.bigcode.cl"]
    NEXOR_WEBHOOK_KEY: str = ""
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

    class Config:
        env_file = ".env"


settings = Settings()

# Fail-fast (hallazgo revision 2026-07-16): la app NO debe arrancar con la clave
# JWT por defecto — cualquiera podria firmar tokens validos.
if settings.SECRET_KEY == "changeme-in-production":
    raise RuntimeError(
        "SECRET_KEY no esta configurada (backend/.env). La app no arranca con la "
        "clave por defecto."
    )
