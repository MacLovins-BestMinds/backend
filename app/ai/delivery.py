"""POST /api/ai/rounds/{id}/delivery: запись → WAV → Whisper → метрики кодом + оценка содержания Gemini.

Разминка (mode=warmup) идёт через тот же пайплайн: 20–40 с и упрощённая рубрика.
Язык речи определяет распознавание (speech_lang в ответе); на нём весь разбор: советы, подписи, оценки.
Не определило — язык речи этого раунда из прошлого разбора, иначе STT_LANGUAGE. Язык интерфейса не участвует.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace

from pydantic import BaseModel, Field

from app.ai import game_api, llm, pronunciation
from app.ai.audio import to_wav16k
from app.ai.delivery_metrics import PaceMode, analyze, filler_candidates, word_spans
from app.ai.pitch import Pitch, known_speech_lang, resolve_pitch
from app.ai.schemas import (
    ContentScore,
    CriterionScore,
    DeliveryResponse,
    DeliveryScore,
    GazePoint,
    Metrics,
    PronunciationAssessment,
    Scores,
    WordMark,
)
from app.ai.stt import Transcript, Word, speech_lang, transcribe
from app.core.lang import default_lang, language_name, pick

logger = logging.getLogger(__name__)

NO_SPEECH_TIPS = {
    "en": "We didn't hear any speech — check the microphone and speak louder.",
    "ru": "Мы не услышали речи — проверь микрофон и говори громче.",
    "ro": "Nu am auzit nimic — verifică microfonul și vorbește mai tare.",
}
NO_SPEECH_TIP = NO_SPEECH_TIPS["en"]


class FillerVerdicts(BaseModel):
    fillers: list[int] = Field(description="номера кандидатов, которые в своём контексте — слова-паразиты")


CONTEXT_WORDS = 6


async def judge_fillers(transcript: Transcript, lang: str = "en") -> dict[int, bool] | None:
    """Паразит ли «like», «so», «you know» («deci», «знаете») в этом месте — решает LLM по смыслу фразы.

    lang — язык речи. Возвращает {номер слова: паразит?} для всех двусмысленных слов; None — LLM не ответил,
    тогда разбор оценивает такие слова по положению во фразе.
    """
    words = transcript.words
    candidates = filler_candidates(words, lang)
    if not candidates:
        return {}
    lines = []
    for n, (i, span, _) in enumerate(candidates, 1):
        before = " ".join(w.text for w in words[max(0, i - CONTEXT_WORDS) : i])
        marked = " ".join(w.text for w in words[i : i + span])
        after = " ".join(w.text for w in words[i + span : i + span + CONTEXT_WORDS])
        lines.append(f"{n}. …{before} [{marked}] {after}…")
    try:
        verdict = await llm.generate(
            "filler_judge",
            FillerVerdicts,
            transcript=transcript.text,
            candidates="\n".join(lines),
            speech_language=language_name(lang),
        )
    except Exception:
        logger.exception("delivery: не удалось оценить паразиты по смыслу — считаю по положению во фразе")
        return None
    chosen = set(verdict.fillers)
    return {i: n in chosen for n, (i, _, _) in enumerate(candidates, 1)}


# Строгость разбора содержания по уровню раунда
CONTENT_LEVELS = {
    "easy": (
        "EASY. This is a beginner's practice talk on an everyday topic, not a business pitch. A clear main point, "
        "a couple of reasons or a small personal story and a closing line are enough for 80+ on structure and persuasion. "
        '"Why us" and "call to action" may be as simple as "that is why I love it" or "try it". Be encouraging.'
    ),
    "medium": (
        "MEDIUM. Expect a clear position, at least two reasons each backed by an example, an answer to one obvious "
        "objection and a conclusion. An opinion with no support cannot score above 70 on persuasion."
    ),
    "hard": (
        "HARD. Judge like a demanding coach. Expect an accurate explanation of the idea, a concrete example or number, "
        "an answer to the strongest objection and a real call to action. Vague, generic or inaccurate talk cannot score "
        "above 60 on any criterion."
    ),
}


class ContentAssessment(BaseModel):
    criteria: list[CriterionScore]
    tips: list[str] = Field(max_length=3)


def _metrics_summary(m: Metrics) -> str:
    return (
        f"- duration: {m.duration_sec:.0f} s, pace: {m.wpm} words/min\n"
        f"- filler words: {m.fillers} ({m.fillers_per_min} per minute)\n"
        f"- pauses longer than 3 s: {m.long_pauses}\n"
        f"- swear words: {m.profanity}\n"
        + (
            f"- eye contact with the audience: {m.gaze_on_ratio:.0%} of the time"
            if m.gaze_on_ratio is not None
            else "- eye contact was not measured"
        )
    )


MAX_NOTES_CHARS = 2000


def _notes_block(pitch: Pitch, notes: str) -> str:
    """Заметки с подготовки: что игрок собирался сказать. Для своего питча текст уже передан отдельно."""
    notes = notes.strip()[:MAX_NOTES_CHARS]
    if not notes or notes == (pitch.own_text or "").strip():
        return ""
    return f'- Preparation notes of the player (what they planned to say):\n"""\n{notes}\n"""'


async def assess_content(
    pitch: Pitch, transcript: str, metrics: Metrics, notes: str = "", speech: str = "en"
) -> ContentAssessment:
    """speech — язык речи: на нём и цитаты, и советы."""
    if not transcript:
        return ContentAssessment(criteria=[], tips=[pick(NO_SPEECH_TIPS, speech)])
    if pitch.is_warmup:
        return await llm.generate(
            "warmup_score",
            ContentAssessment,
            brief=pitch.brief,
            transcript=transcript,
            metrics=_metrics_summary(metrics),
            speech_language=language_name(speech),
        )
    own = pitch.is_own
    return await llm.generate(
        "content_score",
        ContentAssessment,
        title=pitch.title,
        brief=pitch.brief,
        audience=pitch.audience_ru,
        own_text=f'- Prepared text of the player:\n"""\n{pitch.own_text}\n"""' if own else "",
        notes=_notes_block(pitch, notes),
        extra_criteria="- `audience_fit` — does the speaker talk in this audience's language and about what matters to it." if own else "",
        transcript=transcript,
        metrics=_metrics_summary(metrics),
        level_rules=CONTENT_LEVELS.get(pitch.difficulty, CONTENT_LEVELS["easy"]),
        speech_language=language_name(speech),
    )


PRONUNCIATION_WEIGHT = 0.3  # доля произношения в «Подаче» для английских питчей


def with_pronunciation(score: DeliveryScore, pron: PronunciationAssessment | None) -> DeliveryScore:
    """Подача = 70% наших метрик + 30% произношения Azure; без оценки Azure — только наши метрики."""
    if pron is None:
        return score
    total = round((1 - PRONUNCIATION_WEIGHT) * score.total + PRONUNCIATION_WEIGHT * pron.overall_score)
    return score.model_copy(update={"total": total, "pronunciation": pron.overall_score})


def char_times(word: Word, length: int) -> list[float] | None:
    """Время каждой буквы слова ровно под его место в transcript (без пробелов вокруг токена), иначе None."""
    if not word.chars:
        return None
    lead = len(word.text) - len(word.text.lstrip())
    times = word.chars[lead : lead + length]
    return [round(t, 2) for t in times] if len(times) == length else None


async def run_delivery(
    round_id: str,
    audio: bytes,
    gaze: Sequence[GazePoint],
    notes: str = "",
    pace: PaceMode = "normal",
    limits: tuple[int, int] | None = None,
) -> DeliveryResponse:
    """limits — своя длина питча (мин, макс в секундах), которую игрок выбрал вместо лимитов уровня."""
    pitch = await asyncio.to_thread(resolve_pitch, round_id)
    if limits:
        pitch = replace(pitch, min_sec=limits[0], max_sec=limits[1])
    wav = await to_wav16k(audio)
    # язык речи, если распознавание его не определит: как в прошлом разборе этого раунда, иначе STT_LANGUAGE
    expected = await asyncio.to_thread(known_speech_lang, round_id) or default_lang()
    # Произношение (Azure) оценивается только для английской речи, параллельно с распознаванием и оценкой
    # содержания; сбой → None. Ждём английскую речь — оценка стартует сразу и отменяется, если речь не английская;
    # иначе стартует, когда распознавание скажет, что речь английская.
    pronunciation_task = asyncio.create_task(pronunciation.assess(wav)) if expected == "en" else None
    try:
        transcript = await transcribe(wav)
        speech = speech_lang(transcript, expected)
        if speech != "en" and pronunciation_task is not None:
            pronunciation_task.cancel()
            pronunciation_task = None
        elif speech == "en" and pronunciation_task is None:
            pronunciation_task = asyncio.create_task(pronunciation.assess(wav))
        verdicts = await judge_fillers(transcript, speech)
        analysis = analyze(transcript, gaze, pitch.min_sec, pitch.max_sec, verdicts, pace, speech)
        # без LLM честной оценки содержания нет: ошибка уходит клиенту (502), повтор берёт распознавание из кэша
        content = await assess_content(pitch, transcript.text, analysis.metrics, notes, speech)
    except BaseException:
        if pronunciation_task is not None:
            pronunciation_task.cancel()
        raise
    pron = await pronunciation_task if pronunciation_task is not None else None
    delivery_score = with_pronunciation(analysis.score, pron)

    content_total = round(sum(c.score for c in content.criteria) / len(content.criteria)) if content.criteria else 0
    response = DeliveryResponse(
        transcript=transcript.text,
        words=[
            WordMark(start=a, end=b, t=round(w.start, 2), t_end=round(w.end, 2), c=char_times(w, b - a))
            for w, (a, b) in zip(transcript.words, word_spans(transcript), strict=True)
            if b > a
        ],
        scores=Scores(content=ContentScore(total=content_total, criteria=content.criteria), delivery=delivery_score),
        metrics=analysis.metrics,
        events=analysis.events,
        tips=content.tips,
        pronunciation=pron,
        speech_lang=speech,
    )
    await asyncio.to_thread(game_api.save_ai_result, round_id, "delivery", response.model_dump(mode="json"))
    return response
