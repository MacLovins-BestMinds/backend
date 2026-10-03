"""Роутер /api/ai. При ?mock=1 или AI_MOCK=1 отвечает моками; нереализованное без мока — 501."""

import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
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

_gaze_adapter = TypeAdapter(list[GazePoint])


def is_mock(mock: Annotated[bool, Query()] = False) -> bool:
    return mock or get_settings().ai_mock


def require_mock(use_mock: Annotated[bool, Depends(is_mock)]) -> None:
    if not use_mock:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "Ещё не реализовано, используйте ?mock=1")


MockOnly = Depends(require_mock)


def parse_gaze(gaze: Annotated[str, Form()] = "[]") -> list[GazePoint]:
    try:
        return _gaze_adapter.validate_json(gaze)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"gaze: {e.errors(include_url=False)}") from e


@router.post("/refine", dependencies=[MockOnly])
async def refine(body: RefineRequest) -> RefineResponse:
    return mocks.refine(body.text, body.mode)


async def read_audio(upload: UploadFile) -> bytes:
    data = await upload.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "audio: пустой файл")
    if len(data) > get_settings().max_audio_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "audio: файл слишком большой")
    return data


@router.post("/rounds/{round_id}/delivery")
async def delivery(
    round_id: str,
    audio: Annotated[UploadFile, File(description="запись выступления, m4a/AAC")],
    gaze: Annotated[list[GazePoint], Depends(parse_gaze)],
    use_mock: Annotated[bool, Depends(is_mock)],
) -> DeliveryResponse:
    if use_mock:
        return mocks.delivery()
    data = await read_audio(audio)
    try:
        return await run_delivery(round_id, data, gaze)
    except AudioConversionError as e:
        logger.warning("delivery: ffmpeg не прочитал запись, round=%s: %s", round_id, e)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "audio: не удалось прочитать запись, нужен m4a/AAC"
        ) from e
    except (OpenAIError, genai_errors.APIError) as e:
        logger.exception("delivery: ошибка внешнего AI-сервиса, round=%s", round_id)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "AI-сервис недоступен, попробуйте ещё раз") from e


@router.post("/rounds/{round_id}/jury/questions", dependencies=[MockOnly])
async def jury_questions(round_id: str) -> JuryQuestionsResponse:
    return mocks.jury_questions(round_id)


@router.post("/rounds/{round_id}/jury/answer", dependencies=[MockOnly])
async def jury_answer(
    round_id: str,
    question_id: Annotated[str, Form()],
    audio: Annotated[UploadFile, File(description="ответ на вопрос, m4a")],
) -> JuryAnswerResponse:
    return mocks.jury_answer()


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
