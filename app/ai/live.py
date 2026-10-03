"""WS /api/ai/live: поток PCM → Deepgram → события зала filler / long_pause / pace с задержкой до ~1 с.

Состояние анализа хранится по round_id: при переподключении телефона время продолжается с того же места.
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlencode

from fastapi import WebSocket, WebSocketDisconnect, status
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from app.ai.config import get_settings
from app.ai.delivery_metrics import PACE_RANGE_WPM, PACE_WINDOW_SEC, find_fillers
from app.ai.schemas import FillerEvent, LiveEvent, LongPauseEvent, PaceEvent
from app.ai.stt import Word

logger = logging.getLogger(__name__)

PCM_BYTES_PER_SEC = 16_000 * 2  # 16 кГц, моно, 16 бит
LONG_PAUSE_SEC = 3.0
BURST_COUNT, BURST_WINDOW_SEC = 3, 20.0
PACE_COOLDOWN_SEC = 15.0
SESSION_TTL_SEC = 3600
_SENTENCE_END = (".", "!", "?", "…")


@dataclass
class LiveAnalyzer:
    audio_t: float = 0.0  # сколько аудио получено за весь раунд, с
    offset: float = 0.0  # начало текущего потока Deepgram на шкале раунда
    first_word_t: float | None = None
    last_speech_t: float = 0.0
    sentence_ended: bool = True
    pause_reported: bool = False
    last_burst_t: float = float("-inf")
    last_pace_t: float = float("-inf")
    filler_times: deque[float] = field(default_factory=deque)
    word_times: deque[float] = field(default_factory=deque)

    def begin_stream(self) -> None:
        """Новое соединение с Deepgram: его таймкоды начинаются с нуля."""
        self.offset = self.audio_t

    def on_audio(self, nbytes: int) -> list[LiveEvent]:
        self.audio_t += nbytes / PCM_BYTES_PER_SEC
        silence = self.audio_t - self.last_speech_t
        # пауза «посреди фразы»: человек уже говорил, последняя фраза не закончена
        mid_phrase = self.first_word_t is not None and not self.sentence_ended
        if mid_phrase and not self.pause_reported and silence > LONG_PAUSE_SEC:
            self.pause_reported = True
            return [LongPauseEvent(t=round(self.last_speech_t, 2), duration=round(silence, 1))]
        return []

    def on_words(self, words: list[Word], is_final: bool) -> list[LiveEvent]:
        """words — уже на шкале раунда. Промежуточные результаты двигают только «последнюю речь»."""
        if not words:
            return []
        if words[-1].end > self.last_speech_t:
            self.last_speech_t = words[-1].end
            self.sentence_ended = words[-1].text.rstrip().endswith(_SENTENCE_END)
            self.pause_reported = False
        if self.first_word_t is None:
            self.first_word_t = words[0].start
        if not is_final:
            return []

        events: list[LiveEvent] = []
        fillers = find_fillers(words)
        for t, word in fillers:
            self.filler_times.append(t)
            while self.filler_times[0] < t - BURST_WINDOW_SEC:
                self.filler_times.popleft()
            burst = len(self.filler_times) >= BURST_COUNT and t - self.last_burst_t > BURST_WINDOW_SEC
            if burst:
                self.last_burst_t = t
            events.append(FillerEvent(t=round(t, 2), word=word, burst=burst))

        filler_starts = {t for t, _ in fillers}
        self.word_times.extend(w.start for w in words if w.start not in filler_starts)
        now = words[-1].end
        while self.word_times and self.word_times[0] < now - PACE_WINDOW_SEC:
            self.word_times.popleft()
        if now - self.first_word_t >= PACE_WINDOW_SEC and now - self.last_pace_t >= PACE_COOLDOWN_SEC:
            wpm = round(len(self.word_times) * 60 / PACE_WINDOW_SEC)
            if not PACE_RANGE_WPM[0] <= wpm <= PACE_RANGE_WPM[1]:
                self.last_pace_t = now
                verdict = "fast" if wpm > PACE_RANGE_WPM[1] else "slow"
                events.append(PaceEvent(t=round(now, 2), wpm=wpm, verdict=verdict))
        return events


_sessions: dict[str, tuple[LiveAnalyzer, float]] = {}


def get_session(round_id: str) -> LiveAnalyzer:
    now = time.monotonic()
    for rid, (_, seen) in list(_sessions.items()):
        if now - seen > SESSION_TTL_SEC:
            del _sessions[rid]
    analyzer = _sessions[round_id][0] if round_id in _sessions else LiveAnalyzer()
    _sessions[round_id] = (analyzer, now)
    return analyzer


def parse_results(message: str | bytes, offset: float) -> tuple[list[Word], bool] | None:
    data = json.loads(message)
    if data.get("type") != "Results":
        return None
    alternatives = data.get("channel", {}).get("alternatives") or [{}]
    words = [
        Word(w.get("punctuated_word") or w["word"], w["start"] + offset, w["end"] + offset)
        for w in alternatives[0].get("words", [])
    ]
    return words, bool(data.get("is_final"))


def _deepgram_url() -> str:
    s = get_settings()
    params = {
        "model": s.deepgram_model,
        "language": s.stt_language,
        "encoding": "linear16",
        "sample_rate": 16_000,
        "channels": 1,
        "punctuate": "true",
        "interim_results": "true",
        "filler_words": "true",
    }
    return f"{s.deepgram_url}?{urlencode(params)}"


async def _send(websocket: WebSocket, events: list[LiveEvent]) -> None:
    for event in events:
        await websocket.send_json(event.model_dump())


async def _app_to_deepgram(websocket: WebSocket, dg: ClientConnection, analyzer: LiveAnalyzer) -> None:
    try:
        while True:
            chunk = await websocket.receive_bytes()
            await dg.send(chunk)
            await _send(websocket, analyzer.on_audio(len(chunk)))
    except WebSocketDisconnect:
        await dg.send(json.dumps({"type": "CloseStream"}))


async def _deepgram_to_app(websocket: WebSocket, dg: ClientConnection, analyzer: LiveAnalyzer) -> None:
    async for message in dg:
        if parsed := parse_results(message, analyzer.offset):
            await _send(websocket, analyzer.on_words(*parsed))


async def _close(websocket: WebSocket, reason: str) -> None:
    try:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason=reason)
    except RuntimeError:
        pass  # уже закрыт


async def run_live(websocket: WebSocket, round_id: str) -> None:
    settings = get_settings()
    if not settings.deepgram_api_key:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="DEEPGRAM_API_KEY is not set")
        return
    await websocket.accept()
    analyzer = get_session(round_id)
    analyzer.begin_stream()
    headers = {"Authorization": f"Token {settings.deepgram_api_key}"}
    try:
        async with connect(_deepgram_url(), additional_headers=headers) as dg:
            app_task = asyncio.create_task(_app_to_deepgram(websocket, dg, analyzer))
            dg_task = asyncio.create_task(_deepgram_to_app(websocket, dg, analyzer))
            done, pending = await asyncio.wait({app_task, dg_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                if exc := task.exception():
                    raise exc
            if dg_task in done:  # Deepgram закрыл поток сам — пусть приложение переподключится
                await _close(websocket, "speech stream ended, reconnect")
    except (WebSocketException, OSError):
        logger.exception("live: Deepgram недоступен, round=%s", round_id)
        await _close(websocket, "speech service unavailable")
    except (WebSocketDisconnect, RuntimeError):
        pass  # приложение отключилось — оно переподключится с тем же round_id
