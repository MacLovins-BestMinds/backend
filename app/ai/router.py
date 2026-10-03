"""Роутер /api/ai. При ?mock=1 или AI_MOCK=1 отвечает моками; нереализованное без мока — 501."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

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
from app.ai.config import get_settings
from app.ai.delivery import run_delivery
from app.ai.jury import MissingResultError, run_jury_answer, run_jury_questions
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

PCM_BYTES_PER_SEC = 16_000 * 2  # 16 кГц, моно, 16 бит
MOCK_LIVE_EVENT_EVERY_SEC = 5.0

# round_id попадает в путь к mp3 на диске — только безопасные символы
RoundId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]

_gaze_adapter = TypeAdapter(list[GazePoint])


def is_mock(mock: Annotated[bool, Query()] = False) -> bool:
    return mock or get_settings().ai_mock


def require_mock(use_mock: Annotated[bool, Depends(is_mock)]) -> None:
    if not use_mock:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Ещё не реализовано, используйте ?mock=1")


MockOnly = Depends(require_mock)
UseMock = Annotated[bool, Depends(is_mock)]


def parse_gaze(gaze: Annotated[str, Form()] = "[]") -> list[GazePoint]:
    try:
        return _gaze_adapter.validate_json(gaze)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"gaze: {e.errors(include_url=False)}") from e


async def read_audio(upload: UploadFile) -> bytes:
    data = await upload.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "audio: пустой файл")
    if len(data) > get_settings().max_audio_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "audio: файл слишком большой")
    return data


@contextmanager
def ai_errors(op: str, round_id: str) -> Iterator[None]:
    """Единое отображение ошибок пайплайна в HTTP-ответы."""
    try:
        yield
    except AudioConversionError as e:
        logger.warning("%s: ffmpeg не прочитал запись, round=%s: %s", op, round_id, e)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "audio: не удалось прочитать запись, нужен m4a/AAC"
        ) from e
    except MissingResultError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except (OpenAIError, genai_errors.APIError) as e:
        logger.exception("%s: ошибка внешнего AI-сервиса, round=%s", op, round_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "AI-сервис недоступен, попробуйте ещё раз") from e


@router.post("/refine", dependencies=[MockOnly])
async def refine(body: RefineRequest) -> RefineResponse:
    return mocks.refine(body.text, body.mode)


@router.post("/rounds/{round_id}/delivery")
async def delivery(
    round_id: RoundId,
    audio: Annotated[UploadFile, File(description="запись выступления, m4a/AAC")],
    gaze: Annotated[list[GazePoint], Depends(parse_gaze)],
    use_mock: UseMock,
) -> DeliveryResponse:
    if use_mock:
        return mocks.delivery()
    data = await read_audio(audio)
    with ai_errors("delivery", round_id):
        return await run_delivery(round_id, data, gaze)


@router.post("/rounds/{round_id}/jury/questions")
async def jury_questions(round_id: RoundId, use_mock: UseMock) -> JuryQuestionsResponse:
    """2–3 вопроса жюри с mp3-озвучкой. Требует выполненного delivery; повторный вызов отдаёт те же вопросы."""
    if use_mock:
        return mocks.jury_questions(round_id)
    with ai_errors("jury_questions", round_id):
        return await run_jury_questions(round_id)


@router.post("/rounds/{round_id}/jury/answer")
async def jury_answer(
    round_id: RoundId,
    question_id: Annotated[str, Form()],
    audio: Annotated[UploadFile, File(description="ответ на вопрос, m4a")],
    use_mock: UseMock,
) -> JuryAnswerResponse:
    if use_mock:
        return mocks.jury_answer()
    data = await read_audio(audio)
    with ai_errors("jury_answer", round_id):
        try:
            return await run_jury_answer(round_id, question_id, data)
        except KeyError as e:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Вопрос {question_id} не найден") from e


@router.websocket("/live")
async def live(websocket: WebSocket, round_id: str, mock: bool = False) -> None:
    """Приложение шлёт бинарные куски PCM по 250 мс, сервер отвечает событиями filler/long_pause/pace."""
    if not (mock or get_settings().ai_mock):
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="not implemented, use ?mock=1")
        return

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
