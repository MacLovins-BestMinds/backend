"""Фоновые задачи разбора после delivery: ход мысли (flow) и «лучшая версия» своим голосом (better_version).

- Старт: сразу после успешного delivery (роутер) и по GET, если результата нет или он по прошлой записи раунда.
  Delivery их не ждёт: это asyncio-задачи в том же процессе.
- Дедупликация: на раунд и вид — одна задача в памяти процесса (бэкенд — один процесс uvicorn, как в limits.py).
  Новая запись раунда (повторный delivery) отменяет задачу по старой и запускает новую.
- Состояние — AiResult того же вида: pending → ready | failed | unavailable; source — отпечаток записи, по которой
  посчитан результат. Pending без живой задачи (сервер перезапустился посреди работы) считается брошенной и
  перезапускается по GET; failed — не раньше RETRY_FAILED_SEC и не больше MAX_ATTEMPTS попыток.
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.ai import better, cache, flow, game_api
from app.ai.config import get_settings
from app.ai.jury import RoundStateError
from app.ai.pitch import RoundNotFoundError
from app.ai.schemas import BetterVersionResponse, FlowResponse

logger = logging.getLogger(__name__)

KINDS = ("flow", "better_version")
STALE_PENDING_SEC = 600  # pending дольше — задача точно умерла
RETRY_FAILED_SEC = 300
MAX_ATTEMPTS = 3
_PROCESS_STARTED = time.time()  # pending, начатая до старта процесса, брошена: процесс один


@dataclass(slots=True)
class _Job:
    source: str
    task: asyncio.Task[None]


_running: dict[tuple[str, str], _Job] = {}


def source_of(delivery: dict[str, Any]) -> str:
    """Отпечаток записи раунда: повторный delivery с другой записью — новые результаты."""
    words = json.dumps(delivery.get("words") or [], sort_keys=True)
    return cache.make_key(delivery.get("transcript") or "", words)[:16]


def enabled(kind: str) -> bool:
    s = get_settings()
    return s.review_jobs_enabled and (kind != "better_version" or s.better_version_enabled)


def _runner(kind: str) -> Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]:
    # ищется при каждом вызове, а не таблицей при импорте, — тесты подменяют функции модулей
    return flow.run_flow if kind == "flow" else better.run_better_version


async def _job(kind: str, round_id: str, delivery: dict[str, Any], source: str, attempts: int) -> None:
    base = {"source": source, "attempts": attempts}
    await asyncio.to_thread(game_api.save_ai_result, round_id, kind, {"status": "pending", **base, "started_at": time.time()})
    try:
        result = await _runner(kind)(round_id, delivery)
    except better.Unavailable as e:
        result = {"status": "unavailable", "reason": e.code}
    except Exception:  # noqa: BLE001 — любой сбой задачи = failed, delivery и сервер живут дальше
        logger.exception("jobs: %s round=%s не получился (попытка %d)", kind, round_id, attempts)
        result = {"status": "failed"}
    await asyncio.to_thread(game_api.save_ai_result, round_id, kind, {**result, **base, "finished_at": time.time()})


def start(kind: str, round_id: str, delivery: dict[str, Any], attempts: int = 1) -> None:
    """Запустить задачу, если по этой записи она ещё не идёт. Синхронная: между проверкой и запуском нет await,
    поэтому два одновременных запроса не запустят две задачи."""
    source = source_of(delivery)
    key = (kind, round_id)
    job = _running.get(key)
    if job is not None and not job.task.done():
        if job.source == source:
            return
        job.task.cancel()  # считает по прошлой записи раунда
    task = asyncio.create_task(_job(kind, round_id, delivery, source, attempts), name=f"{kind}:{round_id}")
    _running[key] = _Job(source, task)

    def forget(done: asyncio.Task[None]) -> None:
        if (current := _running.get(key)) is not None and current.task is done:
            del _running[key]

    task.add_done_callback(forget)


def after_delivery(round_id: str, delivery: dict[str, Any]) -> None:
    """Сразу после успешного delivery: ход мысли и лучшая версия по новой записи — в фоне."""
    for kind in KINDS:
        if enabled(kind):
            start(kind, round_id, delivery)


def _needs_run(kind: str, round_id: str, stored: dict[str, Any]) -> bool:
    """Сохранённый результат по текущей записи надо пересчитать: брошен, упал давно, файла нет, включили обратно."""
    status, now = stored.get("status"), time.time()
    if status == "pending":
        started = stored.get("started_at") or 0
        return started < _PROCESS_STARTED or now - started > STALE_PENDING_SEC
    if status == "failed":
        return stored.get("attempts", 0) < MAX_ATTEMPTS and now - (stored.get("finished_at") or 0) > RETRY_FAILED_SEC
    if status == "unavailable":
        return stored.get("reason") == "disabled" and enabled(kind)
    if status == "ready" and kind == "better_version":
        return not better.audio_path(round_id).exists()
    return status != "ready"


def _disabled(kind: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": "disabled"} if kind == "better_version" else {"status": "failed"}


async def state(kind: str, round_id: str) -> dict[str, Any]:
    """Состояние результата раунда; если результата нет, он по прошлой записи или брошен — запускает задачу."""
    if await asyncio.to_thread(game_api.get_round, round_id) is None:
        raise RoundNotFoundError(f"Round {round_id} not found")
    delivery = await asyncio.to_thread(game_api.get_ai_result, round_id, "delivery")
    if delivery is None:
        raise RoundStateError("no_delivery")
    source = source_of(delivery)
    job = _running.get((kind, round_id))
    if job is not None and not job.task.done() and job.source == source:
        return {"status": "pending"}

    stored = await asyncio.to_thread(game_api.get_ai_result, round_id, kind)
    current = stored if stored and stored.get("source") == source else None
    if current is not None and not _needs_run(kind, round_id, current):
        return current
    if not enabled(kind):
        return current if current is not None and current.get("status") != "pending" else _disabled(kind)
    if current is not None and current.get("status") in ("pending", "failed"):
        attempts = current.get("attempts", 0) + 1
        if attempts > MAX_ATTEMPTS:  # брошенная pending после всех попыток
            return {**current, "status": "failed"}
    else:
        attempts = 1
    start(kind, round_id, delivery, attempts)
    return {"status": "pending"}


# --- ответы API ---


def flow_view(stored: dict[str, Any]) -> FlowResponse:
    status = stored.get("status")
    if status == "ready":
        return FlowResponse(status="ready", summary=stored.get("summary"), moments=stored.get("moments") or [])
    return FlowResponse(status="pending" if status == "pending" else "failed")


def better_view(round_id: str, stored: dict[str, Any], lang: str) -> BetterVersionResponse:
    """lang — язык интерфейса: на нём причина, почему версии нет."""
    status = stored.get("status")
    if status == "ready" and better.audio_path(round_id).exists():
        return BetterVersionResponse(status="ready", audio_url=better.audio_url(round_id), text=stored.get("text"))
    if status == "pending":
        return BetterVersionResponse(status="pending")
    if status == "unavailable":
        return BetterVersionResponse(status="unavailable", reason=better.reason_text(stored.get("reason"), lang))
    return BetterVersionResponse(status="failed", reason=better.reason_text("failed", lang))


def review_view(
    kind: str, round_id: str, stored: dict[str, Any] | None, delivery: dict[str, Any] | None, lang: str
) -> dict[str, Any] | None:
    """Для разбора из истории: сохранённый результат в форме ответа GET, без запуска задач.
    None — результата нет или он по прошлой записи раунда (его пересчитает GET)."""
    if not stored or not delivery or stored.get("source") != source_of(delivery):
        return None
    view = flow_view(stored) if kind == "flow" else better_view(round_id, stored, lang)
    return view.model_dump(mode="json")
