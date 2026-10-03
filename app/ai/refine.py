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
    "hook": "Hook",
    "problem": "Problem",
    "solution": "Solution",
    "why_us": "Why us",
    "call_to_action": "Call to action",
}
MISSING_HINTS: dict[BlockKind, str] = {
    "hook": "No hook — open with a line that grabs the audience in the first seconds.",
    "problem": "No problem — say whose pain this is and why it matters.",
    "solution": "No solution — explain what the product is and how it helps.",
    "why_us": "No \"why us\" — add a result, a number or what sets you apart.",
    "call_to_action": "No call to action — say what the listener should do after the pitch.",
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
            notes = missing or ["The text is sorted into five blocks; your words are almost unchanged."]
        else:
            draft = await llm.generate(
                "refine_improve",
                ImproveDraft,
                text=req.text,
                audience=AUDIENCE_RU[req.audience],
                audience_focus=AUDIENCE_FOCUS[req.audience],
            )
            blocks = to_blocks(draft.blocks)
            notes = [f"Weak spot: {w}" for w in draft.weaknesses] + [f"Changed: {c}" for c in draft.changes]
        return RefineResponse(text=render(blocks), notes=notes, blocks=blocks)
    except (OpenAIError, genai_errors.APIError) as e:
        logger.warning("run_refine: сбой LLM (%s), используем резервную разметку блоков", e)
        blocks = _fallback_blocks(req.text)
        notes = ["AI is unavailable right now: the text was split by paragraphs without edits — check the blocks yourself or try again later."]
        return RefineResponse(text=render(blocks), notes=notes, blocks=blocks)
