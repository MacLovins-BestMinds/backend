"""Озвучка реплик жюри (OpenAI TTS) в mp3."""

from app.ai import cache
from app.ai.clients import openai_client
from app.ai.config import get_settings


async def synthesize(text: str, voice: str, instructions: str) -> bytes:
    model = get_settings().tts_model
    key = cache.make_key(model, voice, instructions, text)
    if cached := await cache.get("tts", key, "mp3"):
        return cached

    response = await openai_client().audio.speech.create(
        model=model,
        voice=voice,
        input=text,
        instructions=instructions,
        response_format="mp3",
    )
    await cache.put("tts", key, "mp3", response.content)
    return response.content
