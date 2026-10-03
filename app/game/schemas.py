from typing import List, Optional, Literal
from pydantic import BaseModel
from datetime import datetime


class CategoryOut(BaseModel):
    id: str
    title: str


class CasePublic(BaseModel):
    id: str
    title: str
    brief: str
    audience: str
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


class RoundCreateRequest(BaseModel):
    user_id: str
    mode: Literal["training", "daily", "own", "warmup"]
    case_id: Optional[str] = None
    own: Optional[OwnPitchInput] = None


class RoundCreateResponse(BaseModel):
    round_id: str
    prep_sec: int
    pitch_min_sec: int
    pitch_max_sec: int


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


class LeaderboardEntry(BaseModel):
    nick: str
    score: float


class AuthRequest(BaseModel):
    nick: str


class AuthResponse(BaseModel):
    user_id: str
    nick: str
    rank: RankInfo
