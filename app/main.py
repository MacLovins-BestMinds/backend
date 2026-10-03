from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.core.config import settings
from app.core.db import init_db, engine
from app.game.service import seed_cases_from_json, seed_demo_pitcher
from app.game.router import router as game_router
from app.ai.router import router as ai_router
from app.auth.router import router as auth_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Инициализация таблиц базы данных
    init_db()
    # 2. Загрузка тем из content/topics.json и создание демо-пользователя «Питчер»
    with Session(engine) as session:
        seed_cases_from_json(session)
        seed_demo_pitcher(session)
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Stage Zero — мобильная игра-тренажёр выступлений перед нарисованной публикой и столом жюри.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS для локальной разработки и мобильного приложения
cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",")] if settings.CORS_ORIGINS != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=settings.CORS_ORIGINS != "*",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Раздача статики для mp3 голосов жюри (/static/audio/...)
settings.STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(settings.STATIC_DIR)), name="static")

# Подключение игрового, AI и Auth роутеров
app.include_router(auth_router, prefix="/api")
app.include_router(game_router, prefix="/api")
app.include_router(ai_router, prefix="/api")


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "Stage Zero Backend",
        "docs": "/docs",
        "game_api": "/api/game",
        "ai_api": "/api/ai"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=True)
