from app.ai.delivery_metrics import analyze, fillers_score, find_fillers, gaze_on_ratio, pace_score, timing_score
from app.ai.schemas import GazePoint
from app.ai.stt import Transcript, Word, to_latin


def _words(*texts: str, step: float = 0.4) -> list[Word]:
    return [Word(t, i * step, i * step + 0.3) for i, t in enumerate(texts)]


def test_scores_follow_spec_points() -> None:
    assert (fillers_score(1), fillers_score(5), fillers_score(12)) == (100, 40, 0)
    assert pace_score(140) == 100 and pace_score(80) == 0
    assert timing_score(120, 60, 180) == 100 and timing_score(30, 60, 180) == 50


def test_english_fillers_by_context() -> None:
    found = find_fillers(
        _words("Um,", "so", "it's,", "like,", "simple.", "So,", "you", "know,", "it's", "kind", "of", "fine.")
    )
    assert [w for _, w in found] == ["um", "so", "like", "so", "you know", "kind of"]
    # те же слова в обычном смысле — не паразиты
    traps = _words(
        "Do", "you", "know", "why?", "What", "I", "mean", "is", "this.", "I", "like", "it.", "A", "kind", "of", "tool."
    )
    assert find_fillers(traps) == []


def test_fillers_include_bigrams_and_hesitations() -> None:
    found = find_fillers(_words("Ну,", "это", "как", "бы", "ээээ", "продукт", "Вот."))
    assert [w for _, w in found] == ["ну", "как бы", "ээээ", "вот"]


def test_gaze_ratio_uses_change_points() -> None:
    gaze = [GazePoint(t=0, on=True), GazePoint(t=6, on=False), GazePoint(t=8, on=True)]
    assert gaze_on_ratio(gaze, 10) == 0.8


def test_looking_away_is_marked_at_the_word_spoken_then() -> None:
    text = "one two three four five six seven eight nine ten eleven twelve"
    gaze = [GazePoint(t=0, on=True), GazePoint(t=2.0, on=False), GazePoint(t=5.5, on=True)]
    result = analyze(_spoken(text), gaze=gaze, min_sec=60, max_sec=180)
    away = [e for e in result.events if e.type == "gaze_off"]
    assert [(e.t, e.text) for e in away] == [(2.0, "Looking away for 4 s")]
    assert text[away[0].start :].startswith("five")


def test_analyze_detects_long_pause_and_total() -> None:
    words = _words("Представьте", "бабушку") + [Word("утром", 5.0, 5.3)]
    result = analyze(Transcript("…", words, duration=90), gaze=[], min_sec=60, max_sec=180)
    assert result.metrics.long_pauses == 1
    assert [e.type for e in result.events] == ["long_pause"]
    assert 0 <= result.score.total <= 100


def test_pause_after_a_finished_phrase_is_not_marked() -> None:
    words = [Word("Итак.", 0.0, 0.5), Word("Дальше", 2.0, 2.4)]
    result = analyze(Transcript("Итак. Дальше", words, duration=70), gaze=[], min_sec=60, max_sec=180)
    assert result.events == []


def _spoken(text: str) -> Transcript:
    words = [Word(w, i * 0.5, i * 0.5 + 0.4) for i, w in enumerate(text.split())]
    return Transcript(text, words, duration=90)


def test_repeated_words_and_phrases_are_marked_in_the_text() -> None:
    text = "People leave Telegram. Telegram is big, and they will leave again because they will leave anyway."
    result = analyze(_spoken(text), gaze=[], min_sec=60, max_sec=180)
    repeats = [e for e in result.events if e.type == "repeat"]
    assert [text[e.start : e.end] for e in repeats] == ["Telegram", "they will leave"]


def test_repeated_sentence_is_marked_from_its_first_word() -> None:
    text = "The main reason is money. They need money. They need money to pay for the servers."
    result = analyze(_spoken(text), gaze=[], min_sec=60, max_sec=180)
    assert [text[e.start : e.end] for e in result.events if e.type == "repeat"] == ["They need money"]


def test_deliberate_and_distant_repeats_are_not_marked() -> None:
    far = " ".join(f"word{i}" for i in range(30))
    text = f"It is very very good and we know the price. {far} we know the price."
    result = analyze(_spoken(text), gaze=[], min_sec=60, max_sec=180)
    assert [e for e in result.events if e.type == "repeat"] == []


def test_english_mode_spells_russian_words_in_latin() -> None:
    assert to_latin("Ну, это Telegram, э-э-э, хорошо") == "Nu, eto Telegram, e-e-e, khorosho"
    result = analyze(_spoken("So this is, e-e-e, the idea"), gaze=[], min_sec=60, max_sec=180)
    assert [e.text for e in result.events if e.type == "filler"] == ["«e-e-e»"]
    russian = analyze(_spoken("Nu, koroche, eto tipa vazhno"), gaze=[], min_sec=60, max_sec=180)
    assert [e.text for e in russian.events if e.type == "filler"] == ["«nu»", "«koroche»", "«tipa»"]
