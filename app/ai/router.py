import json
import asyncio
from typing import Optional, List
from fastapi import APIRouter, UploadFile, File, Form, Query, Path, WebSocket, WebSocketDisconnect

from app.ai.schemas import (
    RefineRequest,
    RefineResponse,
    DeliveryResponse,
    DeliveryScoreDetails,
    JuryQuestionsResponse,
    JurorQuestion,
    JuryAnswerResponse,
)
from app.game import service

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/refine", response_model=RefineResponse)
def refine_pitch(
    req: RefineRequest,
    mock: int = Query(0, description="1 для мок-ответа")
):
    """
    «Структурировать» раскладывает текст по блокам (хук, проблема, решение, почему мы, призыв).
    «Структурировать и улучшить» добавляет разбор слабых мест и переписанную версию.
    """
    if req.mode == "improve":
        refined_text = (
            f"1. [ХУК]: Знаете ли вы, с чем ежедневно сталкивается аудитория: {req.audience}?\n"
            f"2. [ПРОБЛЕМА]: Существующие методы отнимают слишком много времени и не дают нужного результата.\n"
            f"3. [РЕШЕНИЕ]: {req.text.strip()}\n"
            f"4. [ПОЧЕМУ МЫ]: Быстрый запуск, доказанная надёжность и прозрачная ценность.\n"
            f"5. [ПРИЗЫВ]: Приглашаю вас протестировать наше решение уже сегодня!"
        )
        notes = [
            "Усилен хук в начале для захвата внимания зала",
            f"Акцент сфокусирован под специфику «{req.audience}»",
            "Добавлен понятный и убедительный призыв к действию"
        ]
    else:
        refined_text = (
            f"[ХУК]: Внимание аудитории.\n"
            f"[ПРОБЛЕМА]: Ключевое препятствие.\n"
            f"[РЕШЕНИЕ]: {req.text.strip()}\n"
            f"[ПОЧЕМУ МЫ]: Главное преимущество.\n"
            f"[ПРИЗЫВ]: Конкретный следующий шаг."
        )
        notes = [
            "Текст структурирован по классической формуле питча",
            "Оригинальные мысли и формулировки сохранены"
        ]

    return RefineResponse(text=refined_text, notes=notes)


@router.websocket("/live")
async def live_analysis(websocket: WebSocket, round_id: Optional[str] = Query(None)):
    """
    WebSocket для живых сигналов зала:
    Приложение шлёт бинарные куски PCM 16 кГц, моно, 16 бит по 250 мс.
    Обратно летят события filler, long_pause, pace.
    """
    await websocket.accept()
    try:
        ticks = 0
        while True:
            # Читаем приходящие аудио чанки или текстовые пинги
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=3.0)
                if "bytes" in data:
                    pcm_chunk = data["bytes"]
                elif "text" in data:
                    text_msg = data["text"]
            except asyncio.TimeoutError:
                pass

            ticks += 1
            # Эмулируем события зала для интерактивной реакции
            if ticks % 4 == 1:
                event = {"type": "filler", "word": "э-э-э", "t": ticks * 2}
                await websocket.send_json(event)
            elif ticks % 4 == 2:
                event = {"type": "pace", "wpm": 140, "t": ticks * 2}
                await websocket.send_json(event)
            elif ticks % 4 == 3:
                event = {"type": "attention", "delta": 4, "t": ticks * 2}
                await websocket.send_json(event)

            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


@router.post("/rounds/{round_id}/delivery", response_model=DeliveryResponse)
async def analyze_delivery(
    round_id: str = Path(...),
    audio: UploadFile = File(None),
    gaze: Optional[str] = Form(None),
    mock: int = Query(0, description="1 для мок-ответа")
):
    """
    Анализ подачи (m4a/AAC + MediaPipe gaze):
    - транскрипт
    - оценки (паразиты 30%, темп 20%, взгляд 20%, паузы 15%, тайминг 15%)
    - события зала и советы
    """
    res = DeliveryResponse(
        transcript="Приветствую членов жюри и всех присутствующих! Сегодня мы представляем наш проект...",
        scores=DeliveryScoreDetails(
            total=78.5,
            fillers=80.0,
            pace=85.0,
            gaze=75.0,
            pauses=70.0,
            timing=85.0
        ),
        metrics={
            "words_per_minute": 138,
            "fillers_count": 2,
            "gaze_camera_percent": 76.0,
            "long_pauses_count": 1,
            "duration_sec": 75
        },
        events=[
            {"t": 12, "type": "filler", "word": "ну"},
            {"t": 34, "type": "pause", "duration": 3.2}
        ],
        tips=[
            "Отличный средний темп (138 слов в минуту).",
            "Старайтесь удерживать взгляд на камере телефона во время кульминации питча."
        ]
    )

    service.save_ai_result(round_id, "delivery", res.model_dump())
    service.save_ai_result(round_id, "content", {"score": 76.0, "comment": "Отличная структура и соответствие теме."})

    return res


@router.post("/rounds/{round_id}/jury/questions", response_model=JuryQuestionsResponse)
def get_jury_questions(
    round_id: str = Path(...),
    mock: int = Query(0, description="1 для мок-ответа")
):
    """
    2–3 вопроса жюри, строятся из прикола кейса и сказанного человеком.
    В ответе возвращаются id, juror, text, audio_url (mp3 в /static/audio/).
    """
    trick_text = "А если бабушка принципиально не пользуется смартфоном?"
    round_obj = service.get_round(round_id)
    if round_obj and round_obj.case_id:
        case = service.get_case(round_obj.case_id)
        if case and case.trick:
            trick_text = case.trick

    questions = [
        JurorQuestion(
            id="q1",
            juror="Анна (Строгая)",
            text=f"Ответьте прямо: {trick_text}",
            audio_url="/static/audio/q1.mp3"
        ),
        JurorQuestion(
            id="q2",
            juror="Игорь (Скептик)",
            text="Какова себестоимость решения и кто за него готов платить?",
            audio_url="/static/audio/q2.mp3"
        ),
        JurorQuestion(
            id="q3",
            juror="Михаил (Добряк)",
            text="Что вдохновило вас на выбор именно этой аудитории?",
            audio_url="/static/audio/q3.mp3"
        )
    ]
    return JuryQuestionsResponse(questions=questions)


@router.post("/rounds/{round_id}/jury/answer", response_model=JuryAnswerResponse)
async def submit_jury_answer(
    round_id: str = Path(...),
    question_id: str = Form(...),
    audio: UploadFile = File(None),
    mock: int = Query(0, description="1 для мок-ответа")
):
    """
    Оценка ответа на вопрос жюри: по существу, конкретно, коротко.
    """
    res = JuryAnswerResponse(
        score=82.0,
        comment="Отличный конкретный ответ, каверзный угол отработан аргументированно."
    )
    service.save_ai_result(round_id, "jury", {"question_id": question_id, "score": res.score, "comment": res.comment})
    return res
