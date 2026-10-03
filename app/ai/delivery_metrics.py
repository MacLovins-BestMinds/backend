"""Метрики и оценка подачи кодом — правила из docs/tz («Оценка и звание»)."""

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from app.ai.schemas import DeliveryScore, GazePoint, Metrics, TimelineEvent
from app.ai.stt import Transcript, Word

# Паразиты, которые паразиты всегда (звуки-заминки и русские слова на случай STT_LANGUAGE=ru)
FILLER_WORDS = frozenset(
    {"um", "uh", "er", "erm", "ah", "hmm"} | {"ну", "вот", "короче", "типа", "значит", "блин", "собственно", "кстати"}
)
FILLER_BIGRAMS = frozenset({("как", "бы"), ("это", "самое"), ("в", "общем"), ("так", "сказать"), ("в", "принципе")})
# Английские слова, которые бывают и паразитами, и нормальными словами («I like it», «Do you know why…»):
# считаем их паразитами только в «паразитной» позиции — см. _filler_position
DISCOURSE_WORDS = frozenset({"like", "so", "well", "actually", "basically", "literally", "right", "okay"})
DISCOURSE_BIGRAMS = frozenset({("you", "know"), ("i", "mean")})
HEDGE_BIGRAMS = frozenset({("kind", "of"), ("sort", "of")})
# «a kind of tool» — нормальная речь; «it's kind of simple» — паразит-смягчение
_HEDGE_LEGIT_BEFORE = frozenset(
    {"a", "an", "the", "this", "that", "what", "which", "any", "some", "every", "one", "same"}
)
_HESITATION = re.compile(r"(u+[hm]+|e+r+m*|a+h+|h+m+|m+|э+м*|м+|а+м+|ы+)")
_STRIP = " .,!?;:…—–-«»\"'()"
_SENTENCE_END = (".", "!", "?", "…")
_CLAUSE_END = (*_SENTENCE_END, ",", ";", ":", "—", "–")

GOOD_PAUSE_SEC = (1.0, 2.5)  # после фразы: удачная пауза; длиннее — без штрафа
HESITATION_SEC = 1.0  # посреди фразы от 1 до 3 с — запинка, от 3 с — длинная пауза (штраф)
LONG_PAUSE_SEC = 3.0
GAZE_OFF_SEC = 3.0
PACE_WINDOW_SEC = 20.0  # окно по чистому времени речи
PACE_RANGE_WPM = (100, 180)
SPEECH_GAP_SEC = 1.0  # паузы длиннее не входят во время речи: паузы не должны штрафоваться ещё и через темп

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


def _ends(raw: str, marks: tuple[str, ...]) -> bool:
    return raw.rstrip(" \"'»)").endswith(marks)


def _filler_position(raw: Sequence[str], i: int, j: int, prev_is_filler: bool) -> bool:
    """Слова raw[i..j] стоят как вставка: отделены запятыми/границей фразы или идут сразу после «um»."""
    starts_clause = i == 0 or _ends(raw[i - 1], _CLAUSE_END)
    return (starts_clause and _ends(raw[j], _CLAUSE_END)) or prev_is_filler


def find_fillers(words: Sequence[Word]) -> list[tuple[float, str]]:
    """Слова-паразиты по контексту: «so, the idea…», «about, like, stoicism», но не «Do you know why…»."""
    raw = [w.text for w in words]
    norm = [_norm(r) for r in raw]
    found: list[tuple[float, str]] = []
    prev_is_filler = False
    i = 0
    while i < len(words):
        pair = (norm[i], norm[i + 1]) if i + 1 < len(words) else None
        word = norm[i]
        hit: str | None = None
        span = 1
        if (
            pair in FILLER_BIGRAMS
            or pair in DISCOURSE_BIGRAMS
            and _filler_position(raw, i, i + 1, prev_is_filler)
            or pair in HEDGE_BIGRAMS
            and (i == 0 or norm[i - 1] not in _HEDGE_LEGIT_BEFORE)
        ):
            hit, span = f"{pair[0]} {pair[1]}", 2
        elif (
            word in FILLER_WORDS
            or _HESITATION.fullmatch(word)
            or word in DISCOURSE_WORDS
            and _filler_position(raw, i, i, prev_is_filler)
        ):
            hit = word
        if hit:
            found.append((words[i].start, hit))
        prev_is_filler = hit is not None and (word in FILLER_WORDS or bool(_HESITATION.fullmatch(word)))
        i += span
    return found


def find_gaps(words: Sequence[Word], min_sec: float, max_sec: float = float("inf")) -> list[tuple[float, float]]:
    """Паузы между словами: (время начала паузы, длительность)."""
    gaps = []
    for prev, cur in pairwise(words):
        gap = cur.start - prev.end
        if min_sec <= gap <= max_sec:
            gaps.append((prev.end, round(gap, 2)))
    return gaps


def classify_pauses(words: Sequence[Word]) -> dict[str, list[tuple[float, float]]]:
    """Паузы по положению: после конца фразы — нормально, посреди фразы — запинка или длинная пауза."""
    result: dict[str, list[tuple[float, float]]] = {"good": [], "hesitation": [], "long": []}
    for prev, cur in pairwise(words):
        gap = round(cur.start - prev.end, 2)
        if gap < HESITATION_SEC:
            continue
        if _ends(prev.text, _SENTENCE_END):
            if gap <= GOOD_PAUSE_SEC[1]:
                result["good"].append((prev.end, gap))
        elif gap >= LONG_PAUSE_SEC:
            result["long"].append((prev.end, gap))
        elif not _ends(prev.text, _CLAUSE_END):  # пауза после запятой — естественная, не запинка
            result["hesitation"].append((prev.end, gap))
    return result


def speech_minutes(words: Sequence[Word]) -> float:
    """Чистое время речи: от первого до последнего слова без пауз длиннее SPEECH_GAP_SEC."""
    if not words:
        return 1.0
    span = words[-1].end - words[0].start
    pauses = sum(g for p, c in pairwise(words) if (g := c.start - p.end) > SPEECH_GAP_SEC)
    return max((span - pauses) / 60, 1 / 60)


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
    """Темп по окнам 20 с чистого времени речи (длинные паузы вырезаны) вне диапазона 100–180 слов в минуту."""
    if not words:
        return []
    alerts = []
    speech_t = 0.0  # время речи к началу текущего слова
    window_start_real = words[0].start
    window_start_speech = 0.0
    count = 0
    for prev, cur in zip((None, *words), words, strict=False):
        if prev is not None:
            gap = cur.start - prev.end
            speech_t += (prev.end - prev.start) + (0.0 if gap > SPEECH_GAP_SEC else gap)
        if speech_t - window_start_speech >= PACE_WINDOW_SEC:
            wpm = round(count * 60 / (speech_t - window_start_speech))
            if not PACE_RANGE_WPM[0] <= wpm <= PACE_RANGE_WPM[1]:
                alerts.append((round(window_start_real, 2), wpm))
            window_start_real, window_start_speech, count = cur.start, speech_t, 0
        if cur.start not in filler_starts:
            count += 1
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
    speech_min = speech_minutes(words)

    fillers = find_fillers(words)
    filler_starts = {t for t, _ in fillers}
    pauses = classify_pauses(words)
    long_pauses = pauses["long"]
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
    events += [TimelineEvent(type="long_pause", t=t, text=f"Пауза {d:.1f} с посреди фразы") for t, d in long_pauses]
    events += [
        TimelineEvent(type="hesitation", t=t, text=f"Запинка {d:.1f} с посреди фразы") for t, d in pauses["hesitation"]
    ]
    # без камеры взгляд не измерен — не утверждаем, что он был в зале
    good_pause_text = "Пауза с взглядом в зал" if gaze else "Удачная пауза перед следующей мыслью"
    events += [
        TimelineEvent(type="good_pause", t=t, text=good_pause_text) for t, _ in pauses["good"] if _gaze_on_at(spans, t)
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
