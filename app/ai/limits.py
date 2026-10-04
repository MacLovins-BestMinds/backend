"""Одновременные запросы к ElevenLabs: тариф даёт всего несколько слотов на весь аккаунт.

Слоты делятся на два класса, иначе очередь заблокирует сама себя:
- LIVE — живое распознавание на сцене; держится весь питч. Свободного нет — игрок выходит на сцену без живых
  реакций на паразитов и темп (мягкая деградация), а не ждёт;
- SHORT — короткие запросы: распознавание записи, ответов жюри и озвучка вопросов (по одному, а не тремя сразу).
Сумма классов — не больше слотов тарифа (ELEVENLABS_LIVE_SLOTS + ELEVENLABS_SHORT_SLOTS).

Семафоры живут в памяти: бэкенд — один процесс uvicorn. Для нескольких процессов нужна общая очередь (Redis).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache

from elevenlabs.core.api_error import ApiError

from app.ai.config import get_settings

logger = logging.getLogger(__name__)

RETRY_DELAYS_SEC = (1.0, 2.0, 4.0)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}  # слоты заняты или временный сбой; квота кредитов (401) — не повтор


@lru_cache
def short_slots() -> asyncio.Semaphore:
    return asyncio.Semaphore(get_settings().elevenlabs_short_slots)


_live_in_use = 0  # сколько слотов живого распознавания занято сейчас


async def elevenlabs_call[T](op: str, call: Callable[[], Awaitable[T]]) -> T:
    """Короткий запрос к ElevenLabs: ждёт свой слот, а при 429/5xx повторяет с паузой."""
    async with short_slots():
        for delay in (*RETRY_DELAYS_SEC, None):
            try:
                return await call()
            except ApiError as e:
                if delay is None or e.status_code not in RETRYABLE_STATUS:
                    raise
                logger.warning("elevenlabs %s: %s, повтор через %.0f с", op, e.status_code, delay)
                await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def try_live_slot() -> bool:
    """Занять слот живого распознавания без ожидания; False — все заняты. Освобождать: release_live_slot()."""
    global _live_in_use
    if _live_in_use >= get_settings().elevenlabs_live_slots:
        return False
    _live_in_use += 1
    return True


def release_live_slot() -> None:
    global _live_in_use
    _live_in_use = max(0, _live_in_use - 1)


def live_slots_in_use() -> int:
    return _live_in_use
