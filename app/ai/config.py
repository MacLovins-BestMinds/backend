"""Настройки AI-движка из окружения / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ai_mock: bool = False
    openai_api_key: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    stt_model: str = "whisper-1"
    stt_language: str = "ru"
    tts_model: str = "gpt-4o-mini-tts"
    static_dir: str = "static"
    max_audio_mb: int = 25


@lru_cache
def get_settings() -> AiSettings:
    return AiSettings()
