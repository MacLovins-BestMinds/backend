"""Роутер /api/ai. Пока реализации нет, эндпоинты отвечают моками при ?mock=1 или AI_MOCK=1."""

import os
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
from pydantic import TypeAdapter, ValidationError

from app.ai import mocks
from app.ai.schemas import (
    DeliveryResponse,
    GazePoint,
    JuryAnswerResponse,
    JuryQuestionsResponse,
    RefineRequest,
    RefineResponse,
)

router = APIRouter(prefix="/api/ai", tags=["ai"])

PCM_BYTES_PER_SEC = 16_000 * 2  # 16 кГц, моно, 16 бит
MOCK_LIVE_EVENT_EVERY_SEC = 5.0

_gaze_adapter = TypeAdapter(list[GazePoint])


def _mock_forced() -> bool:
    return os.getenv("AI_MOCK", "0") == "1"


def require_mock(mock: Annotated[bool, Query()] = False) -> None:
    if not (mock or _mock_forced()):
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


@router.post("/rounds/{round_id}/delivery", dependencies=[MockOnly])
async def delivery(
    round_id: str,
    audio: Annotated[UploadFile, File(description="запись выступления, m4a/AAC")],
    gaze: Annotated[list[GazePoint], Depends(parse_gaze)],
) -> DeliveryResponse:
    return mocks.delivery()


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
    if not (mock or _mock_forced()):
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
