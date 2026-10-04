"""POST /api/ai/fit-slides: презентация (PDF или PPTX) + текст питча → что говорить на каждом слайде.

PDF уходит в Gemini целиком — он видит и текст, и картинки слайдов. Из PPTX достаём текст слайдов и заметки
(сам файл Gemini не читает). Слайд про демо в питче превращается в «Demo time.».
"""

import io
from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field

from app.ai import llm
from app.ai.clients import gemini_client
from app.ai.config import get_settings
from app.ai.pitch import AUDIENCE_RU, Audience

MAX_SLIDES = 40
DEMO_TEXT = "Demo time."
PDF, PPTX = "application/pdf", "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class SlidesError(ValueError):
    """Файл не подходит: не PDF и не PPTX, пустой или слишком длинный."""


class FitSlide(BaseModel):
    n: int
    title: str
    kind: Literal["talk", "demo"]
    text: str


class FitSlidesDraft(BaseModel):
    slides: list[FitSlide] = Field(min_length=1, max_length=MAX_SLIDES)


class FitSlidesResponse(BaseModel):
    slides: list[FitSlide]
    text: str = Field(description="весь питч по слайдам одним текстом — его игрок учит и берёт на сцену")


def file_kind(filename: str, data: bytes) -> str:
    """Тип по содержимому, а не по имени: PDF начинается с %PDF, PPTX — zip-архив."""
    if data[:5] == b"%PDF-":
        return PDF
    if data[:2] == b"PK" and filename.lower().endswith(".pptx"):
        return PPTX
    raise SlidesError("Upload the presentation as a PDF or PPTX file")


def pptx_slides(data: bytes) -> list[str]:
    """Текст каждого слайда PPTX: заголовок, надписи и заметки докладчика."""
    from pptx import Presentation  # библиотека нужна только здесь

    out = []
    for slide in Presentation(io.BytesIO(data)).slides:
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        body = [s.text_frame.text.strip() for s in slide.shapes if s.has_text_frame and s != slide.shapes.title]
        notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide and slide.notes_slide.notes_text_frame else ""
        parts = [f"Title: {title}" if title else "Title: (none)"]
        parts += [f"Text: {t}" for t in body if t]
        if notes:
            parts.append(f"Speaker notes: {notes}")
        if len(parts) == 1 and not title:
            parts.append("(no text on this slide — probably a picture or a screenshot)")
        out.append("\n".join(parts))
    return out


def pdf_pages(data: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(data)).pages)


def _audience(raw: str) -> str:
    try:
        return AUDIENCE_RU[Audience(raw)]
    except (ValueError, KeyError):
        return raw or "general public"


def assemble(draft: FitSlidesDraft) -> FitSlidesResponse:
    """Слайд-демо — всегда ровно «Demo time.»; номера идут подряд, что бы ни вернула модель."""
    slides = [
        s.model_copy(update={"n": i, "text": DEMO_TEXT if s.kind == "demo" else s.text.strip()})
        for i, s in enumerate(draft.slides, 1)
    ]
    text = "\n\n".join(f"[Slide {s.n} — {s.title}]\n{s.text}" for s in slides)
    return FitSlidesResponse(slides=slides, text=text)


async def run_fit_slides(filename: str, data: bytes, title: str, text: str, audience: str) -> FitSlidesResponse:
    kind = file_kind(filename, data)
    variables = {"title": title or "(not given)", "audience": _audience(audience), "text": text.strip() or "(the speaker has not written anything yet)"}
    if kind == PPTX:
        slides = pptx_slides(data)
        if not slides:
            raise SlidesError("There are no slides in this presentation")
        if len(slides) > MAX_SLIDES:
            raise SlidesError(f"The presentation has {len(slides)} slides — at most {MAX_SLIDES} fit a short pitch")
        listing = "\n\n".join(f"--- Slide {i} ---\n{s}" for i, s in enumerate(slides, 1))
        return assemble(await llm.generate("fit_slides", FitSlidesDraft, slides=listing, **variables))

    pages = pdf_pages(data)
    if pages == 0:
        raise SlidesError("There are no pages in this PDF")
    if pages > MAX_SLIDES:
        raise SlidesError(f"The PDF has {pages} pages — at most {MAX_SLIDES} fit a short pitch")
    prompt = llm.load_prompt("fit_slides").substitute(
        slides=f"The deck is the attached PDF: each of its {pages} pages is one slide. Look at the pages themselves.", **variables
    )
    response = await gemini_client().aio.models.generate_content(
        model=get_settings().gemini_model,
        contents=[types.Part.from_bytes(data=data, mime_type=PDF), prompt],
        config=types.GenerateContentConfig(temperature=0, seed=0, response_mime_type="application/json", response_schema=FitSlidesDraft),
    )
    return assemble(FitSlidesDraft.model_validate_json(response.text or ""))
