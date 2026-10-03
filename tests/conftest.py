import os

# Тесты не ходят во внешние AI-сервисы и не требуют ключей: AI-эндпоинты отвечают моками
os.environ.setdefault("AI_MOCK", "1")
