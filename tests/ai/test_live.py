from app.ai.live import PCM_BYTES_PER_SEC, LiveAnalyzer
from app.ai.stt import Word


def test_fillers_burst_and_pause_mid_phrase() -> None:
    a = LiveAnalyzer()
    words = [Word("Ну", 0.0, 0.2), Word("вот,", 0.5, 0.7), Word("типа", 1.0, 1.2), Word("продукт", 1.5, 2.0)]
    events = a.on_words(words, is_final=True)
    assert [(e.type, e.burst) for e in events] == [("filler", False), ("filler", False), ("filler", True)]

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
    assert [e.word for e in a.on_partial_text("ну вот")] == ["ну", "вот"]
    assert a.on_partial_text("ну вот") == []  # тот же текст — без повторов
    committed = [Word("Ну", 0.1, 0.2), Word("вот", 0.3, 0.4), Word("типа", 0.5, 0.6), Word("идея.", 0.7, 1.0)]
    assert [e.word for e in a.on_words(committed, is_final=True)] == ["типа"]
