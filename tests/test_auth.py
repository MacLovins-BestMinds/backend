import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_register_success(client):
    response = client.post("/api/auth/register", json={
        "nick": "alex_speaker_auth",
        "password": "SecretPassword123",
        "email": "alex@example.com"
    })
    assert response.status_code == 201
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["nick"] == "alex_speaker_auth"
    assert data["user"]["email"] == "alex@example.com"
    assert data["user"]["auth_provider"] == "local"
    assert data["user"]["rank"]["title"] == "Новичок"


def test_register_duplicate_nick(client):
    response = client.post("/api/auth/register", json={
        "nick": "alex_speaker_auth",
        "password": "AnotherPassword456"
    })
    assert response.status_code == 400
    assert "уже существует" in response.json()["detail"]


def test_login_success(client):
    response = client.post("/api/auth/login", json={
        "nick": "alex_speaker_auth",
        "password": "SecretPassword123"
    })
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["user"]["nick"] == "alex_speaker_auth"


def test_login_wrong_password(client):
    response = client.post("/api/auth/login", json={
        "nick": "alex_speaker_auth",
        "password": "WrongPassword"
    })
    assert response.status_code == 401
    assert "Неверный логин или пароль" in response.json()["detail"]


def test_get_me_authorized(client):
    login_resp = client.post("/api/auth/login", json={
        "nick": "alex_speaker_auth",
        "password": "SecretPassword123"
    })
    token = login_resp.json()["access_token"]

    me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert me_data["nick"] == "alex_speaker_auth"
    assert me_data["auth_provider"] == "local"


def test_get_me_unauthorized(client):
    me_resp = client.get("/api/auth/me")
    assert me_resp.status_code == 401


def test_google_auth_flow(client):
    # Тест авторизации через Google ID Token
    google_resp = client.post("/api/auth/google", json={
        "id_token": "mock_google_token_user1"
    })
    assert google_resp.status_code == 200
    data = google_resp.json()
    assert "access_token" in data
    assert data["user"]["auth_provider"] == "google"
    assert "google_token_user1" in data["user"]["email"]


def test_google_auth_guest_migration(client):
    # Создаем гостя через старый эндпоинт
    guest_resp = client.post("/api/game/auth", json={"nick": "guest_pitcher"})
    guest_id = guest_resp.json()["user_id"]

    # Привязываем Google к этому гостевому аккаунту
    google_resp = client.post("/api/auth/google", json={
        "id_token": "mock_google_token_migrated",
        "guest_user_id": guest_id
    })
    assert google_resp.status_code == 200
    data = google_resp.json()
    assert data["user"]["user_id"] == guest_id
    assert data["user"]["auth_provider"] == "google"


def test_backward_compatibility_guest_auth(client):
    # Старый эндпоинт без паролей продолжает работать
    resp = client.post("/api/game/auth", json={"nick": "retro_guest"})
    assert resp.status_code == 200
    assert resp.json()["nick"] == "retro_guest"
    assert "user_id" in resp.json()
