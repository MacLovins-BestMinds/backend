"""AI-движок Stage Zero. Подключение в app/main.py: `app.include_router(ai_router)`."""

from app.ai.router import router as ai_router

__all__ = ["ai_router"]
