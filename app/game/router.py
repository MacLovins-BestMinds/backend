"""Роутер /api/game. Язык интерфейса — заголовок Accept-Language (en | ru | ro): на нём темы (статические переводы
content/topics.<lang>.json), тексты ошибок, подписи и советы прогресса. На разбор выступлений он не влияет.
"""

from typing import List, Optional
from datetime import datetime
from fastapi import HTTPException, APIRouter, Depends, Query, Path
from sqlmodel import Session

from app.core.db import get_session
from app.game.schemas import (
    SpinResponse,
    DailyResponse,
    RoundCreateRequest,
    RoundCreateResponse,
    RoundFinishResponse,
    ProfileResponse,
    LeaderboardEntry,
    AuthRequest,
    AuthResponse,
    RankInfo,
    RoundSummary,
    CasePublic,
    CategoryOut,
    ProgressResponse,
    RoundReview,
)
from app.game import service
from app.auth.deps import get_current_user, get_current_user_optional
from app.game.models import User
from app.core.lang import UiLang, normalize_lang

router = APIRouter(prefix="/game", tags=["game"])


@router.post("/auth", response_model=AuthResponse)
def auth(
    req: AuthRequest,
    mock: int = Query(0, description="1 для мок-ответа"),
    session: Session = Depends(get_session)
):
    """
    Вход по нику без паролей. Возвращает user_id, nick и текущее звание.
    """
    if mock == 1:
        return AuthResponse(
            user_id="u_mock",
            nick=req.nick,
            rank=RankInfo(title="Novice", trend="flat")
        )

    user = service.get_or_create_user(session, req.nick)
    rank = service.calculate_user_rank(session, user.id)
    return AuthResponse(user_id=user.id, nick=user.nick, rank=rank)


_MOCK_CASE = CasePublic(
    id="favourite_food",
    title="My Favourite Food",
    brief="Tell the room about the food you love most and make them hungry.",
    audience="general public",
    summary="Say what the food is, describe how it tastes, tell when you last had it, "
    "and finish with why everybody should try it.",
    sources=[],
)


@router.get("/spin", response_model=SpinResponse)
def spin(
    lang: UiLang,
    difficulty: str = Query("easy", description="уровень темы: easy | medium | hard"),
    mock: int = Query(0, description="1 для мок-ответа"),
    session: Session = Depends(get_session)
):
    """
    Колесо: категория -> кейс -> готовая тема (без прикола) на языке интерфейса.
    """
    if mock == 1:
        return SpinResponse(
            category=CategoryOut(id="philosophy", title="🏛 Philosophy for Life"),
            case=_MOCK_CASE
        )
    return service.spin_case(session, difficulty if difficulty in service.LEVEL_TIMING else "easy", lang)


@router.get("/daily", response_model=DailyResponse)
def daily(
    lang: UiLang,
    date: Optional[str] = Query(None, description="Дата в формате YYYY-MM-DD"),
    mock: int = Query(0, description="1 для мок-ответа"),
    session: Session = Depends(get_session)
):
    """
    Тема дня: одна тема на всех на сегодня (выбор по хэшу даты), на языке интерфейса.
    """
    if mock == 1:
        return DailyResponse(
            date=date or "2026-10-03",
            case=_MOCK_CASE
        )
    return service.get_daily_case(session, date, lang)


@router.post("/rounds", response_model=RoundCreateResponse)
def create_round(
    req: RoundCreateRequest,
    header_lang: UiLang,
    mock: int = Query(0, description="1 для мок-ответа"),
    current_user: Optional[User] = Depends(get_current_user_optional),
    session: Session = Depends(get_session)
):
    """
    Создание раунда: training | daily | own | warmup.
    С токеном раунд всегда создаётся от имени вошедшего пользователя, user_id из тела не используется.
    Язык интерфейса раунда: поле lang, иначе заголовок Accept-Language, иначе en. Тема в ответе — на этом языке.
    """
    lang = normalize_lang(req.lang) or header_lang
    if mock == 1:
        return RoundCreateResponse(
            round_id="rnd_mock_123",
            prep_sec=300,
            pitch_min_sec=60,
            pitch_max_sec=180,
            lang=lang,
        )
    if current_user:
        req = req.model_copy(update={"user_id": current_user.id})
    return service.create_round(session, req, lang)


@router.post("/rounds/{round_id}/finish", response_model=RoundFinishResponse)
def finish_round(
    lang: UiLang,
    round_id: str = Path(..., description="ID раунда"),
    mock: int = Query(0, description="1 для мок-ответа"),
    session: Session = Depends(get_session)
):
    """
    Завершение раунда: сбор AiResult, расчёт 40/40/20 и пересчёт звания.
    """
    if mock == 1:
        return RoundFinishResponse(
            total=75.5,
            content=70.0,
            delivery=80.0,
            jury=80.0,
            rank=RankInfo(title="Orator", trend="up")
        )
    return service.finish_round(session, round_id, lang)


@router.get("/profile", response_model=ProfileResponse)
def get_profile(
    user_id: Optional[str] = Query(None, description="ID пользователя или ник"),
    mock: int = Query(0, description="1 для мок-ответа"),
    current_user: Optional[User] = Depends(get_current_user_optional),
    session: Session = Depends(get_session)
):
    """
    Профиль: текущее звание, стрелка тренда и последние 5 раундов.
    """
    if mock == 1:
        return ProfileResponse(
            nick="demo_pitcher",
            rank=RankInfo(title="Pitcher", trend="up"),
            last_rounds=[
                RoundSummary(
                    id="rnd_mock_1",
                    mode="training",
                    total=68.0,
                    content=65.0,
                    delivery=70.0,
                    jury=70.0,
                    created_at=datetime.now()
                )
            ]
        )

    target_id = user_id or (current_user.id if current_user else None)
    if not target_id:
        raise HTTPException(status_code=400, detail="Provide user_id or sign in")
    return service.get_user_profile(session, target_id)


@router.get("/progress", response_model=ProgressResponse)
def get_progress(
    lang: UiLang, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """
    История всех раундов и трекер прогресса вошедшего пользователя: баллы, привычки речи, серия дней, советы.
    Подписи, советы и названия тем — на языке интерфейса.
    """
    return service.get_progress(session, current_user, lang)


@router.get("/rounds/{round_id}/review", response_model=RoundReview)
def get_round_review(
    lang: UiLang,
    round_id: str = Path(..., description="ID раунда"),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    """
    Разбор сыгранного раунда из истории (только своего). Название темы — на языке интерфейса.
    """
    return service.get_round_review(session, current_user, round_id, lang)


@router.get("/leaderboard/daily", response_model=List[LeaderboardEntry])
def get_leaderboard(
    date: Optional[str] = Query(None, description="Дата в формате YYYY-MM-DD"),
    mock: int = Query(0, description="1 для мок-ответа"),
    session: Session = Depends(get_session)
):
    """
    Лидерборд: лучший балл каждого за день.
    """
    if mock == 1:
        return [
            LeaderboardEntry(nick="alex", score=88.5),
            LeaderboardEntry(nick="maria", score=79.0)
        ]
    return service.get_daily_leaderboard(session, date)
