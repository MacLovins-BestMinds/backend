"""Контент тем из content/topics.json: какие темы активны, справочные материалы и переводы.

Переводы тем — статические файлы content/topics.<lang>.json (ru, ro), их делает tools/translate_topics.py.
Запись перевода действует, только пока английский оригинал не менялся (source_hash); иначе — английский текст.
"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import settings

# что в теме видит игрок и что переводится; category — название категории («🍕 Food»)
TRANSLATED_FIELDS = ("title", "brief", "audience", "summary", "category")


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


def english_texts(case_id: str) -> dict[str, str]:
    """Английский оригинал того, что переводится; темы нет в topics.json — пустой словарь."""
    topic = _topics().get(case_id)
    if topic is None:
        return {}
    return {
        "title": topic.get("title", ""),
        "brief": topic.get("brief", ""),
        "audience": topic.get("audience", ""),
        "summary": topic.get("summary") or "",
        "category": topic.get("category_title", ""),
    }


def source_hash(texts: dict[str, str]) -> str:
    """Отпечаток английского оригинала: по нему видно, что перевод устарел."""
    return hashlib.sha256(json.dumps(texts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def translations_path(lang: str) -> Path:
    return settings.TOPICS_JSON_PATH.with_name(f"topics.{lang}.json")


@lru_cache
def translations(lang: str) -> dict[str, dict[str, str]]:
    """Переводы тем на язык lang, только актуальные (оригинал с тех пор не менялся). Нет файла — пусто."""
    path = translations_path(lang)
    if lang == "en" or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        entries: dict[str, dict[str, str]] = json.load(f)
    return {
        case_id: entry
        for case_id, entry in entries.items()
        if (texts := english_texts(case_id)) and entry.get("source_hash") == source_hash(texts)
    }


def translated(case_id: str, field: str, english: str | None, lang: str) -> str | None:
    """Текст темы на языке интерфейса; перевода нет (язык en, тема или поле не переведены) — английский."""
    return translations(lang).get(case_id, {}).get(field) or english
