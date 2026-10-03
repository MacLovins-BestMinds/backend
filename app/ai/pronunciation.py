"""Оценка английского произношения: Azure Pronunciation Assessment без эталонного текста.

Работает параллельно с распознаванием Scribe (паразиты, паузы, темп считаются по нему: Azure выкидывает «um»).
Azure обрабатывает звук примерно вдвое быстрее реального времени, поэтому запись режется по паузам на куски,
которые оцениваются параллельно. Любой сбой → None: разбор приходит без блока произношения.
"""

import array
import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from app.ai import cache
from app.ai.config import get_settings
from app.ai.schemas import PronunciationAssessment, PronunciationIssue

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_SEC = SAMPLE_RATE * 2
WAV_HEADER_BYTES = 44
TICKS_PER_SEC = 10_000_000  # Offset/Duration у Azure — в 100 нс

MIN_CHUNK_SEC = 15.0
CUT_SEARCH_SEC = 3.0  # ищем тишину ±3 с от точки разреза
FRAME_SEC = 0.1
WEAK_ACCURACY = 60  # ниже этого Azure считает слово/слог произнесённым неверно
BREAK_CONFIDENCE = 0.75  # порог из документации Azure
MAX_ISSUES = 20
MONOTONE_PROSODY = 75
MONOTONE_SHARE = 0.3


@dataclass(frozen=True, slots=True)
class Chunk:
    offset_sec: float
    pcm: bytes


# --- нарезка по паузам ---


def _frame_energy(pcm: bytes, start: int, end: int) -> float:
    samples = array.array("h", pcm[start:end])
    return sum(abs(x) for x in samples) / max(len(samples), 1)


def split_on_silence(pcm: bytes, max_parallel: int) -> list[Chunk]:
    """Режет PCM 16 кГц на куски не короче MIN_CHUNK_SEC, разрез — в самом тихом месте рядом с целевой точкой."""
    duration = len(pcm) / BYTES_PER_SEC
    target = max(MIN_CHUNK_SEC, duration / max_parallel)
    frame = int(FRAME_SEC * BYTES_PER_SEC) // 2 * 2
    cuts = [0]
    pos = target
    while duration - pos > target / 2:
        lo = int(max(pos - CUT_SEARCH_SEC, cuts[-1] / BYTES_PER_SEC + 1) * BYTES_PER_SEC) // 2 * 2
        hi = int(min(pos + CUT_SEARCH_SEC, duration) * BYTES_PER_SEC) // 2 * 2
        best = min(range(lo, max(hi - frame, lo + 1), frame), key=lambda i: _frame_energy(pcm, i, i + frame))
        cuts.append(best + frame // 2 // 2 * 2)
        pos = cuts[-1] / BYTES_PER_SEC + target
    cuts.append(len(pcm))
    return [Chunk(offset_sec=a / BYTES_PER_SEC, pcm=pcm[a:b]) for a, b in pairwise(cuts) if b > a]


# --- вызов Azure (блокирующий SDK, запускается в потоке) ---


def _assess_chunk(pcm: bytes) -> list[dict[str, Any]]:
    import azure.cognitiveservices.speech as sdk  # нативная библиотека: импорт только когда Azure включён

    s = get_settings()
    config = sdk.SpeechConfig(subscription=s.azure_speech_key, region=s.azure_speech_region)
    config.speech_recognition_language = s.azure_speech_locale
    stream = sdk.audio.PushAudioInputStream(sdk.audio.AudioStreamFormat(SAMPLE_RATE, 16, 1))
    recognizer = sdk.SpeechRecognizer(speech_config=config, audio_config=sdk.audio.AudioConfig(stream=stream))
    assessment = sdk.PronunciationAssessmentConfig(
        reference_text="",  # без эталона: спикер говорит свободный питч
        grading_system=sdk.PronunciationAssessmentGradingSystem.HundredMark,
        granularity=sdk.PronunciationAssessmentGranularity.Phoneme,
        enable_miscue=False,  # в непрерывном режиме не поддерживается
    )
    if s.azure_speech_locale == "en-US":
        assessment.enable_prosody_assessment()
    assessment.apply_to(recognizer)

    segments: list[dict[str, Any]] = []
    errors: list[str] = []
    done = threading.Event()

    def on_recognized(evt: Any) -> None:
        raw = evt.result.properties.get(sdk.PropertyId.SpeechServiceResponse_JsonResult)
        if raw:
            segments.append(json.loads(raw))

    def on_canceled(evt: Any) -> None:
        if evt.cancellation_details.reason != sdk.CancellationReason.EndOfStream:
            errors.append(evt.cancellation_details.error_details or str(evt.cancellation_details.reason))
        done.set()

    recognizer.recognized.connect(on_recognized)
    recognizer.session_stopped.connect(lambda _: done.set())
    recognizer.canceled.connect(on_canceled)
    recognizer.start_continuous_recognition()
    stream.write(pcm)
    stream.close()
    finished = done.wait(s.azure_timeout_sec)
    recognizer.stop_continuous_recognition()
    if errors:
        raise RuntimeError(errors[0])
    if not finished:
        raise TimeoutError("Azure не успел оценить кусок записи")
    return segments


# --- сборка результата ---


def _issue(word: dict[str, Any], offset_sec: float) -> PronunciationIssue | None:
    pa = word.get("PronunciationAssessment", {})
    accuracy = round(pa.get("AccuracyScore", 100))
    prosody = pa.get("Feedback", {}).get("Prosody", {})
    brk = prosody.get("Break", {})
    if pa.get("ErrorType") == "Mispronunciation" or accuracy < WEAK_ACCURACY:
        error = "mispronunciation"
    elif (
        "UnexpectedBreak" in brk.get("ErrorTypes", [])
        or brk.get("UnexpectedBreak", {}).get("Confidence", 0) > BREAK_CONFIDENCE
    ):
        error = "unexpected_break"
    elif (
        "MissingBreak" in brk.get("ErrorTypes", [])
        or brk.get("MissingBreak", {}).get("Confidence", 0) > BREAK_CONFIDENCE
    ):
        error = "missing_break"
    else:
        return None
    weak = [
        syl.get("Grapheme") or syl["Syllable"]
        for syl in word.get("Syllables", [])
        if syl.get("PronunciationAssessment", {}).get("AccuracyScore", 100) < WEAK_ACCURACY
    ]
    t = offset_sec + word.get("Offset", 0) / TICKS_PER_SEC
    return PronunciationIssue(word=word["Word"], t=round(t, 2), accuracy=accuracy, error=error, weak_syllables=weak)


def _tips(issues: list[PronunciationIssue], prosody: int | None, monotone: bool, breaks: int) -> list[str]:
    tips = []
    worst = list(dict.fromkeys(i.word for i in issues if i.error == "mispronunciation"))[:3]
    if worst:
        tips.append(f"Practise these words: {', '.join(worst)} — listen to them in a dictionary and repeat out loud.")
    if monotone or (prosody is not None and prosody < MONOTONE_PROSODY):
        tips.append("Your speech sounds flat: stress key words and numbers, and drop your tone at the end of statements.")
    if breaks >= 2:
        tips.append("There are pauses in the middle of phrases — finish the thought, then pause.")
    return tips or ["Confident pronunciation — keep it up."]


def aggregate(chunks: list[tuple[float, list[dict[str, Any]]]]) -> PronunciationAssessment | None:
    """chunks — (смещение куска в секундах, сегменты JSON Azure). Баллы усредняются с весом по числу слов."""
    weighted: dict[str, float] = {"AccuracyScore": 0, "FluencyScore": 0, "ProsodyScore": 0, "PronScore": 0}
    prosody_words = 0
    words_total = 0
    monotone_words = 0
    issues: list[PronunciationIssue] = []
    for offset_sec, segments in chunks:
        for seg in segments:
            best = (seg.get("NBest") or [{}])[0]
            words = best.get("Words") or []
            pa = best.get("PronunciationAssessment")
            if not words or not pa:
                continue
            n = len(words)
            words_total += n
            for key in ("AccuracyScore", "FluencyScore", "PronScore"):
                weighted[key] += pa.get(key, 0) * n
            if "ProsodyScore" in pa:
                weighted["ProsodyScore"] += pa["ProsodyScore"] * n
                prosody_words += n
            for word in words:
                intonation = word.get("PronunciationAssessment", {}).get("Feedback", {}).get("Prosody", {})
                if "Monotone" in intonation.get("Intonation", {}).get("ErrorTypes", []):
                    monotone_words += 1
                if issue := _issue(word, offset_sec):
                    issues.append(issue)
    if words_total == 0:
        return None

    prosody = round(weighted["ProsodyScore"] / prosody_words) if prosody_words else None
    # Azure ставит Monotone на отдельные слова слишком охотно (и при prosody 90); монотонной считаем речь,
    # только если низкая интонация подтверждается пометкой на заметной доле слов
    monotone = prosody is not None and prosody < MONOTONE_PROSODY and monotone_words / words_total > MONOTONE_SHARE
    breaks = sum(i.error == "unexpected_break" for i in issues)
    issues.sort(key=lambda i: (i.error != "mispronunciation", i.accuracy))
    return PronunciationAssessment(
        overall_score=round(weighted["PronScore"] / words_total),
        accuracy_score=round(weighted["AccuracyScore"] / words_total),
        fluency_score=round(weighted["FluencyScore"] / words_total),
        prosody_score=prosody,
        words_total=words_total,
        mispronounced_words_count=sum(i.error == "mispronunciation" for i in issues),
        unexpected_breaks_count=breaks,
        monotone=monotone,
        words=issues[:MAX_ISSUES],
        tips=_tips(issues, prosody, monotone, breaks),
    )


def enabled() -> bool:
    s = get_settings()
    return bool(s.azure_speech_key) and s.stt_language == "en"


async def assess(wav: bytes) -> PronunciationAssessment | None:
    """WAV 16 кГц моно → оценка произношения или None (Azure выключен, сбой, таймаут)."""
    if not enabled():
        return None
    s = get_settings()
    key = cache.make_key("azure-pa", s.azure_speech_locale, wav)
    if cached := await cache.get("pron", key, "json"):
        return PronunciationAssessment.model_validate_json(cached)

    chunks = split_on_silence(wav[WAV_HEADER_BYTES:], s.azure_max_parallel)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(asyncio.to_thread(_assess_chunk, c.pcm) for c in chunks)),
            timeout=s.azure_timeout_sec + 5,
        )
    except Exception as e:  # noqa: BLE001 — оценка произношения необязательна, разбор не должен падать
        logger.warning("pronunciation: Azure не ответил (%s: %s), разбор без произношения", type(e).__name__, e)
        return None

    result = aggregate([(c.offset_sec, segs) for c, segs in zip(chunks, results, strict=True)])
    if result:
        await cache.put("pron", key, "json", result.model_dump_json().encode())
    return result
