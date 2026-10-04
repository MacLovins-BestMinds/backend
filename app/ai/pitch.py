"""Что питчит игрок в раунде: тема, бриф, аудитория, свой текст и прикол кейса."""

from dataclasses import dataclass

from app.ai import game_api
from app.ai.schemas import Audience

AUDIENCE_RU = {
    Audience.CONTEST_JURY: "contest jury",
    Audience.BUSINESS: "business people",
    Audience.TEACHERS: "teachers",
    Audience.PUBLIC: "general public",
}

# что каждая аудитория спрашивает у спикера (docs/tz, «Вопросы жюри»)
AUDIENCE_FOCUS = {
    Audience.CONTEST_JURY: "novelty of the idea and feasibility",
    Audience.BUSINESS: "money, business model and payback",
    Audience.TEACHERS: "sound reasoning, evidence and consequences",
    Audience.PUBLIC: "benefit for an ordinary person and simplicity",
}


@dataclass(frozen=True, slots=True)
class Pitch:
    title: str
    brief: str
    audience: Audience
    own_text: str | None = None
    quirk: str | None = None  # только для вопросов жюри, в приложение до вопросов не уходит
    is_warmup: bool = False
    min_sec: int = 60
    max_sec: int = 180
    difficulty: str = "easy"  # уровень раунда: от него зависят строгость разбора и жюри

    @property
    def audience_ru(self) -> str:
        return AUDIENCE_RU[self.audience]

    @property
    def is_own(self) -> bool:
        return self.own_text is not None


# минимальная длительность питча по уровням — как LEVEL_TIMING игрового движка
MIN_SEC_BY_LEVEL = {"easy": 60, "medium": 60, "hard": 90}


class RoundNotFoundError(LookupError):
    """Раунда (или его кейса) нет в игровой базе."""


# Разминка «Представься залу»: без кейса и без жюри. Лимиты — как в create_round игрового движка.
WARMUP = Pitch(
    title="Introduce yourself to the audience",
    brief="In 30 seconds, say who you are, what you do and what people should remember you by.",
    audience=Audience.PUBLIC,
    is_warmup=True,
    min_sec=20,
    max_sec=45,
)


def _normalize_audience(raw: str | Audience) -> Audience:
    if isinstance(raw, Audience):
        return raw
    ru_map = {v: k for k, v in AUDIENCE_RU.items()}
    if raw in ru_map:
        return ru_map[raw]
    return Audience(raw)


def resolve_pitch(round_id: str) -> Pitch:
    """Разминка — фиксированное задание; свой питч — данные из раунда; иначе — кейс через get_case."""
    rnd = game_api.get_round(round_id)
    if rnd is None:
        raise RoundNotFoundError(f"Round {round_id} not found")
    if rnd.get("mode") == "warmup":
        return WARMUP
    difficulty = rnd.get("difficulty") or "easy"
    min_sec = MIN_SEC_BY_LEVEL.get(difficulty, 60)
    if own := rnd.get("own"):
        return Pitch(
            title=own["title"],
            brief="The player's own pitch",
            audience=_normalize_audience(own["audience"]),
            own_text=own["text"],
            difficulty=difficulty,
            min_sec=min_sec,
        )
    case = game_api.get_case(rnd["case_id"]) if rnd.get("case_id") else None
    if case is None:
        raise RoundNotFoundError(f"Round {round_id} has no topic")
    return Pitch(
        title=case["title"],
        brief=case["brief"],
        audience=_normalize_audience(case["audience"]),
        quirk=case["quirk"],
        difficulty=difficulty,
        min_sec=min_sec,
    )
