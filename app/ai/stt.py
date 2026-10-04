"""Распознавание записей с таймкодами слов: ElevenLabs Scribe (по умолчанию) или OpenAI Whisper (STT_PROVIDER).

Язык не задаётся: распознавание определяет его само (en | ru | ro), расшифровка остаётся в исходном
алфавите — русская речь кириллицей, румынская с диакритикой.
"""

import json
from dataclasses import asdict, dataclass

from app.ai import cache
from app.ai.clients import elevenlabs_client, openai_client
from app.ai.config import get_settings
from app.ai.limits import elevenlabs_call
from app.core.lang import normalize_lang

# Whisper по умолчанию «вычищает» речь; подсказка с паразитами заставляет их сохранять.
# Scribe распознаёт дословно (no_verbatim=false), подсказка ему не нужна.
FILLER_PROMPT = "Um, uh... so, you know, I mean, basically. Hmm, er, like, well."
WAV_HEADER_BYTES = 44
WAV_BYTES_PER_SEC = 16_000 * 2  # audio.to_wav16k: 16 кГц, моно, 16 бит


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start: float
    end: float
    # когда начинается каждая буква text (секунды); None — провайдер не дал (Whisper, старый кэш)
    chars: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    words: list[Word]
    duration: float
    language: str | None = None  # язык речи по распознаванию: en | ru | ro; None — не определён или другой

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "Transcript":
        raw = json.loads(data)
        return cls(
            text=raw["text"],
            words=[Word(w["text"], w["start"], w["end"], tuple(w["chars"]) if w.get("chars") else None) for w in raw["words"]],
            duration=raw["duration"],
            language=raw.get("language"),
        )


def _char_starts(word) -> tuple[float, ...] | None:
    """Начало каждой буквы слова от Scribe; не сошлось с текстом слова — без букв (подсветка пойдёт по слову)."""
    chars = getattr(word, "characters", None) or []
    if not chars or len(chars) != len(word.text):
        return None
    base = word.start or 0.0
    return tuple(round(c.start if c.start is not None else base, 3) for c in chars)


async def _elevenlabs(wav: bytes) -> Transcript:
    s = get_settings()
    result = await elevenlabs_client().speech_to_text.convert(
        model_id=s.stt_model,
        file=("speech.wav", wav, "audio/wav"),
        # language_code не передаём: Scribe сам определяет язык и возвращает его в language_code
        timestamps_granularity="character",  # время каждой буквы — разбор подсвечивает текст по буквам
        tag_audio_events=False,
        no_verbatim=False,  # оставить «ну», «эм» и оговорки — по ним считается подача
        temperature=0,
        seed=0,
    )
    words = [Word(w.text, w.start or 0.0, w.end or 0.0, _char_starts(w)) for w in result.words or [] if w.type == "word"]
    duration = result.audio_duration_secs or max(0.0, (len(wav) - WAV_HEADER_BYTES) / WAV_BYTES_PER_SEC)
    language = normalize_lang(getattr(result, "language_code", None))
    return Transcript(text=result.text.strip(), words=words, duration=duration, language=language)


async def _openai(wav: bytes) -> Transcript:
    s = get_settings()
    result = await openai_client().audio.transcriptions.create(
        file=("speech.wav", wav, "audio/wav"),
        model=s.stt_model,
        # language не передаём: Whisper определяет язык сам и возвращает его название («english»)
        prompt=FILLER_PROMPT,
        response_format="verbose_json",
        timestamp_granularities=["word"],
        temperature=0,
    )
    words = [Word(w.word, w.start, w.end) for w in result.words or []]
    language = normalize_lang(getattr(result, "language", None))
    return Transcript(text=result.text.strip(), words=words, duration=result.duration, language=language)


async def transcribe(wav: bytes) -> Transcript:
    """wav — 16 кГц моно 16 бит (audio.to_wav16k). Оба провайдера отдают таймкоды слов и язык речи."""
    s = get_settings()
    key = cache.make_key(s.stt_provider, s.stt_model, "auto", "chars", FILLER_PROMPT, wav)
    if cached := await cache.get("stt", key, "json"):
        return Transcript.from_json(cached)

    if s.stt_provider == "elevenlabs":
        transcript = await elevenlabs_call("stt", lambda: _elevenlabs(wav))
    else:
        transcript = await _openai(wav)
    await cache.put("stt", key, "json", transcript.to_json())
    return transcript


def speech_lang(transcript: Transcript, fallback: str) -> str:
    """Язык речи: что определило распознавание, а если не определило (или язык не поддерживается) — fallback."""
    return transcript.language or fallback
