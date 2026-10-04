from app.ai.jury import fallback_questions
from app.ai.pitch import Pitch
from app.ai.schemas import Audience


def test_fallback_questions_are_english_and_cover_all_jurors() -> None:
    pitch = Pitch(title="t", brief="b", audience=Audience.BUSINESS, quirk="А если бабушка без смартфона?")
    questions = fallback_questions(pitch).questions
    assert sorted(q.juror for q in questions) == ["kind", "skeptic", "strict"]
    assert all(q.text.isascii() for q in questions)  # озвучиваются английским голосом


def test_one_question_per_juror_in_table_order():
    """Каждый член жюри задаёт ровно один вопрос: лишние отбрасываются, промолчавшему даётся заготовка."""
    from app.ai.jury import DraftQuestion, DraftQuestions, one_per_juror

    pitch = Pitch(title="t", brief="b", audience=Audience.BUSINESS)
    draft = DraftQuestions(
        questions=[
            DraftQuestion(juror="skeptic", text="Why you?"),
            DraftQuestion(juror="skeptic", text="Second sceptic question"),
            DraftQuestion(juror="strict", text="What does it cost?"),
        ]
    )
    questions = one_per_juror(draft, pitch).questions
    assert [q.juror for q in questions] == ["strict", "kind", "skeptic"]
    assert questions[0].text == "What does it cost?" and questions[2].text == "Why you?"
    assert questions[1].text  # добрый промолчал — взят его заготовленный вопрос


def test_every_difficulty_has_question_rules_and_answer_scoring() -> None:
    from app.ai import llm
    from app.ai.jury import ANSWER_LEVELS, QUESTION_LEVELS

    assert set(QUESTION_LEVELS) == set(ANSWER_LEVELS) == {"easy", "medium", "hard"}
    for level, rules in QUESTION_LEVELS.items():
        prompt = llm.load_prompt("jury_questions").substitute(
            title="t", brief="b", audience="a", audience_focus="f", own_text="", transcript="x", jurors="-", quirk_rule="-",
            level_intro=rules["intro"], level_rules=rules["rules"], level_length=rules["length"], speech_language="English",
        )  # fmt: skip
        assert rules["intro"] in prompt and "$" not in prompt
        answer = llm.load_prompt("jury_answer").substitute(
            juror_name="n", juror_persona="p", title="t", audience="a", question="q", answer="x", level_scoring=ANSWER_LEVELS[level],
            answer_language="English", feedback_language="English",
        )
        assert ANSWER_LEVELS[level] in answer
    # лёгкий уровень не требует цифр, тяжёлый — требует конкретики
    assert "Never ask for numbers" in QUESTION_LEVELS["easy"]["rules"] and "a number" in QUESTION_LEVELS["hard"]["rules"]
