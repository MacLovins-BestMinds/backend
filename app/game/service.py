import json
import logging
import random
import hashlib
from datetime import datetime, date, timedelta, timezone
from typing import Optional, List, Any
from sqlmodel import Session, select, func, desc

from fastapi import HTTPException
from app.core.config import settings
from app.core.db import engine
from app.game.models import User, Case, Round, AiResult, RoundScore, now_utc
from app.game import content
from app.game.schemas import (
    SpinResponse,
    DailyResponse,
    CasePublic,
    CategoryOut,
    RoundCreateRequest,
    RoundCreateResponse,
    RoundFinishResponse,
    RankInfo,
    ProfileResponse,
    RoundSummary,
    LeaderboardEntry,
)


logger = logging.getLogger(__name__)

def get_rank_title(avg_score: float) -> str:
    """
    Пороги званий:
    - Новичок: до 40 (< 40)
    - Спикер: 40–59 (>= 40 и < 60)
    - Питчер: 60–74 (>= 60 и < 75)
    - Оратор: 75–87 (>= 75 и < 88)
    - Легенда: от 88 (>= 88)
    """
    if avg_score < 40.0:
        return "Novice"
    elif avg_score < 60.0:
        return "Speaker"
    elif avg_score < 75.0:
        return "Pitcher"
    elif avg_score < 88.0:
        return "Orator"
    else:
        return "Legend"


def calculate_user_rank(session: Session, user_id: str) -> RankInfo:
    """
    Звание — по среднему баллу последних 5 раундов (разминка считается).
    Тренд — знак разницы между средним последних 5 раундов и 5 предыдущих.
    """
    stmt = (
        select(RoundScore)
        .where(RoundScore.user_id == user_id)
        .order_by(desc(RoundScore.created_at))
    )
    scores = list(session.exec(stmt).all())

    if not scores:
        return RankInfo(title="Novice", trend="flat")

    recent_5 = scores[:5]
    avg_recent = sum(s.total_score for s in recent_5) / len(recent_5)
    title = get_rank_title(avg_recent)

    prev_5 = scores[5:10]
    if not prev_5:
        if len(scores) >= 2:
            first_score = scores[-1].total_score
            diff = avg_recent - first_score
            trend = "up" if diff > 0.5 else ("down" if diff < -0.5 else "flat")
        else:
            trend = "flat"
    else:
        avg_prev = sum(s.total_score for s in prev_5) / len(prev_5)
        diff = avg_recent - avg_prev
        trend = "up" if diff > 0.5 else ("down" if diff < -0.5 else "flat")

    return RankInfo(title=title, trend=trend)


class DictLikeObject(dict):
    """Словарь с поддержкой доступа к полям через точку и через скобки."""
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


AUDIENCE_RU_TO_EN = {
    "бизнесмены": "business",
    "жюри конкурса": "contest_jury",
    "преподаватели": "teachers",
    "широкая публика": "public",
    "business people": "business",
    "contest jury": "contest_jury",
    "general public": "public",
    "business": "business",
    "contest_jury": "contest_jury",
    "teachers": "teachers",
    "public": "public",
}


def get_case(case_id: str, session: Optional[Session] = None) -> Optional[DictLikeObject]:
    """
    Возвращает кейс ИЗ БАЗЫ ВКЛЮЧАЯ ПРИКОЛ (trick / quirk).
    Поддерживает как case.trick, так и case["quirk"] для AI Толика.
    """
    c = None
    if session is not None:
        c = session.get(Case, case_id)
    else:
        with Session(engine) as s:
            c = s.get(Case, case_id)

    if not c:
        return None

    raw_aud = c.audience or "business"
    norm_aud = AUDIENCE_RU_TO_EN.get(raw_aud.lower(), raw_aud)

    return DictLikeObject({
        "id": c.id,
        "category": c.category_id,
        "category_id": c.category_id,
        "category_title": c.category_title,
        "title": c.title,
        "brief": c.brief,
        "audience": norm_aud,
        "audience_ru": raw_aud,
        "quirk": c.trick,
        "trick": c.trick,
    })


def save_ai_result(
    round_id: str,
    kind: str,
    payload: Any,
    session: Optional[Session] = None
) -> AiResult:
    """
    Сохраняет результат работы AI для раунда (delivery, content, jury, live).
    """
    payload_str = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    ai_res = AiResult(round_id=round_id, kind=kind, payload=payload_str)

    if session is not None:
        session.add(ai_res)
        session.commit()
        session.refresh(ai_res)
        return ai_res

    with Session(engine) as s:
        s.add(ai_res)
        s.commit()
        s.refresh(ai_res)
        return ai_res


def get_round(round_id: str, session: Optional[Session] = None) -> Optional[DictLikeObject]:
    """
    Возвращает объект раунда с поддержкой round.mode и round["case_id"].
    """
    r = None
    if session is not None:
        r = session.get(Round, round_id)
    else:
        with Session(engine) as s:
            r = s.get(Round, round_id)

    if not r:
        return None

    own = None
    if r.own_title:
        raw_own_aud = r.own_audience or "public"
        own = {
            "title": r.own_title,
            "text": r.own_text or "",
            "audience": AUDIENCE_RU_TO_EN.get(raw_own_aud.lower(), raw_own_aud),
        }

    return DictLikeObject({
        "id": r.id,
        "user_id": r.user_id,
        "mode": r.mode,
        "case_id": r.case_id,
        "status": r.status,
        "own": own,
        "own_title": r.own_title,
        "own_text": r.own_text,
        "own_audience": r.own_audience,
    })


def get_ai_result(round_id: str, kind: str, session: Optional[Session] = None) -> Optional[dict]:
    """
    Возвращает последний сохраненный результат AI для раунда.
    """
    res = None
    if session is not None:
        stmt = (
            select(AiResult)
            .where(AiResult.round_id == round_id, AiResult.kind == kind)
            .order_by(desc(AiResult.created_at))
        )
        res = session.exec(stmt).first()
    else:
        with Session(engine) as s:
            stmt = (
                select(AiResult)
                .where(AiResult.round_id == round_id, AiResult.kind == kind)
                .order_by(desc(AiResult.created_at))
            )
            res = s.exec(stmt).first()

    if not res:
        return None

    try:
        return json.loads(res.payload)
    except Exception:
        return {"raw": res.payload}


# --------------------------------------------------------------------------
# Игровой движок
# --------------------------------------------------------------------------

def get_or_create_user(session: Session, user_identifier: str) -> User:
    """
    Находит или создаёт пользователя по нику или id без паролей.
    """
    clean_id = user_identifier.strip()
    user = session.get(User, clean_id)
    if not user:
        user = session.exec(select(User).where(User.nick == clean_id)).first()
    if not user:
        clean_nick = clean_id or f"speaker_{random.randint(100, 999)}"
        existing_nick = session.exec(select(User).where(User.nick == clean_nick)).first()
        if existing_nick:
            return existing_nick
        user = User(
            id=f"u_{hashlib.md5(clean_nick.encode()).hexdigest()[:10]}",
            nick=clean_nick
        )
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def _active_cases(session: Session) -> List[Case]:
    """Темы из текущего topics.json (старые остаются в базе ради истории раундов)."""
    cases = [c for c in session.exec(select(Case)).all() if c.id in content.active_ids()]
    if not cases:
        seed_cases_from_json(session)
        cases = [c for c in session.exec(select(Case)).all() if c.id in content.active_ids()]
    return cases


def _public_case(case: Case) -> CasePublic:
    """Тема для приложения: без прикола, но с выжимкой и ссылками для подготовки."""
    summary, sources = content.reading(case.id)
    return CasePublic(
        id=case.id, title=case.title, brief=case.brief, audience=case.audience, summary=summary, sources=sources
    )


def spin_case(session: Session) -> SpinResponse:
    """
    Колесо тем: возвращает случайный кейс БЕЗ прикола.
    """
    cases = _active_cases(session)
    chosen = random.choice(cases)
    return SpinResponse(
        category=CategoryOut(id=chosen.category_id, title=chosen.category_title),
        case=_public_case(chosen),
    )


def get_daily_case(session: Session, target_date: Optional[str] = None) -> DailyResponse:
    """
    Тема дня: детерминированный выбор кейса по хэшу даты (одинаковый для всех).
    """
    if not target_date:
        target_date = date.today().isoformat()

    cases_sorted = sorted(_active_cases(session), key=lambda c: c.id)
    date_hash = int(hashlib.md5(target_date.encode("utf-8")).hexdigest(), 16)
    chosen = cases_sorted[date_hash % len(cases_sorted)]

    return DailyResponse(date=target_date, case=_public_case(chosen))


def create_round(session: Session, req: RoundCreateRequest) -> RoundCreateResponse:
    """
    Создание раунда выступления: training, daily, own, warmup.
    """
    user = get_or_create_user(session, req.user_id)

    # Лимит попыток темы дня в час (P2)
    if req.mode == "daily":
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        recent_daily_count = session.exec(
            select(func.count(Round.id))
            .where(Round.user_id == user.id)
            .where(Round.mode == "daily")
            .where(Round.created_at >= one_hour_ago)
        ).one()
        # Лимит 5 попыток в час для предотвращения спама лидерборда
        if recent_daily_count >= 5:
            raise HTTPException(
                status_code=429,
                detail="Topic of the day is limited to 5 attempts per hour. Try Training mode or come back later."
            )

    round_id = f"rnd_{hashlib.md5(f'{user.id}_{datetime.now(timezone.utc).isoformat()}_{random.random()}'.encode()).hexdigest()[:12]}"

    if req.mode == "warmup":
        prep_sec = 30
        pitch_min_sec = 20
        pitch_max_sec = 45
    else:
        prep_sec = 300
        pitch_min_sec = 60
        pitch_max_sec = 180

    own_title = req.own.title if req.own else None
    own_text = req.own.text if req.own else None
    own_audience = req.own.audience if req.own else None

    case_id = req.case_id
    if req.mode == "daily" and not case_id:
        daily_info = get_daily_case(session)
        case_id = daily_info.case.id
    elif req.mode == "training" and not case_id:
        spin_info = spin_case(session)
        case_id = spin_info.case.id

    round_obj = Round(
        id=round_id,
        user_id=user.id,
        mode=req.mode,
        case_id=case_id,
        own_title=own_title,
        own_text=own_text,
        own_audience=own_audience,
        status="created"
    )
    session.add(round_obj)
    session.commit()

    return RoundCreateResponse(
        round_id=round_id,
        prep_sec=prep_sec,
        pitch_min_sec=pitch_min_sec,
        pitch_max_sec=pitch_max_sec
    )


def _collect_ai_scores(ai_results) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Баллы содержания, подачи и жюри из AiResult (None — результата нет).

    Форматы AI-движка (app/ai):
    - kind="delivery": {"scores": {"content": {"total"}, "delivery": {"total"}}, ...}
    - kind="jury_answer": {"question_id", "score", ...} — по записи на вопрос, балл жюри = среднее
    Старые форматы (kind="content" / "jury", плоский "score") поддерживаются для совместимости.
    """
    content_score = None
    delivery_score = None
    jury_answers: List[float] = []
    jury_score = None

    for res in ai_results:
        try:
            data = json.loads(res.payload)
            if res.kind == "delivery":
                scores = data.get("scores")
                if isinstance(scores, dict) and isinstance(scores.get("delivery"), dict):
                    delivery_score = float(scores["delivery"]["total"])
                    if isinstance(scores.get("content"), dict):
                        content_score = float(scores["content"]["total"])
                elif isinstance(scores, dict) and "total" in scores:
                    delivery_score = float(scores["total"])
                elif "score" in data:
                    delivery_score = float(data["score"])
            elif res.kind == "content":
                content_score = float(data.get("score", data.get("content_score")))
            elif res.kind == "jury_answer":
                jury_answers.append(float(data["score"]))
            elif res.kind == "jury":
                if "score" in data:
                    jury_score = float(data["score"])
                elif isinstance(data.get("scores"), list) and data["scores"]:
                    jury_score = float(sum(data["scores"]) / len(data["scores"]))
        except (ValueError, TypeError, KeyError):
            logger.warning("finish: не удалось разобрать AiResult kind=%s round=%s", res.kind, res.round_id)

    if jury_answers:
        jury_score = sum(jury_answers) / len(jury_answers)
    return content_score, delivery_score, jury_score


def finish_round(session: Session, round_id: str) -> RoundFinishResponse:
    """
    Завершение раунда:
    - собирает AiResult (content, delivery, jury)
    - считает итог: 40% содержание + 40% подача + 20% ответы жюри
    - пересчитывает звание и тренд
    """
    round_obj = session.get(Round, round_id)
    if not round_obj:
        raise HTTPException(status_code=404, detail=f"Round {round_id} not found")

    ai_results = session.exec(select(AiResult).where(AiResult.round_id == round_id)).all()
    content_score, delivery_score, jury_score = _collect_ai_scores(ai_results)

    if content_score is None or delivery_score is None:
        if not settings.MOCK_FALLBACK:
            raise HTTPException(status_code=409, detail="Send the pitch for review (delivery) first")
        # MOCK_FALLBACK: раунд прошёл на моках AI — условные баллы, чтобы игровой цикл работал целиком
        content_score = 74.0 if content_score is None else content_score
        delivery_score = 78.0 if delivery_score is None else delivery_score

    if round_obj.mode == "warmup":
        # в разминке нет жюри: 50% содержание + 50% подача
        jury_score = 0.0
        total_score = round(0.5 * content_score + 0.5 * delivery_score, 1)
    else:
        if jury_score is None:
            # не ответил ни на один вопрос — 0; условные 72 только на моках
            jury_score = 72.0 if settings.MOCK_FALLBACK else 0.0
        total_score = round(0.4 * content_score + 0.4 * delivery_score + 0.2 * jury_score, 1)

    existing_score = session.exec(select(RoundScore).where(RoundScore.round_id == round_id)).first()
    if existing_score:
        existing_score.content_score = content_score
        existing_score.delivery_score = delivery_score
        existing_score.jury_score = jury_score
        existing_score.total_score = total_score
        session.add(existing_score)
    else:
        round_score = RoundScore(
            round_id=round_id,
            user_id=round_obj.user_id,
            content_score=content_score,
            delivery_score=delivery_score,
            jury_score=jury_score,
            total_score=total_score
        )
        session.add(round_score)

    round_obj.status = "finished"
    round_obj.finished_at = datetime.now(timezone.utc)
    session.add(round_obj)
    session.commit()

    rank_info = calculate_user_rank(session, round_obj.user_id)

    return RoundFinishResponse(
        total=total_score,
        content=round(content_score, 1),
        delivery=round(delivery_score, 1),
        jury=round(jury_score, 1),
        rank=rank_info
    )


def get_user_profile(session: Session, user_identifier: str) -> ProfileResponse:
    """
    Профиль: звание, тренд и последние 5 раундов.
    """
    user = get_or_create_user(session, user_identifier)
    rank_info = calculate_user_rank(session, user.id)

    stmt = (
        select(Round, RoundScore)
        .where(Round.user_id == user.id)
        .where(Round.status == "finished")
        .join(RoundScore, Round.id == RoundScore.round_id)
        .order_by(desc(Round.finished_at))
        .limit(10)
    )
    results = session.exec(stmt).all()

    last_rounds: List[RoundSummary] = []
    for rnd, sc in results:
        last_rounds.append(
            RoundSummary(
                id=rnd.id,
                mode=rnd.mode,
                total=sc.total_score,
                content=sc.content_score,
                delivery=sc.delivery_score,
                jury=sc.jury_score,
                created_at=rnd.created_at
            )
        )

    return ProfileResponse(
        nick=user.nick,
        rank=rank_info,
        last_rounds=last_rounds
    )


def get_daily_leaderboard(session: Session, target_date: Optional[str] = None) -> List[LeaderboardEntry]:
    """
    Лидерборд дня: лучший балл каждого за день.
    """
    if not target_date:
        target_date = date.today().isoformat()

    try:
        dt_start = datetime.fromisoformat(target_date).replace(tzinfo=timezone.utc)
    except Exception:
        dt_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    dt_end = dt_start + timedelta(days=1)

    stmt = (
        select(User.nick, func.max(RoundScore.total_score).label("best_score"))
        .join(User, RoundScore.user_id == User.id)
        .where(RoundScore.created_at >= dt_start)
        .where(RoundScore.created_at < dt_end)
        .group_by(User.nick)
        .order_by(desc("best_score"))
        .limit(50)
    )
    rows = session.exec(stmt).all()

    return [LeaderboardEntry(nick=r[0], score=round(r[1], 1)) for r in rows]


# --------------------------------------------------------------------------
# Сид данных
# --------------------------------------------------------------------------

def seed_cases_from_json(session: Session) -> None:
    """
    Загрузка 48 тем из content/topics.json: новые добавляются, существующие обновляются.
    """
    json_path = settings.TOPICS_JSON_PATH
    if not json_path.exists():
        return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    for item in data:
        cat = item.get("category", "")
        if isinstance(cat, dict):
            category_id = cat.get("id", "general")
            category_title = cat.get("title", item.get("category_title", "General"))
        else:
            category_id = str(cat)
            category_title = item.get("category_title", category_id.replace("_", " ").title())

        trick = item.get("quirk") or item.get("trick") or ""

        existing = session.get(Case, item["id"])
        if existing:
            # контент правится в topics.json — обновляем уже засеянные темы, а не только добавляем новые
            existing.category_id = category_id
            existing.category_title = category_title
            existing.title = item["title"]
            existing.brief = item["brief"]
            existing.audience = item["audience"]
            existing.trick = trick
            session.add(existing)
        else:
            new_case = Case(
                id=item["id"],
                category_id=category_id,
                category_title=category_title,
                title=item["title"],
                brief=item["brief"],
                audience=item["audience"],
                trick=trick,
                created_at=now_utc()
            )
            session.add(new_case)
    session.commit()


def seed_demo_pitcher(session: Session) -> None:
    """
    Создает демо-пользователя demo_pitcher со званием «Питчер» (средний балл 60–74)
    и трендом «up».
    """
    demo_user = session.exec(select(User).where(User.nick == "demo_pitcher")).first()
    if not demo_user:
        demo_user = User(id="u_demo_pitcher", nick="demo_pitcher")
        session.add(demo_user)
        session.commit()
        session.refresh(demo_user)

    existing_scores = session.exec(select(RoundScore).where(RoundScore.user_id == demo_user.id)).all()
    if not existing_scores:
        now = datetime.now(timezone.utc)

        # Предыдущие 5 раундов (среднее 62.0)
        prev_scores = [60.0, 61.0, 63.0, 62.0, 64.0]
        for i, sc in enumerate(prev_scores):
            r_id = f"rnd_demo_prev_{i}"
            rnd = Round(
                id=r_id,
                user_id=demo_user.id,
                mode="training",
                status="finished",
                created_at=now - timedelta(days=2, hours=10 - i),
                finished_at=now - timedelta(days=2, hours=10 - i, minutes=-3)
            )
            score = RoundScore(
                round_id=r_id,
                user_id=demo_user.id,
                content_score=sc - 2,
                delivery_score=sc + 2,
                jury_score=sc,
                total_score=sc,
                created_at=rnd.finished_at
            )
            session.add(rnd)
            session.add(score)

        # Последние 5 раундов (среднее 68.5 -> «Питчер», тренд «up»)
        recent_scores = [66.0, 68.0, 71.0, 67.5, 70.0]
        for i, sc in enumerate(recent_scores):
            r_id = f"rnd_demo_rec_{i}"
            rnd = Round(
                id=r_id,
                user_id=demo_user.id,
                mode="training",
                status="finished",
                created_at=now - timedelta(hours=5 - i),
                finished_at=now - timedelta(hours=5 - i, minutes=-3)
            )
            score = RoundScore(
                round_id=r_id,
                user_id=demo_user.id,
                content_score=sc - 2,
                delivery_score=sc + 2,
                jury_score=sc,
                total_score=sc,
                created_at=rnd.finished_at
            )
            session.add(rnd)
            session.add(score)

        session.commit()

    # Добавляем участников в лидерборд на сегодня
    alex_user = session.exec(select(User).where(User.nick == "alex_speaker")).first()
    if not alex_user:
        alex_user = User(id="u_alex", nick="alex_speaker")
        session.add(alex_user)
        session.commit()
        rnd_alex = Round(
            id="rnd_alex_1",
            user_id=alex_user.id,
            mode="daily",
            status="finished",
            created_at=datetime.now(timezone.utc)
        )
        score_alex = RoundScore(
            round_id="rnd_alex_1",
            user_id=alex_user.id,
            content_score=86.0,
            delivery_score=89.0,
            jury_score=85.0,
            total_score=87.0,
            created_at=datetime.now(timezone.utc)
        )
        session.add(rnd_alex)
        session.add(score_alex)
        session.commit()
