"""Вызовы Gemini со строгим JSON-ответом. Промпты — файлы в app/ai/prompts/ с подстановками $var."""

import json
import logging
from functools import lru_cache
from pathlib import Path
from string import Template

from google.genai import types
from pydantic import BaseModel, ValidationError

from app.ai import cache
from app.ai.clients import gemini_client
from app.ai.config import get_settings

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache
def load_prompt(name: str) -> Template:
    return Template((PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8"))


async def generate[T: BaseModel](prompt_name: str, schema: type[T], **variables: object) -> T:
    """Температура 0, фиксированный seed и кэш: одно и то же выступление → одна и та же оценка."""
    model = get_settings().gemini_model
    prompt = load_prompt(prompt_name).substitute(variables)
    key = cache.make_key(model, prompt, json.dumps(schema.model_json_schema(), sort_keys=True))
    if cached := await cache.get("llm", key, "json"):
        try:
            return schema.model_validate_json(cached)
        except ValidationError:
            logger.warning("llm: битая запись кэша %s, запрашиваю заново", key[:12])

    response = await gemini_client().aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            seed=0,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    result = schema.model_validate_json(response.text or "")
    await cache.put("llm", key, "json", result.model_dump_json().encode())
    return result
