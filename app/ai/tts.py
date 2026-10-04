"""Озвучка реплик жюри в mp3: ElevenLabs (по умолчанию) или OpenAI (TTS_PROVIDER).

Приложение проигрывает mp3 по audio_url, поэтому оба провайдера отдают mp3.
"""

from dataclasses import dataclass

from elevenlabs import VoiceSettings

from app.ai import cache
from app.ai.clients import elevenlabs_client, openai_client
from app.ai.config import get_settings
from app.ai.limits import elevenlabs_call

ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"


@dataclass(frozen=True, slots=True)
class Voice:
    """Голос члена жюри для обоих провайдеров."""

    openai_voice: str
    openai_instructions: str  # только для gpt-4o-mini-tts: у ElevenLabs тон задают голос и voice_settings
    elevenlabs_voice_id: str
    stability: float  # ниже — живее и эмоциональнее, выше — ровнее и строже
    style: float  # выразительность 0..1


async def _elevenlabs(text: str, voice: Voice) -> bytes:
    s = get_settings()
    stream = elevenlabs_client().text_to_speech.stream(
        voice.elevenlabs_voice_id,
        text=text,
        model_id=s.tts_model,
        language_code=s.stt_language,
        output_format=ELEVENLABS_OUTPUT_FORMAT,
        voice_settings=VoiceSettings(stability=voice.stability, similarity_boost=0.75, style=voice.style),
        seed=0,
    )
    return b"".join([chunk async for chunk in stream])


async def _openai(text: str, voice: Voice) -> bytes:
    response = await openai_client().audio.speech.create(
        model=get_settings().tts_model,
        voice=voice.openai_voice,
        input=text,
        instructions=voice.openai_instructions,
        response_format="mp3",
    )
    return response.content


async def synthesize(text: str, voice: Voice) -> bytes:
    s = get_settings()
    key = cache.make_key(s.tts_provider, s.tts_model, repr(voice), text)
    if cached := await cache.get("tts", key, "mp3"):
        return cached

    if s.tts_provider == "elevenlabs":
        mp3 = await elevenlabs_call("tts", lambda: _elevenlabs(text, voice))
    else:
        mp3 = await _openai(text, voice)
    await cache.put("tts", key, "mp3", mp3)
    return mp3
