import logging
import secrets
from pathlib import Path
from typing import Optional
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# значение, которое лежало в .env.example в публичном git, — секретом не считается
_PUBLIC_DEV_JWT_SECRET = "stage_zero_super_secret_jwt_key_2026_dev"


class Settings(BaseSettings):
    PROJECT_NAME: str = "Stage Zero Backend"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DATABASE_URL: str = "postgresql://stage_zero:stage_zero_password@localhost:5432/stage_zero"
    CORS_ORIGINS: str = "*"
    # заглушки баллов в finish для раундов, не прошедших через AI (моки, демо); в проде — false
    MOCK_FALLBACK: bool = False
    # нет PostgreSQL → локальный SQLite: удобно для разработки, на сервере — false (падать, а не терять данные)
    DATABASE_SQLITE_FALLBACK: bool = True

    # JWT Авторизация
    JWT_SECRET_KEY: str = ""  # пусто — случайный на каждый запуск (токены не переживут рестарт)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_DAYS: int = 30

    # Google OAuth 2.0
    GOOGLE_CLIENT_ID: Optional[str] = None
    # принимать тестовые Google-токены вида mock_<id> (только для тестов и локальной разработки)
    AUTH_MOCK_GOOGLE: bool = False

    # Почта для кода подтверждения при регистрации. Пусто — письма не уходят, код пишется в лог сервера
    # и отдаётся в ответе регистрации (dev_code), чтобы вход работал на локальной машине.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    EMAIL_CODE_TTL_MIN: int = 15
    # false — регистрация по почте сразу пускает в игру, код из письма не нужен (пока так);
    # true — аккаунт работает только после подтверждения кода
    EMAIL_VERIFICATION: bool = False

    # Пути
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    TOPICS_JSON_PATH: Path = BASE_DIR / "content" / "topics.json"
    STATIC_DIR: Path = BASE_DIR / "static"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def _ensure_jwt_secret(self) -> "Settings":
        if not self.JWT_SECRET_KEY or self.JWT_SECRET_KEY == _PUBLIC_DEV_JWT_SECRET:
            logger.warning(
                "JWT_SECRET_KEY не задан или публичный — сгенерирован случайный, токены не переживут рестарт. "
                "Задайте свой: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
            self.JWT_SECRET_KEY = secrets.token_urlsafe(32)
        return self


settings = Settings()
