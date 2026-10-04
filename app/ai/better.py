"""«Лучшая версия» (GET /api/ai/rounds/{id}/better-version): тот же питч голосом самого игрока, но без паразитов,
оговорок, повторов, запинок и плохих пауз — то же содержание, почти те же слова, похожий темп.

1. Gemini чистит расшифровку на языке речи (промпт better_script.md): убирает лишнее, факты не добавляет.
2. ElevenLabs Instant Voice Cloning клонирует голос по записи раунда (static/recordings), клон читает текст
   на языке речи в темпе игрока; mp3 → static/better/<round_id>.mp3.
3. Клон удаляется сразу после озвучки — всегда, и при ошибке тоже: голос игрока не остаётся в аккаунте.
   Клоны, которые не удалились (сервер упал посреди задачи), подчищает следующая задача.
Все запросы к ElevenLabs — через слоты (app/ai/limits.py). Цена — кредиты ElevenLabs за символы текста.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from elevenlabs import VoiceSettings
from pydantic import BaseModel

from app.ai import cache, llm
from app.ai.audio import to_voice_sample
from app.ai.clients import elevenlabs_client
from app.ai.config import get_settings
from app.ai.flow import speech_seconds, timed_words
from app.ai.limits import elevenlabs_call
from app.core.config import settings as app_settings
from app.core.lang import default_lang, language_name, normalize_lang, pick
from app.recordings import recording_path

logger = logging.getLogger(__name__)

BASE_WPM = 140  # обычный темп голоса ElevenLabs при speed=1
SPEED_RANGE = (0.85, 1.15)  # за этими пределами голос звучит неестественно
LENGTH_RANGE = (0.5, 1.3)  # очищенный текст относительно расшифровки: короче или длиннее — модель что-то выдумала
VOICE_PREFIX = "stager-"
VOICE_DESCRIPTION = (
    "Stager: the user's own voice, cloned from their own pitch recording for their private replay "
    "of a cleaner version of the same pitch. Deleted right after the audio is made."
)
ORPHAN_AGE_SEC = 600
OUTPUT_FORMAT = "mp3_44100_128"

# Почему лучшей версии нет — на языке интерфейса (это сообщение о системе, а не разбор выступления)
REASON_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "disabled": "The better version is turned off on this server.",
        "no_speech": "We didn't hear any speech in the recording.",
        "too_short": "The recording is too short to recreate your voice: at least {sec} seconds of speech are needed.",
        "too_long": "The pitch is too long for the better version (more than {chars} characters).",
        "no_recording": "The recording of this round wasn't saved, so your voice can't be recreated.",
        "voice": "Your voice couldn't be recreated from this recording.",
        "failed": "Couldn't make the better version. Please try again later.",
    },
    "ru": {
        "disabled": "Лучшая версия на этом сервере выключена.",
        "no_speech": "В записи не слышно речи.",
        "too_short": "Запись слишком короткая, чтобы воссоздать твой голос: нужно хотя бы {sec} секунд речи.",
        "too_long": "Питч слишком длинный для лучшей версии (больше {chars} символов).",
        "no_recording": "Запись этого раунда не сохранилась — воссоздать голос не из чего.",
        "voice": "Не получилось воссоздать твой голос по этой записи.",
        "failed": "Не получилось сделать лучшую версию. Попробуй позже.",
    },
    "ro": {
        "disabled": "Versiunea mai bună este dezactivată pe acest server.",
        "no_speech": "Nu se aude nimic în înregistrare.",
        "too_short": "Înregistrarea e prea scurtă pentru a-ți recrea vocea: e nevoie de cel puțin {sec} secunde de vorbire.",
        "too_long": "Pitch-ul e prea lung pentru versiunea mai bună (peste {chars} de caractere).",
        "no_recording": "Înregistrarea acestei runde nu s-a păstrat, așa că vocea nu poate fi recreată.",
        "voice": "Vocea ta nu a putut fi recreată din această înregistrare.",
        "failed": "Nu am reușit să facem versiunea mai bună. Încearcă din nou mai târziu.",
    },
}


class Unavailable(Exception):  # noqa: N818 — не ошибка, а честный ответ «нельзя»
    """Лучшую версию сделать нельзя (запись короткая, нет записи…); code — ключ REASON_TEXTS."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def reason_text(code: str | None, lang: str) -> str:
    s = get_settings()
    texts = pick(REASON_TEXTS, lang)
    text = texts.get(code or "failed", texts["failed"])
    return text.format(sec=round(s.better_version_min_speech_sec), chars=s.better_version_max_chars)


def audio_path(round_id: str) -> Path:
    return app_settings.STATIC_DIR / "better" / f"{round_id}.mp3"


def audio_url(round_id: str) -> str:
    return f"/static/better/{round_id}.mp3"


def tts_model() -> str:
    s = get_settings()
    if s.better_version_model:
        return s.better_version_model
    return s.tts_model if s.tts_model.startswith("eleven") else "eleven_flash_v2_5"


def speed_for(wpm: float | None) -> float:
    """Темп голоса по темпу игрока (слов в минуту без паразитов) — в естественных пределах."""
    if not wpm:
        return 1.0
    lo, hi = SPEED_RANGE
    return round(min(max(wpm / BASE_WPM, lo), hi), 2)


# --- текст ---


class BetterScript(BaseModel):
    text: str


def _hints(delivery: dict[str, Any]) -> str:
    """Паразиты и повторы, которые уже нашёл код, — подсказка модели, что убрать."""
    events = delivery.get("events") or []
    fillers = sorted({e.get("text", "") for e in events if e.get("type") == "filler"} - {""})
    repeats = [e.get("text", "") for e in events if e.get("type") == "repeat"]
    lines = []
    if fillers:
        lines.append("- filler words: " + ", ".join(fillers))
    if repeats:
        lines.append("- repetitions: " + "; ".join(repeats))
    return "\n".join(lines) or "- nothing"


async def write_script(transcript: str, delivery: dict[str, Any], lang: str) -> str:
    result = await llm.generate(
        "better_script",
        BetterScript,
        transcript=transcript,
        hints=_hints(delivery),
        speech_language=language_name(lang),
    )
    text = " ".join(result.text.split())
    lo, hi = LENGTH_RANGE
    if not lo * len(transcript) <= len(text) <= hi * len(transcript):
        raise ValueError(f"better: длина текста {len(text)} против расшифровки {len(transcript)} — не похоже на чистку")
    return text


# --- голос ---

_cloning: set[str] = set()  # имена клонов, с которыми сейчас работают задачи этого процесса


async def _delete_voice(voice_id: str) -> None:
    try:
        await elevenlabs_call("voice_delete", lambda: elevenlabs_client().voices.delete(voice_id))
    except Exception:
        logger.exception("better: не удалил клон голоса %s — его удалит следующая задача", voice_id)


async def _sweep_orphans() -> None:
    """Клоны прошлых задач, которые не удалились (сервер упал посреди работы), — удалить. Ошибка не мешает задаче."""
    try:
        found = await elevenlabs_call(
            "voice_search", lambda: elevenlabs_client().voices.search(search=VOICE_PREFIX, page_size=100)
        )
    except Exception:
        logger.warning("better: не получил список голосов для уборки клонов", exc_info=True)
        return
    now = time.time()
    for voice in found.voices or []:
        name = voice.name or ""
        if not name.startswith(VOICE_PREFIX) or voice.category != "cloned" or name in _cloning:
            continue
        if voice.created_at_unix and now - voice.created_at_unix < ORPHAN_AGE_SEC:
            continue
        logger.warning("better: удаляю забытый клон %s (%s)", name, voice.voice_id)
        await _delete_voice(voice.voice_id)


async def _synthesize(voice_id: str, text: str, lang: str, speed: float) -> bytes:
    model = tts_model()
    # язык навязывают только модели v2.5; multilingual_v2 определяет его по тексту сам
    extra = {"language_code": lang} if model.startswith(("eleven_flash_v2_5", "eleven_turbo_v2_5")) else {}
    stream = elevenlabs_client().text_to_speech.convert(
        voice_id,
        text=text,
        model_id=model,
        output_format=OUTPUT_FORMAT,
        voice_settings=VoiceSettings(stability=0.5, similarity_boost=0.85, style=0.0, use_speaker_boost=True, speed=speed),
        seed=0,
        **extra,
    )
    return b"".join([chunk async for chunk in stream])


async def speak_in_own_voice(round_id: str, sample: bytes, text: str, lang: str, speed: float) -> bytes:
    """Клонировать голос по образцу, прочитать им текст и удалить клон (в finally — и при ошибке, и при отмене)."""
    await _sweep_orphans()
    name = f"{VOICE_PREFIX}{round_id}"
    _cloning.add(name)
    voice_id: str | None = None
    try:
        created = await elevenlabs_call(
            "voice_clone",
            lambda: elevenlabs_client().voices.ivc.create(
                name=name,
                files=[("sample.mp3", sample, "audio/mpeg")],
                description=VOICE_DESCRIPTION,
                labels={},  # без labels SDK шлёт "null", и ElevenLabs отвечает 400 invalid_labels
            ),
        )
        voice_id = created.voice_id
        if created.requires_verification:
            raise Unavailable("voice")
        return await elevenlabs_call("tts_own_voice", lambda: _synthesize(created.voice_id, text, lang, speed))
    finally:
        try:
            if voice_id:
                await asyncio.shield(_delete_voice(voice_id))  # повторная отмена задачи не прервёт удаление
        finally:
            _cloning.discard(name)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)  # атомарно: плеер не получит половину файла


async def run_better_version(round_id: str, delivery: dict[str, Any]) -> dict[str, Any]:
    """{"status": "ready", "text", "audio"}; нельзя сделать — Unavailable(code); сбой сервиса — исключение."""
    s = get_settings()
    if not s.better_version_enabled:
        raise Unavailable("disabled")
    transcript = " ".join((delivery.get("transcript") or "").split())
    words = timed_words(delivery)
    if not transcript or not words:
        raise Unavailable("no_speech")
    if speech_seconds(words) < s.better_version_min_speech_sec:
        raise Unavailable("too_short")
    if len(transcript) > s.better_version_max_chars:
        raise Unavailable("too_long")
    recording = recording_path(round_id)
    if recording is None:
        raise Unavailable("no_recording")

    lang = normalize_lang(delivery.get("speech_lang")) or default_lang()
    script = await write_script(transcript, delivery, lang)
    if len(script) > s.better_version_max_chars:
        raise Unavailable("too_long")
    sample = await to_voice_sample(await asyncio.to_thread(recording.read_bytes))
    speed = speed_for((delivery.get("metrics") or {}).get("wpm"))
    # та же запись и тот же текст (повтор после сбоя, удалённый файл) — mp3 из кэша, без клонирования и кредитов
    key = cache.make_key(tts_model(), OUTPUT_FORMAT, lang, str(speed), script, sample)
    mp3 = await cache.get("better", key, "mp3")
    if mp3 is None:
        mp3 = await speak_in_own_voice(round_id, sample, script, lang, speed)
        await cache.put("better", key, "mp3", mp3)
    await asyncio.to_thread(_write, audio_path(round_id), mp3)
    logger.info("better round=%s: %d символов, speed=%.2f, %s", round_id, len(script), speed, tts_model())
    return {"status": "ready", "text": script, "audio": f"better/{round_id}.mp3", "speed": speed, "lang": lang}
