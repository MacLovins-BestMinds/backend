"""WS /api/ai/live: поток PCM 16 кГц → потоковое распознавание → события зала filler / long_pause / pace (~1 с).

Провайдер — LIVE_STT_PROVIDER: elevenlabs (Scribe v2 Realtime, по умолчанию) или deepgram.
Ключ провайдера остаётся на сервере: телефон шлёт звук только в наш бэкенд.
Состояние анализа хранится по round_id: при переподключении телефона время продолжается с того же места.
"""

import asyncio
import base64
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode

from fastapi import WebSocket, WebSocketDisconnect, status
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from app.ai.clients import MissingKeyError, require
from app.ai.config import AiSettings, get_settings
from app.ai.delivery_metrics import PACE_RANGE_WPM, PACE_WINDOW_SEC, find_fillers
from app.ai.schemas import FillerEvent, LiveEvent, LongPauseEvent, PaceEvent
from app.ai.stt import Word

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
PCM_BYTES_PER_SEC = SAMPLE_RATE * 2  # моно, 16 бит
LONG_PAUSE_SEC = 3.0
BURST_COUNT, BURST_WINDOW_SEC = 3, 20.0
PACE_COOLDOWN_SEC = 15.0
SESSION_TTL_SEC = 3600
VAD_SILENCE_SEC = 1.0  # Scribe фиксирует фразу после секунды тишины → точные таймкоды для темпа
_SENTENCE_END = (".", "!", "?", "…")


class LiveSttError(RuntimeError):
    """Сервис распознавания вернул ошибку (ключ, квота, формат звука)."""


@dataclass
class LiveAnalyzer:
    audio_t: float = 0.0  # сколько аудио получено за весь раунд, с
    offset: float = 0.0  # начало текущего потока распознавания на шкале раунда
    first_word_t: float | None = None
    last_speech_t: float = 0.0
    sentence_ended: bool = True
    pause_reported: bool = False
    last_burst_t: float = float("-inf")
    last_pace_t: float = float("-inf")
    partial_text: str = ""
    segment_fillers_sent: int = 0  # паразиты текущей фразы, уже отправленные по промежуточному тексту
    filler_times: deque[float] = field(default_factory=deque)
    word_times: deque[float] = field(default_factory=deque)

    def begin_stream(self) -> None:
        """Новое соединение с распознаванием: его таймкоды начинаются с нуля."""
        self.offset = self.audio_t
        self.partial_text = ""
        self.segment_fillers_sent = 0

    def on_audio(self, nbytes: int) -> list[LiveEvent]:
        self.audio_t += nbytes / PCM_BYTES_PER_SEC
        silence = self.audio_t - self.last_speech_t
        # пауза «посреди фразы»: человек уже говорил, последняя фраза не закончена
        mid_phrase = self.first_word_t is not None and not self.sentence_ended
        if mid_phrase and not self.pause_reported and silence > LONG_PAUSE_SEC:
            self.pause_reported = True
            return [LongPauseEvent(t=round(self.last_speech_t, 2), duration=round(silence, 1))]
        return []

    def _note_speech(self, end_t: float, last_text: str) -> None:
        if end_t > self.last_speech_t:
            self.last_speech_t = end_t
            self.sentence_ended = last_text.rstrip().endswith(_SENTENCE_END)
            self.pause_reported = False

    def _filler_event(self, t: float, word: str) -> FillerEvent:
        self.filler_times.append(t)
        while self.filler_times[0] < t - BURST_WINDOW_SEC:
            self.filler_times.popleft()
        burst = len(self.filler_times) >= BURST_COUNT and t - self.last_burst_t > BURST_WINDOW_SEC
        if burst:
            self.last_burst_t = t
        return FillerEvent(t=round(t, 2), word=word, burst=burst)

    def on_partial_text(self, text: str) -> list[LiveEvent]:
        """Промежуточный текст без таймкодов (Scribe): речь идёт сейчас, новые паразиты — сразу в зал."""
        text = text.strip()
        if not text or text == self.partial_text:
            return []
        self.partial_text = text
        if self.first_word_t is None:
            self.first_word_t = self.audio_t
        self._note_speech(self.audio_t, text)
        fillers = find_fillers([Word(w, self.audio_t, self.audio_t) for w in text.split()])
        new = fillers[self.segment_fillers_sent :]
        self.segment_fillers_sent = max(self.segment_fillers_sent, len(fillers))
        return [self._filler_event(self.audio_t, word) for _, word in new]

    def on_words(self, words: list[Word], is_final: bool) -> list[LiveEvent]:
        """words — уже на шкале раунда. Промежуточные результаты двигают только «последнюю речь»."""
        if not words:
            return []
        self._note_speech(words[-1].end, words[-1].text)
        if self.first_word_t is None:
            self.first_word_t = words[0].start
        if not is_final:
            return []

        fillers = find_fillers(words)
        # паразиты, уже отправленные по промежуточному тексту, не дублируем
        events: list[LiveEvent] = [self._filler_event(t, w) for t, w in fillers[self.segment_fillers_sent :]]
        self.segment_fillers_sent = 0
        self.partial_text = ""

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


# --- провайдеры потокового распознавания ---


class SttStream(Protocol):
    url: str
    headers: dict[str, str]

    def encode(self, chunk: bytes) -> str | bytes: ...
    def close_message(self) -> str | None: ...
    def handle(self, message: str | bytes, analyzer: LiveAnalyzer) -> list[LiveEvent]: ...


class ElevenLabsStream:
    """Scribe v2 Realtime: PCM 16 кГц в base64-JSON, фиксация фраз по VAD, таймкоды слов в committed-событиях."""

    def __init__(self, s: AiSettings) -> None:
        params = {
            "model_id": s.live_stt_model,
            "audio_format": f"pcm_{SAMPLE_RATE}",
            "language_code": s.stt_language,
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": VAD_SILENCE_SEC,
            "include_timestamps": "true",
        }
        self.url = f"{s.elevenlabs_realtime_url}?{urlencode(params)}"
        self.headers = {"xi-api-key": require(s.elevenlabs_api_key, "ELEVENLABS_API_KEY")}

    def encode(self, chunk: bytes) -> str:
        return json.dumps(
            {
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(chunk).decode(),
                "commit": False,
                "sample_rate": SAMPLE_RATE,
            }
        )

    def close_message(self) -> str:
        # зафиксировать недоговорённую фразу перед закрытием
        return json.dumps(
            {"message_type": "input_audio_chunk", "audio_base_64": "", "commit": True, "sample_rate": SAMPLE_RATE}
        )

    def handle(self, message: str | bytes, analyzer: LiveAnalyzer) -> list[LiveEvent]:
        data = json.loads(message)
        kind = data.get("message_type")
        if kind == "partial_transcript":
            return analyzer.on_partial_text(data.get("text", ""))
        if kind == "committed_transcript_with_timestamps":
            words = [
                Word(w["text"], w["start"] + analyzer.offset, w["end"] + analyzer.offset)
                for w in data.get("words") or []
                if w.get("type") == "word"
            ]
            return analyzer.on_words(words, is_final=True)
        if "error" in data:
            raise LiveSttError(f"{kind}: {data['error']}")
        return []  # session_started, committed_transcript без таймкодов и т.п.


class DeepgramStream:
    """Deepgram: сырой PCM linear16, промежуточные результаты с таймкодами слов."""

    def __init__(self, s: AiSettings) -> None:
        params = {
            "model": s.deepgram_model,
            "language": s.stt_language,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "punctuate": "true",
            "interim_results": "true",
            "filler_words": "true",
        }
        self.url = f"{s.deepgram_url}?{urlencode(params)}"
        self.headers = {"Authorization": f"Token {require(s.deepgram_api_key, 'DEEPGRAM_API_KEY')}"}

    def encode(self, chunk: bytes) -> bytes:
        return chunk

    def close_message(self) -> str:
        return json.dumps({"type": "CloseStream"})

    def handle(self, message: str | bytes, analyzer: LiveAnalyzer) -> list[LiveEvent]:
        data = json.loads(message)
        if data.get("type") != "Results":
            return []
        alternatives = data.get("channel", {}).get("alternatives") or [{}]
        words = [
            Word(w.get("punctuated_word") or w["word"], w["start"] + analyzer.offset, w["end"] + analyzer.offset)
            for w in alternatives[0].get("words", [])
        ]
        return analyzer.on_words(words, bool(data.get("is_final")))


def make_stream(s: AiSettings) -> SttStream:
    return ElevenLabsStream(s) if s.live_stt_provider == "elevenlabs" else DeepgramStream(s)


# --- проксирование приложение ↔ распознавание ---


async def _send(websocket: WebSocket, events: list[LiveEvent]) -> None:
    for event in events:
        await websocket.send_json(event.model_dump())


async def _app_to_stt(websocket: WebSocket, stt: ClientConnection, stream: SttStream, analyzer: LiveAnalyzer) -> None:
    try:
        while True:
            chunk = await websocket.receive_bytes()
            await stt.send(stream.encode(chunk))
            await _send(websocket, analyzer.on_audio(len(chunk)))
    except WebSocketDisconnect:
        if message := stream.close_message():
            await stt.send(message)


async def _stt_to_app(websocket: WebSocket, stt: ClientConnection, stream: SttStream, analyzer: LiveAnalyzer) -> None:
    async for message in stt:
        await _send(websocket, stream.handle(message, analyzer))


async def _close(websocket: WebSocket, reason: str) -> None:
    try:
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason=reason)  # reason ≤ 123 байт
    except RuntimeError:
        pass  # уже закрыт


async def run_live(websocket: WebSocket, round_id: str) -> None:
    settings = get_settings()
    try:
        stream = make_stream(settings)
    except MissingKeyError as e:
        logger.error("live: %s", e)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="speech service is not configured")
        return
    await websocket.accept()
    analyzer = get_session(round_id)
    analyzer.begin_stream()
    try:
        async with connect(stream.url, additional_headers=stream.headers) as stt:
            app_task = asyncio.create_task(_app_to_stt(websocket, stt, stream, analyzer))
            stt_task = asyncio.create_task(_stt_to_app(websocket, stt, stream, analyzer))
            done, pending = await asyncio.wait({app_task, stt_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                if exc := task.exception():
                    raise exc
            if stt_task in done:  # распознавание закрыло поток само — пусть приложение переподключится
                await _close(websocket, "speech stream ended, reconnect")
    except LiveSttError as e:
        logger.error("live: ошибка распознавания (%s), round=%s: %s", settings.live_stt_provider, round_id, e)
        await _close(websocket, "speech service error")
    except (WebSocketException, OSError):
        logger.exception("live: %s недоступен, round=%s", settings.live_stt_provider, round_id)
        await _close(websocket, "speech service unavailable")
    except (WebSocketDisconnect, RuntimeError):
        pass  # приложение отключилось — оно переподключится с тем же round_id
