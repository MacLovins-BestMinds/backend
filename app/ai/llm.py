"""Вызовы Gemini со строгим JSON-ответом. Промпты — файлы в app/ai/prompts/ с подстановками $var."""

import asyncio
import json
import logging
from functools import lru_cache
from pathlib import Path
from string import Template

from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.ai import cache
from app.ai.clients import gemini_client
from app.ai.config import get_settings

logger = logging.getLogger(__name__)
PROMPTS_DIR = Path(__file__).parent / "prompts"

RETRY_DELAYS_SEC = (0.5,)  # 2 попытки на модель: при перегрузке быстрее уйти на запасную
RETRYABLE_CODES = {429, 500, 503, 504}  # перегрузка и временные сбои Gemini


async def _call(model: str, prompt: str, schema: type[BaseModel]) -> types.GenerateContentResponse:
    return await gemini_client().aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            seed=0,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )


async def _generate_with_fallback(prompt: str, schema: type[BaseModel]) -> types.GenerateContentResponse:
    """Повторы при перегрузке, затем по очереди запасные модели (GEMINI_FALLBACK_MODELS)."""
    last_error: errors.APIError | None = None
    for model in get_settings().gemini_models:
        for delay in (*RETRY_DELAYS_SEC, None):
            try:
                return await _call(model, prompt, schema)
            except errors.APIError as e:
                if e.code not in RETRYABLE_CODES:
                    raise
                last_error = e
                logger.warning("gemini %s: %s (детали: %s)", model, e.code, str(e)[:120])
                err_str = str(e).lower()
                # При исчерпании суточной квоты модели сразу переходим к следующей без задержки
                if e.code == 429 and ("per day" in err_str or "free_tier" in err_str or "quota" in err_str):
                    break
                if delay:
                    await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


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

    response = await _generate_with_fallback(prompt, schema)
    result = schema.model_validate_json(response.text or "")
    await cache.put("llm", key, "json", result.model_dump_json().encode())
    return result
