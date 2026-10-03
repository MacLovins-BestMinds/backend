import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app.core.db import engine
from app.game.models import User, Case, Round, RoundScore
from app.game import service


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_root_status(client):
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert data["docs"] == "/docs"


def test_demo_pitcher_seeded(client):
    # Проверка, что сид создал пользователя demo_pitcher со званием «Питчер» и трендом «up»
    response = client.get("/api/game/profile?user_id=demo_pitcher")
    assert response.status_code == 200
    data = response.json()
    assert data["nick"] == "demo_pitcher"
    assert data["rank"]["title"] == "Питчер"
    assert data["rank"]["trend"] == "up"
    assert len(data["last_rounds"]) >= 5


def test_spin_no_trick_leaked(client):
    # Колесо: категория -> кейс -> готовая тема. Прикол кейса НИКОГДА не уходит на фронт!
    response = client.get("/api/game/spin")
    assert response.status_code == 200
    data = response.json()
    assert "category" in data
    assert "id" in data["category"]
    assert "title" in data["category"]
    assert "case" in data
    assert "id" in data["case"]
    assert "title" in data["case"]
    assert "brief" in data["case"]
    assert "audience" in data["case"]
    assert "trick" not in data["case"]
    assert "quirk" not in data["case"]


def test_daily_deterministic(client):
    # Тема дня одинаковая для всех по хэшу даты
    r1 = client.get("/api/game/daily?date=2026-10-03")
    r2 = client.get("/api/game/daily?date=2026-10-03")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["case"]["id"] == r2.json()["case"]["id"]
    assert "trick" not in r1.json()["case"]


def test_tolik_functions_get_case_with_trick():
    # Толик получает кейс С ПРИКОЛОМ через get_case
    with Session(engine) as session:
        first_case = session.exec(select(Case)).first()
        assert first_case is not None
        case_with_trick = service.get_case(first_case.id)
        assert case_with_trick is not None
        assert case_with_trick.trick != ""


def test_round_flow(client):
    # 1. Авторизация по нику
    auth_resp = client.post("/api/game/auth", json={"nick": "egor_test"})
    assert auth_resp.status_code == 200
    user_id = auth_resp.json()["user_id"]

    # 2. Создание раунда тренировки
    round_resp = client.post("/api/game/rounds", json={
        "user_id": user_id,
        "mode": "training",
        "case_id": "health-01"
    })
    assert round_resp.status_code == 200
    round_data = round_resp.json()
    round_id = round_data["round_id"]
    assert round_data["prep_sec"] == 300
    assert round_data["pitch_min_sec"] == 60
    assert round_data["pitch_max_sec"] == 180

    # 3. Вопросы жюри для Толика (включают каверзный угол кейса)
    q_resp = client.post(f"/api/ai/rounds/{round_id}/jury/questions")
    assert q_resp.status_code == 200
    questions = q_resp.json()["questions"]
    assert len(questions) >= 2
    assert questions[0]["audio_url"].startswith("/static/audio/")

    # 4. Анализ подачи delivery
    deliv_resp = client.post(
        f"/api/ai/rounds/{round_id}/delivery",
        data={"gaze": '[{"t": 0.5, "on": true}]'}
    )
    assert deliv_resp.status_code == 200
    assert "scores" in deliv_resp.json()

    # 5. Ответ на вопрос жюри
    ans_resp = client.post(
        f"/api/ai/rounds/{round_id}/jury/answer",
        data={"question_id": "q1"}
    )
    assert ans_resp.status_code == 200
    assert "score" in ans_resp.json()

    # 6. Завершение раунда: сбор оценок, расчет 40/40/20, пересчет звания
    finish_resp = client.post(f"/api/game/rounds/{round_id}/finish")
    assert finish_resp.status_code == 200
    finish_data = finish_resp.json()
    assert "total" in finish_data
    assert "content" in finish_data
    assert "delivery" in finish_data
    assert "jury" in finish_data
    assert "rank" in finish_data
    assert finish_data["rank"]["title"] in ["Новичок", "Спикер", "Питчер", "Оратор", "Легенда"]


def test_static_audio(client):
    # Раздача статики для mp3 жюри
    resp = client.get("/static/audio/q1.mp3")
    assert resp.status_code == 200


def test_leaderboard(client):
    # Лидерборд дня
    resp = client.get("/api/game/leaderboard/daily")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_mock_parameter(client):
    # Любой эндпоинт с ?mock=1 отдает пример ответа
    resp = client.get("/api/game/spin?mock=1")
    assert resp.status_code == 200
    assert resp.json()["case"]["title"] != ""


def test_websocket_live(client):
    # WebSocket для передачи PCM аудио кусков и получения сигналов зала
    with client.websocket_connect("/api/ai/live?round_id=test_rnd") as ws:
        ws.send_bytes(b"\x00" * 4000)
        event = ws.receive_json()
        assert "type" in event
        assert "t" in event
