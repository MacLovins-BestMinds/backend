"""Общие клиенты внешних AI-сервисов (создаются лениво, один раз на процесс)."""

from functools import lru_cache

from google import genai
from openai import AsyncOpenAI

from app.ai.config import get_settings


class MissingKeyError(RuntimeError):
    """В окружении нет ключа к AI-сервису."""


def _require(key: str, env_name: str) -> str:
    if not key:
        raise MissingKeyError(f"AI не настроен: задайте {env_name} в .env или используйте ?mock=1")
    return key


@lru_cache
def openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=_require(get_settings().openai_api_key, "OPENAI_API_KEY"))


@lru_cache
def gemini_client() -> genai.Client:
    return genai.Client(api_key=_require(get_settings().gemini_api_key, "GEMINI_API_KEY"))
