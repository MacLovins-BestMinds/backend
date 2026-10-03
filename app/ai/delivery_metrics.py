"""Метрики и оценка подачи кодом — правила из docs/tz («Оценка и звание»)."""

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from app.ai.schemas import DeliveryScore, GazePoint, Metrics, TimelineEvent
from app.ai.stt import Transcript, Word

FILLER_WORDS = frozenset({"ну", "вот", "короче", "типа", "значит", "блин", "собственно", "кстати"})
FILLER_BIGRAMS = frozenset({("как", "бы"), ("это", "самое"), ("в", "общем"), ("так", "сказать"), ("в", "принципе")})
_HESITATION = re.compile(r"(э+м*|м+|а+м+|ы+)")
_STRIP = " .,!?;:…—–-«»\"'()"

LONG_PAUSE_SEC = 3.0
GOOD_PAUSE_SEC = (1.0, 2.0)
GAZE_OFF_SEC = 3.0
PACE_WINDOW_SEC = 20.0
PACE_RANGE_WPM = (100, 180)

WEIGHTS = {"fillers": 0.30, "pace": 0.20, "gaze": 0.20, "pauses": 0.15, "timing": 0.15}


@dataclass(frozen=True, slots=True)
class DeliveryAnalysis:
    metrics: Metrics
    score: DeliveryScore
    events: list[TimelineEvent]


def _norm(word: str) -> str:
    return word.lower().strip(_STRIP).replace("ё", "е")


def _interp(x: float, points: Sequence[tuple[float, float]]) -> float:
    """Кусочно-линейная функция по точкам (x, y), за краями — константа."""
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in pairwise(points):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


# --- детекторы ---


def find_fillers(words: Sequence[Word]) -> list[tuple[float, str]]:
    norm = [_norm(w.text) for w in words]
    found: list[tuple[float, str]] = []
    i = 0
    while i < len(words):
        if i + 1 < len(words) and (norm[i], norm[i + 1]) in FILLER_BIGRAMS:
            found.append((words[i].start, f"{norm[i]} {norm[i + 1]}"))
            i += 2
            continue
        if norm[i] in FILLER_WORDS or _HESITATION.fullmatch(norm[i]):
            found.append((words[i].start, norm[i]))
        i += 1
    return found


def find_gaps(words: Sequence[Word], min_sec: float, max_sec: float = float("inf")) -> list[tuple[float, float]]:
    """Паузы между словами: (время начала паузы, длительность)."""
    gaps = []
    for prev, cur in pairwise(words):
        gap = cur.start - prev.end
        if min_sec <= gap <= max_sec:
            gaps.append((prev.end, round(gap, 2)))
    return gaps


def _gaze_spans(gaze: Sequence[GazePoint], duration: float) -> list[tuple[float, float, bool]]:
    """Таймлайн взгляда как отрезки (start, end, on). До первой точки — состояние первой точки."""
    points = sorted(gaze, key=lambda p: p.t)
    spans = []
    for i, p in enumerate(points):
        start = 0.0 if i == 0 else min(p.t, duration)
        end = points[i + 1].t if i + 1 < len(points) else duration
        end = min(end, duration)
        if end > start:
            spans.append((start, end, p.on))
    return spans


def gaze_on_ratio(gaze: Sequence[GazePoint], duration: float) -> float:
    if not gaze or duration <= 0:
        return 1.0  # приложение не прислало взгляд — не штрафуем
    on = sum(end - start for start, end, is_on in _gaze_spans(gaze, duration) if is_on)
    return round(on / duration, 3)


def _gaze_on_at(spans: list[tuple[float, float, bool]], t: float) -> bool:
    if not spans:
        return True
    idx = max(0, bisect_right([s[0] for s in spans], t) - 1)
    return spans[idx][2]


def pace_alerts(words: Sequence[Word], filler_starts: set[float]) -> list[tuple[float, int]]:
    """Темп по полным окнам 20 с вне диапазона 100–180 слов в минуту."""
    if not words:
        return []
    alerts = []
    window_start = words[0].start
    while window_start + PACE_WINDOW_SEC <= words[-1].end:
        window_end = window_start + PACE_WINDOW_SEC
        count = sum(1 for w in words if window_start <= w.start < window_end and w.start not in filler_starts)
        wpm = round(count * 60 / PACE_WINDOW_SEC)
        if not PACE_RANGE_WPM[0] <= wpm <= PACE_RANGE_WPM[1]:
            alerts.append((round(window_start, 2), wpm))
        window_start = window_end
    return alerts


# --- оценки 0–100 ---


def fillers_score(per_min: float) -> int:
    return round(_interp(per_min, [(1, 100), (5, 40), (12, 0)]))


def pace_score(wpm: float) -> int:
    return round(_interp(wpm, [(80, 0), (120, 100), (160, 100), (200, 0)]))


def pauses_score(long_pauses: int) -> int:
    return max(0, 100 - 10 * long_pauses)


def gaze_score(ratio: float) -> int:
    return round(_interp(ratio, [(0, 0), (0.7, 100)]))


def timing_score(duration: float, min_sec: float, max_sec: float) -> int:
    return round(_interp(duration, [(0, 0), (min_sec, 100), (max_sec, 100), (max_sec + 60, 0)]))


def analyze(transcript: Transcript, gaze: Sequence[GazePoint], min_sec: float, max_sec: float) -> DeliveryAnalysis:
    words = transcript.words
    duration = transcript.duration
    speech_min = max((words[-1].end - words[0].start) / 60, 1 / 60) if words else 1.0

    fillers = find_fillers(words)
    filler_starts = {t for t, _ in fillers}
    long_pauses = find_gaps(words, LONG_PAUSE_SEC)
    spans = _gaze_spans(gaze, duration)
    ratio = gaze_on_ratio(gaze, duration)
    content_words = len(words) - sum(len(f.split()) for _, f in fillers)
    wpm = round(content_words / speech_min)
    fillers_per_min = round(len(fillers) / speech_min, 1)

    parts = {
        "fillers": fillers_score(fillers_per_min),
        "pace": pace_score(wpm),
        "gaze": gaze_score(ratio),
        "pauses": pauses_score(len(long_pauses)),
        "timing": timing_score(duration, min_sec, max_sec),
    }
    total = round(sum(parts[k] * w for k, w in WEIGHTS.items()))

    events = [TimelineEvent(type="filler", t=t, text=f"«{w}»") for t, w in fillers]
    events += [TimelineEvent(type="long_pause", t=t, text=f"Пауза {d:.1f} с") for t, d in long_pauses]
    events += [
        TimelineEvent(type="good_pause", t=t, text="Пауза с взглядом в зал")
        for t, _ in find_gaps(words, *GOOD_PAUSE_SEC)
        if _gaze_on_at(spans, t)
    ]
    events += [
        TimelineEvent(type="gaze_off", t=round(start, 2), text=f"Взгляд мимо зала {end - start:.0f} с")
        for start, end, on in spans
        if not on and end - start > GAZE_OFF_SEC
    ]
    for t, window_wpm in pace_alerts(words, filler_starts):
        verdict = "слишком быстро" if window_wpm > PACE_RANGE_WPM[1] else "слишком медленно"
        events.append(TimelineEvent(type="pace", t=t, text=f"Темп {window_wpm} слов/мин — {verdict}"))
    events.sort(key=lambda e: e.t)

    return DeliveryAnalysis(
        metrics=Metrics(
            duration_sec=round(duration, 1),
            words=content_words,
            wpm=wpm,
            fillers=len(fillers),
            fillers_per_min=fillers_per_min,
            long_pauses=len(long_pauses),
            gaze_on_ratio=ratio,
        ),
        score=DeliveryScore(total=total, **parts),
        events=events,
    )
