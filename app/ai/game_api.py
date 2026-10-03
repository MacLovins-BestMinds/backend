"""Точка стыка с игровым бэкендом Егора.

Пока `app.game` не готов, используются заглушки с тем же интерфейсом:
`get_case(case_id)` (с приколом), `get_round(round_id)` и `save_ai_result(round_id, kind, payload)`.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from app.game import get_case, get_round, save_ai_result  # type: ignore[attr-defined]
except ImportError:
    logger.warning("app.game недоступен — используются заглушки get_case/get_round/save_ai_result")

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
        return {"id": round_id, "mode": "training", "case_id": "health-01", "own": None}

    def save_ai_result(round_id: str, kind: str, payload: dict[str, Any]) -> None:
        logger.info("save_ai_result(stub) round=%s kind=%s", round_id, kind)


__all__ = ["get_case", "get_round", "save_ai_result"]
