"""GET /api/ai/rounds/{id}/flow и /better-version: фоновые задачи после delivery, дедупликация, восстановление
после перезапуска, разбор из истории. Внешние вызовы подменены: задачи — счётчики, ElevenLabs и Gemini не нужны."""

import asyncio
import importlib
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.ai import better, flow, jobs, mocks
from app.ai.config import get_settings
from app.core.config import settings as app_settings
from app.game import service
from app.main import app

ai_router = importlib.import_module("app.ai.router")

MOMENT = {"t": 0.0, "end": 2.4, "kind": "hook", "tone": "good", "quote": "Привет.", "comment": "Сразу к делу."}
DELIVERY = {
    "transcript": "Привет. Это питч.",
    "words": [{"start": 0, "end": 7, "t": 0.1, "t_end": 0.6}, {"start": 8, "end": 11, "t": 1.0, "t_end": 1.3}],
    "scores": {"content": {"total": 60}, "delivery": {"total": 70}},
    "metrics": {"duration_sec": 40, "wpm": 130, "fillers_per_min": 1.0, "long_pauses": 0},
    "events": [],
    "speech_lang": "ru",
}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _wait_jobs(timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while any(not job.task.done() for job in jobs._running.values()) and time.time() < deadline:
        time.sleep(0.02)


@pytest.fixture
def live(client, monkeypatch, tmp_path):
    """Настоящий режим (без моков) с фоновыми задачами; задачи подменены счётчиками запусков."""
    monkeypatch.setenv("AI_MOCK", "0")
    monkeypatch.setenv("REVIEW_JOBS_ENABLED", "1")
    monkeypatch.setattr(app_settings, "STATIC_DIR", tmp_path)
    get_settings.cache_clear()
    runs: dict[str, list[str]] = {"flow": [], "better_version": []}

    async def fake_flow(round_id, delivery):
        runs["flow"].append(round_id)
        await asyncio.sleep(0.05)
        return {"status": "ready", "summary": "Линия есть.", "moments": [MOMENT]}

    async def fake_better(round_id, delivery):
        runs["better_version"].append(round_id)
        await asyncio.sleep(0.05)
        better.audio_path(round_id).parent.mkdir(parents=True, exist_ok=True)
        better.audio_path(round_id).write_bytes(b"mp3")
        return {"status": "ready", "text": "Привет. Это питч.", "audio": f"better/{round_id}.mp3"}

    monkeypatch.setattr(flow, "run_flow", fake_flow)
    monkeypatch.setattr(better, "run_better_version", fake_better)
    yield runs
    _wait_jobs()
    get_settings.cache_clear()


def _round(client: TestClient, headers: dict | None = None, delivery: dict | None = DELIVERY) -> str:
    body = {"user_id": f"ins_{uuid.uuid4().hex[:8]}", "mode": "training", "case_id": "favourite_food"}
    rid = client.post("/api/game/rounds", json=body, headers=headers or {}).json()["round_id"]
    if delivery is not None:
        service.save_ai_result(rid, "delivery", delivery)
    return rid


def _wait(client: TestClient, url: str, headers: dict | None = None, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while True:
        body = client.get(url, headers=headers or {}).json()
        if body["status"] != "pending" or time.time() > deadline:
            return body
        time.sleep(0.02)


def test_mock_responses_follow_the_contract(client) -> None:
    flow_body = client.get("/api/ai/rounds/rnd_x/flow?mock=1").json()
    assert flow_body["status"] == "ready" and flow_body["summary"]
    assert {m["kind"] for m in flow_body["moments"]} >= {"hook", "weak", "strong_close"}
    assert all(set(m) == {"t", "end", "kind", "tone", "quote", "comment"} for m in flow_body["moments"])
    assert all(m["tone"] == ("good" if m["kind"] in ("hook", "strong", "strong_close") else "bad") for m in flow_body["moments"])
    better_body = client.get("/api/ai/rounds/rnd_x/better-version?mock=1").json()
    assert set(better_body) == {"status", "audio_url", "text", "reason"}
    assert better_body["status"] == "ready" and better_body["audio_url"].startswith("/static/") and better_body["reason"] is None


def test_unknown_round_and_round_without_delivery(live, client) -> None:
    assert client.get("/api/ai/rounds/rnd_nope_404/flow").status_code == 404
    rid = _round(client, delivery=None)
    response = client.get(f"/api/ai/rounds/{rid}/better-version", headers={"Accept-Language": "ru"})
    assert response.status_code == 409 and response.json()["detail"].startswith("Сначала отправь питч")
    assert live == {"flow": [], "better_version": []}


def test_flow_is_computed_once_and_then_served(live, client) -> None:
    rid = _round(client)
    first = client.get(f"/api/ai/rounds/{rid}/flow").json()
    second = client.get(f"/api/ai/rounds/{rid}/flow").json()  # пока считается — не запускать вторую задачу
    assert first == second == {"status": "pending", "summary": None, "moments": []}
    ready = _wait(client, f"/api/ai/rounds/{rid}/flow")
    assert ready == {"status": "ready", "summary": "Линия есть.", "moments": [MOMENT]}
    assert client.get(f"/api/ai/rounds/{rid}/flow").json() == ready
    assert live["flow"] == [rid] and live["better_version"] == []


def test_delivery_starts_both_jobs_in_the_background(live, client, monkeypatch) -> None:
    rid = _round(client, delivery=None)

    async def fake_run_delivery(round_id, *_args):
        result = mocks.delivery().model_copy(update={"speech_lang": "ru"})
        service.save_ai_result(round_id, "delivery", result.model_dump(mode="json"))
        return result

    monkeypatch.setattr(ai_router, "run_delivery", fake_run_delivery)
    response = client.post(
        f"/api/ai/rounds/{rid}/delivery", files={"audio": ("pitch.m4a", b"audio-bytes", "audio/m4a")}
    )
    assert response.status_code == 200
    assert (app_settings.STATIC_DIR / "recordings" / f"{rid}.m4a").exists()  # клону голоса нужна запись
    _wait_jobs()
    assert live == {"flow": [rid], "better_version": [rid]}
    assert client.get(f"/api/ai/rounds/{rid}/flow").json()["status"] == "ready"
    better_body = client.get(f"/api/ai/rounds/{rid}/better-version").json()
    assert better_body == {"status": "ready", "audio_url": f"/static/better/{rid}.mp3", "text": "Привет. Это питч.", "reason": None}
    assert live == {"flow": [rid], "better_version": [rid]}  # GET не запускает заново


def test_abandoned_pending_is_restarted_and_failed_retries_are_limited(live, client) -> None:
    source = jobs.source_of(DELIVERY)
    rid = _round(client)
    # сервер перезапустился посреди задачи: pending из прошлого процесса
    service.save_ai_result(rid, "flow", {"status": "pending", "source": source, "attempts": 1, "started_at": jobs._PROCESS_STARTED - 5})
    assert _wait(client, f"/api/ai/rounds/{rid}/flow")["status"] == "ready" and live["flow"] == [rid]

    rid2 = _round(client)
    service.save_ai_result(rid2, "flow", {"status": "failed", "source": source, "attempts": 1, "finished_at": time.time()})
    assert client.get(f"/api/ai/rounds/{rid2}/flow").json()["status"] == "failed"  # упала только что — не долбим
    service.save_ai_result(rid2, "flow", {"status": "failed", "source": source, "attempts": 1, "finished_at": time.time() - 3600})
    assert _wait(client, f"/api/ai/rounds/{rid2}/flow")["status"] == "ready" and live["flow"] == [rid, rid2]

    rid3 = _round(client)
    service.save_ai_result(rid3, "flow", {"status": "failed", "source": source, "attempts": 3, "finished_at": time.time() - 3600})
    assert client.get(f"/api/ai/rounds/{rid3}/flow").json()["status"] == "failed" and rid3 not in live["flow"]


def test_result_of_an_earlier_recording_is_recomputed(live, client) -> None:
    rid = _round(client)
    service.save_ai_result(rid, "flow", {"status": "ready", "source": "old-recording", "summary": "Старое.", "moments": []})
    assert _wait(client, f"/api/ai/rounds/{rid}/flow")["summary"] == "Линия есть." and live["flow"] == [rid]


def test_failures_and_unavailable_reasons(live, client, monkeypatch) -> None:
    async def broken(round_id, delivery):
        raise RuntimeError("gemini down")

    async def too_short(round_id, delivery):
        raise better.Unavailable("too_short")

    monkeypatch.setattr(flow, "run_flow", broken)
    monkeypatch.setattr(better, "run_better_version", too_short)
    rid = _round(client)
    assert _wait(client, f"/api/ai/rounds/{rid}/flow") == {"status": "failed", "summary": None, "moments": []}
    ru = {"Accept-Language": "ru"}
    body = _wait(client, f"/api/ai/rounds/{rid}/better-version", ru)
    assert body == {"status": "unavailable", "audio_url": None, "text": None, "reason": better.reason_text("too_short", "ru")}
    assert "10" in body["reason"]


def test_round_review_includes_both_results(live, client) -> None:
    nick = f"insights_{uuid.uuid4().hex[:8]}"
    token = client.post("/api/auth/register", json={"nick": nick, "password": "secret123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    rid = _round(client, headers)
    assert client.post(f"/api/game/rounds/{rid}/finish").status_code == 200

    review = client.get(f"/api/game/rounds/{rid}/review", headers=headers).json()
    assert review["flow"] is None and review["better_version"] is None  # разбор задачи не запускает
    assert live == {"flow": [], "better_version": []}

    _wait(client, f"/api/ai/rounds/{rid}/flow")
    _wait(client, f"/api/ai/rounds/{rid}/better-version")
    review = client.get(f"/api/game/rounds/{rid}/review", headers=headers).json()
    assert review["flow"] == {"status": "ready", "summary": "Линия есть.", "moments": [MOMENT]}
    assert review["better_version"]["audio_url"] == f"/static/better/{rid}.mp3"
    assert review["delivery"]["transcript"] == DELIVERY["transcript"]  # прежние поля на месте


def test_without_background_jobs_only_stored_results_are_served(client, monkeypatch) -> None:
    monkeypatch.setenv("AI_MOCK", "0")  # REVIEW_JOBS_ENABLED=0 — из conftest
    get_settings.cache_clear()
    monkeypatch.setattr(flow, "run_flow", lambda *_: pytest.fail("задача не должна запускаться"))
    try:
        rid = _round(client)
        assert client.get(f"/api/ai/rounds/{rid}/flow").json()["status"] == "failed"
        assert client.get(f"/api/ai/rounds/{rid}/better-version").json()["reason"] == better.reason_text("disabled", "en")
        service.save_ai_result(rid, "flow", {"status": "ready", "source": jobs.source_of(DELIVERY), "summary": "Есть.", "moments": []})
        assert client.get(f"/api/ai/rounds/{rid}/flow").json()["summary"] == "Есть."
    finally:
        get_settings.cache_clear()
