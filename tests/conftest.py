import os

# Тесты не ходят во внешние AI-сервисы и не требуют ключей: AI-эндпоинты отвечают моками
os.environ.setdefault("AI_MOCK", "1")
# Игровые тесты проходят раунд на моках и используют тестовые Google-токены mock_<id>
os.environ.setdefault("MOCK_FALLBACK", "1")
os.environ.setdefault("AUTH_MOCK_GOOGLE", "1")
# Фоновые задачи после delivery (ход мысли, «лучшая версия») ходят в Gemini и ElevenLabs — в тестах выключены
# всегда, а не setdefault: в контейнере .env задаёт AI_MOCK=0. Тесты задач включают их сами и мокают вызовы
os.environ["REVIEW_JOBS_ENABLED"] = "0"
