from app.ai.jury import fallback_questions
from app.ai.pitch import Pitch
from app.ai.schemas import Audience


def test_fallback_questions_keep_case_quirk_for_skeptic() -> None:
    pitch = Pitch(title="t", brief="b", audience=Audience.BUSINESS, quirk="А если бабушка без смартфона?")
    questions = fallback_questions(pitch).questions
    assert questions[0].juror == "skeptic" and questions[0].text == pitch.quirk
    assert [q.juror for q in questions].count("skeptic") == 1
