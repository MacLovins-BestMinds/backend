"""Язык интерфейса в игровом API: rounds.lang, Accept-Language, статические переводы тем и подписи прогресса."""

import json
import uuid

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session

from app.ai.jury import ANSWER_TEXTS
from app.ai.pitch import resolve_pitch
from app.core import db
from app.core.config import settings
from app.core.lang import default_lang
from app.game import content, service
from app.game.models import Round
from app.main import app


def _new_round(client: TestClient, headers: dict[str, str] | None = None, **body: object) -> dict:
    payload = {"user_id": f"lang_{uuid.uuid4().hex[:8]}", "mode": "training", "case_id": "favourite_food", **body}
    response = client.post("/api/game/rounds", json=payload, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def test_round_language_comes_from_body_then_header_then_default() -> None:
    with TestClient(app) as client:
        from_body = _new_round(client, {"Accept-Language": "ru"}, lang="ro")
        from_header = _new_round(client, {"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
        unsupported = _new_round(client, {"Accept-Language": "ro"}, lang="de")
        default = _new_round(client)
    assert (from_body["lang"], from_header["lang"], unsupported["lang"]) == ("ro", "ru", "ro")
    assert default["lang"] == default_lang()

    rid = from_body["round_id"]
    with Session(db.engine) as session:
        assert session.get(Round, rid).lang == "ro"
    assert service.get_round(rid)["lang"] == "ro"
    assert resolve_pitch(rid).ui_lang == "ro"
    # тема раунда приходит в ответе: по-английски без перевода (английский интерфейс)
    assert default["case"]["id"] == "favourite_food" and default["case"]["title"] == "My Favourite Food"
    assert "quirk" not in default["case"] and "trick" not in default["case"]


@pytest.mark.parametrize("lang", ["ru", "ro"])
def test_every_topic_has_a_fresh_static_translation(lang) -> None:
    """Если тему добавили или поменяли в topics.json — перезапустите tools/translate_topics.py."""
    entries = json.loads(content.translations_path(lang).read_text(encoding="utf-8"))
    fresh = content.translations(lang)
    for case_id in content.active_ids():
        entry = fresh.get(case_id)
        assert entry is not None, f"{lang}/{case_id}: нет перевода или он устарел"
        assert entry["title"].strip() and entry["brief"].strip() and entry["audience"].strip() and entry["category"].strip()
    # переведено, а не скопировано (названия вроде «Ikigai» могут совпасть с английскими)
    same = [cid for cid in content.active_ids() if fresh[cid]["brief"] == content.english_texts(cid)["brief"]]
    assert same == []
    assert set(entries) == content.active_ids()  # удалённых тем в файле нет
    assert set(entries["my_morning"]) <= {*content.TRANSLATED_FIELDS, "source_hash"}


def test_missing_or_stale_translation_falls_back_to_english(monkeypatch, tmp_path) -> None:
    topics = [{"id": "a", "title": "Apple", "brief": "Eat it.", "audience": "teachers", "category_title": "🍎 Fruit"},
              {"id": "b", "title": "Banana", "brief": "Peel it.", "audience": "teachers", "category_title": "🍎 Fruit"}]  # fmt: skip
    (tmp_path / "topics.json").write_text(json.dumps(topics), encoding="utf-8")
    monkeypatch.setattr(settings, "TOPICS_JSON_PATH", tmp_path / "topics.json")
    content._topics.cache_clear()
    content.translations.cache_clear()
    try:
        fresh_hash = content.source_hash(content.english_texts("a"))
        ro = {"a": {"title": "Măr", "source_hash": fresh_hash}, "b": {"title": "Banană", "source_hash": "old"}}
        content.translations_path("ro").write_text(json.dumps(ro, ensure_ascii=False), encoding="utf-8")
        assert content.translated("a", "title", "Apple", "ro") == "Măr"
        assert content.translated("a", "brief", "Eat it.", "ro") == "Eat it."  # поля нет — английский
        assert content.translated("b", "title", "Banana", "ro") == "Banana"  # оригинал поменялся — английский
        assert content.translated("a", "title", "Apple", "ru") == "Apple"  # файла нет
        assert content.translated("a", "title", "Apple", "en") == "Apple"
    finally:
        content._topics.cache_clear()
        content.translations.cache_clear()


def test_topics_come_in_the_interface_language() -> None:
    ro, ru = content.translations("ro"), content.translations("ru")
    with TestClient(app) as client:
        english = client.get("/api/game/spin").json()
        spin = client.get("/api/game/spin", headers={"Accept-Language": "ro-RO,ro;q=0.9"}).json()
        daily = client.get("/api/game/daily?date=2026-10-04", headers={"Accept-Language": "ru"}).json()
        created = _new_round(client, {"Accept-Language": "ru"})
    assert english["case"]["title"] == content.english_texts(english["case"]["id"])["title"]
    case = spin["case"]
    assert (case["title"], case["brief"], case["audience"]) == tuple(ro[case["id"]][k] for k in ("title", "brief", "audience"))
    assert spin["category"]["title"] == ro[case["id"]]["category"] and "quirk" not in case
    assert daily["case"]["title"] == ru[daily["case"]["id"]]["title"]
    assert created["case"]["id"] == "favourite_food" and created["case"]["title"] == ru["favourite_food"]["title"]
    assert created["case"]["summary"] == ru["favourite_food"]["summary"]


def _signup(client: TestClient) -> dict[str, str]:
    nick = f"multilang_{uuid.uuid4().hex[:8]}"
    token = client.post("/api/auth/register", json={"nick": nick, "password": "secret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_progress_and_review_speak_the_interface_language() -> None:
    with TestClient(app) as client:
        headers = _signup(client)
        rid = client.post(
            "/api/game/rounds", json={"user_id": "x", "mode": "training", "case_id": "favourite_food"}, headers=headers
        ).json()["round_id"]
        service.save_ai_result(
            rid,
            "delivery",
            {
                "scores": {"content": {"total": 50}, "delivery": {"total": 80}},
                "metrics": {"duration_sec": 90, "wpm": 150, "fillers_per_min": 4.5, "long_pauses": 0},
                "events": [],
            },
        )
        assert client.post(f"/api/game/rounds/{rid}/finish").status_code == 200

        english = client.get("/api/game/progress", headers=headers).json()
        romanian = client.get("/api/game/progress", headers={**headers, "Accept-Language": "ro"}).json()
        review = client.get(f"/api/game/rounds/{rid}/review", headers={**headers, "Accept-Language": "ru"}).json()

    assert english["history"][0]["title"] == "My Favourite Food"
    assert romanian["history"][0]["title"] == content.translations("ro")["favourite_food"]["title"]
    assert [s["title"] for s in romanian["skills"]] == ["Conținut", "Prezentare", "Răspunsuri la juriu"]
    assert {h["key"]: h["unit"] for h in romanian["habits"]}["wpm"] == "cuvinte/min"
    insights = {i["title"]: i["text"] for i in romanian["insights"]}
    assert insights["Cuvinte de umplutură"].startswith("4,5 pe minut.")
    assert "Punctul tău forte: prezentare" in insights
    assert review["round"]["title"] == content.translations("ru")["favourite_food"]["title"]


def test_interface_language_changes_error_texts_but_not_jury_texts() -> None:
    with TestClient(app) as client:
        user = f"daily_{uuid.uuid4().hex[:8]}"
        for _ in range(5):
            assert client.post("/api/game/rounds", json={"user_id": user, "mode": "daily"}).status_code == 200
        limited = client.post("/api/game/rounds", json={"user_id": user, "mode": "daily"}, headers={"Accept-Language": "ro"})
        skipped = client.post(
            "/api/ai/rounds/r1/jury/skip?mock=1", data={"question_id": "q1"}, headers={"Accept-Language": "ru"}
        )
    assert limited.status_code == 429 and limited.json()["detail"].startswith("Tema zilei poate fi jucată")
    assert skipped.json()["comment"] == ANSWER_TEXTS[default_lang()]["skipped"]  # язык речи неизвестен — STT_LANGUAGE


def test_lang_column_is_added_to_an_existing_rounds_table(monkeypatch, tmp_path) -> None:
    old = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with old.begin() as conn:
        conn.execute(text("CREATE TABLE rounds (id VARCHAR PRIMARY KEY, difficulty VARCHAR NOT NULL DEFAULT 'easy')"))
        conn.execute(text("INSERT INTO rounds (id) VALUES ('rnd_old')"))
    monkeypatch.setattr(db, "engine", old)
    db._add_missing_columns()
    assert "lang" in {c["name"] for c in inspect(old).get_columns("rounds")}
    with old.connect() as conn:
        assert conn.execute(text("SELECT lang FROM rounds WHERE id = 'rnd_old'")).scalar() == "en"
