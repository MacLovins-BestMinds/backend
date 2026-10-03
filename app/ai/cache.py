"""Дисковый кэш ответов AI-сервисов для показа.

Ключ — sha256 от всех входов вызова (модель, параметры, промпт/аудио), поэтому повтор того же запроса
не идёт в сеть, отвечает мгновенно и всегда одинаково. Выключается AI_CACHE=0, сбрасывается удалением папки.
"""

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from app.ai.config import get_settings

logger = logging.getLogger(__name__)


def make_key(*parts: str | bytes) -> str:
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8, "big"))  # длина-префикс: ("ab","c") ≠ ("a","bc")
        digest.update(data)
    return digest.hexdigest()


def _path(namespace: str, key: str, ext: str) -> Path:
    return Path(get_settings().ai_cache_dir) / namespace / f"{key}.{ext}"


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)  # атомарно: параллельный читатель не увидит половину файла


async def get(namespace: str, key: str, ext: str) -> bytes | None:
    if not get_settings().ai_cache:
        return None
    data = await asyncio.to_thread(_read, _path(namespace, key, ext))
    logger.info("cache %s %s/%s", "hit" if data is not None else "miss", namespace, key[:12])
    return data


async def put(namespace: str, key: str, ext: str, data: bytes) -> None:
    if get_settings().ai_cache:
        await asyncio.to_thread(_write, _path(namespace, key, ext), data)
