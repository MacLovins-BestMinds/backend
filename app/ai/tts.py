"""Озвучка реплик жюри (OpenAI TTS) в mp3."""

from app.ai.clients import openai_client
from app.ai.config import get_settings


async def synthesize(text: str, voice: str, instructions: str) -> bytes:
    response = await openai_client().audio.speech.create(
        model=get_settings().tts_model,
        voice=voice,
        input=text,
        instructions=instructions,
        response_format="mp3",
    )
    return response.content
