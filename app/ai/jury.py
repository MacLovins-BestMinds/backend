"""Вопросы жюри с озвучкой и оценка ответов.

POST /api/ai/rounds/{id}/jury/questions и POST /api/ai/rounds/{id}/jury/answer.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from app.ai import game_api, llm
from app.ai.audio import to_wav16k
from app.ai.config import get_settings
from app.ai.pitch import AUDIENCE_FOCUS, Pitch, resolve_pitch
from app.ai.schemas import JurorId, JuryAnswerResponse, JuryQuestion, JuryQuestionsResponse
from app.ai.stt import transcribe
from app.ai.tts import synthesize


@dataclass(frozen=True, slots=True)
class Juror:
    name: str
    persona: str
    voice: str
    voice_style: str


# Черновые характеры; финальные даёт универсал (docs/tz/universal.md)
JURORS: dict[JurorId, Juror] = {
    "strict": Juror(
        name="Марина Викторовна",
        persona="Строгая, бывший директор акселератора. Говорит сухо и по делу, требует цифр и сроков.",
        voice="coral",
        voice_style="Говори по-русски строго и сухо, чётко, в среднем темпе, без улыбки в голосе.",
    ),
    "kind": Juror(
        name="Борис",
        persona="Добряк, предприниматель. Поддерживает, но спрашивает о людях, которым продукт поможет.",
        voice="ash",
        voice_style="Говори по-русски тепло и дружелюбно, с лёгкой улыбкой, неторопливо.",
    ),
    "skeptic": Juror(
        name="Глеб",
        persona="Скептик, инвестор. Сомневается во всём и ищет слабое место идеи.",
        voice="onyx",
        voice_style="Говори по-русски с недоверием и лёгкой иронией, делай паузу перед главным словом.",
    ),
}


class MissingResultError(LookupError):
    """Нужный предыдущий шаг раунда ещё не выполнен (например, нет delivery)."""


class DraftQuestion(BaseModel):
    juror: JurorId
    text: str


class DraftQuestions(BaseModel):
    questions: list[DraftQuestion] = Field(min_length=2, max_length=3)


class AnswerAssessment(BaseModel):
    score: int = Field(ge=0, le=100)
    comment: str


def _audio_dir(round_id: str) -> Path:
    return Path(get_settings().static_dir) / "jury" / round_id


def _audio_url(round_id: str, question_id: str) -> str:
    return f"/static/jury/{round_id}/{question_id}.mp3"


def _quirk_rule(pitch: Pitch) -> str:
    if pitch.quirk:
        return f"- Первый вопрос задаёт скептик, и он строится на этом каверзном углу (перефразируй под сказанное): «{pitch.quirk}»"
    return "- Первый вопрос — о самом слабом месте питча с точки зрения этой аудитории."


async def _draft(pitch: Pitch, transcript: str) -> DraftQuestions:
    return await llm.generate(
        "jury_questions",
        DraftQuestions,
        title=pitch.title,
        brief=pitch.brief,
        audience=pitch.audience_ru,
        audience_focus=AUDIENCE_FOCUS[pitch.audience],
        own_text=f'- Подготовленный текст спикера:\n"""\n{pitch.own_text}\n"""' if pitch.is_own else "",
        transcript=transcript or "(спикер ничего не сказал)",
        jurors="\n".join(f"- `{jid}` — {j.name}. {j.persona}" for jid, j in JURORS.items()),
        quirk_rule=_quirk_rule(pitch),
    )


async def _voice(round_id: str, question_id: str, juror: JurorId, text: str) -> None:
    j = JURORS[juror]
    mp3 = await synthesize(text, j.voice, j.voice_style)
    path = _audio_dir(round_id) / f"{question_id}.mp3"
    await asyncio.to_thread(path.write_bytes, mp3)


async def run_jury_questions(round_id: str) -> JuryQuestionsResponse:
    # повторный вызов (переподключение, показ) отдаёт уже готовые вопросы без новых затрат
    if cached := await asyncio.to_thread(game_api.get_ai_result, round_id, "jury_questions"):
        return JuryQuestionsResponse.model_validate(cached)

    delivery = await asyncio.to_thread(game_api.get_ai_result, round_id, "delivery")
    if delivery is None:
        raise MissingResultError("Сначала отправьте выступление в delivery")
    pitch = await asyncio.to_thread(resolve_pitch, round_id)
    draft = await _draft(pitch, delivery["transcript"])

    questions = [
        JuryQuestion(id=f"q{i}", juror=q.juror, text=q.text, audio_url=_audio_url(round_id, f"q{i}"))
        for i, q in enumerate(draft.questions, start=1)
    ]
    _audio_dir(round_id).mkdir(parents=True, exist_ok=True)
    await asyncio.gather(*(_voice(round_id, q.id, q.juror, q.text) for q in questions))

    response = JuryQuestionsResponse(questions=questions)
    await asyncio.to_thread(game_api.save_ai_result, round_id, "jury_questions", response.model_dump(mode="json"))
    return response


async def run_jury_answer(round_id: str, question_id: str, audio: bytes) -> JuryAnswerResponse:
    saved = await asyncio.to_thread(game_api.get_ai_result, round_id, "jury_questions")
    if saved is None:
        raise MissingResultError("Сначала запросите вопросы жюри")
    question = next((q for q in JuryQuestionsResponse.model_validate(saved).questions if q.id == question_id), None)
    if question is None:
        raise KeyError(question_id)

    answer = (await transcribe(await to_wav16k(audio))).text
    if not answer:
        result = JuryAnswerResponse(score=0, comment="Ответа не прозвучало.")
    else:
        pitch = await asyncio.to_thread(resolve_pitch, round_id)
        juror = JURORS[question.juror]
        assessment = await llm.generate(
            "jury_answer",
            AnswerAssessment,
            juror_name=juror.name,
            juror_persona=juror.persona,
            title=pitch.title,
            audience=pitch.audience_ru,
            question=question.text,
            answer=answer,
        )
        result = JuryAnswerResponse(score=assessment.score, comment=assessment.comment)

    payload = {"question_id": question_id, "answer": answer, **result.model_dump(mode="json")}
    await asyncio.to_thread(game_api.save_ai_result, round_id, "jury_answer", payload)
    return result
