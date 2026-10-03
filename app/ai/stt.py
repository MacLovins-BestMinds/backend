"""Распознавание записи с таймкодами слов (OpenAI Whisper)."""

from dataclasses import dataclass
from functools import lru_cache

from openai import AsyncOpenAI

from app.ai.config import get_settings

# Whisper по умолчанию «вычищает» речь; подсказка с паразитами заставляет их сохранять
FILLER_PROMPT = "Ну, эм... ээ, как бы, вот, короче, типа, это самое. Ммм, значит, в общем."


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    words: list[Word]
    duration: float


@lru_cache
def _client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=get_settings().openai_api_key)


async def transcribe(wav: bytes) -> Transcript:
    settings = get_settings()
    result = await _client().audio.transcriptions.create(
        file=("speech.wav", wav, "audio/wav"),
        model=settings.stt_model,
        language=settings.stt_language,
        prompt=FILLER_PROMPT,
        response_format="verbose_json",
        timestamp_granularities=["word"],
        temperature=0,
    )
    words = [Word(w.word, w.start, w.end) for w in result.words or []]
    return Transcript(text=result.text.strip(), words=words, duration=result.duration)
