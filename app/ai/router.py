"""Роутер /api/ai. При ?mock=1 или AI_MOCK=1 все эндпоинты отвечают примерами ответов (моками)."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from elevenlabs.core.api_error import ApiError as ElevenLabsApiError
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from google.genai import errors as genai_errors
from openai import OpenAIError
from pydantic import TypeAdapter, ValidationError

from app.ai import mocks
from app.ai.audio import AudioConversionError
from app.ai.clients import MissingKeyError
from app.ai.config import get_settings
from app.ai.delivery import run_delivery
from app.ai.delivery_metrics import PaceMode
from app.ai.health import HealthResponse, run_health
from app.ai.jury import Difficulty, RoundStateError, run_jury_answer, run_jury_questions, run_jury_skip
from app.ai.live import PCM_BYTES_PER_SEC, run_live
from app.ai.pitch import RoundNotFoundError
from app.ai.refine import run_refine
from app.recordings import save_recording
from app.ai.slides import FitSlide, FitSlidesResponse, SlidesError, assemble, FitSlidesDraft, run_fit_slides
from app.ai.schemas import (
    DeliveryResponse,
    GazePoint,
    JuryAnswerResponse,
    JuryQuestionsResponse,
    RefineRequest,
    RefineResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai", tags=["ai"])

MOCK_LIVE_EVENT_EVERY_SEC = 5.0

# round_id попадает в путь к mp3 на диске — только безопасные символы
RoundId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]

_gaze_adapter = TypeAdapter(list[GazePoint])


def is_mock(mock: Annotated[bool, Query()] = False) -> bool:
    return mock or get_settings().ai_mock


UseMock = Annotated[bool, Depends(is_mock)]


def parse_gaze(gaze: Annotated[str, Form()] = "[]") -> list[GazePoint]:
    try:
        return _gaze_adapter.validate_json(gaze)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"gaze: {e.errors(include_url=False)}") from e


async def read_audio(upload: UploadFile) -> bytes:
    data = await upload.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "audio: empty file")
    if len(data) > get_settings().max_audio_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "audio: file is too large")
    return data


@contextmanager
def ai_errors(op: str, round_id: str) -> Iterator[None]:
    """Единое отображение ошибок пайплайна в HTTP-ответы."""
    try:
        yield
    except AudioConversionError as e:
        logger.warning("%s: ffmpeg не прочитал запись, round=%s: %s", op, round_id, e)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "audio: could not read the recording, m4a/AAC is expected"
        ) from e
    except RoundNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except RoundStateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except MissingKeyError as e:
        logger.error("%s: %s", op, e)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    except (OpenAIError, genai_errors.APIError, ElevenLabsApiError) as e:
        logger.exception("%s: ошибка внешнего AI-сервиса, round=%s", op, round_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The AI service is unavailable, please try again") from e


@router.get("/health")
async def health() -> HealthResponse:
    """Проверка внешних AI-сервисов с текущими ключами (Gemini, ElevenLabs, Azure) — запускать перед показом."""
    return await run_health()


@router.post("/refine")
async def refine(body: RefineRequest, use_mock: UseMock) -> RefineResponse:
    """structure — разложить свой текст по блокам почти без правок; improve — слабые места и переписанная версия."""
    if use_mock:
        return mocks.refine(body.mode)
    with ai_errors("refine", "-"):
        return await run_refine(body)


MAX_SLIDES_MB = 20


@router.post("/fit-slides")
async def fit_slides(
    file: Annotated[UploadFile, File(description="презентация: PDF или PPTX")],
    use_mock: UseMock,
    title: Annotated[str, Form()] = "",
    text: Annotated[str, Form(description="текст или тезисы питча — их раскладываем по слайдам")] = "",
    audience: Annotated[str, Form()] = "public",
) -> FitSlidesResponse:
    """Подогнать питч под презентацию: что говорить на каждом слайде. Слайд про демо → «Demo time.»."""
    if use_mock:
        return assemble(
            FitSlidesDraft(
                slides=[
                    FitSlide(n=1, title="The problem", kind="talk", text="Older people miss their medicine every day."),
                    FitSlide(n=2, title="Demo", kind="demo", text=""),
                    FitSlide(n=3, title="What we ask", kind="talk", text="We are looking for pharmacy chains as partners."),
                ]
            )
        )
    data = await file.read()
    if len(data) > MAX_SLIDES_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"The file is larger than {MAX_SLIDES_MB} MB")
    with ai_errors("fit_slides", "-"):
        try:
            return await run_fit_slides(file.filename or "", data, title, text, audience)
        except SlidesError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


@router.post("/rounds/{round_id}/delivery")
async def delivery(
    round_id: RoundId,
    audio: Annotated[UploadFile, File(description="запись выступления, m4a/AAC")],
    gaze: Annotated[list[GazePoint], Depends(parse_gaze)],
    use_mock: UseMock,
    notes: Annotated[str, Form(description="заметки с подготовки, необязательно")] = "",
    pace: Annotated[PaceMode, Form(description="темп, который выбрал игрок: slow | normal | fast")] = "normal",
    min_sec: Annotated[int | None, Form(ge=10, le=1800, description="свой лимит питча, нижняя граница, с")] = None,
    max_sec: Annotated[int | None, Form(ge=10, le=1800, description="свой лимит питча, верхняя граница, с")] = None,
) -> DeliveryResponse:
    if use_mock:
        return mocks.delivery()
    # игрок сам выбрал длину питча — тайминг оценивается по ней, а не по лимитам уровня
    if (min_sec is None) != (max_sec is None) or (min_sec is not None and max_sec is not None and min_sec >= max_sec):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "min_sec and max_sec go together, min_sec < max_sec")
    limits = (min_sec, max_sec) if min_sec is not None and max_sec is not None else None
    data = await read_audio(audio)
    with ai_errors("delivery", round_id):
        result = await run_delivery(round_id, data, gaze, notes, pace, limits)
    # звук остаётся на сервере, чтобы раунд из истории можно было переслушать
    save_recording(round_id, audio.filename, data)
    return result


@router.post("/rounds/{round_id}/jury/questions")
async def jury_questions(round_id: RoundId, use_mock: UseMock, difficulty: Difficulty | None = None) -> JuryQuestionsResponse:
    """2–3 вопроса жюри с mp3-озвучкой. Требует выполненного delivery; повторный вызов отдаёт те же вопросы."""
    if use_mock:
        return mocks.jury_questions(round_id)
    with ai_errors("jury_questions", round_id):
        return await run_jury_questions(round_id, difficulty)


@router.post("/rounds/{round_id}/jury/answer")
async def jury_answer(
    round_id: RoundId,
    question_id: Annotated[str, Form()],
    audio: Annotated[UploadFile, File(description="ответ на вопрос, m4a")],
    use_mock: UseMock,
    difficulty: Annotated[Difficulty | None, Form(description="переопределить уровень раунда: easy | medium | hard")] = None,
) -> JuryAnswerResponse:
    if use_mock:
        return mocks.jury_answer()
    data = await read_audio(audio)
    with ai_errors("jury_answer", round_id):
        try:
            return await run_jury_answer(round_id, question_id, data, difficulty)
        except KeyError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Question {question_id} not found") from e


@router.post("/rounds/{round_id}/jury/skip")
async def jury_skip(round_id: RoundId, question_id: Annotated[str, Form()], use_mock: UseMock) -> JuryAnswerResponse:
    """Пропустить вопрос жюри: ответ засчитывается с 0 баллов."""
    if use_mock:
        return JuryAnswerResponse(score=0, comment="Skipped — no points for this question.")
    with ai_errors("jury_skip", round_id):
        try:
            return await run_jury_skip(round_id, question_id)
        except KeyError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Question {question_id} not found") from e


@router.websocket("/live")
async def live(websocket: WebSocket, round_id: str, mock: bool = False, pace: str = "normal") -> None:
    """Приложение шлёт бинарные куски PCM 16 кГц по 250 мс, сервер отвечает событиями filler/long_pause/pace."""
    if mock or get_settings().ai_mock:
        await _live_mock(websocket)
    else:
        await run_live(websocket, round_id, pace)


async def _live_mock(websocket: WebSocket) -> None:
    await websocket.accept()
    events = mocks.live_events()
    received_bytes = 0
    next_event_at = MOCK_LIVE_EVENT_EVERY_SEC
    try:
        while True:
            received_bytes += len(await websocket.receive_bytes())
            elapsed = received_bytes / PCM_BYTES_PER_SEC
            if elapsed >= next_event_at:
                event = next(events).model_copy(update={"t": round(elapsed, 2)})
                await websocket.send_json(event.model_dump())
                next_event_at += MOCK_LIVE_EVENT_EVERY_SEC
    except WebSocketDisconnect:
        pass
