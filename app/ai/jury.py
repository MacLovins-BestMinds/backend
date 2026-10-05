"""Вопросы жюри с озвучкой и оценка ответов.

POST /api/ai/rounds/{id}/jury/questions и POST /api/ai/rounds/{id}/jury/answer.
Всё — на языке речи игрока: вопросы и голос жюри — на языке питча (speech_lang из разбора), оценка ответа
и комментарий — на языке ответа. Язык интерфейса здесь только для текстов ошибок.
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
from app.ai.stt import speech_lang, transcribe
from app.ai.tts import Voice, synthesize
from app.core.lang import default_lang, language_name, normalize_lang, pick

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Juror:
    name: str
    persona: str
    openai_voice: str
    voice_style: str  # инструкция тона для OpenAI TTS
    stability: float  # тон для ElevenLabs: ниже — живее, выше — ровнее
    style: float


# Черновые характеры; финальные даёт универсал (docs/tz/universal.md). Пол — как на рисунках жюри и у голосов:
# в русском и румынском от него зависят формы («я бы хотел» / «хотела»)
JURORS: dict[JurorId, Juror] = {
    "strict": Juror(
        name="Boris",
        persona="A man. Strict and a bit grumpy, but not an expert in anything. Speaks dryly and asks plain, short questions.",
        openai_voice="coral",
        voice_style="Speak English in a strict, dry, precise tone, medium pace, no smile in the voice.",
        stability=0.75,
        style=0.1,
    ),
    "kind": Juror(
        name="Marina",
        persona="A woman. Kind-hearted and easily impressed. Supportive, asks simple questions out of curiosity.",
        openai_voice="ash",
        voice_style="Speak English warmly and kindly, with a light smile, unhurried.",
        stability=0.45,
        style=0.4,
    ),
    "skeptic": Juror(
        name="Gleb",
        persona="A man. A bit of a doubter who does not know the subject. Asks one naive 'but what if' question.",
        openai_voice="onyx",
        voice_style="Speak English with doubt and light irony, pause before the key word.",
        stability=0.35,
        style=0.6,
    ),
}


# key → текст ошибки на языке интерфейса (роутер отдаёт его в 409)
ROUND_STATE_ERRORS: dict[str, dict[str, str]] = {
    "en": {
        "warmup": "The warm-up has no jury questions",
        "no_delivery": "Send the pitch for review (delivery) first",
        "no_questions": "Request the jury questions first",
    },
    "ru": {
        "warmup": "В разминке нет вопросов жюри",
        "no_delivery": "Сначала отправь питч на разбор (delivery)",
        "no_questions": "Сначала запроси вопросы жюри",
    },
    "ro": {
        "warmup": "Încălzirea nu are întrebări de la juriu",
        "no_delivery": "Trimite mai întâi pitch-ul la analiză (delivery)",
        "no_questions": "Cere mai întâi întrebările juriului",
    },
}


class RoundStateError(RuntimeError):
    """Действие невозможно в текущем состоянии раунда: нет delivery, нет вопросов, разминка без жюри."""

    def __init__(self, key: str) -> None:
        super().__init__(ROUND_STATE_ERRORS["en"][key])
        self.key = key

    def message(self, lang: str) -> str:
        return pick(ROUND_STATE_ERRORS, lang)[self.key]


# ответы, которые жюри даёт без LLM, — на языке речи игрока
ANSWER_TEXTS: dict[str, dict[str, str]] = {
    "en": {"silence": "We didn't hear an answer.", "skipped": "Skipped — no points for this question."},
    "ru": {"silence": "Мы не услышали ответа.", "skipped": "Пропущено — за этот вопрос баллов нет."},
    "ro": {"silence": "Nu am auzit niciun răspuns.", "skipped": "Ai sărit peste — nu primești puncte pentru întrebare."},
}


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


def voice_for(juror_id: JurorId, lang: str = "en") -> Voice:
    """Голос ElevenLabs по языку речи: носитель языка (ELEVENLABS_VOICE_ID_<ID>_<LANG>, есть у ru и ro),
    иначе свой голос члена жюри (ELEVENLABS_VOICE_ID_<ID>), иначе общий ELEVENLABS_VOICE_ID.

    lang — язык речи: для OpenAI TTS он попадает в инструкцию тона («Speak Russian in a strict…»).
    """
    s = get_settings()
    j = JURORS[juror_id]
    native_voice_id = getattr(s, f"elevenlabs_voice_id_{juror_id}_{normalize_lang(lang)}", "")
    own_voice_id = getattr(s, f"elevenlabs_voice_id_{juror_id}")
    return Voice(
        openai_voice=j.openai_voice,
        openai_instructions=j.voice_style.replace("English", language_name(lang)),
        elevenlabs_voice_id=native_voice_id or own_voice_id or s.elevenlabs_voice_id,
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


# Заготовленные вопросы на русском и румынском — для игроков, которые питчат на этих языках
_FALLBACK_TEXTS: dict[str, dict[Audience, tuple[str, str, str]]] = {
    "ru": {
        Audience.CONTEST_JURY: (
            "Какие у вас сроки запуска и какой бюджет нужен, чтобы это сделать?",
            "Расскажите подробнее: какую настоящую проблему людей решает ваш проект?",
            "Чем вы отличаетесь от существующих решений и почему вас не скопируют за пару месяцев?",
        ),
        Audience.BUSINESS: (
            "Какая у вас юнит-экономика и когда вы выйдете на окупаемость?",
            "Кто ваш первый платящий клиент и почему он выберет именно вас?",
            "Рынок переполнен. Как вы будете привлекать клиентов дешевле конкурентов?",
        ),
        Audience.TEACHERS: (
            "На каких исследованиях или данных основан ваш подход?",
            "Как ваш проект поможет ученикам больше вовлекаться?",
            "Какие долгосрочные риски у вашего подхода в образовании?",
        ),
        Audience.PUBLIC: (
            "Простыми словами: сколько это будет стоить обычному человеку?",
            "Зачем обычному человеку пользоваться вашим продуктом каждый день?",
            "А не надуманная ли это проблема? Люди вроде и так неплохо справляются.",
        ),
    },
    "ro": {
        Audience.CONTEST_JURY: (
            "Care este termenul de lansare și ce buget vă trebuie ca să construiți asta?",
            "Spuneți-ne mai multe: ce problemă reală a oamenilor rezolvă proiectul vostru?",
            "Prin ce vă deosebiți de soluțiile existente și de ce nu vă poate copia cineva în câteva luni?",
        ),
        Audience.BUSINESS: (
            "Care este economia pe unitate și când vă recuperați investiția?",
            "Cine este primul vostru client plătitor și de ce v-ar alege pe voi?",
            "Piața e aglomerată. Cum veți atrage clienți mai ieftin decât concurenții?",
        ),
        Audience.TEACHERS: (
            "Pe ce cercetări sau date se bazează abordarea voastră?",
            "Cum îi va face proiectul vostru pe elevi mai implicați?",
            "Care sunt riscurile pe termen lung ale abordării voastre în educație?",
        ),
        Audience.PUBLIC: (
            "Pe scurt: cât o să coste asta pentru un om obișnuit?",
            "De ce ar vrea un om obișnuit să folosească produsul vostru în fiecare zi?",
            "Nu e o problemă inventată? Oamenii par să se descurce bine și fără asta.",
        ),
    },
}
FALLBACK_QUESTIONS: dict[str, dict[Audience, list[DraftQuestion]]] = {
    "en": AUDIENCE_FALLBACK_QUESTIONS,
    **{
        lang: {
            audience: [DraftQuestion(juror=juror, text=text) for juror, text in zip(JURORS, texts, strict=True)]
            for audience, texts in by_audience.items()
        }
        for lang, by_audience in _FALLBACK_TEXTS.items()
    },
}


async def _draft(pitch: Pitch, transcript: str, difficulty: str = "easy", lang: str = "en") -> DraftQuestions:
    """lang — язык речи игрока: на нём вопросы, их озвучит голос жюри."""
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
            speech_language=language_name(lang),
        )
    except (OpenAIError, genai_errors.APIError) as e:
        logger.warning("_draft: сбой LLM (%s), запасные вопросы для аудитории %s", e, pitch.audience)
        return fallback_questions(pitch, lang)


def fallback_questions(pitch: Pitch, lang: str = "en") -> DraftQuestions:
    """Заготовленные вопросы по аудитории на языке речи игрока (en | ru | ro).

    Прикол кейса без LLM под сказанное не переформулировать, поэтому скептик задаёт заготовленный вопрос.
    """
    return DraftQuestions(questions=list(pick(FALLBACK_QUESTIONS, lang)[pitch.audience]))


def one_per_juror(draft: DraftQuestions, pitch: Pitch, lang: str = "en") -> DraftQuestions:
    """Ровно три вопроса — по одному от каждого члена жюри, в порядке стола: строгий, добрый, скептик.

    Лишние вопросы одного члена жюри отбрасываются; если кто-то промолчал, берётся его заготовленный вопрос.
    """
    spare = {q.juror: q for q in pick(FALLBACK_QUESTIONS, lang)[pitch.audience]}
    asked: dict[JurorId, DraftQuestion] = {}
    for q in draft.questions:
        asked.setdefault(q.juror, q)
    return DraftQuestions(questions=[asked.get(juror_id, spare[juror_id]) for juror_id in JURORS])


async def _voice(round_id: str, question_id: str, juror: JurorId, text: str, lang: str) -> None:
    mp3 = await synthesize(text, voice_for(juror, lang), lang)
    path = _audio_dir(round_id) / f"{question_id}.mp3"
    await asyncio.to_thread(path.write_bytes, mp3)


def _round_speech_lang(delivery: dict | None) -> str:
    """Язык речи раунда — из разбора выступления; разбора нет или в нём нет языка (старый) — STT_LANGUAGE."""
    return normalize_lang((delivery or {}).get("speech_lang")) or default_lang()


async def run_jury_questions(round_id: str, difficulty: str | None = None) -> JuryQuestionsResponse:
    # повторный вызов (переподключение, показ) отдаёт уже готовые вопросы без новых затрат
    if cached := await asyncio.to_thread(game_api.get_ai_result, round_id, "jury_questions"):
        return JuryQuestionsResponse.model_validate(cached)

    pitch = await asyncio.to_thread(resolve_pitch, round_id)
    if pitch.is_warmup:
        raise RoundStateError("warmup")
    delivery = await asyncio.to_thread(game_api.get_ai_result, round_id, "delivery")
    if delivery is None:
        raise RoundStateError("no_delivery")
    # вопросы и голос жюри — на языке, на котором игрок питчил
    lang = _round_speech_lang(delivery)
    # уровень задан раундом; параметр запроса нужен только чтобы переопределить его
    draft = one_per_juror(await _draft(pitch, delivery["transcript"], difficulty or pitch.difficulty, lang), pitch, lang)

    questions = [
        JuryQuestion(id=f"q{i}", juror=q.juror, text=q.text, audio_url=_audio_url(round_id, f"q{i}"))
        for i, q in enumerate(draft.questions, start=1)
    ]
    _audio_dir(round_id).mkdir(parents=True, exist_ok=True)
    # озвучки идут через слоты ElevenLabs (app/ai/limits.py): при одном коротком слоте — по очереди, не тремя сразу
    await asyncio.gather(*(_voice(round_id, q.id, q.juror, q.text, lang) for q in questions))

    response = JuryQuestionsResponse(questions=questions)
    await asyncio.to_thread(game_api.save_ai_result, round_id, "jury_questions", response.model_dump(mode="json"))
    return response


async def _find_question(round_id: str, question_id: str) -> JuryQuestion:
    saved = await asyncio.to_thread(game_api.get_ai_result, round_id, "jury_questions")
    if saved is None:
        raise RoundStateError("no_questions")
    question = next((q for q in JuryQuestionsResponse.model_validate(saved).questions if q.id == question_id), None)
    if question is None:
        raise KeyError(question_id)
    return question


async def run_jury_answer(
    round_id: str, question_id: str, audio: bytes, difficulty: str | None = None
) -> JuryAnswerResponse:
    question = await _find_question(round_id, question_id)
    delivery = await asyncio.to_thread(game_api.get_ai_result, round_id, "delivery")
    # язык ответа определяет распознавание; не определило — язык, на котором игрок питчил, иначе STT_LANGUAGE
    transcript = await transcribe(await to_wav16k(audio))
    answer, answer_lang = transcript.text, speech_lang(transcript, _round_speech_lang(delivery))
    if not answer:
        result = JuryAnswerResponse(score=0, comment=pick(ANSWER_TEXTS, answer_lang)["silence"])
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
            answer_language=language_name(answer_lang),
        )
        result = JuryAnswerResponse(score=assessment.score, comment=assessment.comment)

    payload = {"question_id": question_id, "answer": answer, "speech_lang": answer_lang, **result.model_dump(mode="json")}
    await asyncio.to_thread(game_api.save_ai_result, round_id, "jury_answer", payload)
    return result


async def run_jury_skip(round_id: str, question_id: str) -> JuryAnswerResponse:
    """Игрок пропустил вопрос: 0 баллов, иначе трудные вопросы выгодно пропускать — балл жюри это среднее ответов."""
    await _find_question(round_id, question_id)
    delivery = await asyncio.to_thread(game_api.get_ai_result, round_id, "delivery")
    result = JuryAnswerResponse(score=0, comment=pick(ANSWER_TEXTS, _round_speech_lang(delivery))["skipped"])
    payload = {"question_id": question_id, "answer": "", "skipped": True, **result.model_dump(mode="json")}
    await asyncio.to_thread(game_api.save_ai_result, round_id, "jury_answer", payload)
    return result
