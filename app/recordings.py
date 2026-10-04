"""Записи выступлений: звук из delivery сохраняется, чтобы раунд из истории можно было переслушать.

Файлы лежат в STATIC_DIR/recordings (папка смонтирована в docker-compose и переживает перезапуск)
и раздаются как /static/recordings/<round_id>.<ext>. Видео на сервер не уходит — только звук.
"""

import logging
import mimetypes
import re
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

# во что пишут телефон (m4a/aac), Chrome (webm), Safari (mp4) и запасной WAV
EXTENSIONS = {".m4a", ".mp4", ".aac", ".webm", ".ogg", ".wav", ".mp3"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# В slim-образе Python не знает .m4a/.aac и отдаёт их как application/octet-stream — Safari такое не играет.
# StaticFiles берёт тип из этого же реестра при каждом запросе
for _type, _ext in (("audio/mp4", ".m4a"), ("audio/aac", ".aac"), ("audio/webm", ".webm"), ("audio/ogg", ".ogg"), ("audio/wav", ".wav")):
    mimetypes.add_type(_type, _ext)


def _folder() -> Path:
    return settings.STATIC_DIR / "recordings"


def save_recording(round_id: str, filename: str | None, data: bytes) -> None:
    """Сохранить звук раунда; повторная отправка заменяет прежний файл. Ошибка записи разбор не ломает."""
    if not _SAFE_ID.match(round_id) or not data:
        return
    ext = Path(filename or "").suffix.lower()
    if ext not in EXTENSIONS:
        ext = ".m4a"
    folder = _folder()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(f"{round_id}.*"):
            old.unlink(missing_ok=True)
        (folder / f"{round_id}{ext}").write_bytes(data)
    except OSError as e:
        logger.warning("recordings: не сохранил запись round=%s: %s", round_id, e)


def recording_url(round_id: str) -> str | None:
    """Адрес записи раунда (/static/...) или None, если её нет (раунды до этой версии, моки)."""
    if not _SAFE_ID.match(round_id):
        return None
    for path in sorted(_folder().glob(f"{round_id}.*")):
        if path.suffix.lower() in EXTENSIONS:
            return f"/static/recordings/{path.name}"
    return None
