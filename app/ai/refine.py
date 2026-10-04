"""POST /api/ai/refine: «Структурировать» и «Структурировать и улучшить» свой питч."""

import logging
from typing import get_args

from google.genai import errors as genai_errors
from openai import OpenAIError
from pydantic import BaseModel, Field

from app.ai import llm
from app.ai.pitch import AUDIENCE_FOCUS, AUDIENCE_RU
from app.ai.schemas import BlockKind, PitchBlock, RefineMode, RefineRequest, RefineResponse
from app.core.lang import default_lang, language_name, normalize_lang, pick, text_lang

logger = logging.getLogger(__name__)

BLOCK_KINDS: tuple[BlockKind, ...] = get_args(BlockKind.__value__)
# Подписи и заметки — на языке текста игрока (его называет модель, без LLM — угадываем по буквам)
BLOCK_TITLES_BY_LANG: dict[str, dict[BlockKind, str]] = {
    "en": {
        "hook": "Hook",
        "problem": "Problem",
        "solution": "Solution",
        "why_us": "Why us",
        "call_to_action": "Call to action",
    },
    "ru": {
        "hook": "Хук",
        "problem": "Проблема",
        "solution": "Решение",
        "why_us": "Почему мы",
        "call_to_action": "Призыв к действию",
    },
    "ro": {
        "hook": "Cârlig",
        "problem": "Problema",
        "solution": "Soluția",
        "why_us": "De ce noi",
        "call_to_action": "Îndemn la acțiune",
    },
}
BLOCK_TITLES = BLOCK_TITLES_BY_LANG["en"]
MISSING_HINTS_BY_LANG: dict[str, dict[BlockKind, str]] = {
    "en": {
        "hook": "No hook — open with a line that grabs the audience in the first seconds.",
        "problem": "No problem — say whose pain this is and why it matters.",
        "solution": "No solution — explain what the product is and how it helps.",
        "why_us": "No \"why us\" — add a result, a number or what sets you apart.",
        "call_to_action": "No call to action — say what the listener should do after the pitch.",
    },
    "ru": {
        "hook": "Нет хука — начни с фразы, которая зацепит зал в первые секунды.",
        "problem": "Нет проблемы — скажи, чья это боль и почему она важна.",
        "solution": "Нет решения — объясни, что это за продукт и как он помогает.",
        "why_us": "Нет «почему мы» — добавь результат, цифру или то, чем ты отличаешься.",
        "call_to_action": "Нет призыва к действию — скажи, что слушателю сделать после питча.",
    },
    "ro": {
        "hook": "Lipsește cârligul — începe cu o frază care prinde publicul din primele secunde.",
        "problem": "Lipsește problema — spune a cui este durerea și de ce contează.",
        "solution": "Lipsește soluția — explică ce este produsul și cum ajută.",
        "why_us": "Lipsește „de ce noi” — adaugă un rezultat, o cifră sau ce te face diferit.",
        "call_to_action": "Lipsește îndemnul la acțiune — spune ce trebuie să facă ascultătorul după pitch.",
    },
}
MISSING_HINTS = MISSING_HINTS_BY_LANG["en"]
NOTE_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "sorted": "The text is sorted into five blocks; your words are almost unchanged.",
        "weak": "Weak spot: {text}",
        "changed": "Changed: {text}",
        "offline": "AI is unavailable right now: the text was split by paragraphs without edits — check the blocks "
        "yourself or try again later.",
    },
    "ru": {
        "sorted": "Текст разложен на пять блоков, твои слова почти не изменены.",
        "weak": "Слабое место: {text}",
        "changed": "Изменено: {text}",
        "offline": "AI сейчас недоступен: текст разбит по абзацам без правок — проверь блоки сам или попробуй позже.",
    },
    "ro": {
        "sorted": "Textul este împărțit în cinci blocuri, cuvintele tale aproape nu s-au schimbat.",
        "weak": "Punct slab: {text}",
        "changed": "Modificat: {text}",
        "offline": "AI-ul nu este disponibil acum: textul a fost împărțit pe paragrafe, fără modificări — verifică "
        "blocurile singur sau încearcă mai târziu.",
    },
}

LANGUAGE_FIELD = "ISO 639-1 code of the language of the speaker's text: en, ru, ro, …"


class DraftBlock(BaseModel):
    kind: BlockKind
    text: str


class StructureDraft(BaseModel):
    language: str = Field("", description=LANGUAGE_FIELD)
    blocks: list[DraftBlock]


class ImproveDraft(BaseModel):
    language: str = Field("", description=LANGUAGE_FIELD)
    weaknesses: list[str] = Field(max_length=4)
    blocks: list[DraftBlock]
    changes: list[str] = Field(max_length=4)


def to_blocks(draft: list[DraftBlock], lang: str = "en") -> list[PitchBlock]:
    """Все пять блоков в каноническом порядке; если модель повторила блок, тексты склеиваются."""
    titles = pick(BLOCK_TITLES_BY_LANG, lang)
    texts: dict[BlockKind, list[str]] = {kind: [] for kind in BLOCK_KINDS}
    for block in draft:
        if text := block.text.strip():
            texts[block.kind].append(text)
    return [PitchBlock(kind=k, title=titles[k], text=" ".join(texts[k])) for k in BLOCK_KINDS]


def render(blocks: list[PitchBlock]) -> str:
    return "\n".join(f"{b.title}: {b.text}" for b in blocks if b.text)


def _fallback_blocks(text: str, lang: str = "en") -> list[PitchBlock]:
    titles = pick(BLOCK_TITLES_BY_LANG, lang)
    lines = [p.strip() for p in text.split("\n") if p.strip()]
    if not lines:
        lines = [text.strip()]
    count = len(lines)
    parts = {
        "hook": lines[0] if count > 0 else "",
        "problem": lines[1] if count > 1 else "",
        "solution": lines[2] if count > 2 else "",
        "why_us": lines[3] if count > 3 else "",
        "call_to_action": " ".join(lines[4:]) if count > 4 else "",
    }
    return [PitchBlock(kind=k, title=titles[k], text=parts[k]) for k in BLOCK_KINDS]


async def run_refine(req: RefineRequest, ui_lang: str = "en") -> RefineResponse:
    """Ответ — на языке текста игрока (не понять — STT_LANGUAGE); ui_lang — только для заметки «AI недоступен»."""
    guessed = text_lang(req.text) or default_lang()
    languages = {"fallback_language": language_name(guessed)}
    try:
        if req.mode is RefineMode.STRUCTURE:
            draft = await llm.generate(
                "refine_structure", StructureDraft, text=req.text, audience=AUDIENCE_RU[req.audience], **languages
            )
            lang = normalize_lang(draft.language) or guessed
            blocks = to_blocks(draft.blocks, lang)
            missing = [pick(MISSING_HINTS_BY_LANG, lang)[b.kind] for b in blocks if not b.text]
            notes = missing or [pick(NOTE_TEXTS, lang)["sorted"]]
        else:
            draft = await llm.generate(
                "refine_improve",
                ImproveDraft,
                text=req.text,
                audience=AUDIENCE_RU[req.audience],
                audience_focus=AUDIENCE_FOCUS[req.audience],
                **languages,
            )
            lang = normalize_lang(draft.language) or guessed
            blocks = to_blocks(draft.blocks, lang)
            texts = pick(NOTE_TEXTS, lang)
            notes = [texts["weak"].format(text=w) for w in draft.weaknesses]
            notes += [texts["changed"].format(text=c) for c in draft.changes]
        return RefineResponse(text=render(blocks), notes=notes, blocks=blocks)
    except (OpenAIError, genai_errors.APIError) as e:
        logger.warning("run_refine: сбой LLM (%s), используем резервную разметку блоков", e)
        blocks = _fallback_blocks(req.text, guessed)
        notes = [pick(NOTE_TEXTS, ui_lang)["offline"]]  # по сути сообщение об ошибке — на языке интерфейса
        return RefineResponse(text=render(blocks), notes=notes, blocks=blocks)
