"""Общие клиенты внешних AI-сервисов (создаются лениво, один раз на процесс)."""

from functools import lru_cache

from google import genai
from openai import AsyncOpenAI

from app.ai.config import get_settings


@lru_cache
def openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=get_settings().openai_api_key)


@lru_cache
def gemini_client() -> genai.Client:
    return genai.Client(api_key=get_settings().gemini_api_key)
