import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from app.core.config import settings


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(days=settings.ACCESS_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "iat": now})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except Exception:
        return None


def verify_google_token(id_token_str: str) -> Dict[str, Any]:
    """
    Верифицирует Google ID Token.
    Тестовые токены mock_<id> принимаются только при AUTH_MOCK_GOOGLE=true.
    В продакшене верифицирует через официальную библиотеку google-auth.
    """
    if settings.AUTH_MOCK_GOOGLE and id_token_str.startswith("mock_"):
        clean_id = id_token_str[5:]
        return {
            "sub": f"google_sub_{clean_id}",
            "email": f"speaker_{clean_id}@gmail.com",
            "name": f"Google Speaker {clean_id}",
            "picture": "https://lh3.googleusercontent.com/a/default-user"
        }

    if not settings.GOOGLE_CLIENT_ID:
        raise ValueError("Вход через Google не настроен: задайте GOOGLE_CLIENT_ID")

    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests

        request = requests.Request()
        id_info = id_token.verify_oauth2_token(
            id_token_str, request, settings.GOOGLE_CLIENT_ID
        )
        return id_info
    except Exception as e:
        raise ValueError(f"Недействительный Google токен: {str(e)}")
