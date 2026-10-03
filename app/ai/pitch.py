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

    @property
    def audience_ru(self) -> str:
        return AUDIENCE_RU[self.audience]

    @property
    def is_own(self) -> bool:
        return self.own_text is not None


def resolve_pitch(round_id: str) -> Pitch:
    """Для своего питча — данные из раунда, иначе — из кейса через get_case."""
    rnd = game_api.get_round(round_id)
    if own := rnd.get("own"):
        return Pitch(
            title=own["title"], brief="Свой питч игрока", audience=Audience(own["audience"]), own_text=own["text"]
        )
    case = game_api.get_case(rnd["case_id"])
    return Pitch(title=case["title"], brief=case["brief"], audience=Audience(case["audience"]), quirk=case["quirk"])
