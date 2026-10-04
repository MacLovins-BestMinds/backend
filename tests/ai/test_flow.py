"""Ход мысли (app/ai/flow.py): фразы со временем, проверка моментов модели, короткие питчи. Без внешних API."""

import asyncio

import pytest

from app.ai import flow, llm
from app.ai.flow import FlowDraft, MomentDraft, assemble, build_segments, run_flow, timed_words
from app.ai.pitch import Pitch
from app.ai.schemas import Audience

RU = (
    "Ну, представьте: бабушка забыла таблетку. Это случается каждый день с миллионами людей. "
    "Мы сделали умную таблетницу, она пищит и пишет семье. Вот, ну, как бы, это, короче, вот так. "
    "Пилот в трёх аптеках показал, что пропусков стало вдвое меньше. Давайте поговорим после питча."
)


def _delivery(text: str, step: float = 0.5, lang: str = "ru") -> dict:
    """Сохранённый delivery: слова со временем и местом в тексте, как их пишет run_delivery."""
    words, cursor = [], 0
    for k, token in enumerate(text.split()):
        at = text.index(token, cursor)
        cursor = at + len(token)
        words.append({"start": at, "end": cursor, "t": round(k * step, 2), "t_end": round(k * step + 0.4, 2)})
    metrics = {"duration_sec": len(words) * step, "words": len(words), "wpm": 120, "fillers": 3,
               "fillers_per_min": 2.0, "long_pauses": 0}  # fmt: skip
    return {"transcript": text, "words": words, "metrics": metrics, "events": [], "speech_lang": lang}


def _parts(delivery: dict):
    words = timed_words(delivery)
    return words, build_segments(words, delivery["transcript"])


def test_segments_are_sentences_with_times() -> None:
    words, segments = _parts(_delivery(RU))
    assert [s.text for s in segments][:2] == ["Ну, представьте: бабушка забыла таблетку.", "Это случается каждый день с миллионами людей."]
    assert segments[0].start == 0.0 and segments[0].end == pytest.approx(4 * 0.5 + 0.4)
    assert segments[1].start == 5 * 0.5
    assert segments[-1].text == "Давайте поговорим после питча." and segments[-1].last == len(words) - 1


def test_long_sentence_is_cut_at_a_comma_and_unpunctuated_text_at_pauses() -> None:
    long = " ".join(f"слово{i}" for i in range(20)) + ", и ещё " + " ".join(f"хвост{i}" for i in range(5)) + "."
    _, segments = _parts(_delivery(long))
    assert len(segments) == 2 and segments[0].text.endswith(",")
    raw = _delivery("one two three four five six seven eight", lang="en")
    raw["words"][4]["t"] = raw["words"][3]["t_end"] + 1.5  # пауза перед «five»
    _, segments = _parts(raw)
    assert [s.text for s in segments] == ["one two three four", "five six seven eight"]


def test_old_delivery_without_words_gets_proportional_times() -> None:
    old = {"transcript": "Hello there. Bye now.", "metrics": {"duration_sec": 21}}
    words = timed_words(old)
    assert [w.text for w in words] == ["Hello", "there.", "Bye", "now."]
    assert words[0].start == 0 and words[-1].end == pytest.approx(21)


def _draft(*moments: tuple, summary: str = "Итог.") -> FlowDraft:
    return FlowDraft(
        summary=summary,
        moments=[MomentDraft(kind=k, first=a, last=b, quote=q, comment=c) for k, a, b, q, c in moments],
    )


def test_moments_get_times_and_quotes_from_the_transcript() -> None:
    d = _delivery(RU)
    words, segments = _parts(d)
    draft = _draft(
        ("hook", 0, 0, "представьте: бабушка забыла таблетку", "Картинка сразу."),
        ("strong", 4, 4, "пропусков стало вдвое меньше", "Результат в цифрах."),
        ("rambling", 3, 3, "совсем не то", "Вода."),  # такой цитаты нет — цитатой становится сама фраза
        ("strong_close", 5, 5, "Давайте поговорим", "Понятный призыв."),
    )
    moments = assemble(draft, segments, words, RU)
    assert [m.kind for m in moments] == ["hook", "rambling", "strong", "strong_close"]  # по времени
    assert [m.tone for m in moments] == ["good", "bad", "good", "good"]
    hook = moments[0]
    assert hook.quote == "представьте: бабушка забыла таблетку." and (hook.t, hook.end) == (segments[0].start, segments[0].end)
    assert moments[1].quote == segments[3].text  # вместо выдуманной цитаты — фраза из расшифровки
    assert all(m.quote in RU for m in moments)


def test_invalid_overlapping_and_misplaced_moments_are_fixed_or_dropped() -> None:
    d = _delivery(RU)
    words, segments = _parts(d)
    draft = _draft(
        ("weak", 7, 9, "нет такой фразы", "Номера вне расшифровки."),
        ("strong", 2, 2, "умную таблетницу", "  "),  # пустой комментарий
        ("hook", 4, 4, "Пилот в трёх аптеках", "Не начало — значит, просто сильное место."),
        ("weak_close", 1, 1, "Это случается каждый день", "Не конец — значит, просто слабое место."),
        ("off_topic", 1, 2, "миллионами людей", "Пересекается с предыдущим."),
        # фраза №3, но цитата — из соседней №2: верим цитате (паразиты при сравнении пропускаются)
        ("strong", 3, 3, "она, ну, пищит и пишет семье", "Понятно, что делает."),
    )
    moments = assemble(draft, segments, words, RU)
    assert [(m.kind, m.tone) for m in moments] == [("weak", "bad"), ("strong", "good"), ("strong", "good")]
    assert moments[0].t == segments[1].start
    assert moments[1].quote == "она пищит и пишет семье." and moments[1].t == segments[2].start


def test_only_one_hook_and_one_close() -> None:
    d = _delivery(RU)
    words, segments = _parts(d)
    draft = _draft(
        ("hook", 0, 0, "бабушка забыла таблетку", "Раз."),
        ("hook", 1, 1, "Это случается каждый день", "Два."),
        ("weak_close", 4, 4, "Пилот в трёх аптеках", "Три."),
        ("strong_close", 5, 5, "Давайте поговорим после питча", "Четыре."),
    )
    kinds = [m.kind for m in assemble(draft, segments, words, RU)]
    assert kinds == ["hook", "strong", "weak", "strong_close"]


def _no_llm(monkeypatch) -> None:
    async def boom(*_a, **_k):
        raise AssertionError("LLM не должен вызываться")

    monkeypatch.setattr(llm, "generate", boom)


def test_empty_and_very_short_pitches_get_an_honest_summary_without_llm(monkeypatch) -> None:
    _no_llm(monkeypatch)
    empty = asyncio.run(run_flow("r", {"transcript": "", "words": [], "speech_lang": "ro"}))
    assert empty == {"status": "ready", "summary": flow.SHORT_TEXTS["ro"]["empty"], "moments": []}
    short = asyncio.run(run_flow("r", _delivery("Ну, это наш продукт. Он хороший.")))
    assert short["summary"] == flow.SHORT_TEXTS["ru"]["short"] and short["moments"] == []


def test_run_flow_sends_numbered_segments_in_the_speech_language(monkeypatch) -> None:
    calls: dict = {}

    async def fake_generate(name, schema, **variables):
        calls.update(variables, name=name)
        return _draft(("hook", 0, 0, "бабушка забыла таблетку", "Сразу видно проблему."), summary="  Линия есть.  ")

    monkeypatch.setattr(llm, "generate", fake_generate)
    monkeypatch.setattr(flow, "resolve_pitch", lambda _rid: Pitch(title="Таблетница", brief="Убеди зал", audience=Audience.PUBLIC))
    result = asyncio.run(run_flow("r", _delivery(RU)))
    assert calls["name"] == "flow_review" and calls["speech_language"] == "Russian"
    assert calls["segments"].splitlines()[0] == "[0] 0.0–2.4 s: Ну, представьте: бабушка забыла таблетку."
    assert calls["last_index"] == 5 and "pace: 120 words/min" in calls["metrics"]
    assert result["status"] == "ready" and result["summary"] == "Линия есть."
    assert result["moments"] == [
        {"t": 0.0, "end": 2.4, "kind": "hook", "tone": "good", "quote": "бабушка забыла таблетку.", "comment": "Сразу видно проблему."}
    ]
