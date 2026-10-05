"""Примеры ответов /api/ai для режима ?mock=1. Прикол кейса сюда не попадает."""

from collections.abc import Iterator
from itertools import cycle

from app.ai.refine import DraftBlock, render, to_blocks
from app.ai.schemas import (
    BetterVersionResponse,
    ContentScore,
    CriterionScore,
    DeliveryResponse,
    DeliveryScore,
    FillerEvent,
    FlowMoment,
    FlowResponse,
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
    "if the box isn't opened on time. A pilot in three pha- pharmacies, two hundred families in a month. "
    "I think we're looking for pharmacy chains as partners — let's talk after the pitch. So yeah."
)


def _at(fragment: str, point: bool = False) -> dict[str, int]:
    """Место фрагмента в мок-расшифровке; point — точка сразу после него (пауза стоит между словами)."""
    start = _TRANSCRIPT.index(fragment)
    end = start + len(fragment)
    return {"start": end if point else start, "end": end}


def refine(mode: RefineMode) -> RefineResponse:
    blocks = to_blocks(
        [
            DraftBlock(kind="hook", text="Imagine your grandmother can't remember if she took her pill."),
            DraftBlock(kind="problem", text="Older people miss their medicine every day."),
            DraftBlock(kind="solution", text="A smart pill box that beeps and notifies the family."),
            DraftBlock(kind="why_us", text="A pilot in three pharmacies, two hundred families in a month."),
            DraftBlock(kind="call_to_action", text="We're looking for pharmacy chains as partners."),
        ]
    )
    if mode is RefineMode.STRUCTURE:
        notes = ["The text is sorted into five blocks; your words are almost unchanged."]
    else:
        blocks[1].text = "Older people miss [what share] of their doses — and end up in hospital."
        notes = [
            "Weak spot: there is no number for the size of the problem — business people have nothing to hold on to.",
            "Weak spot: you never say how much the device costs.",
            "Changed: the problem is tied to its consequences, with a placeholder left for the number.",
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
            stumbles=1,
            weak_phrases=2,
            pitch_variation=2.6,
            monotone=False,
            fades=1,
        ),
        events=[
            TimelineEvent(type="filler", t=8.2, text="«um»", **_at("Um")),
            TimelineEvent(type="gaze_off", t=31.0, text="Looking away for 4 s"),
            TimelineEvent(type="long_pause", t=52.4, text="Pause of 3.6 s mid-phrase", **_at("notifies the family", point=True)),
            TimelineEvent(type="energy", t=58.0, text="The voice fades at the end of the phrase (−11 dB)", **_at("time.")),
            TimelineEvent(type="stumble", t=61.5, text="Stumble: «pha- pharmacies»", **_at("pha- pharmacies")),
            TimelineEvent(type="weak_phrase", t=70.0, text="Hedging: «i think»", **_at("I think")),
            TimelineEvent(type="weak_phrase", t=84.0, text="Weak ending: «so yeah»", **_at("So yeah")),
        ],
        tips=[
            "Open with a number: how many doses older people miss.",
            "Look at the screen when you give the pilot results — it is your strongest moment.",
            "Replace \"um\" with a short pause.",
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
                "Practise these words: pharmacy, beeps, missed — listen to them in a dictionary and repeat out loud.",
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
    return JuryAnswerResponse(score=68, comment="To the point, but a concrete number was missing.")


def live_events() -> Iterator[LiveEvent]:
    """Бесконечная последовательность событий; t проставляет вызывающий код."""
    return cycle(
        [
            FillerEvent(t=0, word="um"),
            PaceEvent(t=0, wpm=188, verdict="fast"),
            LongPauseEvent(t=0, duration=3.4),
            FillerEvent(t=0, word="you know"),
        ]
    )


def flow() -> FlowResponse:
    return FlowResponse(
        status="ready",
        summary=(
            "The pitch runs in a clear line: a picture of the problem, the solution, proof and a concrete ask. "
            "The weak link is the proof — the pilot numbers say how many, not how well. "
            "Add one result from the pilot and the whole argument gets stronger."
        ),
        moments=[
            FlowMoment(
                t=0.2, end=6.8, kind="hook", tone="good", quote="Imagine it's eight in the morning",
                comment="A vivid picture right away: the room sees the problem through one grandmother.",
            ),
            FlowMoment(
                t=15.2, end=24.0, kind="strong", tone="good", quote="it beeps, lights up and notifies the family",
                comment="The solution fits in one breath: what it does and who it helps.",
            ),
            FlowMoment(
                t=24.0, end=31.5, kind="weak", tone="bad",
                quote="A pilot in three pharmacies, two hundred families in a month",
                comment="Numbers without a result: say what changed for those families — fewer missed pills?",
            ),
            FlowMoment(
                t=31.5, end=38.0, kind="strong_close", tone="good", quote="let's talk after the pitch",
                comment="A concrete ask and a next step — the ending lands.",
            ),
        ],
    )  # fmt: skip


def better_version() -> BetterVersionResponse:
    return BetterVersionResponse(
        status="ready",
        audio_url="/static/audio/q1.mp3",  # мок: любой mp3 из статики
        text=_TRANSCRIPT.replace("Um, this", "This"),
    )
