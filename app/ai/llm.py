"""Вызовы Gemini со строгим JSON-ответом. Промпты — файлы в app/ai/prompts/ с подстановками $var."""

from functools import lru_cache
from pathlib import Path
from string import Template

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.ai.config import get_settings

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache
def _client() -> genai.Client:
    return genai.Client(api_key=get_settings().gemini_api_key)


@lru_cache
def load_prompt(name: str) -> Template:
    return Template((PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8"))


async def generate[T: BaseModel](prompt_name: str, schema: type[T], **variables: object) -> T:
    """Температура 0 и фиксированный seed: одно и то же выступление → одна и та же оценка."""
    prompt = load_prompt(prompt_name).substitute(variables)
    response = await _client().aio.models.generate_content(
        model=get_settings().gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            seed=0,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    return schema.model_validate_json(response.text or "")
