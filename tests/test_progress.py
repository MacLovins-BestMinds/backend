import uuid

from fastapi.testclient import TestClient

from app.game import service
from app.main import app


def _signup(client: TestClient, nick: str) -> dict[str, str]:
    token = client.post("/api/auth/register", json={"nick": nick, "password": "secret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _play(client: TestClient, headers: dict[str, str], content: int, delivery: int, fillers: float) -> str:
    # user_id в теле чужой: с токеном раунд всё равно создаётся от имени вошедшего
    body = {"user_id": "somebody_else", "mode": "training", "case_id": "favourite_food"}
    rid = client.post("/api/game/rounds", json=body, headers=headers).json()["round_id"]
    service.save_ai_result(
        rid,
        "delivery",
        {
            "transcript": "My favourite food is pizza.",
            "scores": {"content": {"total": content}, "delivery": {"total": delivery}},
            "metrics": {"duration_sec": 90, "wpm": 150, "fillers_per_min": fillers, "long_pauses": 1, "gaze_on_ratio": None},
            "events": [{"type": "repeat", "t": 3, "text": "Repeated: «pizza»"}, {"type": "filler", "t": 5, "text": "«um»"}],
        },
    )
    service.save_ai_result(rid, "jury_questions", {"questions": [{"id": "q1", "juror": "strict", "text": "Why pizza?"}]})
    service.save_ai_result(rid, "jury_answer", {"question_id": "q1", "score": 70, "comment": "Fine."})
    assert client.post(f"/api/game/rounds/{rid}/finish").status_code == 200
    return rid


def test_progress_lists_every_round_with_speech_habits() -> None:
    with TestClient(app) as client:
        headers = _signup(client, f"progress_{uuid.uuid4().hex[:8]}")
        first = _play(client, headers, content=50, delivery=60, fillers=5.0)
        second = _play(client, headers, content=90, delivery=60, fillers=4.0)

        data = client.get("/api/game/progress", headers=headers).json()
        assert data["rounds_total"] == 2 and data["streak_days"] == 1
        assert [r["id"] for r in data["history"]] == [second, first]  # новые сверху
        newest = data["history"][0]
        assert (newest["title"], newest["repeats"], newest["wpm"]) == ("My Favourite Food", 1, 150)
        assert data["best"] == 0.4 * 90 + 0.4 * 60 + 0.2 * 70
        # раунды на 58 и 74 → среднее 66, это «Pitcher»; до «Orator» (75) не хватает 9
        assert (data["rank"]["title"], data["next_rank"]) == ("Pitcher", {"title": "Orator", "points_needed": 9.0})
        habits = {h["key"]: h for h in data["habits"]}
        assert habits["fillers_per_min"]["value"] == 4.5 and habits["fillers_per_min"]["better"] == "lower"
        assert any(i["title"] == "Filler words" for i in data["insights"])


def test_progress_and_review_need_the_owner() -> None:
    with TestClient(app) as client:
        owner = _signup(client, f"owner_{uuid.uuid4().hex[:8]}")
        stranger = _signup(client, f"stranger_{uuid.uuid4().hex[:8]}")
        rid = _play(client, owner, content=80, delivery=80, fillers=1.0)

        assert client.get("/api/game/progress").status_code == 401
        assert client.get(f"/api/game/rounds/{rid}/review", headers=stranger).status_code == 404
        review = client.get(f"/api/game/rounds/{rid}/review", headers=owner).json()
        assert review["delivery"]["transcript"] == "My favourite food is pizza."
        assert review["jury_questions"][0]["text"] == "Why pizza?" and review["jury_answers"][0]["score"] == 70
        assert review["result"]["total"] == 0.4 * 80 + 0.4 * 80 + 0.2 * 70


def test_guest_nickname_can_be_secured_with_a_password() -> None:
    nick = f"guest_{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        guest_id = client.post("/api/game/auth", json={"nick": nick}).json()["user_id"]
        claimed = client.post("/api/auth/register", json={"nick": nick, "password": "secret123"})
        assert claimed.status_code == 201 and claimed.json()["user"]["user_id"] == guest_id  # история остаётся
        assert client.post("/api/auth/register", json={"nick": nick, "password": "other456"}).status_code == 400
        assert client.post("/api/auth/login", json={"nick": nick, "password": "secret123"}).status_code == 200
        assert client.post("/api/auth/login", json={"nick": nick, "password": "wrong"}).status_code == 401
