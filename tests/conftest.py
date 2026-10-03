import os

# Тесты не ходят во внешние AI-сервисы и не требуют ключей: AI-эндпоинты отвечают моками
os.environ.setdefault("AI_MOCK", "1")
# Игровые тесты проходят раунд на моках и используют тестовые Google-токены mock_<id>
os.environ.setdefault("MOCK_FALLBACK", "1")
os.environ.setdefault("AUTH_MOCK_GOOGLE", "1")
