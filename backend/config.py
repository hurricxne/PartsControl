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
    # Con False, main.py ni importa esos modulos -> create_all no crea sus tablas.
    MONZA_CONTAB_ENABLED: bool = True
    # create_all al arrancar: crea tablas que falten (NO agrega columnas a tablas
    # existentes; para eso estan backend/migrations/).
    AUTO_CREATE_TABLES: bool = True
    # Wasabil (facturador electrónico — emisión de DTE al SII). El token se genera en
    # https://app.wasabil.com/api-tokens y va SOLO en backend/.env (nunca en git).
    # Sin token, el módulo wasabil_dte permite previsualizar pero NO emitir.
    WASABIL_API_TOKEN: str = ""
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
