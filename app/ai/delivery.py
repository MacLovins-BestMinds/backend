"""POST /api/ai/rounds/{id}/delivery: запись → WAV → Whisper → метрики кодом + оценка содержания Gemini.

Разминка (mode=warmup) идёт через тот же пайплайн: 20–40 с и упрощённая рубрика.
"""

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, Field

from app.ai import game_api, llm, pronunciation
from app.ai.audio import to_wav16k
from app.ai.delivery_metrics import analyze
from app.ai.pitch import Pitch, resolve_pitch
from app.ai.schemas import (
    ContentScore,
    CriterionScore,
    DeliveryResponse,
    DeliveryScore,
    GazePoint,
    Metrics,
    PronunciationAssessment,
    Scores,
)
from app.ai.stt import transcribe

NO_SPEECH_TIP = "Мы не услышали речь — проверь микрофон и говори громче."


class ContentAssessment(BaseModel):
    criteria: list[CriterionScore]
    tips: list[str] = Field(max_length=3)


def _metrics_summary(m: Metrics) -> str:
    return (
        f"- длительность: {m.duration_sec:.0f} с, темп: {m.wpm} слов/мин\n"
        f"- слова-паразиты: {m.fillers} ({m.fillers_per_min} в минуту)\n"
        f"- паузы дольше 3 с: {m.long_pauses}\n"
        + (f"- взгляд в зал: {m.gaze_on_ratio:.0%} времени" if m.gaze_on_ratio is not None else "- взгляд не измерялся")
    )


MAX_NOTES_CHARS = 2000


def _notes_block(pitch: Pitch, notes: str) -> str:
    """Заметки с подготовки: что игрок собирался сказать. Для своего питча текст уже передан отдельно."""
    notes = notes.strip()[:MAX_NOTES_CHARS]
    if not notes or notes == (pitch.own_text or "").strip():
        return ""
    return f'- Заметки игрока на подготовке (что он собирался сказать):\n"""\n{notes}\n"""'


async def assess_content(pitch: Pitch, transcript: str, metrics: Metrics, notes: str = "") -> ContentAssessment:
    if not transcript:
        return ContentAssessment(criteria=[], tips=[NO_SPEECH_TIP])
    if pitch.is_warmup:
        return await llm.generate(
            "warmup_score",
            ContentAssessment,
            brief=pitch.brief,
            transcript=transcript,
            metrics=_metrics_summary(metrics),
        )
    own = pitch.is_own
    return await llm.generate(
        "content_score",
        ContentAssessment,
        title=pitch.title,
        brief=pitch.brief,
        audience=pitch.audience_ru,
        own_text=f'- Подготовленный текст игрока:\n"""\n{pitch.own_text}\n"""' if own else "",
        notes=_notes_block(pitch, notes),
        extra_criteria="- `audience_fit` — говорит ли на языке этой аудитории и о том, что ей важно." if own else "",
        transcript=transcript,
        metrics=_metrics_summary(metrics),
    )


PRONUNCIATION_WEIGHT = 0.3  # доля произношения в «Подаче» для английских питчей


def with_pronunciation(score: DeliveryScore, pron: PronunciationAssessment | None) -> DeliveryScore:
    """Подача = 70% наших метрик + 30% произношения Azure; без оценки Azure — только наши метрики."""
    if pron is None:
        return score
    total = round((1 - PRONUNCIATION_WEIGHT) * score.total + PRONUNCIATION_WEIGHT * pron.overall_score)
    return score.model_copy(update={"total": total, "pronunciation": pron.overall_score})


async def run_delivery(round_id: str, audio: bytes, gaze: Sequence[GazePoint], notes: str = "") -> DeliveryResponse:
    pitch = await asyncio.to_thread(resolve_pitch, round_id)
    wav = await to_wav16k(audio)
    # произношение (Azure) оценивается параллельно с распознаванием и оценкой содержания; сбой → None
    pronunciation_task = asyncio.create_task(pronunciation.assess(wav))
    transcript = await transcribe(wav)
    analysis = analyze(transcript, gaze, pitch.min_sec, pitch.max_sec)
    # без LLM честной оценки содержания нет: ошибка уходит клиенту (502), повтор берёт распознавание из кэша
    try:
        content = await assess_content(pitch, transcript.text, analysis.metrics, notes)
    except BaseException:
        pronunciation_task.cancel()
        raise
    pron = await pronunciation_task
    delivery_score = with_pronunciation(analysis.score, pron)

    content_total = round(sum(c.score for c in content.criteria) / len(content.criteria)) if content.criteria else 0
    response = DeliveryResponse(
        transcript=transcript.text,
        scores=Scores(content=ContentScore(total=content_total, criteria=content.criteria), delivery=delivery_score),
        metrics=analysis.metrics,
        events=analysis.events,
        tips=content.tips,
        pronunciation=pron,
    )
    await asyncio.to_thread(game_api.save_ai_result, round_id, "delivery", response.model_dump(mode="json"))
    return response
