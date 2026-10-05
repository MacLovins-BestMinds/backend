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
            "events": [
                {"type": "repeat", "t": 3, "text": "Repeated: «pizza»"},
                {"type": "filler", "t": 5, "text": "«um»"},
                {"type": "weak_phrase", "t": 1, "text": "Hedging: «i think»"},
                {"type": "weak_phrase", "t": 2, "text": "Hedging: «maybe»"},
                {"type": "weak_phrase", "t": 7, "text": "Weak ending: «that's it»"},
            ],
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
        assert (newest["title"], newest["repeats"], newest["weak_phrases"], newest["wpm"]) == ("My Favourite Food", 1, 3, 150)
        assert data["best"] == 0.4 * 90 + 0.4 * 60 + 0.2 * 70
        # раунды на 58 и 74 → среднее 66, это «Pitcher»; до «Orator» (75) не хватает 9
        assert (data["rank"]["title"], data["next_rank"]) == ("Pitcher", {"title": "Orator", "points_needed": 9.0})
        habits = {h["key"]: h for h in data["habits"]}
        assert habits["fillers_per_min"]["value"] == 4.5 and habits["fillers_per_min"]["better"] == "lower"
        assert habits["weak_phrases"]["value"] == 3 and habits["weak_phrases"]["title"] == "Hedging and apologies"
        assert {i["title"] for i in data["insights"]} >= {"Filler words", "Hedging and apologies"}


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


def test_email_signup_needs_the_code_before_sign_in(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION", True)
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    nick = f"mail_{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        signup = client.post("/api/auth/signup", json={"email": email.upper(), "nick": nick, "password": "secret123"})
        assert signup.status_code == 201
        code = signup.json()["dev_code"]  # почта в тестах не настроена — код приходит в ответе
        assert (signup.json()["email"], signup.json()["sent"], signup.json()["access_token"]) == (email, False, None) and len(code) == 6

        login = {"nick": email, "password": "secret123"}
        assert client.post("/api/auth/login", json=login).status_code == 403  # почта ещё не подтверждена
        wrong = "000000" if code != "000000" else "111111"
        assert client.post("/api/auth/verify", json={"email": email, "code": wrong}).status_code == 400
        verified = client.post("/api/auth/verify", json={"email": email, "code": code})
        assert verified.status_code == 200 and verified.json()["user"]["nick"] == nick
        assert client.post("/api/auth/login", json=login).status_code == 200
        # та же почта второй раз — нельзя; чужой ник — нельзя
        assert client.post("/api/auth/signup", json={"email": email, "nick": "other", "password": "secret123"}).status_code == 400
        taken = client.post("/api/auth/signup", json={"email": f"x{email}", "nick": nick, "password": "another1"})
        assert taken.status_code == 400 and "taken" in taken.json()["detail"]


def test_email_signup_keeps_the_history_of_a_guest_nickname(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION", True)
    nick = f"old_{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    with TestClient(app) as client:
        guest_id = client.post("/api/game/auth", json={"nick": nick}).json()["user_id"]
        code = client.post("/api/auth/signup", json={"email": email, "nick": nick, "password": "secret123"}).json()["dev_code"]
        user = client.post("/api/auth/verify", json={"email": email, "code": code}).json()["user"]
        assert user["user_id"] == guest_id


def test_google_button_is_offered_only_with_a_real_client_id(monkeypatch) -> None:
    from app.core.config import settings

    with TestClient(app) as client:
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "your-google-client-id.apps.googleusercontent.com")
        assert client.get("/api/auth/config").json() == {"google_client_id": None}  # заглушка из примера
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com")
        assert client.get("/api/auth/config").json() == {"google_client_id": "123-abc.apps.googleusercontent.com"}


def test_google_sign_in_finishes_an_unconfirmed_email_signup(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUTH_MOCK_GOOGLE", True)
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION", True)
    tag = uuid.uuid4().hex[:8]
    email = f"speaker_{tag}@gmail.com"  # такую почту отдаёт тестовый токен mock_<tag>
    with TestClient(app) as client:
        client.post("/api/auth/signup", json={"email": email, "nick": f"g_{tag}", "password": "secret123"})
        google = client.post("/api/auth/google", json={"id_token": f"mock_{tag}"})
        assert google.status_code == 200 and google.json()["user"]["nick"] == f"g_{tag}"
        # почта подтверждена Google — вход по паролю теперь тоже работает
        assert client.post("/api/auth/login", json={"nick": email, "password": "secret123"}).status_code == 200


def test_signup_without_verification_signs_in_at_once() -> None:
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    nick = f"quick_{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        signup = client.post("/api/auth/signup", json={"email": email, "nick": nick, "password": "secret123"}).json()
        assert signup["access_token"] and signup["user"]["nick"] == nick and signup["dev_code"] is None
        headers = {"Authorization": f"Bearer {signup['access_token']}"}
        assert client.get("/api/game/progress", headers=headers).json()["nick"] == nick
        assert client.post("/api/auth/login", json={"nick": email, "password": "secret123"}).status_code == 200


def test_difficulty_sets_topic_level_timing_and_stays_with_the_round() -> None:
    from app.ai.pitch import resolve_pitch
    from app.game import content

    with TestClient(app) as client:
        headers = _signup(client, f"level_{uuid.uuid4().hex[:8]}")
        for level, timing in {"easy": (300, 60), "medium": (240, 60), "hard": (180, 90)}.items():
            topics = {client.get(f"/api/game/spin?difficulty={level}").json()["case"]["id"] for _ in range(8)}
            assert {content.level(t) for t in topics} == {level}  # колесо даёт темы только своего уровня
            body = {"user_id": "x", "mode": "training", "case_id": next(iter(topics)), "difficulty": level}
            created = client.post("/api/game/rounds", json=body, headers=headers).json()
            assert (created["prep_sec"], created["pitch_min_sec"]) == timing
            pitch = resolve_pitch(created["round_id"])
            assert (pitch.difficulty, pitch.min_sec) == (level, timing[1])  # жюри и разбор узнают уровень из раунда

        service.save_ai_result(created["round_id"], "delivery", {"transcript": "…", "scores": {"content": {"total": 70}, "delivery": {"total": 70}}})
        client.post(f"/api/game/rounds/{created['round_id']}/finish")
        assert client.get("/api/game/progress", headers=headers).json()["history"][0]["difficulty"] == "hard"
        # тема дня одна на всех — из простых
        assert content.level(client.get("/api/game/daily").json()["case"]["id"]) == "easy"
