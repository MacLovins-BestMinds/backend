import hashlib
import random
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.db import get_session
from app.game.models import User, now_utc
from app.game import service
from app.auth.schemas import (
    UserRegisterRequest,
    UserLoginRequest,
    GoogleAuthRequest,
    UserAuthOut,
    TokenResponse,
)
from app.auth.security import (
    hash_password,
    verify_password,
    create_access_token,
    verify_google_token,
)
from app.auth.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


def build_user_out(user: User, session: Session) -> UserAuthOut:
    rank = service.calculate_user_rank(session, user.id)
    return UserAuthOut(
        user_id=user.id,
        nick=user.nick,
        email=user.email,
        avatar_url=user.avatar_url,
        auth_provider=user.auth_provider,
        rank=rank
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(req: UserRegisterRequest, session: Session = Depends(get_session)):
    """
    Регистрация нового пользователя по логину и паролю.
    """
    clean_nick = req.nick.strip()
    if len(clean_nick) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The nickname must be at least 2 characters long"
        )

    # Проверка уникальности ника
    existing_nick = session.exec(select(User).where(User.nick == clean_nick)).first()
    if existing_nick:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A user with the nickname \"{clean_nick}\" already exists"
        )

    # Проверка уникальности email, если указан
    if req.email:
        clean_email = req.email.strip().lower()
        existing_email = session.exec(select(User).where(User.email == clean_email)).first()
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"A user with the email \"{clean_email}\" already exists"
            )
    else:
        clean_email = None

    user_id = f"u_{hashlib.md5(f'{clean_nick}_{now_utc().isoformat()}'.encode()).hexdigest()[:10]}"
    new_user = User(
        id=user_id,
        nick=clean_nick,
        email=clean_email,
        password_hash=hash_password(req.password),
        auth_provider="local",
        created_at=now_utc()
    )
    session.add(new_user)
    session.commit()
    session.refresh(new_user)

    token = create_access_token({"sub": new_user.id, "nick": new_user.nick})
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=build_user_out(new_user, session)
    )


@router.post("/login", response_model=TokenResponse)
def login(req: UserLoginRequest, session: Session = Depends(get_session)):
    """
    Вход по логину (нику или email) и паролю.
    """
    identifier = req.nick.strip()
    user = session.exec(
        select(User).where((User.nick == identifier) | (User.email == identifier.lower()))
    ).first()

    if not user or not user.password_hash or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong login or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token({"sub": user.id, "nick": user.nick})
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=build_user_out(user, session)
    )


@router.post("/google", response_model=TokenResponse)
def google_auth(req: GoogleAuthRequest, session: Session = Depends(get_session)):
    """
    Авторизация через Google ID Token (Google Sign-In на клиенте).
    Автоматически регистрирует нового пользователя или связывает с существующим гостем.
    """
    try:
        id_info = verify_google_token(req.id_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

    google_sub = id_info.get("sub")
    email = id_info.get("email", "").lower()
    name = id_info.get("name") or email.split("@")[0] if email else "GoogleSpeaker"
    picture = id_info.get("picture")

    user = None

    # 1. Если передан guest_user_id, связываем гостевой аккаунт
    if req.guest_user_id:
        guest_user = session.get(User, req.guest_user_id)
        if guest_user:
            user = guest_user
            user.google_id = google_sub
            if email and not user.email:
                user.email = email
            if picture and not user.avatar_url:
                user.avatar_url = picture
            user.auth_provider = "google"
            session.add(user)
            session.commit()
            session.refresh(user)

    # 2. Если не гость, ищем существующего пользователя по google_id
    if not user:
        user = session.exec(select(User).where(User.google_id == google_sub)).first()

    # 3. Ищем по email
    if not user and email:
        user = session.exec(select(User).where(User.email == email)).first()
        if user:
            user.google_id = google_sub
            if not user.avatar_url and picture:
                user.avatar_url = picture
            session.add(user)
            session.commit()
            session.refresh(user)

    # 4. Если пользователь все еще не найден — создаем нового
    if not user:
        clean_nick = name.strip()
        existing_nick = session.exec(select(User).where(User.nick == clean_nick)).first()
        if existing_nick:
            clean_nick = f"{clean_nick}_{random.randint(100, 999)}"

        user_id = f"u_g_{hashlib.md5(f'{google_sub}_{email}'.encode()).hexdigest()[:10]}"
        user = User(
            id=user_id,
            nick=clean_nick,
            email=email,
            google_id=google_sub,
            avatar_url=picture,
            auth_provider="google",
            created_at=now_utc()
        )
        session.add(user)
        session.commit()
        session.refresh(user)

    token = create_access_token({"sub": user.id, "nick": user.nick, "email": user.email})
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user=build_user_out(user, session)
    )


@router.get("/me", response_model=UserAuthOut)
def get_me(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    """
    Получение данных текущего авторизованного пользователя по Bearer токену.
    """
    return build_user_out(current_user, session)
