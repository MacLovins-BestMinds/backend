"""POST /api/ai/refine: «Структурировать» и «Структурировать и улучшить» свой питч."""

import logging
from typing import get_args

from google.genai import errors as genai_errors
from openai import OpenAIError
from pydantic import BaseModel, Field

from app.ai import llm
from app.ai.pitch import AUDIENCE_FOCUS, AUDIENCE_RU
from app.ai.schemas import BlockKind, PitchBlock, RefineMode, RefineRequest, RefineResponse

logger = logging.getLogger(__name__)

BLOCK_KINDS: tuple[BlockKind, ...] = get_args(BlockKind.__value__)
BLOCK_TITLES: dict[BlockKind, str] = {
    "hook": "Хук",
    "problem": "Проблема",
    "solution": "Решение",
    "why_us": "Почему мы",
    "call_to_action": "Призыв",
}
MISSING_HINTS: dict[BlockKind, str] = {
    "hook": "Нет хука — начни с фразы, которая зацепит зал с первых секунд.",
    "problem": "Нет проблемы — скажи, чья это боль и почему она важна.",
    "solution": "Нет решения — объясни, что за продукт и как он помогает.",
    "why_us": "Нет блока «почему мы» — добавь результат, цифру или отличие от других.",
    "call_to_action": "Нет призыва — скажи, что слушатель должен сделать после питча.",
}


class DraftBlock(BaseModel):
    kind: BlockKind
    text: str


class StructureDraft(BaseModel):
    blocks: list[DraftBlock]


class ImproveDraft(BaseModel):
    weaknesses: list[str] = Field(max_length=4)
    blocks: list[DraftBlock]
    changes: list[str] = Field(max_length=4)


def to_blocks(draft: list[DraftBlock]) -> list[PitchBlock]:
    """Все пять блоков в каноническом порядке; если модель повторила блок, тексты склеиваются."""
    texts: dict[BlockKind, list[str]] = {kind: [] for kind in BLOCK_KINDS}
    for block in draft:
        if text := block.text.strip():
            texts[block.kind].append(text)
    return [PitchBlock(kind=k, title=BLOCK_TITLES[k], text=" ".join(texts[k])) for k in BLOCK_KINDS]


def render(blocks: list[PitchBlock]) -> str:
    return "\n".join(f"{b.title}: {b.text}" for b in blocks if b.text)


def _fallback_blocks(text: str) -> list[PitchBlock]:
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
    return [PitchBlock(kind=k, title=BLOCK_TITLES[k], text=parts[k]) for k in BLOCK_KINDS]


async def run_refine(req: RefineRequest) -> RefineResponse:
    try:
        if req.mode is RefineMode.STRUCTURE:
            draft = await llm.generate(
                "refine_structure", StructureDraft, text=req.text, audience=AUDIENCE_RU[req.audience]
            )
            blocks = to_blocks(draft.blocks)
            missing = [MISSING_HINTS[b.kind] for b in blocks if not b.text]
            notes = missing or ["Текст разложен по пяти блокам, слова почти не менялись."]
        else:
            draft = await llm.generate(
                "refine_improve",
                ImproveDraft,
                text=req.text,
                audience=AUDIENCE_RU[req.audience],
                audience_focus=AUDIENCE_FOCUS[req.audience],
            )
            blocks = to_blocks(draft.blocks)
            notes = [f"Слабое место: {w}" for w in draft.weaknesses] + [f"Изменено: {c}" for c in draft.changes]
        return RefineResponse(text=render(blocks), notes=notes, blocks=blocks)
    except (OpenAIError, genai_errors.APIError) as e:
        logger.warning("run_refine: сбой LLM (%s), используем резервную разметку блоков", e)
        blocks = _fallback_blocks(req.text)
        notes = ["ИИ сейчас недоступен: текст разложен по абзацам без правок — проверь блоки сам или попробуй позже."]
        return RefineResponse(text=render(blocks), notes=notes, blocks=blocks)
