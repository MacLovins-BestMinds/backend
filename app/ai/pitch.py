"""Что питчит игрок в раунде: тема, бриф, аудитория, свой текст и прикол кейса."""

from dataclasses import dataclass

from app.ai import game_api
from app.ai.schemas import Audience

AUDIENCE_RU = {
    Audience.CONTEST_JURY: "жюри конкурса",
    Audience.BUSINESS: "бизнесмены",
    Audience.TEACHERS: "преподаватели",
    Audience.PUBLIC: "широкая публика",
}

# что каждая аудитория спрашивает у спикера (docs/tz, «Вопросы жюри»)
AUDIENCE_FOCUS = {
    Audience.CONTEST_JURY: "новизна идеи и реализуемость",
    Audience.BUSINESS: "деньги, бизнес-модель и окупаемость",
    Audience.TEACHERS: "обоснованность, доказательства и последствия",
    Audience.PUBLIC: "польза для обычного человека и простота",
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

    @property
    def audience_ru(self) -> str:
        return AUDIENCE_RU[self.audience]

    @property
    def is_own(self) -> bool:
        return self.own_text is not None


# Разминка «Представься залу»: 30 секунд, без кейса и без жюри
WARMUP = Pitch(
    title="Представься залу",
    brief="За 30 секунд расскажи, кто ты, чем занимаешься и чем тебя запомнить.",
    audience=Audience.PUBLIC,
    is_warmup=True,
    min_sec=20,
    max_sec=40,
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
    if rnd.get("mode") == "warmup":
        return WARMUP
    if own := rnd.get("own"):
        return Pitch(
            title=own["title"], brief="Свой питч игрока", audience=_normalize_audience(own["audience"]), own_text=own["text"]
        )
    case = game_api.get_case(rnd["case_id"])
    return Pitch(title=case["title"], brief=case["brief"], audience=_normalize_audience(case["audience"]), quirk=case["quirk"])
