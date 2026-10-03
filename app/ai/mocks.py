"""Примеры ответов /api/ai для режима ?mock=1. Прикол кейса сюда не попадает."""

from collections.abc import Iterator
from itertools import cycle

from app.ai.refine import DraftBlock, render, to_blocks
from app.ai.schemas import (
    ContentScore,
    CriterionScore,
    DeliveryResponse,
    DeliveryScore,
    FillerEvent,
    JuryAnswerResponse,
    JuryQuestion,
    JuryQuestionsResponse,
    LiveEvent,
    LongPauseEvent,
    Metrics,
    PaceEvent,
    PronunciationAssessment,
    PronunciationIssue,
    RefineMode,
    RefineResponse,
    Scores,
    TimelineEvent,
)

_TRANSCRIPT = (
    "Imagine it's eight in the morning and your grandmother can't remember if she took her blood pressure pill. "
    "Um, this happens every day to millions of older people. "
    "We built a smart pill box: it beeps, lights up and notifies the family "
    "if the box isn't opened on time. A pilot in three pharmacies, two hundred families in a month. "
    "We're looking for pharmacy chains as partners — let's talk after the pitch."
)


def refine(mode: RefineMode) -> RefineResponse:
    blocks = to_blocks(
        [
            DraftBlock(kind="hook", text="Представьте, бабушка не помнит, выпила ли таблетку."),
            DraftBlock(kind="problem", text="Пожилые пропускают приём лекарств каждый день."),
            DraftBlock(kind="solution", text="Умная таблетница с сигналом и уведомлением родственникам."),
            DraftBlock(kind="why_us", text="Пилот в трёх аптеках, двести семей за месяц."),
            DraftBlock(kind="call_to_action", text="Ищем партнёров среди аптечных сетей."),
        ]
    )
    if mode is RefineMode.STRUCTURE:
        notes = ["Текст разложен по пяти блокам, слова почти не менялись."]
    else:
        blocks[1].text = "Пожилые пропускают [какую долю] приёмов лекарств — и попадают в больницу."
        notes = [
            "Слабое место: нет цифры масштаба проблемы — бизнесу не за что зацепиться.",
            "Слабое место: не сказано, сколько стоит устройство.",
            "Изменено: проблема привязана к последствиям, оставлена заглушка для цифры.",
        ]
    return RefineResponse(text=render(blocks), notes=notes, blocks=blocks)


def delivery() -> DeliveryResponse:
    return DeliveryResponse(
        transcript=_TRANSCRIPT,
        scores=Scores(
            content=ContentScore(
                total=72,
                criteria=[
                    CriterionScore(name="topic", score=85, quote="We built a smart pill box"),
                    CriterionScore(name="structure", score=70, quote="We're looking for pharmacy chains as partners"),
                    CriterionScore(name="clarity", score=75, quote="it beeps, lights up and notifies the family"),
                    CriterionScore(
                        name="persuasion",
                        score=60,
                        quote="A pilot in three pharmacies, two hundred families in a month",
                    ),
                ],
            ),
            delivery=DeliveryScore(total=78, fillers=85, pace=100, gaze=70, pauses=90, timing=100),
        ),
        metrics=Metrics(
            duration_sec=94.5,
            words=212,
            wpm=135,
            fillers=3,
            fillers_per_min=1.9,
            long_pauses=1,
            gaze_on_ratio=0.64,
        ),
        events=[
            TimelineEvent(type="filler", t=8.2, text="«um»"),
            TimelineEvent(type="gaze_off", t=31.0, text="Взгляд мимо зала 4 секунды"),
            TimelineEvent(type="long_pause", t=52.4, text="Пауза 3.6 секунды посреди фразы"),
            TimelineEvent(type="good_pause", t=70.1, text="Удачная пауза перед цифрами"),
        ],
        tips=[
            "Начни с цифры: сколько приёмов лекарств пропускают пожилые.",
            "Смотри в телефон, когда называешь результаты пилота — это самый сильный момент.",
            "Замени «um» короткой паузой.",
        ],
        pronunciation=PronunciationAssessment(
            overall_score=84,
            accuracy_score=86,
            fluency_score=88,
            prosody_score=79,
            words_total=212,
            mispronounced_words_count=3,
            unexpected_breaks_count=1,
            monotone=False,
            words=[
                PronunciationIssue(
                    word="pharmacy", t=41.2, accuracy=37, error="mispronunciation", weak_syllables=["pha"]
                ),
                PronunciationIssue(
                    word="beeps", t=24.8, accuracy=42, error="mispronunciation", weak_syllables=["eeps"]
                ),
                PronunciationIssue(word="missed", t=36.0, accuracy=55, error="mispronunciation", weak_syllables=[]),
                PronunciationIssue(word="families", t=33.1, accuracy=81, error="unexpected_break", weak_syllables=[]),
            ],
            tips=[
                "Потренируй произношение слов: pharmacy, beeps, missed — послушай их в словаре и повтори вслух.",
            ],
        ),
    )


def jury_questions(round_id: str) -> JuryQuestionsResponse:
    questions = [
        ("strict", "What if grandma doesn't use a smartphone — who gets the notification?"),
        ("kind", "Two hundred families in a month — how many of them stayed with you?"),
        ("skeptic", "How much does the pill box cost, and who pays for it?"),
    ]
    return JuryQuestionsResponse(
        questions=[
            JuryQuestion(id=f"q{i}", juror=juror, text=text, audio_url=f"/static/jury/{round_id}/q{i}.mp3")
            for i, (juror, text) in enumerate(questions, start=1)
        ]
    )


def jury_answer() -> JuryAnswerResponse:
    return JuryAnswerResponse(score=68, comment="По существу, но не хватило конкретной цифры.")


def live_events() -> Iterator[LiveEvent]:
    """Бесконечная последовательность событий; t проставляет вызывающий код."""
    return cycle(
        [
            FillerEvent(t=0, word="ну"),
            PaceEvent(t=0, wpm=188, verdict="fast"),
            LongPauseEvent(t=0, duration=3.4),
            FillerEvent(t=0, word="как бы"),
        ]
    )
