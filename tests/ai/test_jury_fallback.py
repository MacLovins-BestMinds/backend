from app.ai.jury import fallback_questions
from app.ai.pitch import Pitch
from app.ai.schemas import Audience


def test_fallback_questions_are_english_and_cover_all_jurors() -> None:
    pitch = Pitch(title="t", brief="b", audience=Audience.BUSINESS, quirk="А если бабушка без смартфона?")
    questions = fallback_questions(pitch).questions
    assert sorted(q.juror for q in questions) == ["kind", "skeptic", "strict"]
    assert all(q.text.isascii() for q in questions)  # озвучиваются английским голосом
