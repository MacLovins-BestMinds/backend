import asyncio
from types import SimpleNamespace

from google.genai import errors
from pydantic import BaseModel

from app.ai import llm


class Answer(BaseModel):
    score: int


def test_overloaded_model_retries_then_falls_back(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_call(model, prompt, schema):
        calls.append(model)
        if model == "primary":
            raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
        return SimpleNamespace(text='{"score": 5}')

    async def no_sleep(_):
        return None

    monkeypatch.setattr(llm, "_call", fake_call)
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(gemini_models=["primary", "backup"]))

    response = asyncio.run(llm._generate_with_fallback("p", Answer))
    assert response.text == '{"score": 5}'
    assert calls == ["primary", "primary", "backup"]
