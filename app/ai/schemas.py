"""Pydantic-схемы роутера /api/ai — строго по контрактам из docs/tz (Часть 1)."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.core.lang import Lang


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


class ProfanityEvent(BaseModel):
    """Игрок выругался — зал и жюри реагируют сразу."""

    type: Literal["profanity"] = "profanity"
    t: float
    word: str


class ContentEvent(BaseModel):
    """Раз в несколько секунд: насколько последние слова по теме и содержательны, плюс подсказка на экран."""

    type: Literal["content"] = "content"
    t: float
    score: int = Field(ge=0, le=100, description="0 — не по теме или вода, 100 — по теме и по делу")
    comment: str = Field("", description="короткая подсказка игроку, может быть пустой")


LiveEvent = FillerEvent | LongPauseEvent | PaceEvent | ContentEvent | ProfanityEvent


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
    gaze: int | None = Field(None, ge=0, le=100, description="null — взгляд не измерялся (нет камеры)")
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
    profanity: int = Field(0, description="сколько раз прозвучала ругань")
    gaze_on_ratio: float | None = Field(None, ge=0, le=1, description="null — взгляд не измерялся")


class TimelineEvent(BaseModel):
    """Маркер на таймлайне разбора."""

    # good_pause больше не выдаётся (паузу «после фразы» нельзя отличить от смеха или заминки), тип оставлен для старых записей
    type: Literal["filler", "repeat", "profanity", "long_pause", "hesitation", "pace", "gaze_off", "good_pause"]
    t: float
    text: str
    start: int | None = Field(None, description="позиция в transcript (символы): начало отмеченного места")
    end: int | None = Field(None, description="конец отмеченного места; равен start, если это точка между словами (пауза, темп)")


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


class WordMark(BaseModel):
    """Слово расшифровки: где оно стоит в transcript (символы) и когда звучит в записи (секунды)."""

    start: int
    end: int
    t: float
    t_end: float
    # начало каждой буквы слова в записи (секунды), по одной на символ transcript[start:end]; None — нет данных
    c: list[float] | None = None


class DeliveryResponse(BaseModel):
    transcript: str
    words: list[WordMark] = Field(default_factory=list, description="слова со временем — приложение подсвечивает текущее")
    scores: Scores
    metrics: Metrics
    events: list[TimelineEvent]
    tips: list[str] = Field(max_length=3)
    pronunciation: PronunciationAssessment | None = Field(None, description="null — Azure не настроен или не ответил")
    speech_lang: Lang = Field(
        "en", description="язык речи по распознаванию; не определило — как в прошлом разборе раунда, иначе STT_LANGUAGE"
    )


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


# --- GET /api/ai/rounds/{id}/flow: ход мысли по расшифровке ---


type FlowKind = Literal["hook", "strong", "weak", "off_topic", "rambling", "strong_close", "weak_close"]
GOOD_FLOW_KINDS = frozenset({"hook", "strong", "strong_close"})


class FlowMoment(BaseModel):
    """Момент питча на таймлайне разбора: где зацепил зал, где сильная мысль, где поплыл."""

    t: float = Field(description="секунды от начала выступления")
    end: float
    kind: FlowKind
    tone: Literal["good", "bad"] = Field(description="good — hook, strong, strong_close; bad — остальные")
    quote: str = Field(description="дословно из расшифровки")
    comment: str


class FlowResponse(BaseModel):
    status: Literal["pending", "ready", "failed"]
    summary: str | None = None
    moments: list[FlowMoment] = Field(default_factory=list, description="по времени; пусто у очень короткого питча")


# --- GET /api/ai/rounds/{id}/better-version: тот же питч своим голосом, без паразитов и запинок ---


class BetterVersionResponse(BaseModel):
    status: Literal["pending", "ready", "failed", "unavailable"]
    audio_url: str | None = Field(None, description="/static/better/<round_id>.mp3, когда status=ready")
    text: str | None = Field(None, description="очищенный текст, который прочитал голос")
    reason: str | None = Field(None, description="почему нет (unavailable, failed) — на языке интерфейса")
