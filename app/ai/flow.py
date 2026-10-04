"""Ход мысли: точный разбор полной расшифровки после выступления (GET /api/ai/rounds/{id}/flow).

Расшифровка из сохранённого delivery режется на фразы со временем; Gemini получает их пронумерованными и отвечает
моментами со ссылками на номера фраз, а не временем. Время, цитату и тон проставляет код: цитата всегда берётся
из расшифровки (фрагмент модели только ищется в ней), моменты с неверными номерами отбрасываются.
Всё — на языке речи раунда (speech_lang из delivery).
"""

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.ai import llm
from app.ai.delivery import _metrics_summary
from app.ai.delivery_metrics import _CLAUSE_END, _SENTENCE_END, FILLER_WORDS, _ends, _is_hesitation, _norm
from app.ai.pitch import resolve_pitch
from app.ai.schemas import GOOD_FLOW_KINDS, FlowKind, FlowMoment, Metrics
from app.core.lang import default_lang, language_name, normalize_lang, pick

logger = logging.getLogger(__name__)

SEGMENT_MAX_WORDS = 30
SEGMENT_SOFT_WORDS = 18  # длинное предложение режется по запятой
NO_PUNCT_PAUSE_SEC = 0.7  # расшифровка без знаков препинания режется по паузам
MIN_WORDS = 20  # меньше — питч слишком короткий для разбора хода мысли
MIN_SEGMENTS = 2
MAX_MOMENTS = 10
MAX_SPAN_SEGMENTS = 5
QUOTE_MAX_WORDS = 25
OPENING_SHARE = 0.25  # hook — в первых двух фразах или в первой четверти питча
CLOSING_SHARE = 0.75  # концовка — в последних двух фразах или в последней четверти

SHORT_TEXTS = {
    "en": {
        "empty": "We didn't hear any speech, so there is no flow of thought to review yet.",
        "short": "The pitch is too short to review its flow of thought. Speak for at least half a minute: an opening "
        "that hooks the room, one main point with an example and a clear ending.",
    },
    "ru": {
        "empty": "Мы не услышали речи — разбирать ход мысли пока не из чего.",
        "short": "Питч слишком короткий, чтобы разобрать ход мысли. Говори хотя бы полминуты: зацепи зал в начале, "
        "раскрой одну главную мысль на примере и чётко закончи.",
    },
    "ro": {
        "empty": "Nu am auzit nimic, așa că deocamdată nu avem ce analiza.",
        "short": "Pitch-ul e prea scurt ca să-i analizăm firul ideilor. Vorbește măcar jumătate de minut: un început "
        "care prinde sala, o idee principală cu un exemplu și un final clar.",
    },
}

# Строгость по уровню раунда (как CONTENT_LEVELS в delivery.py, но про ход мысли)
FLOW_LEVELS = {
    "easy": "EASY. A beginner's practice talk on an everyday topic, not a business pitch. Do not expect a problem, a "
    "solution or a call to action: a clear main point, a reason or a small story and a closing line are enough. "
    "Be encouraging, but honest.",
    "medium": "MEDIUM. Expect a clear position, reasons backed by examples, an answer to an obvious objection and a "
    "conclusion. An opinion without support is a weak moment.",
    "hard": "HARD. Judge like a demanding coach: expect an accurate idea, concrete examples or numbers, an answer to the "
    "strongest objection and a real call to action. Vague or generic talk is weak.",
}


@dataclass(frozen=True, slots=True)
class TimedWord:
    text: str
    start: float
    end: float
    a: int  # место в transcript (символы): начало и конец слова
    b: int


@dataclass(frozen=True, slots=True)
class Segment:
    first: int  # номера первого и последнего слова, включительно
    last: int
    start: float
    end: float
    text: str


_TOKEN = re.compile(r"\S+")


def timed_words(delivery: dict[str, Any]) -> list[TimedWord]:
    """Слова расшифровки со временем и местом в тексте — из сохранённого разбора (поле words).

    В старых разборах words нет: время слова оценивается по его месту в тексте и длительности записи.
    """
    text = delivery.get("transcript") or ""
    try:
        marks = [m for m in delivery.get("words") or [] if m["end"] > m["start"]]
        if marks:
            return [TimedWord(text[m["start"] : m["end"]], float(m["t"]), float(m["t_end"]), m["start"], m["end"]) for m in marks]
        duration = float((delivery.get("metrics") or {}).get("duration_sec") or 0)
    except (KeyError, TypeError, ValueError):
        logger.warning("flow: битые слова в разборе — считаю без них")
        return []
    if not text.strip() or duration <= 0:
        return []
    scale = duration / len(text)
    return [TimedWord(m.group(), m.start() * scale, m.end() * scale, m.start(), m.end()) for m in _TOKEN.finditer(text)]


def is_filler(token: str) -> bool:
    return token in FILLER_WORDS or _is_hesitation(token)


def speech_seconds(words: Sequence[TimedWord]) -> float:
    """Сколько секунд человек говорил: сумма слов и коротких пауз между ними (паузы длиннее 1 с не в счёт)."""
    total = sum(w.end - w.start for w in words)
    total += sum(gap for p, c in zip(words, words[1:], strict=False) if 0 < (gap := c.start - p.end) <= 1.0)
    return total


def build_segments(words: Sequence[TimedWord], text: str) -> list[Segment]:
    """Фразы со временем: по концу предложения, длинное — ещё и по запятой; без знаков препинания — по паузам."""
    punctuated = any(_ends(w.text, _SENTENCE_END) for w in words)
    segments: list[Segment] = []
    begin = 0
    for i, w in enumerate(words):
        n = i - begin + 1
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None:
            cut = True
        elif punctuated:
            cut = (
                _ends(w.text, _SENTENCE_END)
                or n >= SEGMENT_MAX_WORDS
                or (n >= SEGMENT_SOFT_WORDS and _ends(w.text, _CLAUSE_END))
            )
        else:
            cut = n >= SEGMENT_SOFT_WORDS or (n >= 4 and nxt.start - w.end >= NO_PUNCT_PAUSE_SEC)
        if cut:
            first = words[begin]
            segments.append(Segment(begin, i, first.start, w.end, text[first.a : w.b].strip()))
            begin = i + 1
    return segments


class MomentDraft(BaseModel):
    kind: FlowKind
    first: int = Field(description="index of the first segment of the moment")
    last: int = Field(description="index of the last segment of the moment, inclusive")
    quote: str = Field(description="exact continuous fragment of those segments, 3-15 words")
    comment: str


class FlowDraft(BaseModel):
    summary: str
    moments: list[MomentDraft]


def _find_quote(quote: str, words: Sequence[TimedWord], lo: int, hi: int) -> tuple[int, int] | None:
    """Где цитата модели стоит среди слов lo..hi: (первое, последнее слово). Паразиты при сравнении пропускаются —
    модель часто «чистит» цитату от «ну» и «um»."""
    wanted = [t for t in (_norm(x) for x in quote.split()) if t and not is_filler(t)]
    if not wanted:
        return None
    index = [k for k in range(lo, hi + 1) if (t := _norm(words[k].text)) and not is_filler(t)]
    tokens = [_norm(words[k].text) for k in index]
    for s in range(len(tokens) - len(wanted) + 1):
        if tokens[s : s + len(wanted)] == wanted:
            return index[s], index[s + len(wanted) - 1]
    return None


def _quote(words: Sequence[TimedWord], text: str, i: int, j: int) -> str:
    """Цитата из самой расшифровки, не длиннее QUOTE_MAX_WORDS слов."""
    clipped = min(j, i + QUOTE_MAX_WORDS - 1)
    quote = text[words[i].a : words[clipped].b].strip()
    return f"{quote}…" if clipped < j else quote


def _placed(kind: str, first: int, last: int, segments: Sequence[Segment], duration: float) -> str:
    """hook бывает только в начале, концовка — только в конце; в середине это просто сильное или слабое место."""
    if kind == "hook" and not (first <= 1 or segments[first].start <= OPENING_SHARE * duration):
        return "strong"
    if kind in ("strong_close", "weak_close") and not (
        last >= len(segments) - 2 or segments[first].start >= CLOSING_SHARE * duration
    ):
        return "strong" if kind == "strong_close" else "weak"
    return kind


def assemble(draft: FlowDraft, segments: Sequence[Segment], words: Sequence[TimedWord], text: str) -> list[FlowMoment]:
    """Проверить моменты модели и проставить время, цитату и тон. Неверные номера фраз и пустые комментарии —
    в корзину; пересекающиеся моменты — первый по времени; hook один (первый), концовка одна (последняя)."""
    n = len(segments)
    duration = segments[-1].end if segments else 0.0
    seg_of = {k: s for s, seg in enumerate(segments) for k in range(seg.first, seg.last + 1)}
    found: list[tuple[int, int, str, str, str]] = []  # (первая фраза, последняя, вид, цитата, комментарий)
    for m in draft.moments:
        first, last, comment = m.first, m.last, m.comment.strip()
        if not (0 <= first <= last < n) or last - first >= MAX_SPAN_SEGMENTS or not comment:
            continue
        lo, hi = segments[max(0, first - 1)].first, segments[min(n - 1, last + 1)].last
        if hit := _find_quote(m.quote, words, lo, hi):
            i, j = hit
            if seg_of[i] < first or seg_of[j] > last:  # модель ошиблась номером на соседнюю фразу — верим цитате
                first, last = seg_of[i], seg_of[j]
            quote = _quote(words, text, i, j)
        else:  # такого фрагмента нет — цитатой будет сама фраза
            quote = _quote(words, text, segments[first].first, segments[first].last)
        found.append((first, last, _placed(m.kind, first, last, segments, duration), quote, comment))

    found.sort(key=lambda f: (f[0], f[1]))
    kept: list[tuple[int, int, str, str, str]] = []
    for item in found:
        if kept and item[0] <= kept[-1][1]:
            continue
        kept.append(item)
    closes = [k for k, item in enumerate(kept) if item[2] in ("strong_close", "weak_close")]
    hooks = [k for k, item in enumerate(kept) if item[2] == "hook"]
    demote = {"hook": "strong", "strong_close": "strong", "weak_close": "weak"}
    for k in hooks[1:] + closes[:-1]:
        first, last, kind, quote, comment = kept[k]
        kept[k] = (first, last, demote[kind], quote, comment)
    if len(kept) > MAX_MOMENTS:  # начало и концовку сохраняем, середину режем
        keep = set(hooks[:1] + closes[-1:])
        middle = [k for k in range(len(kept)) if k not in keep][: MAX_MOMENTS - len(keep)]
        kept = [kept[k] for k in sorted(keep | set(middle))]
    return [
        FlowMoment(
            t=round(segments[first].start, 2),
            end=round(segments[last].end, 2),
            kind=kind,
            tone="good" if kind in GOOD_FLOW_KINDS else "bad",
            quote=quote,
            comment=comment,
        )
        for first, last, kind, quote, comment in kept
    ]


def _metrics_text(delivery: dict[str, Any]) -> str:
    try:
        return _metrics_summary(Metrics.model_validate(delivery.get("metrics") or {}))
    except ValidationError:
        return "- not measured"


async def run_flow(round_id: str, delivery: dict[str, Any]) -> dict[str, Any]:
    """Ход мысли по сохранённому delivery: {"status": "ready", "summary", "moments"}. Ошибка LLM — исключение."""
    lang = normalize_lang(delivery.get("speech_lang")) or default_lang()
    text = delivery.get("transcript") or ""
    words = timed_words(delivery)
    if not words:
        return {"status": "ready", "summary": pick(SHORT_TEXTS, lang)["empty"], "moments": []}
    segments = build_segments(words, text)
    if sum(not is_filler(_norm(w.text)) for w in words) < MIN_WORDS or len(segments) < MIN_SEGMENTS:
        return {"status": "ready", "summary": pick(SHORT_TEXTS, lang)["short"], "moments": []}

    pitch = await asyncio.to_thread(resolve_pitch, round_id)
    draft = await llm.generate(
        "flow_review",
        FlowDraft,
        title=pitch.title,
        brief=pitch.brief,
        audience=pitch.audience_ru,
        own_text=f'- Prepared text of the speaker:\n"""\n{pitch.own_text}\n"""' if pitch.is_own else "",
        metrics=_metrics_text(delivery),
        segments="\n".join(f"[{k}] {s.start:.1f}–{s.end:.1f} s: {s.text}" for k, s in enumerate(segments)),
        last_index=len(segments) - 1,
        level_rules=FLOW_LEVELS.get(pitch.difficulty, FLOW_LEVELS["easy"]),
        speech_language=language_name(lang),
    )
    summary = draft.summary.strip()
    if not summary:
        raise ValueError("flow: пустой summary от модели")
    moments = assemble(draft, segments, words, text)
    logger.info("flow round=%s: %d моментов из %d предложенных", round_id, len(moments), len(draft.moments))
    return {"status": "ready", "summary": summary, "moments": [m.model_dump(mode="json") for m in moments]}
