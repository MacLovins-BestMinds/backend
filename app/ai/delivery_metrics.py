"""Метрики и оценка подачи кодом — правила из docs/tz («Оценка и звание»)."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

from app.ai.schemas import DeliveryScore, GazePoint, Metrics, TimelineEvent
from app.ai.stt import Transcript, Word, to_latin

# Паразиты, которые паразиты всегда (звуки-заминки и русские слова на случай STT_LANGUAGE=ru)
_RU_FILLERS = {"ну", "вот", "короче", "типа", "значит", "блин", "собственно", "кстати"}
_RU_BIGRAMS = {("как", "бы"), ("это", "самое"), ("в", "общем"), ("так", "сказать"), ("в", "принципе")}
# русские паразиты ищем и латиницей: в английском режиме расшифровка переведена в латиницу («nu», «koroche»)
FILLER_WORDS = frozenset({"um", "uh", "er", "erm", "ah", "hmm"} | _RU_FILLERS | {to_latin(w) for w in _RU_FILLERS})
FILLER_BIGRAMS = frozenset(_RU_BIGRAMS | {(to_latin(a), to_latin(b)) for a, b in _RU_BIGRAMS})
# Английские слова, которые бывают и паразитами, и нормальными словами («I like it», «Do you know why…»):
# считаем их паразитами только в «паразитной» позиции — см. _filler_position
DISCOURSE_WORDS = frozenset({"like", "so", "well", "actually", "basically", "literally", "right", "okay"})
DISCOURSE_BIGRAMS = frozenset({("you", "know"), ("i", "mean")})
HEDGE_BIGRAMS = frozenset({("kind", "of"), ("sort", "of")})
# «a kind of tool» — нормальная речь; «it's kind of simple» — паразит-смягчение
_HEDGE_LEGIT_BEFORE = frozenset(
    {"a", "an", "the", "this", "that", "what", "which", "any", "some", "every", "one", "same"}
)
# «e-e-e», «aaa» — так выглядит русское «э-э-э» после перевода расшифровки в латиницу
_HESITATION = re.compile(r"(u+[hm]+|e+r+m*|a+h+|h+m+|m+|e{2,}|a{2,}|y{2,}|э+м*|м+|а+м+|ы+)")
# слова, которые повторяют нарочно («very, very good») или по грамматике («that that», «had had»)
_REPEAT_OK = frozenset({"very", "really", "so", "much", "many", "long", "no", "yes", "bye", "ha", "that", "had", "is"})
_STOPWORDS = frozenset(
    "a an the and or but of to in on at for from with by as is are was were be it its this that these those "
    "i you he she we they my your our their me him her us them do does did not no so if then there here".split()
)
# Ругань: слово считается бранным, если начинается с одного из корней. Русские корни — и кириллицей,
# и в том виде, в каком они выходят после перевода расшифровки в латиницу («blyat», «khuy», «pizdets»).
_SWEAR_EN = ("fuck", "motherfuck", "shit", "bullshit", "bitch", "asshole", "bastard", "cunt", "dickhead", "piss", "wtf")
_SWEAR_RU = (
    "бля", "сука", "сучк", "хуй", "хуя", "хуе", "хуё", "хую", "хуи", "нахуй", "нахуя", "похуй", "нихуя",
    "пизд", "ебат", "ебал", "ебан", "ебуч", "ёб", "заеб", "заёб", "уеб", "уёб", "наеб", "мудак", "мудил", "говн",
    "пидор", "пидар",
)  # fmt: skip
SWEAR_STEMS = _SWEAR_EN + _SWEAR_RU + tuple(to_latin(s) for s in _SWEAR_RU)
_CENSORED = re.compile(r"[a-zа-яё]\*{2,}\w*")  # распознавание иногда само ставит звёздочки: «f***»
PROFANITY_COST, PROFANITY_MAX = 8, 32  # столько баллов подачи снимает каждое бранное слово и не больше скольких всего
REPEAT_WINDOW_WORDS = 15  # повтор фразы считается ошибкой, только если он рядом; дальше — нормальный возврат к мысли
REPEAT_MAX_LEN = 8
_STRIP = " .,!?;:…—–-«»\"'()"
_SENTENCE_END = (".", "!", "?", "…")
_CLAUSE_END = (*_SENTENCE_END, ",", ";", ":", "—", "–")

HESITATION_SEC = 1.0  # посреди фразы от 1 до 3 с — запинка, от 3 с — длинная пауза (штраф)
LONG_PAUSE_SEC = 3.0
GAZE_OFF_SEC = 3.0
PACE_WINDOW_SEC = 20.0  # окно по чистому времени речи
PACE_RANGE_WPM = (100, 180)
# Игрок сам выбирает, каким темпом хочет говорить. Спокойный: за медленную речь не ругаем вообще.
# Быстрый: медленная речь — уже ошибка, зал скучает.
PaceMode = Literal["slow", "normal", "fast"]
PACE_RANGES: dict[str, tuple[int, int]] = {"slow": (0, 170), "normal": PACE_RANGE_WPM, "fast": (140, 220)}
_PACE_CURVES: dict[str, list[tuple[float, float]]] = {
    "slow": [(0, 100), (150, 100), (200, 0)],
    "normal": [(80, 0), (120, 100), (160, 100), (200, 0)],
    "fast": [(110, 0), (150, 100), (200, 100), (240, 0)],
}
SPEECH_GAP_SEC = 1.0  # паузы длиннее не входят во время речи: паузы не должны штрафоваться ещё и через темп

WEIGHTS = {"fillers": 0.30, "pace": 0.20, "gaze": 0.20, "pauses": 0.15, "timing": 0.15}


@dataclass(frozen=True, slots=True)
class DeliveryAnalysis:
    metrics: Metrics
    score: DeliveryScore
    events: list[TimelineEvent]


def _norm(word: str) -> str:
    return word.lower().strip(_STRIP).replace("ё", "е")


def _is_hesitation(word: str) -> bool:
    return bool(_HESITATION.fullmatch(word.replace("-", "")))


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


def find_fillers(words: Sequence[Word], verdicts: Mapping[int, bool] | None = None) -> list[tuple[float, str]]:
    """Слова-паразиты по контексту: «so, the idea…», «about, like, stoicism», но не «Do you know why…»."""
    return [(words[i].start, hit) for i, _, hit in filler_hits(words, verdicts)]


def filler_candidates(words: Sequence[Word]) -> list[tuple[int, int, str]]:
    """Слова, которые бывают и паразитами, и обычными словами («like», «so», «you know», «kind of»):
    (номер первого слова, сколько слов, фраза). Паразит ли это здесь — решается по смыслу, см. verdicts."""
    norm = [_norm(w.text) for w in words]
    found: list[tuple[int, int, str]] = []
    i = 0
    while i < len(norm):
        pair = (norm[i], norm[i + 1]) if i + 1 < len(norm) else None
        if pair in DISCOURSE_BIGRAMS or pair in HEDGE_BIGRAMS:
            found.append((i, 2, f"{pair[0]} {pair[1]}"))
            i += 2
            continue
        if norm[i] in DISCOURSE_WORDS:
            found.append((i, 1, norm[i]))
        i += 1
    return found


def filler_hits(words: Sequence[Word], verdicts: Mapping[int, bool] | None = None) -> list[tuple[int, int, str]]:
    """То же, что find_fillers, но с местом в тексте: (номер первого слова, сколько слов, паразит).

    verdicts — решение по смыслу для двусмысленных слов (номер первого слова → паразит или нет), его даёт LLM.
    Без него (None) такие слова оцениваются по положению во фразе; пустой словарь — двусмысленные не считаются.
    """
    raw = [w.text for w in words]
    norm = [_norm(r) for r in raw]
    found: list[tuple[int, int, str]] = []
    prev_is_filler = False
    i = 0
    while i < len(words):
        pair = (norm[i], norm[i + 1]) if i + 1 < len(words) else None
        word = norm[i]
        hit: str | None = None
        span = 1
        if verdicts is not None:
            # двусмысленные слова — по смыслу; однозначные («um», «э-э», «типа») — всегда паразиты
            if pair in FILLER_BIGRAMS or (pair in DISCOURSE_BIGRAMS or pair in HEDGE_BIGRAMS) and verdicts.get(i, False):
                hit, span = f"{pair[0]} {pair[1]}", 2
            elif pair in DISCOURSE_BIGRAMS or pair in HEDGE_BIGRAMS:
                span = 2  # обычная фраза («you know the answer») — второе слово отдельно не проверяем
            elif word in FILLER_WORDS or _is_hesitation(word) or word in DISCOURSE_WORDS and verdicts.get(i, False):
                hit = word
        elif (
            pair in FILLER_BIGRAMS
            or pair in DISCOURSE_BIGRAMS
            and _filler_position(raw, i, i + 1, prev_is_filler)
            or pair in HEDGE_BIGRAMS
            and (i == 0 or norm[i - 1] not in _HEDGE_LEGIT_BEFORE)
        ):
            hit, span = f"{pair[0]} {pair[1]}", 2
        elif (
            word in FILLER_WORDS
            or _is_hesitation(word)
            or word in DISCOURSE_WORDS
            and _filler_position(raw, i, i, prev_is_filler)
        ):
            hit = word
        if hit:
            found.append((i, span, hit))
        prev_is_filler = hit is not None and (word in FILLER_WORDS or _is_hesitation(word))
        i += span
    return found


def find_profanity(words: Sequence[Word]) -> list[tuple[int, str]]:
    """Ругань: (номер слова, слово). На сцене её быть не должно — ни в питче, ни в ответах жюри."""
    found = []
    for i, w in enumerate(words):
        raw = w.text.lower().strip(_STRIP.replace("*", ""))
        word = _norm(w.text)
        if word.startswith(SWEAR_STEMS) or _CENSORED.fullmatch(raw):
            found.append((i, word or raw))
    return found


def find_repeats(words: Sequence[Word]) -> list[tuple[int, int]]:
    """Повторы слов и выражений: (номер первого слова повтора, сколько слов).

    Ошибкой считается: слово, сказанное два раза подряд («Telegram. Telegram»), фраза из трёх и более слов,
    повторённая рядом («they will leave… they will leave»), и пара слов, прозвучавшая рядом в третий раз.
    """
    norm = [_norm(w.text).replace("-", "") for w in words]
    found: list[tuple[int, int]] = []
    i = 0
    while i < len(norm):
        length = 0
        lo = max(0, i - REPEAT_WINDOW_WORDS)
        # самая длинная фраза, начинающаяся здесь и уже звучавшая рядом; последним словом предложения фраза
        # не начинается — иначе в «…is money. They need money. They need money» отметится «money. They need»
        longest = 1 if _ends(words[i].text, _SENTENCE_END) else min(REPEAT_MAX_LEN, len(norm) - i)
        for n in range(longest, 1, -1):
            gram = norm[i : i + n]
            if not all(gram) or all(w in _STOPWORDS for w in gram):
                continue
            earlier = sum(norm[j : j + n] == gram for j in range(lo, i - n + 1))
            adjacent = i >= n and norm[i - n : i] == gram
            if earlier >= (1 if n >= 3 or adjacent else 2):
                length = n
                break
        if not length and i > 0 and norm[i] and norm[i] == norm[i - 1] and norm[i] not in _REPEAT_OK:
            length = 1
        if length:
            found.append((i, length))
            i += length
        else:
            i += 1
    return found


def word_spans(transcript: Transcript) -> list[tuple[int, int]]:
    """Где каждое слово стоит в transcript.text (символы) — чтобы приложение отметило ошибку прямо в тексте."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for w in transcript.words:
        token = w.text.strip()
        at = transcript.text.find(token, cursor) if token else -1
        if at < 0:  # текст и слова разошлись — ставим точку на текущем месте
            spans.append((cursor, cursor))
            continue
        spans.append((at, at + len(token)))
        cursor = at + len(token)
    return spans


def find_gaps(words: Sequence[Word], min_sec: float, max_sec: float = float("inf")) -> list[tuple[float, float]]:
    """Паузы между словами: (время начала паузы, длительность)."""
    gaps = []
    for prev, cur in pairwise(words):
        gap = cur.start - prev.end
        if min_sec <= gap <= max_sec:
            gaps.append((prev.end, round(gap, 2)))
    return gaps


def classify_pauses(words: Sequence[Word]) -> dict[str, list[tuple[float, float]]]:
    """Только плохие паузы — посреди фразы: запинка или длинная пауза. Пауза после конца фразы не отмечается."""
    result: dict[str, list[tuple[float, float]]] = {"hesitation": [], "long": []}
    for prev, cur in pairwise(words):
        gap = round(cur.start - prev.end, 2)
        if gap < HESITATION_SEC:
            continue
        if _ends(prev.text, _SENTENCE_END):
            continue
        if gap >= LONG_PAUSE_SEC:
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


def gaze_on_ratio(gaze: Sequence[GazePoint], duration: float) -> float | None:
    """Доля времени со взглядом в зал; None — взгляд не измерялся (нет камеры, например в вебе)."""
    if not gaze or duration <= 0:
        return None
    on = sum(end - start for start, end, is_on in _gaze_spans(gaze, duration) if is_on)
    return round(on / duration, 3)


def pace_alerts(
    words: Sequence[Word], filler_starts: set[float], limits: tuple[int, int] = PACE_RANGE_WPM
) -> list[tuple[float, int]]:
    """Темп по окнам 20 с чистого времени речи (длинные паузы вырезаны) вне коридора limits (слов в минуту)."""
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
            if not limits[0] <= wpm <= limits[1]:
                alerts.append((round(window_start_real, 2), wpm))
            window_start_real, window_start_speech, count = cur.start, speech_t, 0
        if cur.start not in filler_starts:
            count += 1
    return alerts


# --- оценки 0–100 ---


def fillers_score(per_min: float) -> int:
    return round(_interp(per_min, [(1, 100), (5, 40), (12, 0)]))


def pace_score(wpm: float, mode: str = "normal") -> int:
    return round(_interp(wpm, _PACE_CURVES[mode]))


def pauses_score(long_pauses: int) -> int:
    return max(0, 100 - 10 * long_pauses)


def gaze_score(ratio: float) -> int:
    return round(_interp(ratio, [(0, 0), (0.7, 100)]))


def timing_score(duration: float, min_sec: float, max_sec: float) -> int:
    return round(_interp(duration, [(0, 0), (min_sec, 100), (max_sec, 100), (max_sec + 60, 0)]))


def analyze(
    transcript: Transcript,
    gaze: Sequence[GazePoint],
    min_sec: float,
    max_sec: float,
    filler_verdicts: Mapping[int, bool] | None = None,
    pace: str = "normal",
) -> DeliveryAnalysis:
    words = transcript.words
    duration = transcript.duration
    speech_min = speech_minutes(words)

    hits = filler_hits(words, filler_verdicts)
    fillers = [(words[i].start, hit) for i, _, hit in hits]
    filler_starts = {t for t, _ in fillers}
    spans_in_text = word_spans(transcript)
    by_end = {w.end: i for i, w in enumerate(words)}
    by_start = {w.start: i for i, w in enumerate(words)}

    def after_word(t: float) -> dict[str, int | None]:
        """Точка в тексте сразу после слова, которое закончилось в момент t (пауза стоит между словами)."""
        i = by_end.get(t)
        return {"start": spans_in_text[i][1], "end": spans_in_text[i][1]} if i is not None else {}

    def before_word(t: float) -> dict[str, int | None]:
        i = by_start.get(t)
        return {"start": spans_in_text[i][0], "end": spans_in_text[i][0]} if i is not None else {}

    def at_time(t: float) -> dict[str, int | None]:
        """Точка в тексте перед первым словом, прозвучавшим не раньше момента t (взгляд к словам не привязан)."""
        i = next((k for k, w in enumerate(words) if w.end >= t), None)
        return {"start": spans_in_text[i][0], "end": spans_in_text[i][0]} if i is not None else {}

    def over_words(i: int, n: int) -> dict[str, int | None]:
        return {"start": spans_in_text[i][0], "end": spans_in_text[i + n - 1][1]}

    pauses = classify_pauses(words)
    long_pauses = pauses["long"]
    spans = _gaze_spans(gaze, duration)
    ratio = gaze_on_ratio(gaze, duration)
    content_words = len(words) - sum(len(f.split()) for _, f in fillers)
    wpm = round(content_words / speech_min)
    fillers_per_min = round(len(fillers) / speech_min, 1)

    parts = {
        "fillers": fillers_score(fillers_per_min),
        "pace": pace_score(wpm, pace),
        "gaze": gaze_score(ratio) if ratio is not None else None,
        "pauses": pauses_score(len(long_pauses)),
        "timing": timing_score(duration, min_sec, max_sec),
    }
    # без камеры взгляд не измерен: его вес делится между остальными частями, а не даётся даром
    measured = {k: w for k, w in WEIGHTS.items() if parts[k] is not None}
    total = round(sum(parts[k] * w for k, w in measured.items()) / sum(measured.values()))

    events = [TimelineEvent(type="filler", t=words[i].start, text=f"«{hit}»", **over_words(i, n)) for i, n, hit in hits]
    filler_words = {i + k for i, n, _ in hits for k in range(n)}
    for i, n in find_repeats(words):
        if filler_words.isdisjoint(range(i, i + n)):  # «um, um» уже отмечено как паразит
            phrase = " ".join(_norm(w.text) for w in words[i : i + n])
            events.append(TimelineEvent(type="repeat", t=words[i].start, text=f"Repeated: «{phrase}»", **over_words(i, n)))
    swears = find_profanity(words)
    total = max(0, total - min(PROFANITY_MAX, PROFANITY_COST * len(swears)))  # ругань бьёт по подаче напрямую
    events += [
        TimelineEvent(type="profanity", t=words[i].start, text=f"Swearing: «{word}»", **over_words(i, 1)) for i, word in swears
    ]
    events += [
        TimelineEvent(type="long_pause", t=t, text=f"Pause of {d:.1f} s mid-phrase", **after_word(t))
        for t, d in long_pauses
    ]
    events += [
        TimelineEvent(type="hesitation", t=t, text=f"Hesitation of {d:.1f} s mid-phrase", **after_word(t))
        for t, d in pauses["hesitation"]
    ]
    events += [
        TimelineEvent(type="gaze_off", t=round(start, 2), text=f"Looking away for {end - start:.0f} s", **at_time(start))
        for start, end, on in spans
        if not on and end - start > GAZE_OFF_SEC
    ]
    for t, window_wpm in pace_alerts(words, filler_starts, PACE_RANGES[pace]):
        verdict = "too fast" if window_wpm > PACE_RANGES[pace][1] else "too slow"
        events.append(TimelineEvent(type="pace", t=t, text=f"Pace {window_wpm} words/min — {verdict}", **before_word(t)))
    events.sort(key=lambda e: e.t)

    return DeliveryAnalysis(
        metrics=Metrics(
            duration_sec=round(duration, 1),
            words=content_words,
            wpm=wpm,
            fillers=len(fillers),
            fillers_per_min=fillers_per_min,
            long_pauses=len(long_pauses),
            profanity=len(swears),
            gaze_on_ratio=ratio,
        ),
        score=DeliveryScore(total=total, **parts),
        events=events,
    )
