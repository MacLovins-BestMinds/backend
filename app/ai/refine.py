"""POST /api/ai/refine: «Структурировать» и «Структурировать и улучшить» свой питч."""

from typing import get_args

from pydantic import BaseModel, Field

from app.ai import llm
from app.ai.pitch import AUDIENCE_FOCUS, AUDIENCE_RU
from app.ai.schemas import BlockKind, PitchBlock, RefineMode, RefineRequest, RefineResponse

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


async def run_refine(req: RefineRequest) -> RefineResponse:
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
