from app.ai.delivery_metrics import analyze, find_profanity, fillers_score, find_fillers, gaze_on_ratio, pace_score, timing_score
from app.ai.schemas import GazePoint
from app.ai.stt import Transcript, Word


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


def test_ambiguous_words_are_fillers_only_when_the_meaning_says_so() -> None:
    words = _spoken("I like pizza and it was like really good um yes").words
    # «like» №1 — глагол, «like» №6 — паразит: так решил LLM
    assert [w for _, w in find_fillers(words, {1: False, 6: True})] == ["like", "um"]
    # в живом потоке двусмысленные слова сразу не отмечаются
    assert [w for _, w in find_fillers(words, {})] == ["um"]


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


def test_russian_speech_stays_in_cyrillic_and_its_fillers_are_found() -> None:
    result = analyze(_spoken("So this is, e-e-e, the idea"), gaze=[], min_sec=60, max_sec=180)
    assert [e.text for e in result.events if e.type == "filler"] == ["«e-e-e»"]
    russian = analyze(_spoken("Ну, короче, это типа важно, э-э"), gaze=[], min_sec=60, max_sec=180, lang="ru")
    assert [e.text for e in russian.events if e.type == "filler"] == ["«ну»", "«короче»", "«типа»", "«э-э»"]


def test_swearing_is_marked_and_costs_delivery_points() -> None:
    text = "This fucking pizza is suka good, blyat, and the Shiitake are fine"
    polite = "This tasty pizza is very good, indeed, and the Shiitake are fine"  # те же слова по счёту и темпу
    rude = analyze(_spoken(text), gaze=[], min_sec=60, max_sec=180)
    clean = analyze(_spoken(polite), gaze=[], min_sec=60, max_sec=180)
    marked = [text[e.start : e.end].strip(",") for e in rude.events if e.type == "profanity"]
    assert marked == ["fucking", "suka", "blyat"]  # «Shiitake» и прочие обычные слова не задеты
    assert rude.metrics.profanity == 3 and clean.metrics.profanity == 0
    assert rude.score.total == clean.score.total - 24  # по 8 баллов за слово


def test_innocent_words_are_not_swearing() -> None:
    words = _spoken("The government made a sukiyaki assessment in Scunthorpe, f*** that").words
    assert [w for _, w in find_profanity(words)] == ["f***"]


def test_chosen_pace_decides_what_counts_as_too_slow() -> None:
    assert pace_score(90, "slow") == 100  # спокойный темп: за медленную речь не ругаем
    assert pace_score(90, "normal") < 30 and pace_score(90, "fast") == 0
    assert pace_score(190, "fast") == 100 and pace_score(200, "slow") == 0
    # 60 слов за 40 секунд речи — 90 слов в минуту
    words = [Word(f"word{i}", i * 0.66, i * 0.66 + 0.4) for i in range(70)]
    slow = analyze(Transcript(" ".join(w.text for w in words), words, duration=60), [], 60, 180, pace="slow")
    fast = analyze(Transcript(" ".join(w.text for w in words), words, duration=60), [], 60, 180, pace="fast")
    assert [e for e in slow.events if e.type == "pace"] == []
    assert all("too slow" in e.text for e in fast.events if e.type == "pace") and any(e.type == "pace" for e in fast.events)


def test_parallel_constructions_are_not_repeats_but_false_starts_are() -> None:
    # повтор в начале и в конце соседних частей фразы — приём, а не ошибка; запинка «если я, если я» — ошибка
    text = "Да, если я, если я ругаюсь, им это не нравится, если я молчу, им это не нравится."
    result = analyze(_spoken(text), gaze=[], min_sec=60, max_sec=180, lang="ru")
    assert [text[e.start : e.end] for e in result.events if e.type == "repeat"] == ["если я"]
    anaphora = "We help farmers. We help them sell. We help them grow."
    assert [e for e in analyze(_spoken(anaphora), gaze=[], min_sec=60, max_sec=180).events if e.type == "repeat"] == []
    # оборванное начало и повтор с начала — фальстарт, даже если между ними пара слов
    restart = "Если он сейчас-- вот какая-то тема, если он сейчас начнёт говорить, все проснутся."
    result = analyze(_spoken(restart), gaze=[], min_sec=60, max_sec=180, lang="ru")
    assert [restart[e.start : e.end] for e in result.events if e.type == "repeat"] == ["если он сейчас"]
