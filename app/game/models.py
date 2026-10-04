from datetime import datetime, timezone
from typing import Optional
from sqlmodel import Field, SQLModel


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: str = Field(primary_key=True)
    nick: str = Field(index=True, unique=True)
    email: Optional[str] = Field(default=None, index=True, nullable=True)
    password_hash: Optional[str] = Field(default=None, nullable=True)
    google_id: Optional[str] = Field(default=None, index=True, nullable=True)
    avatar_url: Optional[str] = Field(default=None, nullable=True)
    auth_provider: str = Field(default="guest")  # "email" | "local" | "google" | "guest"
    # вход по почте: аккаунт работает только после подтверждения кода из письма
    email_verified: bool = Field(default=False)
    verify_code: Optional[str] = Field(default=None, nullable=True)
    verify_expires: Optional[datetime] = Field(default=None, nullable=True)
    created_at: datetime = Field(default_factory=now_utc)


class Case(SQLModel, table=True):
    __tablename__ = "cases"

    id: str = Field(primary_key=True)
    category_id: str = Field(index=True)
    category_title: str
    title: str
    brief: str
    audience: str
    trick: str  # Прикол кейса — секретный угол для жюри, НИКОГДА не отдавать фронту!
    level: str = Field(default="easy", index=True)  # уровень сложности темы: easy | medium | hard
    created_at: datetime = Field(default_factory=now_utc)


class Round(SQLModel, table=True):
    __tablename__ = "rounds"

    id: str = Field(primary_key=True)
    user_id: str = Field(index=True)
    mode: str = Field(index=True)  # training | daily | own | warmup
    case_id: Optional[str] = Field(default=None, index=True)
    own_title: Optional[str] = None
    own_text: Optional[str] = None
    own_audience: Optional[str] = None
    status: str = Field(default="created", index=True)  # created, pitching, jury, finished
    difficulty: str = Field(default="easy")  # уровень сложности всего раунда: easy | medium | hard
    # язык интерфейса раунда: en | ru | ro — для тем и текстов ошибок; разбор идёт на языке речи (по записи)
    lang: str = Field(default="en")
    created_at: datetime = Field(default_factory=now_utc)
    finished_at: Optional[datetime] = None


class AiResult(SQLModel, table=True):
    __tablename__ = "ai_results"

    id: Optional[int] = Field(default=None, primary_key=True)
    round_id: str = Field(index=True)
    kind: str = Field(index=True)  # delivery | content | jury | live
    payload: str  # JSON строка с результатами анализа
    created_at: datetime = Field(default_factory=now_utc)


class RoundScore(SQLModel, table=True):
    __tablename__ = "round_scores"

    id: Optional[int] = Field(default=None, primary_key=True)
    round_id: str = Field(index=True, unique=True)
    user_id: str = Field(index=True)
    content_score: float
    delivery_score: float
    jury_score: float
    total_score: float
    created_at: datetime = Field(default_factory=now_utc)
