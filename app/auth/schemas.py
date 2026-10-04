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


class SignupRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=200, description="Почта — по ней потом входят")
    nick: str = Field(..., min_length=2, max_length=50, description="Ник — его видно в лидерборде")
    password: str = Field(..., min_length=6, max_length=100)


class VerifyRequest(BaseModel):
    email: str
    code: str = Field(..., min_length=4, max_length=10)


class ResendRequest(BaseModel):
    email: str


class AuthConfig(BaseModel):
    google_client_id: Optional[str] = Field(None, description="Client ID для кнопки Google; null — вход через Google выключен")


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


class SignupResponse(BaseModel):
    email: str
    sent: bool = Field(description="письмо с кодом ушло; false — почта на сервере не настроена")
    dev_code: Optional[str] = Field(None, description="код в ответе — только когда почта на сервере не настроена")
    access_token: Optional[str] = Field(None, description="токен — когда подтверждение почты выключено и вход уже выполнен")
    user: Optional[UserAuthOut] = None
