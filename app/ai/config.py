"""Настройки AI-движка из окружения / .env.

Провайдеры переключаются без изменения кода:
- STT_PROVIDER — распознавание записей (delivery, ответы жюри): elevenlabs (Scribe) | openai (Whisper);
- LIVE_STT_PROVIDER — живой поток для реакций зала: elevenlabs (Scribe Realtime) | deepgram;
- TTS_PROVIDER — голоса жюри: elevenlabs | openai.
При переключении провайдера поменяйте и модель (STT_MODEL / LIVE_STT_MODEL / TTS_MODEL), см. .env.example.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ai_mock: bool = False

    # ключи
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""  # голос по умолчанию для всех членов жюри
    elevenlabs_voice_id_strict: str = ""  # необязательно: свой голос для каждого члена жюри
    elevenlabs_voice_id_kind: str = ""
    elevenlabs_voice_id_skeptic: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    deepgram_api_key: str = ""

    # LLM
    gemini_model: str = "gemini-3.5-flash-lite"
    # при перегрузке или исчерпанной квоте основной — по очереди, через запятую; пусто — без запасных
    gemini_fallback_models: str = "gemini-3.1-flash-lite,gemini-flash-lite-latest,gemini-3.5-flash,gemini-3.8-flash"

    @property
    def gemini_models(self) -> list[str]:
        """Основная модель и запасные без повторов."""
        names = [self.gemini_model, *self.gemini_fallback_models.split(",")]
        return [m for m in dict.fromkeys(n.strip() for n in names) if m]

    # распознавание записей
    stt_provider: Literal["elevenlabs", "openai"] = "elevenlabs"
    stt_model: str = "scribe_v2"  # openai: whisper-1
    stt_language: str = "en"  # язык выступлений: распознавание, живой зал, голоса жюри

    # живой поток
    live_stt_provider: Literal["elevenlabs", "deepgram"] = "elevenlabs"
    live_stt_model: str = "scribe_v2_realtime"
    elevenlabs_realtime_url: str = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
    deepgram_url: str = "wss://api.deepgram.com/v1/listen"
    deepgram_model: str = "nova-3"
    # одновременные запросы к ElevenLabs (тариф — 3 на аккаунт): живая сцена держит слот весь питч,
    # короткие запросы (распознавание записи и ответов, озвучка) идут через свои слоты по очереди (app/ai/limits.py)
    elevenlabs_live_slots: int = Field(default=2, ge=0)
    elevenlabs_short_slots: int = Field(default=1, ge=1)

    # озвучка
    tts_provider: Literal["elevenlabs", "openai"] = "elevenlabs"
    tts_model: str = "eleven_flash_v2_5"  # openai: gpt-4o-mini-tts

    # Azure Pronunciation Assessment — оценка английского произношения; без ключа просто выключена
    azure_speech_key: str = ""
    azure_speech_region: str = "eastus"
    azure_speech_locale: str = "en-US"  # интонацию (prosody) Azure оценивает только для en-US
    azure_max_parallel: int = 8  # запись режется по паузам на куски, они оцениваются параллельно
    azure_timeout_sec: float = 20.0

    static_dir: str = "static"
    ai_cache: bool = True
    ai_cache_dir: str = ".ai_cache"
    max_audio_mb: int = 25

    def problems(self) -> list[str]:
        """Чего не хватает для выбранных провайдеров (пусто — всё настроено)."""
        need: dict[str, str] = {"GEMINI_API_KEY": self.gemini_api_key}
        if "elevenlabs" in {self.stt_provider, self.live_stt_provider, self.tts_provider}:
            need["ELEVENLABS_API_KEY"] = self.elevenlabs_api_key
        if self.tts_provider == "elevenlabs":
            need["ELEVENLABS_VOICE_ID"] = self.elevenlabs_voice_id
        if "openai" in {self.stt_provider, self.tts_provider}:
            need["OPENAI_API_KEY"] = self.openai_api_key
        if self.live_stt_provider == "deepgram":
            need["DEEPGRAM_API_KEY"] = self.deepgram_api_key
        problems = [f"{name} не задан" for name, value in need.items() if not value]

        expected_models = [
            ("STT", self.stt_provider, self.stt_model, {"elevenlabs": "scribe", "openai": "whisper"}),
            ("TTS", self.tts_provider, self.tts_model, {"elevenlabs": "eleven", "openai": "gpt"}),
        ]
        for kind, provider, model, prefixes in expected_models:
            if not model.startswith(prefixes[provider]):
                problems.append(f"{kind}_PROVIDER={provider}, но {kind}_MODEL={model} — модель другого провайдера")
        return problems


@lru_cache
def get_settings() -> AiSettings:
    return AiSettings()


def validate_ai_settings() -> None:
    """Вызывается при старте приложения: без ключей сервер не поднимается. При AI_MOCK=1 проверка пропускается."""
    settings = get_settings()
    if settings.ai_mock:
        return
    if problems := settings.problems():
        raise RuntimeError(
            "AI-движок не настроен:\n  - "
            + "\n  - ".join(problems)
            + "\nЗаполните backend/.env по образцу .env.example или запустите с AI_MOCK=1."
        )
