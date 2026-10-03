from typing import Optional
from pydantic import BaseModel, Field
from app.game.schemas import RankInfo


class UserRegisterRequest(BaseModel):
    nick: str = Field(..., min_length=2, max_length=50, description="Никнейм пользователя")
    password: str = Field(..., min_length=6, max_length=100, description="Пароль")
    email: Optional[str] = Field(None, description="Email (опционально)")


class UserLoginRequest(BaseModel):
    nick: str = Field(..., description="Никнейм или email")
    password: str = Field(..., description="Пароль")


class GoogleAuthRequest(BaseModel):
    id_token: str = Field(..., description="Google ID Token, полученный от Google Sign-In SDK")
    guest_user_id: Optional[str] = Field(None, description="ID гостевого аккаунта для переноса прогресса")


class UserAuthOut(BaseModel):
    user_id: str
    nick: str
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    auth_provider: str
    rank: RankInfo


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserAuthOut
