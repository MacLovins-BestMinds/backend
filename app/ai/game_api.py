"""Точка стыка с игровым бэкендом Егора.

Нужные функции: `get_case(case_id)` (с приколом), `get_round(round_id)`,
`save_ai_result(round_id, kind, payload)` и `get_ai_result(round_id, kind)` (последний результат такого вида).
Пока `app.game` не готов, работают заглушки, которые хранят результаты в памяти процесса.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from app.game import get_ai_result, get_case, get_round, save_ai_result  # type: ignore[attr-defined]
except ImportError:
    logger.warning("app.game недоступен — используются заглушки game_api")

    _results: dict[tuple[str, str], dict[str, Any]] = {}

    def get_case(case_id: str) -> dict[str, Any]:
        return {
            "id": case_id,
            "category": "health",
            "title": "Умная таблетница",
            "brief": "Ты придумал таблетницу, которая напоминает пожилым о лекарствах. Убеди зал, что она нужна.",
            "audience": "business",
            "quirk": "А если бабушка не пользуется смартфоном?",
        }

    def get_round(round_id: str) -> dict[str, Any]:
        if round_id.startswith("warmup"):  # mode: training | daily | own | warmup; lang — язык интерфейса
            return {"id": round_id, "mode": "warmup", "case_id": None, "own": None, "lang": "en"}
        return {"id": round_id, "mode": "training", "case_id": "health-01", "own": None, "lang": "en"}

    def save_ai_result(round_id: str, kind: str, payload: dict[str, Any]) -> None:
        _results[round_id, kind] = payload

    def get_ai_result(round_id: str, kind: str) -> dict[str, Any] | None:
        return _results.get((round_id, kind))


__all__ = ["get_ai_result", "get_case", "get_round", "save_ai_result"]
