from types import SimpleNamespace

from app.ai.delivery import char_times
from app.ai.stt import Transcript, Word, _char_starts


def test_char_starts_from_scribe_word():
    word = SimpleNamespace(text="hello", start=1.0, characters=[SimpleNamespace(start=1.0 + i * 0.05) for i in range(5)])
    assert _char_starts(word) == (1.0, 1.05, 1.1, 1.15, 1.2)


def test_char_starts_mismatch_or_missing():
    assert _char_starts(SimpleNamespace(text="hi", start=0.0, characters=None)) is None
    assert _char_starts(SimpleNamespace(text="hi", start=0.0, characters=[SimpleNamespace(start=0.0)])) is None


def test_char_times_aligns_to_stripped_token():
    word = Word(" ну", 2.0, 2.3, (1.95, 2.0, 2.15))
    assert char_times(word, 2) == [2.0, 2.15]
    assert char_times(Word("ну", 2.0, 2.3), 2) is None
    assert char_times(Word("ну", 2.0, 2.3, (2.0,)), 2) is None


def test_transcript_json_roundtrip_with_and_without_chars():
    t = Transcript("a bc", [Word("a", 0.0, 0.1, (0.0,)), Word("bc", 0.2, 0.4)], 0.5, "en")
    back = Transcript.from_json(t.to_json())
    assert back.words[0].chars == (0.0,) and back.words[1].chars is None
    # старый кэш без поля chars
    old = b'{"text":"a","words":[{"text":"a","start":0.0,"end":0.1}],"duration":0.2}'
    assert Transcript.from_json(old).words[0].chars is None
