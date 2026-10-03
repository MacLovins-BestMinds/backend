import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Stage Zero Backend"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DATABASE_URL: str = "postgresql://stage_zero:stage_zero_password@localhost:5432/stage_zero"
    CORS_ORIGINS: str = "*"
    MOCK_FALLBACK: bool = True

    # JWT Авторизация
    JWT_SECRET_KEY: str = "stage_zero_super_secret_jwt_key_2026_dev"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_DAYS: int = 30

    # Google OAuth 2.0
    GOOGLE_CLIENT_ID: Optional[str] = None

    # Пути
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    TOPICS_JSON_PATH: Path = BASE_DIR / "content" / "topics.json"
    STATIC_DIR: Path = BASE_DIR / "static"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
