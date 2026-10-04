"""Контент тем из content/topics.json: какие темы активны и справочные материалы к ним."""

import json
from functools import lru_cache
from typing import Any

from app.core.config import settings


@lru_cache
def _topics() -> dict[str, dict[str, Any]]:
    if not settings.TOPICS_JSON_PATH.exists():
        return {}
    with open(settings.TOPICS_JSON_PATH, encoding="utf-8") as f:
        return {item["id"]: item for item in json.load(f)}


def active_ids() -> set[str]:
    """Темы из текущего topics.json; старые темы остаются в базе ради истории раундов, но не выпадают."""
    return set(_topics())


def level(case_id: str) -> str:
    """Уровень сложности темы: easy | medium | hard."""
    return _topics().get(case_id, {}).get("level", "easy")


def reading(case_id: str) -> tuple[str | None, list[dict[str, str]]]:
    """Короткая выжимка (англ.) и проверенные ссылки на статьи по теме."""
    topic = _topics().get(case_id, {})
    return topic.get("summary") or None, topic.get("sources", [])
