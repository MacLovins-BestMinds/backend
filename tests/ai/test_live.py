from app.ai.live import CHECK_EVERY_SEC, PCM_BYTES_PER_SEC, LiveAnalyzer, LiveCheck, content_score
from app.ai.stt import Word


def test_fillers_burst_and_pause_mid_phrase() -> None:
    a = LiveAnalyzer()
    # «вот» — двусмысленное слово, в живом потоке сразу не отмечается (его судит проверка содержания по смыслу)
    words = [Word("Ну", 0.0, 0.2), Word("вот,", 0.5, 0.7), Word("короче,", 0.8, 1.0), Word("типа", 1.0, 1.2), Word("продукт", 1.5, 2.0)]
    events = a.on_words(words, is_final=True)
    assert [(e.word, e.burst) for e in events] == [("ну", False), ("короче", False), ("типа", True)]

    a.on_audio(2 * PCM_BYTES_PER_SEC)  # до конца речи
    assert a.on_audio(int(3.5 * PCM_BYTES_PER_SEC))[0].type == "long_pause"
    assert a.on_audio(PCM_BYTES_PER_SEC) == []  # одна пауза — одно событие


def test_no_pause_after_sentence_end_and_offset_on_reconnect() -> None:
    a = LiveAnalyzer()
    a.on_words([Word("Спасибо.", 0.0, 0.8)], is_final=True)
    assert a.on_audio(5 * PCM_BYTES_PER_SEC) == []
    a.begin_stream()
    assert a.offset == 5.0


def test_partial_fillers_are_instant_and_not_duplicated_on_commit() -> None:
    a = LiveAnalyzer()
    a.on_audio(PCM_BYTES_PER_SEC)
    assert [e.word for e in a.on_partial_text("ну короче")] == ["ну", "короче"]
    assert a.on_partial_text("ну короче") == []  # тот же текст — без повторов
    committed = [Word("Ну", 0.1, 0.2), Word("короче", 0.3, 0.4), Word("типа", 0.5, 0.6), Word("идея.", 0.7, 1.0)]
    assert [e.word for e in a.on_words(committed, is_final=True)] == ["типа"]


def test_content_check_waits_for_enough_new_words_and_time() -> None:
    a = LiveAnalyzer()
    a.on_audio(int(PCM_BYTES_PER_SEC * (CHECK_EVERY_SEC + 1)))
    a.on_partial_text("my favourite food is pizza")
    assert not a.check_due()  # слов пока мало
    a.on_partial_text("my favourite food is pizza because my grandmother made it every single Sunday")
    assert a.check_due()  # фраза ещё не закончена, но слов уже хватает
    a.checked_words, a.last_check_t = len(a.spoken()), a.audio_t
    assert not a.check_due()


def test_ambiguous_fillers_are_left_to_the_content_check() -> None:
    a = LiveAnalyzer()
    events = a.on_partial_text("So, I like pizza, um, a lot")
    assert [e.word for e in events] == ["um"]


def test_off_topic_or_empty_talk_scores_low() -> None:
    assert content_score(LiveCheck(relevance=95, substance=90)) >= 90
    assert content_score(LiveCheck(relevance=95, substance=10)) < 45  # по теме, но вода
    assert content_score(LiveCheck(relevance=10, substance=90)) < 45  # содержательно, но не о том


def test_swearing_is_reported_at_once_and_only_once() -> None:
    a = LiveAnalyzer()
    events = a.on_partial_text("this is fucking great")
    assert [(e.type, e.word) for e in events] == [("profanity", "fucking")]
    assert a.on_partial_text("this is fucking great pizza") == []
    final = a.on_words([Word(w, i * 0.4, i * 0.4 + 0.3) for i, w in enumerate("this is fucking great pizza.".split())], is_final=True)
    assert [e for e in final if e.type == "profanity"] == []
