"""GET /api/ai/health: живы ли внешние AI-сервисы с текущими ключами — проверить перед показом."""

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
from pydantic import BaseModel

from app.ai.clients import elevenlabs_client, gemini_client
from app.ai.config import get_settings

CHECK_TIMEOUT_SEC = 8.0


class ServiceCheck(BaseModel):
    ok: bool
    ms: int
    detail: str


class HealthResponse(BaseModel):
    ok: bool
    mock: bool
    checks: dict[str, ServiceCheck]


async def _gemini() -> str:
    model = await gemini_client().aio.models.get(model=get_settings().gemini_model)
    return f"model {model.name}"


async def _elevenlabs() -> str:
    s = get_settings()
    voice = await elevenlabs_client().voices.get(s.elevenlabs_voice_id)
    return f"voice \"{voice.name}\"; STT {s.stt_model} + {s.live_stt_model}, TTS {s.tts_model}"


async def _azure() -> str:
    s = get_settings()
    url = f"https://{s.azure_speech_region}.api.cognitive.microsoft.com/sts/v1.0/issueToken"
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SEC) as client:
        response = await client.post(url, headers={"Ocp-Apim-Subscription-Key": s.azure_speech_key})
    response.raise_for_status()
    return f"region {s.azure_speech_region}, {s.azure_speech_locale}"


async def _run(check: Callable[[], Awaitable[str]]) -> ServiceCheck:
    started = time.perf_counter()
    try:
        detail = await asyncio.wait_for(check(), CHECK_TIMEOUT_SEC)
        ok = True
    except Exception as e:  # noqa: BLE001 — любая ошибка = сервис недоступен; текст ошибки без ключей
        detail, ok = f"{type(e).__name__}: {str(e)[:160]}", False
    return ServiceCheck(ok=ok, ms=round((time.perf_counter() - started) * 1000), detail=detail)


async def run_health() -> HealthResponse:
    s = get_settings()
    checks: dict[str, Callable[[], Awaitable[str]]] = {"gemini": _gemini}
    if "elevenlabs" in {s.stt_provider, s.live_stt_provider, s.tts_provider}:
        checks["elevenlabs"] = _elevenlabs
    if s.azure_speech_key:  # оценивается только английская речь, но ключ проверяем всегда
        checks["azure_pronunciation"] = _azure
    results = await asyncio.gather(*(_run(fn) for fn in checks.values()))
    by_name = dict(zip(checks, results, strict=True))
    return HealthResponse(ok=all(r.ok for r in results), mock=s.ai_mock, checks=by_name)
