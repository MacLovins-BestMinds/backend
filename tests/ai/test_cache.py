import asyncio
from types import SimpleNamespace

from pydantic import BaseModel

from app.ai import llm
from app.ai.config import get_settings


class Answer(BaseModel):
    score: int


def test_same_prompt_hits_disk_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AI_CACHE_DIR", str(tmp_path))
    get_settings.cache_clear()
    calls = []

    async def generate_content(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text='{"score": 7}')

    fake = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    monkeypatch.setattr(llm, "gemini_client", lambda: fake)

    async def twice() -> list[Answer]:
        kw = {"juror_name": "Глеб", "juror_persona": "", "title": "t", "audience": "a", "question": "q", "answer": "x", "level_scoring": "-",
              "answer_language": "Russian", "feedback_language": "English"}  # fmt: skip
        return [await llm.generate("jury_answer", Answer, **kw) for _ in range(2)]

    try:
        assert asyncio.run(twice()) == [Answer(score=7)] * 2
        assert len(calls) == 1
        assert len(list(tmp_path.rglob("*.json"))) == 1
    finally:
        get_settings.cache_clear()
