from fastapi.testclient import TestClient

from app.game import service
from app.main import app


def _round(client: TestClient, user_id: str, mode: str) -> str:
    body = {"user_id": user_id, "mode": mode} | ({"case_id": "health-01"} if mode == "training" else {})
    resp = client.post("/api/game/rounds", json=body)
    assert resp.status_code == 200
    return resp.json()["round_id"]


def test_finish_uses_ai_engine_results() -> None:
    delivery = {"transcript": "…", "scores": {"content": {"total": 90}, "delivery": {"total": 60}}}
    with TestClient(app) as client:
        user_id = client.post("/api/game/auth", json={"nick": "finish_test"}).json()["user_id"]

        training = _round(client, user_id, "training")
        service.save_ai_result(training, "delivery", delivery)
        service.save_ai_result(training, "jury_answer", {"question_id": "q1", "score": 80})
        service.save_ai_result(training, "jury_answer", {"question_id": "q2", "score": 40})
        data = client.post(f"/api/game/rounds/{training}/finish").json()
        assert (data["content"], data["delivery"], data["jury"]) == (90, 60, 60)
        assert data["total"] == 0.4 * 90 + 0.4 * 60 + 0.2 * 60

        warmup = _round(client, user_id, "warmup")
        service.save_ai_result(warmup, "delivery", delivery)
        data = client.post(f"/api/game/rounds/{warmup}/finish").json()
        assert data["jury"] == 0 and data["total"] == 75  # без жюри: 50/50
