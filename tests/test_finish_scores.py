from fastapi.testclient import TestClient

from app.game import service
from app.main import app


def _round(client: TestClient, user_id: str, mode: str) -> str:
    body = {"user_id": user_id, "mode": mode} | ({"case_id": "favourite_food"} if mode == "training" else {})
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


def test_finish_rejects_unknown_round_and_round_without_review(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "MOCK_FALLBACK", False)
    with TestClient(app) as client:
        assert client.post("/api/game/rounds/rnd_missing_404/finish").status_code == 404
        user_id = client.post("/api/game/auth", json={"nick": "finish_409"}).json()["user_id"]
        rid = _round(client, user_id, "training")
        assert client.post(f"/api/game/rounds/{rid}/finish").status_code == 409


def test_google_mock_tokens_need_explicit_flag(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUTH_MOCK_GOOGLE", False)
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", None)
    with TestClient(app) as client:
        resp = client.post("/api/auth/google", json={"id_token": "any-string-is-not-a-token"})
        assert resp.status_code == 400


def test_spin_returns_updated_topic_content() -> None:
    with TestClient(app) as client:
        case = client.get("/api/game/spin").json()["case"]
        # простые темы: вместо статьи — короткий план выступления, читать по ним нечего
        assert case["summary"] and case["sources"] == []
        assert "quirk" not in case and "trick" not in case
