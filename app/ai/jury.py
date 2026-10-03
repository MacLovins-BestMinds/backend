"""Вопросы жюри с озвучкой и оценка ответов.

POST /api/ai/rounds/{id}/jury/questions и POST /api/ai/rounds/{id}/jury/answer.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from google.genai import errors as genai_errors
from openai import OpenAIError
from pydantic import BaseModel, Field

from app.ai import game_api, llm
from app.ai.audio import to_wav16k
from app.ai.config import get_settings
from app.ai.pitch import AUDIENCE_FOCUS, Pitch, resolve_pitch
from app.ai.schemas import Audience, JurorId, JuryAnswerResponse, JuryQuestion, JuryQuestionsResponse
from app.ai.stt import transcribe
from app.ai.tts import Voice, synthesize

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Juror:
    name: str
    persona: str
    openai_voice: str
    voice_style: str  # инструкция тона для OpenAI TTS
    stability: float  # тон для ElevenLabs: ниже — живее, выше — ровнее
    style: float


# Черновые характеры; финальные даёт универсал (docs/tz/universal.md)
JURORS: dict[JurorId, Juror] = {
    "strict": Juror(
        name="Марина Викторовна",
        persona="Строгая, бывший директор акселератора. Говорит сухо и по делу, требует цифр и сроков.",
        openai_voice="coral",
        voice_style="Говори по-русски строго и сухо, чётко, в среднем темпе, без улыбки в голосе.",
        stability=0.75,
        style=0.1,
    ),
    "kind": Juror(
        name="Борис",
        persona="Добряк, предприниматель. Поддерживает, но спрашивает о людях, которым продукт поможет.",
        openai_voice="ash",
        voice_style="Говори по-русски тепло и дружелюбно, с лёгкой улыбкой, неторопливо.",
        stability=0.45,
        style=0.4,
    ),
    "skeptic": Juror(
        name="Глеб",
        persona="Скептик, инвестор. Сомневается во всём и ищет слабое место идеи.",
        openai_voice="onyx",
        voice_style="Говори по-русски с недоверием и лёгкой иронией, делай паузу перед главным словом.",
        stability=0.35,
        style=0.6,
    ),
}


class RoundStateError(RuntimeError):
    """Действие невозможно в текущем состоянии раунда: нет delivery, нет вопросов, разминка без жюри."""


class DraftQuestion(BaseModel):
    juror: JurorId
    text: str


class DraftQuestions(BaseModel):
    questions: list[DraftQuestion] = Field(min_length=2, max_length=3)


class AnswerAssessment(BaseModel):
    score: int = Field(ge=0, le=100)
    comment: str


def voice_for(juror_id: JurorId) -> Voice:
    """Голос ElevenLabs: свой у члена жюри (ELEVENLABS_VOICE_ID_<ID>) или общий ELEVENLABS_VOICE_ID."""
    s = get_settings()
    j = JURORS[juror_id]
    own_voice_id = getattr(s, f"elevenlabs_voice_id_{juror_id}")
    return Voice(
        openai_voice=j.openai_voice,
        openai_instructions=j.voice_style,
        elevenlabs_voice_id=own_voice_id or s.elevenlabs_voice_id,
        stability=j.stability,
        style=j.style,
    )


def _audio_dir(round_id: str) -> Path:
    return Path(get_settings().static_dir) / "jury" / round_id


def _audio_url(round_id: str, question_id: str) -> str:
    return f"/static/jury/{round_id}/{question_id}.mp3"


def _quirk_rule(pitch: Pitch) -> str:
    if pitch.quirk:
        return f"- Первый вопрос задаёт скептик, и он строится на этом каверзном углу (перефразируй под сказанное): «{pitch.quirk}»"
    return "- Первый вопрос — о самом слабом месте питча с точки зрения этой аудитории."


AUDIENCE_FALLBACK_QUESTIONS: dict[Audience, list[DraftQuestion]] = {
    Audience.CONTEST_JURY: [
        DraftQuestion(juror="strict", text="Марина Викторовна у микрофона. Назовите конкретные сроки запуска и бюджет, за который вы планируете это реализовать?"),
        DraftQuestion(juror="kind", text="Борис на связи. Расскажите подробнее: какую главную боль реальных людей решает ваш проект?"),
        DraftQuestion(juror="skeptic", text="В чём ваше ключевое инновационное отличие от существующих решений и почему вас нельзя повторить за пару месяцев?"),
    ],
    Audience.BUSINESS: [
        DraftQuestion(juror="strict", text="Марина Викторовна у микрофона. Какова плановая экономика единицы и когда вы выйдете на операционную окупаемость?"),
        DraftQuestion(juror="kind", text="Борис на связи. Кто ваш первый платящий клиент и почему он выберет именно вас?"),
        DraftQuestion(juror="skeptic", text="Рынок переполнен предложениями. За счёт каких каналов вы рассчитываете привлекать клиентов дешевле конкурентов?"),
    ],
    Audience.TEACHERS: [
        DraftQuestion(juror="strict", text="Марина Викторовна у микрофона. На каких проверенных исследованиях или данных строится ваша методология?"),
        DraftQuestion(juror="kind", text="Борис на связи. Как ваш проект поможет повысить вовлечённость и интерес учащихся?"),
        DraftQuestion(juror="skeptic", text="Каковы долгосрочные риски применения вашего подхода в образовательном процессе?"),
    ],
    Audience.PUBLIC: [
        DraftQuestion(juror="strict", text="Марина Викторовна у микрофона. Объясните простыми словами: сколько это будет стоить для конечного пользователя?"),
        DraftQuestion(juror="kind", text="Борис на связи. Почему обычному человеку захочется пользоваться вашим продуктом каждый день?"),
        DraftQuestion(juror="skeptic", text="Не кажется ли вам, что эта проблема надумана и люди отлично справляются без этого решения?"),
    ],
}


async def _draft(pitch: Pitch, transcript: str) -> DraftQuestions:
    try:
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
    except (OpenAIError, genai_errors.APIError) as e:
        logger.warning("_draft: сбой LLM (%s), используем резервные вопросы для аудитории %s", e, pitch.audience)
        return DraftQuestions(
            questions=AUDIENCE_FALLBACK_QUESTIONS.get(
                pitch.audience, AUDIENCE_FALLBACK_QUESTIONS[Audience.CONTEST_JURY]
            )
        )


async def _voice(round_id: str, question_id: str, juror: JurorId, text: str) -> None:
    mp3 = await synthesize(text, voice_for(juror))
    path = _audio_dir(round_id) / f"{question_id}.mp3"
    await asyncio.to_thread(path.write_bytes, mp3)


async def run_jury_questions(round_id: str) -> JuryQuestionsResponse:
    # повторный вызов (переподключение, показ) отдаёт уже готовые вопросы без новых затрат
    if cached := await asyncio.to_thread(game_api.get_ai_result, round_id, "jury_questions"):
        return JuryQuestionsResponse.model_validate(cached)

    pitch = await asyncio.to_thread(resolve_pitch, round_id)
    if pitch.is_warmup:
        raise RoundStateError("В разминке нет вопросов жюри")
    delivery = await asyncio.to_thread(game_api.get_ai_result, round_id, "delivery")
    if delivery is None:
        raise RoundStateError("Сначала отправьте выступление в delivery")
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
        raise RoundStateError("Сначала запросите вопросы жюри")
    question = next((q for q in JuryQuestionsResponse.model_validate(saved).questions if q.id == question_id), None)
    if question is None:
        raise KeyError(question_id)

    answer = (await transcribe(await to_wav16k(audio))).text
    if not answer:
        result = JuryAnswerResponse(score=0, comment="Ответа не прозвучало.")
    else:
        pitch = await asyncio.to_thread(resolve_pitch, round_id)
        juror = JURORS[question.juror]
        try:
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
        except (OpenAIError, genai_errors.APIError) as e:
            logger.warning("run_jury_answer: сбой LLM (%s), используем резервную оценку", e)
            result = JuryAnswerResponse(score=80, comment="Ответ принят. Аргументация понятна, продолжайте уверенно защищать свой проект.")

    payload = {"question_id": question_id, "answer": answer, **result.model_dump(mode="json")}
    await asyncio.to_thread(game_api.save_ai_result, round_id, "jury_answer", payload)
    return result
