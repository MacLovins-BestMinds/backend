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
from app.core.lang import pick
from app.recordings import recording_url
from app.game.schemas import (
    SpinResponse,
    DailyResponse,
    CasePublic,
    CategoryOut,
    RoundCreateRequest,
    RoundCreateResponse,
    HistoryRound,
    SkillTrend,
    Insight,
    NextRank,
    ProgressResponse,
    RoundReview,
    RoundFinishResponse,
    RankInfo,
    ProfileResponse,
    RoundSummary,
    LeaderboardEntry,
)


logger = logging.getLogger(__name__)

# Тексты ошибок, которые видит игрок, — на языке интерфейса
ERROR_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "daily_limit": "Topic of the day is limited to 5 attempts per hour. Try Training mode or come back later.",
        "no_round": "Round {round_id} not found",
        "no_delivery": "Send the pitch for review (delivery) first",
        "not_finished": "This round was not finished",
    },
    "ru": {
        "daily_limit": "Тему дня можно пройти не больше 5 раз в час. Попробуй тренировку или вернись позже.",
        "no_round": "Раунд {round_id} не найден",
        "no_delivery": "Сначала отправь питч на разбор (delivery)",
        "not_finished": "Этот раунд не был завершён",
    },
    "ro": {
        "daily_limit": "Tema zilei poate fi jucată de cel mult 5 ori pe oră. Încearcă modul Antrenament sau revino mai târziu.",
        "no_round": "Runda {round_id} nu a fost găsită",
        "no_delivery": "Trimite mai întâi pitch-ul la analiză (delivery)",
        "not_finished": "Această rundă nu a fost terminată",
    },
}


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
        "difficulty": r.difficulty or "easy",
        "lang": r.lang or "en",
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
            .order_by(desc(AiResult.created_at), desc(AiResult.id))
        )
        res = session.exec(stmt).first()
    else:
        with Session(engine) as s:
            stmt = (
                select(AiResult)
                .where(AiResult.round_id == round_id, AiResult.kind == kind)
                .order_by(desc(AiResult.created_at), desc(AiResult.id))
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


# Уровень сложности раунда: время на подготовку и рамки длительности питча (секунды)
LEVEL_TIMING = {"easy": (300, 60, 180), "medium": (240, 60, 180), "hard": (180, 90, 180)}


def _active_cases(session: Session, level: Optional[str] = None) -> List[Case]:
    """Темы из текущего topics.json (старые остаются в базе ради истории раундов); level — только этого уровня."""
    cases = [c for c in session.exec(select(Case)).all() if c.id in content.active_ids()]
    if not cases:
        seed_cases_from_json(session)
        cases = [c for c in session.exec(select(Case)).all() if c.id in content.active_ids()]
    if level:
        cases = [c for c in cases if content.level(c.id) == level] or cases
    return cases


def _public_case(case: Case, lang: str = "en") -> CasePublic:
    """Тема для приложения на языке интерфейса: без прикола, но с выжимкой и ссылками для подготовки."""
    summary, sources = content.reading(case.id)

    def text(field: str, english: Optional[str]) -> Optional[str]:
        return content.translated(case.id, field, english, lang)

    return CasePublic(
        id=case.id,
        title=text("title", case.title),
        brief=text("brief", case.brief),
        audience=text("audience", case.audience),
        summary=text("summary", summary),
        sources=sources,
    )


def _case_titles(session: Session, lang: str) -> dict[str, str]:
    """Названия всех тем (и старых, не из topics.json) на языке интерфейса — для истории раундов."""
    return {c.id: content.translated(c.id, "title", c.title, lang) for c in session.exec(select(Case)).all()}


def spin_case(session: Session, level: str = "easy", lang: str = "en") -> SpinResponse:
    """
    Колесо тем: возвращает случайный кейс выбранного уровня БЕЗ прикола, на языке интерфейса.
    """
    cases = _active_cases(session, level)
    chosen = random.choice(cases)
    category = content.translated(chosen.id, "category", chosen.category_title, lang)
    return SpinResponse(
        category=CategoryOut(id=chosen.category_id, title=category),
        case=_public_case(chosen, lang),
    )


def get_daily_case(session: Session, target_date: Optional[str] = None, lang: str = "en") -> DailyResponse:
    """
    Тема дня: детерминированный выбор кейса по хэшу даты (одинаковый для всех), на языке интерфейса.
    """
    if not target_date:
        target_date = date.today().isoformat()

    # тема дня одна на всех — берём из простых, чтобы она подходила любому уровню
    cases_sorted = sorted(_active_cases(session, "easy"), key=lambda c: c.id)
    date_hash = int(hashlib.md5(target_date.encode("utf-8")).hexdigest(), 16)
    chosen = cases_sorted[date_hash % len(cases_sorted)]

    return DailyResponse(date=target_date, case=_public_case(chosen, lang))


def create_round(session: Session, req: RoundCreateRequest, lang: str = "en") -> RoundCreateResponse:
    """
    Создание раунда выступления: training, daily, own, warmup.
    lang — язык интерфейса раунда (роутер уже выбрал его из req.lang и Accept-Language).
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
            raise HTTPException(status_code=429, detail=pick(ERROR_TEXTS, lang)["daily_limit"])

    round_id = f"rnd_{hashlib.md5(f'{user.id}_{datetime.now(timezone.utc).isoformat()}_{random.random()}'.encode()).hexdigest()[:12]}"

    if req.mode == "warmup":
        prep_sec = 30
        pitch_min_sec = 20
        pitch_max_sec = 45
    else:
        prep_sec, pitch_min_sec, pitch_max_sec = LEVEL_TIMING[req.difficulty]

    own_title = req.own.title if req.own else None
    own_text = req.own.text if req.own else None
    own_audience = req.own.audience if req.own else None

    case_id = req.case_id
    if req.mode == "daily" and not case_id:
        daily_info = get_daily_case(session)
        case_id = daily_info.case.id
    elif req.mode == "training" and not case_id:
        spin_info = spin_case(session, req.difficulty)
        case_id = spin_info.case.id

    round_obj = Round(
        id=round_id,
        user_id=user.id,
        mode=req.mode,
        case_id=case_id,
        own_title=own_title,
        own_text=own_text,
        own_audience=own_audience,
        difficulty=req.difficulty,
        lang=lang,
        status="created"
    )
    session.add(round_obj)
    session.commit()

    case = session.get(Case, case_id) if case_id and req.mode in ("training", "daily") else None
    return RoundCreateResponse(
        round_id=round_id,
        prep_sec=prep_sec,
        pitch_min_sec=pitch_min_sec,
        pitch_max_sec=pitch_max_sec,
        lang=lang,
        case=_public_case(case, lang) if case else None,
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


def finish_round(session: Session, round_id: str, lang: str = "en") -> RoundFinishResponse:
    """
    Завершение раунда:
    - собирает AiResult (content, delivery, jury)
    - считает итог: 40% содержание + 40% подача + 20% ответы жюри
    - пересчитывает звание и тренд
    """
    round_obj = session.get(Round, round_id)
    if not round_obj:
        raise HTTPException(status_code=404, detail=pick(ERROR_TEXTS, lang)["no_round"].format(round_id=round_id))

    ai_results = session.exec(select(AiResult).where(AiResult.round_id == round_id)).all()
    content_score, delivery_score, jury_score = _collect_ai_scores(ai_results)

    if content_score is None or delivery_score is None:
        if not settings.MOCK_FALLBACK:
            raise HTTPException(status_code=409, detail=pick(ERROR_TEXTS, lang)["no_delivery"])
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
# История и прогресс
# --------------------------------------------------------------------------

RANKS = [("Novice", 0.0), ("Speaker", 40.0), ("Pitcher", 60.0), ("Orator", 75.0), ("Legend", 88.0)]
PACE_OK = (100, 180)  # слов в минуту — тот же коридор, что в разборе подачи
# Подписи истории и прогресса — на языке интерфейса (en | ru | ro)
MODE_TITLES_BY_LANG: dict[str, dict[str, str]] = {
    "en": {"daily": "Topic of the day", "warmup": "Warm-up", "own": "Own pitch", "training": "Training"},
    "ru": {"daily": "Тема дня", "warmup": "Разминка", "own": "Свой питч", "training": "Тренировка"},
    "ro": {"daily": "Tema zilei", "warmup": "Încălzire", "own": "Pitch propriu", "training": "Antrenament"},
}
MODE_TITLES = MODE_TITLES_BY_LANG["en"]
# названия навыков и привычек и единицы: key → (название, единица)
SKILL_TITLES: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "content": ("Content", ""),
        "delivery": ("Delivery", ""),
        "jury": ("Jury answers", ""),
        "fillers_per_min": ("Filler words", "per min"),
        "wpm": ("Pace", "words/min"),
        "long_pauses": ("Long pauses", "per pitch"),
        "repeats": ("Repeats", "per pitch"),
        "weak_phrases": ("Hedging and apologies", "per pitch"),
    },
    "ru": {
        "content": ("Содержание", ""),
        "delivery": ("Подача", ""),
        "jury": ("Ответы жюри", ""),
        "fillers_per_min": ("Слова-паразиты", "в мин"),
        "wpm": ("Темп", "слов/мин"),
        "long_pauses": ("Длинные паузы", "за питч"),
        "repeats": ("Повторы", "за питч"),
        "weak_phrases": ("Неуверенные фразы", "за питч"),
    },
    "ro": {
        "content": ("Conținut", ""),
        "delivery": ("Prezentare", ""),
        "jury": ("Răspunsuri la juriu", ""),
        "fillers_per_min": ("Cuvinte de umplutură", "pe min"),
        "wpm": ("Ritm", "cuvinte/min"),
        "long_pauses": ("Pauze lungi", "pe pitch"),
        "repeats": ("Repetări", "pe pitch"),
        "weak_phrases": ("Fraze nesigure", "pe pitch"),
    },
}
# советы прогресса: key → (заголовок, текст); в тексте — подстановки str.format
INSIGHT_TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "first": ("Play your first round", "One pitch is enough to see your pace, fillers and pauses."),
        "strong": ("Your strong side: {skill}", "{value} on average over your last rounds."),
        "weak": ("Work on: {skill}", "{value} on average. {advice}"),
        "fewer_fillers": ("Fewer filler words", "Down to {value} per minute from {before}. Keep replacing them with a short pause."),
        "fillers": ("Filler words", "{value} per minute. When you feel one coming, close your mouth and breathe — silence sounds confident."),
        "fast": ("You speak too fast", "{value} words per minute; the room follows best at 120–160. Pause after every finished thought."),
        "slow": ("You speak too slowly", "{value} words per minute; aim for 120–160. Prepare your first two sentences so you start with energy."),
        "pauses": ("Long pauses mid-phrase", "About {value} per pitch. Finish the sentence first, then think about the next one."),
        "repeats": ("Repeated phrases", "About {value} per pitch. If you lose the thread, say the next point instead of restarting the sentence."),
        "weak_phrases": ("Hedging and apologies", "About {value} per pitch: \"I think\", \"maybe\", \"sorry\", \"that's it\". State it as a fact and finish with a clear last line — the room believes a speaker who believes themselves."),
        "improving": ("You are improving", "Your last three rounds average {recent}, your first three — {first}."),
    },
    "ru": {
        "first": ("Сыграй первый раунд", "Одного питча хватит, чтобы увидеть твой темп, слова-паразиты и паузы."),
        "strong": ("Твоя сильная сторона: {skill}", "{value} в среднем за последние раунды."),
        "weak": ("Над чем поработать: {skill}", "{value} в среднем. {advice}"),
        "fewer_fillers": ("Меньше слов-паразитов", "Теперь {value} в минуту вместо {before}. Продолжай заменять их короткой паузой."),
        "fillers": ("Слова-паразиты", "{value} в минуту. Когда чувствуешь, что сейчас вырвется паразит, закрой рот и вдохни — тишина звучит уверенно."),
        "fast": ("Ты говоришь слишком быстро", "{value} слов в минуту; зал лучше всего успевает за 120–160. Делай паузу после каждой законченной мысли."),
        "slow": ("Ты говоришь слишком медленно", "{value} слов в минуту; целься в 120–160. Подготовь первые два предложения, чтобы начать энергично."),
        "pauses": ("Длинные паузы посреди фразы", "Около {value} за питч. Сначала договори предложение, потом думай о следующем."),
        "repeats": ("Повторы фраз", "Около {value} за питч. Если потерял нить, переходи к следующей мысли, а не начинай предложение заново."),
        "weak_phrases": ("Неуверенные фразы", "Около {value} за питч: «мне кажется», «наверное», «извините», «вот как-то так». Говори утверждением и заканчивай чёткой последней фразой — зал верит тому, кто верит себе."),
        "improving": ("Ты растёшь", "Последние три раунда — в среднем {recent}, первые три — {first}."),
    },
    "ro": {
        "first": ("Joacă prima rundă", "Un singur pitch e de ajuns ca să-ți vezi ritmul, cuvintele de umplutură și pauzele."),
        "strong": ("Punctul tău forte: {skill}", "{value} în medie în ultimele runde."),
        "weak": ("Lucrează la: {skill}", "{value} în medie. {advice}"),
        "fewer_fillers": ("Mai puține cuvinte de umplutură", "Ai coborât la {value} pe minut de la {before}. Continuă să le înlocuiești cu o scurtă pauză."),
        "fillers": ("Cuvinte de umplutură", "{value} pe minut. Când simți că vine unul, închide gura și respiră — tăcerea sună a încredere."),
        "fast": ("Vorbești prea repede", "{value} cuvinte pe minut; sala te urmărește cel mai bine la 120–160. Fă o pauză după fiecare idee încheiată."),
        "slow": ("Vorbești prea încet", "{value} cuvinte pe minut; țintește 120–160. Pregătește primele două propoziții ca să pornești cu energie."),
        "pauses": ("Pauze lungi în mijlocul frazei", "Cam {value} pe pitch. Termină întâi propoziția, apoi gândește-te la următoarea."),
        "repeats": ("Fraze repetate", "Cam {value} pe pitch. Dacă pierzi firul, spune următoarea idee în loc să reiei propoziția."),
        "weak_phrases": ("Fraze nesigure", "Cam {value} pe pitch: „cred că”, „poate”, „scuze”, „cam asta e”. Afirmă ca pe un fapt și încheie cu o frază clară — sala îl crede pe cel care se crede pe sine."),
        "improving": ("Progresezi", "Ultimele trei runde au media {recent}, primele trei — {first}."),
    },
}
# совет к самому слабому навыку
ADVICE_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "content": "Before you speak, decide on one main point and two reasons. Say the point first.",
        "delivery": "Slow down at the start and keep a steady pace — delivery is where you lose the most points.",
        "jury": "Answer the question in the first sentence, then explain. The jury scores the first ten seconds hardest.",
    },
    "ru": {
        "content": "Перед выступлением выбери одну главную мысль и две причины. Начни с главной мысли.",
        "delivery": "Начинай медленнее и держи ровный темп — больше всего баллов ты теряешь на подаче.",
        "jury": "Отвечай на вопрос в первом же предложении, потом объясняй. Первые десять секунд жюри оценивает строже всего.",
    },
    "ro": {
        "content": "Înainte să vorbești, alege o idee principală și două argumente. Spune ideea la început.",
        "delivery": "Începe mai încet și păstrează un ritm constant — la prezentare pierzi cele mai multe puncte.",
        "jury": "Răspunde la întrebare din prima propoziție, apoi explică. Juriul punctează cel mai sever primele zece secunde.",
    },
}


def _num(value: float, digits: int, lang: str) -> str:
    """Число для текста: в русском и румынском дробная часть — через запятую."""
    text = f"{value:.{digits}f}"
    return text if lang == "en" else text.replace(".", ",")


def _delivery_payload(session: Session, round_id: str) -> Optional[dict]:
    res = session.exec(
        select(AiResult).where(AiResult.round_id == round_id).where(AiResult.kind == "delivery").order_by(desc(AiResult.id))
    ).first()
    if not res:
        return None
    try:
        return json.loads(res.payload)
    except ValueError:
        return None


def _history_round(
    session: Session, rnd: Round, sc: RoundScore, titles: dict[str, str], lang: str = "en"
) -> HistoryRound:
    """Заголовок — свой питч, название темы (titles — уже на языке интерфейса) или режим раунда."""
    payload = _delivery_payload(session, rnd.id) or {}
    metrics = payload.get("metrics") or {}
    events = payload.get("events") or []
    title = rnd.own_title or titles.get(rnd.case_id or "") or pick(MODE_TITLES_BY_LANG, lang).get(rnd.mode, rnd.mode)
    return HistoryRound(
        id=rnd.id,
        mode=rnd.mode,
        difficulty=rnd.difficulty or "easy",
        title=title,
        created_at=rnd.finished_at or rnd.created_at,
        total=sc.total_score,
        content=sc.content_score,
        delivery=sc.delivery_score,
        jury=sc.jury_score,
        duration_sec=metrics.get("duration_sec"),
        wpm=metrics.get("wpm"),
        fillers_per_min=metrics.get("fillers_per_min"),
        long_pauses=metrics.get("long_pauses"),
        repeats=sum(1 for e in events if e.get("type") == "repeat") if payload else None,
        weak_phrases=sum(1 for e in events if e.get("type") == "weak_phrase") if payload else None,
        gaze_on_ratio=metrics.get("gaze_on_ratio"),
    )


def _mean(values: list) -> Optional[float]:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _trend(key: str, rounds: List[HistoryRound], better: str = "higher", lang: str = "en") -> SkillTrend:
    """Среднее за последние 5 раундов против 5 предыдущих (rounds — от новых к старым)."""
    recent = _mean([getattr(r, key) for r in rounds[:5]])
    before = _mean([getattr(r, key) for r in rounds[5:10]])
    delta = round(recent - before, 1) if recent is not None and before is not None else None
    title, unit = pick(SKILL_TITLES, lang)[key]
    return SkillTrend(key=key, title=title, value=recent, delta=delta, better=better, unit=unit)


def _streak_days(rounds: List[HistoryRound]) -> int:
    """Сколько дней подряд есть хотя бы один раунд; серия жива, если последний раунд — сегодня или вчера."""
    days = sorted({r.created_at.date() for r in rounds}, reverse=True)
    if not days or (datetime.now(timezone.utc).date() - days[0]).days > 1:
        return 0
    streak = 1
    for newer, older in zip(days, days[1:]):
        if (newer - older).days != 1:
            break
        streak += 1
    return streak


def _insights(
    rounds: List[HistoryRound], skills: List[SkillTrend], habits: List[SkillTrend], lang: str = "en"
) -> List[Insight]:
    """Что получается и над чем работать — по цифрам последних раундов, без общих слов."""
    texts = pick(INSIGHT_TEXTS, lang)

    def insight(kind: str, key: str, **values: object) -> Insight:
        title, text = texts[key]
        return Insight(kind=kind, title=title.format(**values), text=text.format(**values))

    out: List[Insight] = []
    if not rounds:
        return [insight("focus", "first")]

    scored = [s for s in skills if s.value is not None and (s.key != "jury" or s.value > 0)]
    if len(scored) >= 2:
        best, worst = max(scored, key=lambda s: s.value), min(scored, key=lambda s: s.value)
        if best.value - worst.value >= 5:
            out.append(insight("good", "strong", skill=best.title.lower(), value=_num(best.value, 0, lang)))
            advice = pick(ADVICE_TEXTS, lang)[worst.key]
            out.append(insight("focus", "weak", skill=worst.title.lower(), value=_num(worst.value, 0, lang), advice=advice))

    by_key = {h.key: h for h in habits}
    fillers = by_key.get("fillers_per_min")
    if fillers and fillers.value is not None:
        if fillers.delta is not None and fillers.delta <= -0.5:
            before = _num(fillers.value - fillers.delta, 1, lang)
            out.append(insight("good", "fewer_fillers", value=_num(fillers.value, 1, lang), before=before))
        elif fillers.value >= 3:
            out.append(insight("focus", "fillers", value=_num(fillers.value, 1, lang)))
    pace = by_key.get("wpm")
    if pace and pace.value is not None:
        if pace.value > PACE_OK[1]:
            out.append(insight("focus", "fast", value=_num(pace.value, 0, lang)))
        elif pace.value < PACE_OK[0]:
            out.append(insight("focus", "slow", value=_num(pace.value, 0, lang)))
    pauses = by_key.get("long_pauses")
    if pauses and pauses.value is not None and pauses.value >= 1.5:
        out.append(insight("focus", "pauses", value=_num(pauses.value, 0, lang)))
    repeats = by_key.get("repeats")
    if repeats and repeats.value is not None and repeats.value >= 3:
        out.append(insight("focus", "repeats", value=_num(repeats.value, 0, lang)))
    weak = by_key.get("weak_phrases")
    if weak and weak.value is not None and weak.value >= 3:
        out.append(insight("focus", "weak_phrases", value=_num(weak.value, 0, lang)))

    if len(rounds) >= 3:
        recent, first = _mean([r.total for r in rounds[:3]]), _mean([r.total for r in rounds[-3:]])
        if recent is not None and first is not None and recent - first >= 5 and len(rounds) >= 6:
            out.append(insight("good", "improving", recent=_num(recent, 0, lang), first=_num(first, 0, lang)))
    return out[:5]


def get_progress(session: Session, user: User, lang: str = "en") -> ProgressResponse:
    """История всех раундов и трекер прогресса: баллы, привычки речи, серия дней, советы.

    lang — язык интерфейса: подписи, советы и названия тем (переводы из content/topics.<lang>.json).
    """
    rows = session.exec(
        select(Round, RoundScore)
        .where(Round.user_id == user.id)
        .where(Round.status == "finished")
        .join(RoundScore, Round.id == RoundScore.round_id)
        .order_by(desc(Round.finished_at))
        .limit(200)
    ).all()
    titles = _case_titles(session, lang)
    history = [_history_round(session, rnd, sc, titles, lang) for rnd, sc in rows]

    rank = calculate_user_rank(session, user.id)
    rank_score = _mean([r.total for r in history[:5]]) or 0.0
    upcoming = next(((title, floor) for title, floor in RANKS if floor > rank_score), None)
    skills = [
        _trend("content", history, lang=lang),
        _trend("delivery", history, lang=lang),
        _trend("jury", [r for r in history if r.mode != "warmup"], lang=lang),
    ]
    habits = [
        _trend("fillers_per_min", history, better="lower", lang=lang),
        _trend("wpm", history, better="range", lang=lang),
        _trend("long_pauses", history, better="lower", lang=lang),
        _trend("repeats", history, better="lower", lang=lang),
        _trend("weak_phrases", history, better="lower", lang=lang),
    ]
    return ProgressResponse(
        nick=user.nick,
        rank=rank,
        rank_score=rank_score,
        next_rank=NextRank(title=upcoming[0], points_needed=round(upcoming[1] - rank_score, 1)) if upcoming else None,
        rounds_total=len(history),
        minutes_total=round(sum(r.duration_sec or 0 for r in history) / 60, 1),
        average=_mean([r.total for r in history]) or 0.0,
        best=max((r.total for r in history), default=0.0),
        streak_days=_streak_days(history),
        skills=skills,
        habits=habits,
        insights=_insights(history, skills, habits, lang),
        history=history,
    )


def get_round_review(session: Session, user: User, round_id: str, lang: str = "en") -> RoundReview:
    """Разбор раунда из истории. Чужой раунд открыть нельзя."""
    rnd = session.get(Round, round_id)
    if not rnd or rnd.user_id != user.id:
        raise HTTPException(status_code=404, detail=pick(ERROR_TEXTS, lang)["no_round"].format(round_id=round_id))
    sc = session.exec(select(RoundScore).where(RoundScore.round_id == round_id)).first()
    if not sc:
        raise HTTPException(status_code=409, detail=pick(ERROR_TEXTS, lang)["not_finished"])
    titles = _case_titles(session, lang)
    results = session.exec(select(AiResult).where(AiResult.round_id == round_id).order_by(AiResult.id)).all()
    questions: list[dict] = []
    answers: dict[str, dict] = {}
    insights: dict[str, dict] = {}  # ход мысли и лучшая версия: последнее состояние каждого вида
    for res in results:
        try:
            data = json.loads(res.payload)
        except ValueError:
            continue
        if res.kind == "jury_questions":
            questions = data.get("questions", [])
        elif res.kind == "jury_answer" and data.get("question_id"):
            answers[data["question_id"]] = data  # повторный ответ на тот же вопрос заменяет прежний
        elif res.kind in ("flow", "better_version"):
            insights[res.kind] = data
    order = [q.get("id") for q in questions]
    delivery = _delivery_payload(session, round_id)
    from app.ai.jobs import review_view  # noqa: PLC0415 — app.ai импортирует app.game, наверху нельзя
    return RoundReview(
        round=_history_round(session, rnd, sc, titles, lang),
        result=RoundFinishResponse(
            total=sc.total_score,
            content=round(sc.content_score, 1),
            delivery=round(sc.delivery_score, 1),
            jury=round(sc.jury_score, 1),
            rank=calculate_user_rank(session, user.id),
        ),
        delivery=delivery,
        jury_questions=questions,
        jury_answers=sorted(answers.values(), key=lambda a: order.index(a["question_id"]) if a["question_id"] in order else 99),
        audio_url=recording_url(round_id),
        flow=review_view("flow", round_id, insights.get("flow"), delivery, lang),
        better_version=review_view("better_version", round_id, insights.get("better_version"), delivery, lang),
    )


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
            existing.level = item.get("level", "easy")
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
                level=item.get("level", "easy"),
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
