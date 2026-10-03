"""Распознавание записи с таймкодами слов (OpenAI Whisper)."""

import json
from dataclasses import asdict, dataclass

from app.ai import cache
from app.ai.clients import openai_client
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

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "Transcript":
        raw = json.loads(data)
        return cls(text=raw["text"], words=[Word(**w) for w in raw["words"]], duration=raw["duration"])


async def transcribe(wav: bytes) -> Transcript:
    settings = get_settings()
    key = cache.make_key(settings.stt_model, settings.stt_language, FILLER_PROMPT, wav)
    if cached := await cache.get("stt", key, "json"):
        return Transcript.from_json(cached)

    result = await openai_client().audio.transcriptions.create(
        file=("speech.wav", wav, "audio/wav"),
        model=settings.stt_model,
        language=settings.stt_language,
        prompt=FILLER_PROMPT,
        response_format="verbose_json",
        timestamp_granularities=["word"],
        temperature=0,
    )
    words = [Word(w.word, w.start, w.end) for w in result.words or []]
    transcript = Transcript(text=result.text.strip(), words=words, duration=result.duration)
    await cache.put("stt", key, "json", transcript.to_json())
    return transcript
