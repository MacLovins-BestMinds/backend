"""Pydantic-схемы роутера /api/ai — строго по контрактам из docs/tz (Часть 1)."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Audience(StrEnum):
    CONTEST_JURY = "contest_jury"
    BUSINESS = "business"
    TEACHERS = "teachers"
    PUBLIC = "public"


type JurorId = Literal["strict", "kind", "skeptic"]


class RefineMode(StrEnum):
    STRUCTURE = "structure"
    IMPROVE = "improve"


# --- POST /api/ai/refine ---


type BlockKind = Literal["hook", "problem", "solution", "why_us", "call_to_action"]


class RefineRequest(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    audience: Audience
    mode: RefineMode


class PitchBlock(BaseModel):
    kind: BlockKind
    title: str = Field(description="подпись блока для экрана: «Хук», «Проблема», …")
    text: str = Field(description="пусто, если такого блока в тексте нет")


class RefineResponse(BaseModel):
    text: str = Field(description="готовый текст по блокам, с подписями")
    notes: list[str]
    blocks: list[PitchBlock] = Field(default_factory=list)


# --- WS /api/ai/live: события сервер → приложение ---


class FillerEvent(BaseModel):
    type: Literal["filler"] = "filler"
    t: float = Field(description="секунды от начала выступления")
    word: str
    burst: bool = Field(False, description="третий паразит за 20 секунд — «кто-то достаёт телефон»")


class LongPauseEvent(BaseModel):
    type: Literal["long_pause"] = "long_pause"
    t: float
    duration: float


class PaceEvent(BaseModel):
    type: Literal["pace"] = "pace"
    t: float
    wpm: int
    verdict: Literal["fast", "slow"]


LiveEvent = FillerEvent | LongPauseEvent | PaceEvent


# --- POST /api/ai/rounds/{id}/delivery ---


class GazePoint(BaseModel):
    t: float
    on: bool


class CriterionScore(BaseModel):
    name: str
    score: int = Field(ge=0, le=100)
    quote: str = Field(description="цитата из сказанного, на которой основана оценка")


class ContentScore(BaseModel):
    total: int = Field(ge=0, le=100)
    criteria: list[CriterionScore]


class DeliveryScore(BaseModel):
    total: int = Field(ge=0, le=100)
    fillers: int = Field(ge=0, le=100)
    pace: int = Field(ge=0, le=100)
    gaze: int = Field(ge=0, le=100)
    pauses: int = Field(ge=0, le=100)
    timing: int = Field(ge=0, le=100)
    pronunciation: int | None = Field(None, ge=0, le=100, description="балл Azure; входит в total на 30%")


class Scores(BaseModel):
    content: ContentScore
    delivery: DeliveryScore


class Metrics(BaseModel):
    duration_sec: float
    words: int
    wpm: int
    fillers: int
    fillers_per_min: float
    long_pauses: int
    gaze_on_ratio: float = Field(ge=0, le=1)


class TimelineEvent(BaseModel):
    """Маркер на таймлайне разбора."""

    type: Literal["filler", "long_pause", "hesitation", "pace", "gaze_off", "good_pause"]
    t: float
    text: str


class PronunciationIssue(BaseModel):
    """Слово с проблемой произношения или интонации."""

    word: str
    t: float = Field(description="секунды от начала выступления")
    accuracy: int = Field(ge=0, le=100, description="точность звуков слова")
    error: Literal["mispronunciation", "unexpected_break", "missing_break", "monotone"]
    weak_syllables: list[str] = Field(default_factory=list, description="буквы слогов, звучащих хуже всего")


class PronunciationAssessment(BaseModel):
    """Оценка английского произношения (Azure Pronunciation Assessment, без эталонного текста).

    Ударения по слогам Azure не возвращает — оно учтено в prosody_score.
    """

    overall_score: int = Field(ge=0, le=100)
    accuracy_score: int = Field(ge=0, le=100, description="точность звуков")
    fluency_score: int = Field(ge=0, le=100, description="беглость")
    prosody_score: int | None = Field(None, ge=0, le=100, description="интонация, ударения, ритм")
    words_total: int
    mispronounced_words_count: int
    unexpected_breaks_count: int = Field(description="паузы внутри фразы")
    monotone: bool = Field(description="речь звучит монотонно")
    words: list[PronunciationIssue] = Field(default_factory=list, description="проблемные слова, худшие сначала")
    tips: list[str] = Field(default_factory=list)


class DeliveryResponse(BaseModel):
    transcript: str
    scores: Scores
    metrics: Metrics
    events: list[TimelineEvent]
    tips: list[str] = Field(max_length=3)
    pronunciation: PronunciationAssessment | None = Field(None, description="null — Azure не настроен или не ответил")


# --- жюри ---


class JuryQuestion(BaseModel):
    id: str
    juror: JurorId
    text: str
    audio_url: str


class JuryQuestionsResponse(BaseModel):
    questions: list[JuryQuestion]


class JuryAnswerResponse(BaseModel):
    score: int = Field(ge=0, le=100)
    comment: str
