"""Примеры ответов /api/ai для режима ?mock=1. Прикол кейса сюда не попадает."""

from collections.abc import Iterator
from itertools import cycle

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
    RefineMode,
    RefineResponse,
    Scores,
    TimelineEvent,
)

_TRANSCRIPT = (
    "Представьте: бабушка в восемь утра не помнит, выпила ли она таблетку от давления. "
    "Ну, это происходит каждый день с миллионами пожилых людей. "
    "Мы сделали умную таблетницу: она пищит, светится и присылает родственникам уведомление, "
    "если ячейка не открылась вовремя. Пилот в трёх аптеках, двести семей за месяц. "
    "Нам нужны партнёры среди аптечных сетей — давайте поговорим после выступления."
)


def refine(text: str, mode: RefineMode) -> RefineResponse:
    structured = (
        "Хук: Представьте, бабушка не помнит, выпила ли таблетку.\n"
        "Проблема: пожилые пропускают приём лекарств каждый день.\n"
        "Решение: умная таблетница с сигналом и уведомлением родственникам.\n"
        "Почему мы: пилот в трёх аптеках, двести семей за месяц.\n"
        "Призыв: ищем партнёров среди аптечных сетей."
    )
    if mode is RefineMode.STRUCTURE:
        return RefineResponse(text=structured, notes=["Текст разложен по пяти блокам, слова почти не менялись."])
    return RefineResponse(
        text=structured.replace("пропускают приём лекарств", "пропускают каждый третий приём лекарств"),
        notes=[
            "Слабое место: нет цифры масштаба проблемы — добавили долю пропущенных приёмов.",
            "Слабое место: не сказано, сколько стоит устройство.",
            "Изменено: призыв стал конкретным — к кому и зачем обращаемся.",
        ],
    )


def delivery() -> DeliveryResponse:
    return DeliveryResponse(
        transcript=_TRANSCRIPT,
        scores=Scores(
            content=ContentScore(
                total=72,
                criteria=[
                    CriterionScore(name="topic", score=85, quote="Мы сделали умную таблетницу"),
                    CriterionScore(name="structure", score=70, quote="Нам нужны партнёры среди аптечных сетей"),
                    CriterionScore(name="clarity", score=75, quote="она пищит, светится и присылает родственникам уведомление"),
                    CriterionScore(name="persuasion", score=60, quote="Пилот в трёх аптеках, двести семей за месяц"),
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
            TimelineEvent(type="filler", t=8.2, text="«ну»"),
            TimelineEvent(type="gaze_off", t=31.0, text="Взгляд мимо зала 4 секунды"),
            TimelineEvent(type="long_pause", t=52.4, text="Пауза 3.6 секунды посреди фразы"),
            TimelineEvent(type="good_pause", t=70.1, text="Удачная пауза перед цифрами"),
        ],
        tips=[
            "Начни с цифры: сколько приёмов лекарств пропускают пожилые.",
            "Смотри в телефон, когда называешь результаты пилота — это самый сильный момент.",
            "Замени «ну» короткой паузой.",
        ],
    )


def jury_questions(round_id: str) -> JuryQuestionsResponse:
    questions = [
        ("strict", "А если бабушка не пользуется смартфоном, кто получит уведомление?"),
        ("kind", "Двести семей за месяц — сколько из них остались с вами?"),
        ("skeptic", "Сколько стоит таблетница и кто за неё платит?"),
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
