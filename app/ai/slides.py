"""POST /api/ai/fit-slides: презентация (PDF или PPTX) + текст питча → что говорить на каждом слайде.

PDF уходит в Gemini целиком — он видит и текст, и картинки слайдов. Из PPTX достаём текст слайдов и заметки
(сам файл Gemini не читает). Слайд про демо в питче превращается в «Demo time.».
Тексты — на языке текста игрока (его называет модель); текста нет — STT_LANGUAGE.
"""

import io
from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field

from app.ai import llm
from app.ai.clients import gemini_client
from app.ai.config import get_settings
from app.ai.pitch import AUDIENCE_RU, Audience
from app.core.lang import default_lang, language_name, normalize_lang, pick, text_lang

MAX_SLIDES = 40
DEMO_TEXTS = {"en": "Demo time.", "ru": "Время демо.", "ro": "Momentul demo."}
DEMO_TEXT = DEMO_TEXTS["en"]
SLIDE_LABELS = {"en": "Slide", "ru": "Слайд", "ro": "Slide"}
PDF, PPTX = "application/pdf", "application/vnd.openxmlformats-officedocument.presentationml.presentation"

SLIDES_ERRORS: dict[str, dict[str, str]] = {
    "en": {
        "format": "Upload the presentation as a PDF or PPTX file",
        "no_slides": "There are no slides in this presentation",
        "too_many": "The presentation has {count} slides — at most {max} fit a short pitch",
        "no_pages": "There are no pages in this PDF",
        "too_many_pages": "The PDF has {count} pages — at most {max} fit a short pitch",
    },
    "ru": {
        "format": "Загрузи презентацию файлом PDF или PPTX",
        "no_slides": "В презентации нет слайдов",
        "too_many": "В презентации {count} слайдов — в короткий питч помещается не больше {max}",
        "no_pages": "В этом PDF нет страниц",
        "too_many_pages": "В PDF {count} страниц — в короткий питч помещается не больше {max}",
    },
    "ro": {
        "format": "Încarcă prezentarea ca fișier PDF sau PPTX",
        "no_slides": "Prezentarea nu are slide-uri",
        "too_many": "Prezentarea are {count} slide-uri — într-un pitch scurt încap cel mult {max}",
        "no_pages": "Acest PDF nu are pagini",
        "too_many_pages": "PDF-ul are {count} pagini — într-un pitch scurt încap cel mult {max}",
    },
}


class SlidesError(ValueError):
    """Файл не подходит: не PDF и не PPTX, пустой или слишком длинный."""

    def __init__(self, key: str, **values: object) -> None:
        super().__init__(SLIDES_ERRORS["en"][key].format(**values))
        self.key = key
        self.values = values

    def message(self, lang: str) -> str:
        """Текст ошибки на языке интерфейса."""
        return pick(SLIDES_ERRORS, lang)[self.key].format(**self.values)


class FitSlide(BaseModel):
    n: int
    title: str
    kind: Literal["talk", "demo"]
    text: str


class FitSlidesDraft(BaseModel):
    language: str = Field("", description="ISO 639-1 code of the language the slide texts are written in: en, ru, ro, …")
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
    raise SlidesError("format")


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


def assemble(draft: FitSlidesDraft, fallback_lang: str = "en") -> FitSlidesResponse:
    """Слайд-демо — всегда ровно «Demo time.» (на языке текстов); номера идут подряд, что бы ни вернула модель."""
    lang = normalize_lang(draft.language) or fallback_lang
    demo, label = pick(DEMO_TEXTS, lang), pick(SLIDE_LABELS, lang)
    slides = [
        s.model_copy(update={"n": i, "text": demo if s.kind == "demo" else s.text.strip()})
        for i, s in enumerate(draft.slides, 1)
    ]
    text = "\n\n".join(f"[{label} {s.n} — {s.title}]\n{s.text}" for s in slides)
    return FitSlidesResponse(slides=slides, text=text)


async def run_fit_slides(filename: str, data: bytes, title: str, text: str, audience: str) -> FitSlidesResponse:
    """Тексты слайдов — на языке текста игрока; текста нет или язык не понять — STT_LANGUAGE."""
    guessed = text_lang(text) or default_lang()
    kind = file_kind(filename, data)
    variables = {
        "title": title or "(not given)",
        "audience": _audience(audience),
        "text": text.strip() or "(the speaker has not written anything yet)",
        "fallback_language": language_name(guessed),
    }
    if kind == PPTX:
        slides = pptx_slides(data)
        if not slides:
            raise SlidesError("no_slides")
        if len(slides) > MAX_SLIDES:
            raise SlidesError("too_many", count=len(slides), max=MAX_SLIDES)
        listing = "\n\n".join(f"--- Slide {i} ---\n{s}" for i, s in enumerate(slides, 1))
        return assemble(await llm.generate("fit_slides", FitSlidesDraft, slides=listing, **variables), guessed)

    pages = pdf_pages(data)
    if pages == 0:
        raise SlidesError("no_pages")
    if pages > MAX_SLIDES:
        raise SlidesError("too_many_pages", count=pages, max=MAX_SLIDES)
    prompt = llm.load_prompt("fit_slides").substitute(
        slides=f"The deck is the attached PDF: each of its {pages} pages is one slide. Look at the pages themselves.", **variables
    )
    response = await gemini_client().aio.models.generate_content(
        model=get_settings().gemini_model,
        contents=[types.Part.from_bytes(data=data, mime_type=PDF), prompt],
        config=types.GenerateContentConfig(temperature=0, seed=0, response_mime_type="application/json", response_schema=FitSlidesDraft),
    )
    return assemble(FitSlidesDraft.model_validate_json(response.text or ""), guessed)
