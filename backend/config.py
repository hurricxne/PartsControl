from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://machparts_user:password@localhost:3306/machparts_db"
    SECRET_KEY: str = "changeme-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "https://machparts.bigcode.cl"]

    class Config:
        env_file = ".env"


settings = Settings()
