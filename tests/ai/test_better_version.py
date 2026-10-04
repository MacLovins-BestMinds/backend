"""«Лучшая версия» своим голосом (app/ai/better.py): чистка текста, клон голоса и его удаление. Без внешних API."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.ai import better, llm
from app.ai.better import Unavailable, run_better_version, speed_for
from app.ai.config import get_settings
from app.core.config import settings as app_settings

TEXT = (
    "Ну, мы сделали, мы сделали умную таблетницу. Она пищит, когда пора пить лекарство, и пишет семье, "
    "если бабушка забыла. Пилот в трёх аптеках показал, что пропусков стало вдвое меньше."
)
CLEAN = (
    "Мы сделали умную таблетницу. Она пищит, когда пора пить лекарство, и пишет семье, "
    "если бабушка забыла. Пилот в трёх аптеках показал, что пропусков стало вдвое меньше."
)


def _delivery(text: str = TEXT, step: float = 0.5, wpm: int = 168) -> dict:
    words, cursor = [], 0
    for k, token in enumerate(text.split()):
        at = text.index(token, cursor)
        cursor = at + len(token)
        words.append({"start": at, "end": cursor, "t": k * step, "t_end": k * step + 0.4})
    events = [{"type": "filler", "t": 0.0, "text": "«ну»"}, {"type": "repeat", "t": 1.0, "text": "Повтор: «мы сделали»"}]
    return {"transcript": text, "words": words, "metrics": {"wpm": wpm}, "events": events, "speech_lang": "ru"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Кэш и статика во временной папке; запись раунда rnd_b лежит на месте."""
    monkeypatch.setenv("AI_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(app_settings, "STATIC_DIR", tmp_path / "static")
    (tmp_path / "static" / "recordings").mkdir(parents=True)
    (tmp_path / "static" / "recordings" / "rnd_b.m4a").write_bytes(b"m4a")
    get_settings.cache_clear()

    async def sample(data: bytes, max_sec: int = 180) -> bytes:
        return b"sample:" + data

    monkeypatch.setattr(better, "to_voice_sample", sample)
    yield monkeypatch
    get_settings.cache_clear()


class FakeElevenLabs:
    """Клиент ElevenLabs: записывает вызовы; tts_error — озвучка падает."""

    def __init__(self, tts_error: Exception | None = None, voices: list | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.tts_error = tts_error
        self.existing = voices or []
        self.voices = SimpleNamespace(
            ivc=SimpleNamespace(create=self._create), delete=self._delete, search=self._search
        )
        self.text_to_speech = SimpleNamespace(convert=self._convert)

    async def _create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return SimpleNamespace(voice_id="v_clone", requires_verification=False)

    async def _delete(self, voice_id):
        self.calls.append(("delete", {"voice_id": voice_id}))

    async def _search(self, **kwargs):
        self.calls.append(("search", kwargs))
        return SimpleNamespace(voices=self.existing)

    async def _convert(self, voice_id, **kwargs):
        self.calls.append(("tts", {"voice_id": voice_id, **kwargs}))
        if self.tts_error:
            raise self.tts_error
        yield b"mp3-"
        yield b"bytes"

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _llm(monkeypatch, text: str = CLEAN) -> list[dict]:
    calls: list[dict] = []

    async def fake_generate(name, schema, **variables):
        calls.append({"name": name, **variables})
        return schema(text=text)

    monkeypatch.setattr(llm, "generate", fake_generate)
    return calls


def test_speed_follows_the_players_pace_within_natural_limits() -> None:
    assert speed_for(140) == 1.0 and speed_for(154) == 1.1
    assert speed_for(250) == 1.15 and speed_for(60) == 0.85 and speed_for(None) == 1.0


def test_clean_pitch_is_read_by_a_cloned_voice_that_is_deleted_afterwards(env) -> None:
    llm_calls = _llm(env)
    client = FakeElevenLabs()
    env.setattr(better, "elevenlabs_client", lambda: client)
    result = asyncio.run(run_better_version("rnd_b", _delivery()))

    assert result["status"] == "ready" and result["text"] == CLEAN and result["audio"] == "better/rnd_b.mp3"
    assert (app_settings.STATIC_DIR / "better" / "rnd_b.mp3").read_bytes() == b"mp3-bytes"
    assert llm_calls[0]["name"] == "better_script" and llm_calls[0]["speech_language"] == "Russian"
    assert "«ну»" in llm_calls[0]["hints"] and "мы сделали" in llm_calls[0]["hints"]

    assert client.names() == ["search", "create", "tts", "delete"]
    create = client.calls[1][1]
    assert create["name"] == "stager-rnd_b" and "own voice" in create["description"]
    assert create["files"] == [("sample.mp3", b"sample:m4a", "audio/mpeg")]
    assert isinstance(create["labels"], dict)  # без labels SDK шлёт "null" — ElevenLabs отвечает 400
    tts = client.calls[2][1]
    assert tts["voice_id"] == "v_clone" and tts["text"] == CLEAN and tts["language_code"] == "ru"
    assert tts["model_id"] == "eleven_flash_v2_5" and tts["voice_settings"].speed == 1.15  # 168 слов/мин → 1.2 → 1.15
    assert client.calls[3][1] == {"voice_id": "v_clone"}

    # повтор с той же записью и тем же текстом — mp3 из кэша, голос не клонируется второй раз
    (app_settings.STATIC_DIR / "better" / "rnd_b.mp3").unlink()
    asyncio.run(run_better_version("rnd_b", _delivery()))
    assert client.names().count("create") == 1 and (app_settings.STATIC_DIR / "better" / "rnd_b.mp3").exists()


def test_voice_is_deleted_even_when_synthesis_fails(env) -> None:
    _llm(env)
    client = FakeElevenLabs(tts_error=RuntimeError("tts down"))
    env.setattr(better, "elevenlabs_client", lambda: client)
    with pytest.raises(RuntimeError, match="tts down"):
        asyncio.run(run_better_version("rnd_b", _delivery()))
    assert client.names()[-1] == "delete" and not (app_settings.STATIC_DIR / "better" / "rnd_b.mp3").exists()
    assert better._cloning == set()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"transcript": "", "words": []}, "no_speech"),
        ({"words": "short"}, "too_short"),  # подменяется ниже: 6 слов по 0,5 с
        ({"no_recording": True}, "no_recording"),
        ({"max_chars": "150"}, "too_long"),
        ({"disabled": True}, "disabled"),
    ],
)
def test_unavailable_reasons(env, change, code) -> None:
    _llm(env)
    env.setattr(better, "elevenlabs_client", lambda: pytest.fail("ElevenLabs не должен вызываться"))
    delivery = _delivery()
    if change.get("words") == "short":
        delivery = _delivery("Ну, это наш продукт. Он хороший.")
    elif "transcript" in change:
        delivery.update(change)
    if change.get("no_recording"):
        (app_settings.STATIC_DIR / "recordings" / "rnd_b.m4a").unlink()
    if change.get("max_chars"):
        env.setenv("BETTER_VERSION_MAX_CHARS", change["max_chars"])
    if change.get("disabled"):
        env.setenv("BETTER_VERSION_ENABLED", "0")
    get_settings.cache_clear()
    with pytest.raises(Unavailable) as error:
        asyncio.run(run_better_version("rnd_b", delivery))
    assert error.value.code == code
    assert better.reason_text(code, "ru") == better.REASON_TEXTS["ru"][code].format(
        sec=10, chars=get_settings().better_version_max_chars
    )


def test_a_rewrite_that_is_not_a_cleanup_is_rejected(env) -> None:
    _llm(env, text="Коротко.")
    env.setattr(better, "elevenlabs_client", lambda: pytest.fail("ElevenLabs не должен вызываться"))
    with pytest.raises(ValueError, match="не похоже на чистку"):
        asyncio.run(run_better_version("rnd_b", _delivery()))


def test_forgotten_clones_of_earlier_jobs_are_swept(env) -> None:
    old, now = time.time() - 3600, time.time()
    voices = [
        SimpleNamespace(voice_id="v_old", name="stager-rnd_x", category="cloned", created_at_unix=old),
        SimpleNamespace(voice_id="v_fresh", name="stager-rnd_y", category="cloned", created_at_unix=now),
        SimpleNamespace(voice_id="v_jury", name="Marina", category="premade", created_at_unix=old),
        SimpleNamespace(voice_id="v_busy", name="stager-rnd_z", category="cloned", created_at_unix=old),
    ]
    client = FakeElevenLabs(voices=voices)
    env.setattr(better, "elevenlabs_client", lambda: client)
    better._cloning.add("stager-rnd_z")  # с ним сейчас работает другая задача
    try:
        asyncio.run(better._sweep_orphans())
    finally:
        better._cloning.discard("stager-rnd_z")
    assert [kw["voice_id"] for name, kw in client.calls if name == "delete"] == ["v_old"]
