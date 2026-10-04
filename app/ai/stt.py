"""Распознавание записей с таймкодами слов: ElevenLabs Scribe (по умолчанию) или OpenAI Whisper (STT_PROVIDER)."""

import json
from dataclasses import asdict, dataclass

from app.ai import cache
from app.ai.clients import elevenlabs_client, openai_client
from app.ai.config import get_settings
from app.ai.limits import elevenlabs_call

# Whisper по умолчанию «вычищает» речь; подсказка с паразитами заставляет их сохранять.
# Scribe распознаёт дословно (no_verbatim=false), подсказка ему не нужна.
FILLER_PROMPT = "Um, uh... so, you know, I mean, basically. Hmm, er, like, well."
WAV_HEADER_BYTES = 44
WAV_BYTES_PER_SEC = 16_000 * 2  # audio.to_wav16k: 16 кГц, моно, 16 бит


_CYR = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LAT = ("a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
        "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya")  # fmt: skip
_TRANSLIT = {ord(c): lat for c, lat in zip(_CYR, _LAT, strict=True)}
_TRANSLIT |= {ord(c.upper()): lat.capitalize() for c, lat in zip(_CYR, _LAT, strict=True)}


def to_latin(text: str) -> str:
    """Кириллица → латиница. Scribe пишет русские слова по-русски даже с language_code=en,
    а в английском режиме вся расшифровка должна быть английскими буквами."""
    return text.translate(_TRANSLIT)


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


async def _elevenlabs(wav: bytes) -> Transcript:
    s = get_settings()
    result = await elevenlabs_client().speech_to_text.convert(
        model_id=s.stt_model,
        file=("speech.wav", wav, "audio/wav"),
        language_code=s.stt_language,
        timestamps_granularity="word",
        tag_audio_events=False,
        no_verbatim=False,  # оставить «ну», «эм» и оговорки — по ним считается подача
        temperature=0,
        seed=0,
    )
    words = [Word(w.text, w.start or 0.0, w.end or 0.0) for w in result.words or [] if w.type == "word"]
    duration = result.audio_duration_secs or max(0.0, (len(wav) - WAV_HEADER_BYTES) / WAV_BYTES_PER_SEC)
    return Transcript(text=result.text.strip(), words=words, duration=duration)


async def _openai(wav: bytes) -> Transcript:
    s = get_settings()
    result = await openai_client().audio.transcriptions.create(
        file=("speech.wav", wav, "audio/wav"),
        model=s.stt_model,
        language=s.stt_language,
        prompt=FILLER_PROMPT,
        response_format="verbose_json",
        timestamp_granularities=["word"],
        temperature=0,
    )
    words = [Word(w.word, w.start, w.end) for w in result.words or []]
    return Transcript(text=result.text.strip(), words=words, duration=result.duration)


async def transcribe(wav: bytes) -> Transcript:
    """wav — 16 кГц моно 16 бит (audio.to_wav16k). Оба провайдера отдают таймкоды слов."""
    s = get_settings()
    key = cache.make_key(s.stt_provider, s.stt_model, s.stt_language, FILLER_PROMPT, wav)
    if cached := await cache.get("stt", key, "json"):
        return _in_language(Transcript.from_json(cached), s.stt_language)

    if s.stt_provider == "elevenlabs":
        transcript = await elevenlabs_call("stt", lambda: _elevenlabs(wav))
    else:
        transcript = await _openai(wav)
    await cache.put("stt", key, "json", transcript.to_json())
    return _in_language(transcript, s.stt_language)


def _in_language(transcript: Transcript, language: str) -> Transcript:
    if language != "en":
        return transcript
    return Transcript(
        text=to_latin(transcript.text),
        words=[Word(to_latin(w.text), w.start, w.end) for w in transcript.words],
        duration=transcript.duration,
    )
