from typing import List, Optional, Literal
from pydantic import BaseModel, Field
from datetime import datetime

from app.core.lang import Lang


class CategoryOut(BaseModel):
    id: str
    title: str


class SourceLink(BaseModel):
    title: str
    url: str


class CasePublic(BaseModel):
    id: str
    title: str
    brief: str
    audience: str
    summary: Optional[str] = None  # 2–3 предложения из Википедии (англ.) — прочитать на подготовке
    sources: List[SourceLink] = []  # Википедия + 1–2 проверенных сайта
    # ВНИМАНИЕ: trick (прикол кейса) исключен из публичной схемы!


class SpinResponse(BaseModel):
    category: CategoryOut
    case: CasePublic


class DailyResponse(BaseModel):
    date: str
    case: CasePublic


class OwnPitchInput(BaseModel):
    title: str
    text: str
    audience: str


Difficulty = Literal["easy", "medium", "hard"]


class RoundCreateRequest(BaseModel):
    user_id: str
    mode: Literal["training", "daily", "own", "warmup"]
    difficulty: Difficulty = "easy"  # уровень всего раунда: тема, время, строгость разбора, жюри и зала
    case_id: Optional[str] = None
    own: Optional[OwnPitchInput] = None
    # язык интерфейса: en | ru | ro; нет или другой — из Accept-Language, иначе en
    lang: Optional[str] = None


class RoundCreateResponse(BaseModel):
    round_id: str
    prep_sec: int
    pitch_min_sec: int
    pitch_max_sec: int
    lang: Lang = Field("en", description="язык интерфейса раунда: темы и тексты ошибок; разбор — на языке речи")
    case: Optional[CasePublic] = Field(None, description="тема раунда на языке интерфейса; нет — свой питч или разминка")


class RankInfo(BaseModel):
    title: str  # Новичок | Спикер | Питчер | Оратор | Легенда
    trend: str  # up | down | flat


class RoundFinishResponse(BaseModel):
    total: float
    content: float
    delivery: float
    jury: float
    rank: RankInfo


class RoundSummary(BaseModel):
    id: str
    mode: str
    total: float
    content: float
    delivery: float
    jury: float
    created_at: datetime


class ProfileResponse(BaseModel):
    nick: str
    rank: RankInfo
    last_rounds: List[RoundSummary]


class HistoryRound(BaseModel):
    """Один сыгранный раунд в истории: баллы и привычки речи из разбора."""

    id: str
    mode: str
    difficulty: str = "easy"
    title: str
    created_at: datetime
    total: float
    content: float
    delivery: float
    jury: float
    duration_sec: Optional[float] = None
    wpm: Optional[int] = None
    fillers_per_min: Optional[float] = None
    long_pauses: Optional[int] = None
    repeats: Optional[int] = None
    gaze_on_ratio: Optional[float] = None


class SkillTrend(BaseModel):
    """Среднее за последние 5 раундов и изменение к 5 предыдущим (None — сравнивать пока не с чем)."""

    key: str
    title: str
    value: Optional[float] = None
    delta: Optional[float] = None
    better: Literal["higher", "lower", "range"] = "higher"
    unit: str = ""


class Insight(BaseModel):
    kind: Literal["good", "focus"]
    title: str
    text: str


class NextRank(BaseModel):
    title: str
    points_needed: float


class ProgressResponse(BaseModel):
    nick: str
    rank: RankInfo
    rank_score: float
    next_rank: Optional[NextRank] = None
    rounds_total: int
    minutes_total: float
    average: float
    best: float
    streak_days: int
    skills: List[SkillTrend]
    habits: List[SkillTrend]
    insights: List[Insight]
    history: List[HistoryRound]


class RoundReview(BaseModel):
    """Разбор сыгранного раунда из истории. Звук раунда хранится (audio_url), видео — нет."""

    round: HistoryRound
    result: RoundFinishResponse
    delivery: Optional[dict] = None
    jury_questions: List[dict] = []
    jury_answers: List[dict] = []
    # /static/recordings/<round_id>.<ext>; None — записи нет (раунды до хранения записей, моки)
    audio_url: Optional[str] = None
    # как ответы GET /api/ai/rounds/{id}/flow и /better-version; None — ещё не считались (их запускает тот GET)
    flow: Optional[dict] = None
    better_version: Optional[dict] = None


class LeaderboardEntry(BaseModel):
    nick: str
    score: float


class AuthRequest(BaseModel):
    nick: str


class AuthResponse(BaseModel):
    user_id: str
    nick: str
    rank: RankInfo
