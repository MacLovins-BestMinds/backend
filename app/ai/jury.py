"""Вопросы жюри с озвучкой и оценка ответов.

POST /api/ai/rounds/{id}/jury/questions и POST /api/ai/rounds/{id}/jury/answer.
"""

import asyncio
import logging
from typing import Literal
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
        name="Marina",
        persona="Strict and a bit grumpy, but not an expert in anything. Speaks dryly and asks plain, short questions.",
        openai_voice="coral",
        voice_style="Speak English in a strict, dry, precise tone, medium pace, no smile in the voice.",
        stability=0.75,
        style=0.1,
    ),
    "kind": Juror(
        name="Boris",
        persona="Kind-hearted and easily impressed. Supportive, asks simple questions out of curiosity.",
        openai_voice="ash",
        voice_style="Speak English warmly and kindly, with a light smile, unhurried.",
        stability=0.45,
        style=0.4,
    ),
    "skeptic": Juror(
        name="Gleb",
        persona="A bit of a doubter who does not know the subject. Asks one naive 'but what if' question.",
        openai_voice="onyx",
        voice_style="Speak English with doubt and light irony, pause before the key word.",
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
    questions: list[DraftQuestion] = Field(min_length=1, max_length=6)


class AnswerAssessment(BaseModel):
    score: int = Field(ge=0, le=100)
    comment: str


Difficulty = Literal["easy", "medium", "hard"]

# Уровень сложности выбирает игрок: чем выше, тем глубже вопросы и строже оценка ответа.
QUESTION_LEVELS: dict[str, dict[str, str]] = {
    "easy": {
        "intro": "They are ordinary, not very sharp listeners — not experts, not investors. They ask easy questions that anyone could answer on the spot.",
        "rules": (
            "- Keep every question SIMPLE. Each one is about a single thing the speaker actually said, and can be answered "
            'in one or two sentences from personal experience or opinion. Good: "Why do you like it so much?", '
            '"When did you first try it?", "Would you recommend it to a friend?".\n'
            "- Never ask for numbers, prices, budgets, deadlines, metrics, proof, statistics or plans. No trick questions."
        ),
        "length": "A question is ONE short sentence of at most 15 words with simple words",
    },
    "medium": {
        "intro": "They are attentive listeners who followed the pitch closely. They ask fair questions that make the speaker explain one thing a little deeper.",
        "rules": (
            "- Each question picks one thing the speaker said or clearly left out and asks for a reason, an example or a "
            'comparison: "Why does that matter to you?", "What would you do if it went wrong?", "How is it different from…?". '
            "A one-word answer must not be enough.\n"
            "- Do not demand numbers, budgets or business plans."
        ),
        "length": "A question is ONE sentence of at most 22 words",
    },
    "hard": {
        "intro": "They are demanding, sharp jury members who test whether the speaker has really thought the idea through.",
        "rules": (
            "- Each question goes for a weak spot: something the speaker claimed without support, skipped, or got wrong. "
            "Ask for specifics — a concrete example, a number, an answer to an obvious objection, or what happens if it fails.\n"
            "- Be direct and a little uncomfortable, but keep it answerable in 30 seconds."
        ),
        "length": "A question is one or two short sentences",
    },
}
ANSWER_LEVELS: dict[str, str] = {
    "easy": (
        "You are an easy-going listener, not an examiner. Give a `score` from 0 to 100:\n"
        "- 85–100 — the speaker answered the question in their own words, even briefly. One clear sentence with a reason or an example is already a great answer.\n"
        "- 65–84 — they answered, but vaguely or after wandering a little.\n"
        "- 40–64 — they talked about the topic but did not really answer the question.\n"
        "- 0–39 — no answer, silence, or something unrelated.\n\n"
        "Do not ask for numbers, facts or proof, and do not lower the score for a short or simple answer."
    ),
    "medium": (
        "You are a fair but attentive listener. Give a `score` from 0 to 100:\n"
        "- 85–100 — a direct answer backed by a reason or an example.\n"
        "- 65–84 — a direct answer with no real support, or support that arrives after wandering.\n"
        "- 40–64 — the speaker talks around the question.\n"
        "- 0–39 — no answer, silence, or something unrelated.\n\n"
        "Numbers are not required, but a bare yes/no or a single unsupported sentence cannot score above 70."
    ),
    "hard": (
        "You are a demanding examiner. Give a `score` from 0 to 100:\n"
        "- 85–100 — the answer comes in the first sentence, is supported by a concrete fact, number or example, and nothing is off topic.\n"
        "- 65–84 — a direct answer, but the support is vague or generic.\n"
        "- 40–64 — a partial answer, or one that avoids the hard part of the question.\n"
        "- 0–39 — a dodge, silence, or something unrelated.\n\n"
        "A short unsupported answer cannot score above 55. Filler, restarts and wandering lower the score."
    ),
}


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
        return f"- The sceptic's question is built on this tricky angle (rephrase it to fit what was said): {pitch.quirk}"
    return "- The sceptic's question is about the weakest spot of the pitch from this audience's point of view."


AUDIENCE_FALLBACK_QUESTIONS: dict[Audience, list[DraftQuestion]] = {
    Audience.CONTEST_JURY: [
        DraftQuestion(
            juror="strict",
            text="What is your launch timeline, and what budget do you need to build this?",
        ),
        DraftQuestion(
            juror="kind",
            text="Tell us more: what real pain does your project solve for people?",
        ),
        DraftQuestion(
            juror="skeptic",
            text="What makes you different from existing solutions, and why can't someone copy you in a couple of months?",
        ),
    ],
    Audience.BUSINESS: [
        DraftQuestion(
            juror="strict",
            text="What are your unit economics, and when do you break even?",
        ),
        DraftQuestion(juror="kind", text="Who is your first paying customer, and why would they choose you?"),
        DraftQuestion(
            juror="skeptic",
            text="The market is crowded. How will you acquire customers cheaper than your competitors?",
        ),
    ],
    Audience.TEACHERS: [
        DraftQuestion(
            juror="strict",
            text="What research or data is your approach based on?",
        ),
        DraftQuestion(juror="kind", text="How will your project make students more engaged?"),
        DraftQuestion(juror="skeptic", text="What are the long-term risks of using your approach in education?"),
    ],
    Audience.PUBLIC: [
        DraftQuestion(
            juror="strict",
            text="In simple words: how much will this cost an ordinary user?",
        ),
        DraftQuestion(
            juror="kind",
            text="Why would an ordinary person want to use your product every day?",
        ),
        DraftQuestion(
            juror="skeptic",
            text="Isn't this problem made up? People seem to manage fine without it.",
        ),
    ],
}


async def _draft(pitch: Pitch, transcript: str, difficulty: str = "easy") -> DraftQuestions:
    level = QUESTION_LEVELS.get(difficulty, QUESTION_LEVELS["easy"])
    try:
        return await llm.generate(
            "jury_questions",
            DraftQuestions,
            title=pitch.title,
            brief=pitch.brief,
            audience=pitch.audience_ru,
            audience_focus=AUDIENCE_FOCUS[pitch.audience],
            own_text=f'- Prepared text of the speaker:\n"""\n{pitch.own_text}\n"""' if pitch.is_own else "",
            transcript=transcript or "(the speaker said nothing)",
            jurors="\n".join(f"- `{jid}` — {j.name}. {j.persona}" for jid, j in JURORS.items()),
            quirk_rule=_quirk_rule(pitch),
            level_intro=level["intro"],
            level_rules=level["rules"],
            level_length=level["length"],
        )
    except (OpenAIError, genai_errors.APIError) as e:
        logger.warning("_draft: сбой LLM (%s), запасные вопросы для аудитории %s", e, pitch.audience)
        return fallback_questions(pitch)


def fallback_questions(pitch: Pitch) -> DraftQuestions:
    """Заготовленные английские вопросы по аудитории.

    Прикол кейса записан по-русски, а без LLM его не перевести, поэтому скептик задаёт заготовленный вопрос.
    """
    return DraftQuestions(questions=list(AUDIENCE_FALLBACK_QUESTIONS[pitch.audience]))


def one_per_juror(draft: DraftQuestions, pitch: Pitch) -> DraftQuestions:
    """Ровно три вопроса — по одному от каждого члена жюри, в порядке стола: строгий, добрый, скептик.

    Лишние вопросы одного члена жюри отбрасываются; если кто-то промолчал, берётся его заготовленный вопрос.
    """
    spare = {q.juror: q for q in AUDIENCE_FALLBACK_QUESTIONS[pitch.audience]}
    asked: dict[JurorId, DraftQuestion] = {}
    for q in draft.questions:
        asked.setdefault(q.juror, q)
    return DraftQuestions(questions=[asked.get(juror_id, spare[juror_id]) for juror_id in JURORS])


async def _voice(round_id: str, question_id: str, juror: JurorId, text: str) -> None:
    mp3 = await synthesize(text, voice_for(juror))
    path = _audio_dir(round_id) / f"{question_id}.mp3"
    await asyncio.to_thread(path.write_bytes, mp3)


async def run_jury_questions(round_id: str, difficulty: str | None = None) -> JuryQuestionsResponse:
    # повторный вызов (переподключение, показ) отдаёт уже готовые вопросы без новых затрат
    if cached := await asyncio.to_thread(game_api.get_ai_result, round_id, "jury_questions"):
        return JuryQuestionsResponse.model_validate(cached)

    pitch = await asyncio.to_thread(resolve_pitch, round_id)
    if pitch.is_warmup:
        raise RoundStateError("The warm-up has no jury questions")
    delivery = await asyncio.to_thread(game_api.get_ai_result, round_id, "delivery")
    if delivery is None:
        raise RoundStateError("Send the pitch for review (delivery) first")
    # уровень задан раундом; параметр запроса нужен только чтобы переопределить его
    draft = one_per_juror(await _draft(pitch, delivery["transcript"], difficulty or pitch.difficulty), pitch)

    questions = [
        JuryQuestion(id=f"q{i}", juror=q.juror, text=q.text, audio_url=_audio_url(round_id, f"q{i}"))
        for i, q in enumerate(draft.questions, start=1)
    ]
    _audio_dir(round_id).mkdir(parents=True, exist_ok=True)
    # озвучки идут через слоты ElevenLabs (app/ai/limits.py): при одном коротком слоте — по очереди, не тремя сразу
    await asyncio.gather(*(_voice(round_id, q.id, q.juror, q.text) for q in questions))

    response = JuryQuestionsResponse(questions=questions)
    await asyncio.to_thread(game_api.save_ai_result, round_id, "jury_questions", response.model_dump(mode="json"))
    return response


async def run_jury_answer(
    round_id: str, question_id: str, audio: bytes, difficulty: str | None = None
) -> JuryAnswerResponse:
    saved = await asyncio.to_thread(game_api.get_ai_result, round_id, "jury_questions")
    if saved is None:
        raise RoundStateError("Request the jury questions first")
    question = next((q for q in JuryQuestionsResponse.model_validate(saved).questions if q.id == question_id), None)
    if question is None:
        raise KeyError(question_id)

    answer = (await transcribe(await to_wav16k(audio))).text
    if not answer:
        result = JuryAnswerResponse(score=0, comment="We didn't hear an answer.")
    else:
        pitch = await asyncio.to_thread(resolve_pitch, round_id)
        juror = JURORS[question.juror]
        # без LLM честной оценки нет: ошибка уходит клиенту (502), повтор берёт распознавание из кэша
        assessment = await llm.generate(
            "jury_answer",
            AnswerAssessment,
            juror_name=juror.name,
            juror_persona=juror.persona,
            title=pitch.title,
            audience=pitch.audience_ru,
            question=question.text,
            answer=answer,
            level_scoring=ANSWER_LEVELS.get(difficulty or pitch.difficulty, ANSWER_LEVELS["easy"]),
        )
        result = JuryAnswerResponse(score=assessment.score, comment=assessment.comment)

    payload = {"question_id": question_id, "answer": answer, **result.model_dump(mode="json")}
    await asyncio.to_thread(game_api.save_ai_result, round_id, "jury_answer", payload)
    return result
