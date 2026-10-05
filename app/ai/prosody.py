"""Голос по записи: монотонность и затухание к концу фраз — кодом, для любого языка.

Расшифровка говорит, что сказано; здесь слышно, как. По WAV (16 кГц, моно) и таймкодам слов считаются:
- высота голоса каждого слова (автокорреляция по кадрам 32 мс) → разброс по словам в полутонах. Живая речь
  гуляет на 3–5 полутонов; меньше MONOTONE_SEMITONES при достаточном числе слов — монотонно, зал засыпает;
- громкость каждого слова (RMS, дБ) → последнее слово фразы заметно тише остальных — голос «затухает»,
  конец мысли до зала не доходит.
Только относительные меры: громкость микрофона и расстояние до него на них не влияют. Нет numpy, битый WAV,
нет слов — None, разбор идёт без голоса.
"""

import io
import logging
import math
import wave
from collections.abc import Sequence
from dataclasses import dataclass

from app.ai.stt import Word

logger = logging.getLogger(__name__)

FRAME_SEC, HOP_SEC = 0.032, 0.010
F0_MIN, F0_MAX = 70.0, 400.0  # голос человека; выше и ниже — шум и обертоны
VOICED_CORR = 0.5  # нормированная автокорреляция ниже — кадр не звонкий (шипящий, пауза)
NOISE_GAIN, RMS_MIN = 4.0, 0.003  # кадр громче шума в NOISE_GAIN раз (и не тише RMS_MIN) — речь
CHUNK_FRAMES = 2000  # автокорреляция считается кусками — память
MIN_WORD_SEC = 0.08
MIN_VOICED_FRAMES = 3  # меньше звонких кадров в слове — высота слова не определена
PITCH_WORDS_MIN = 6  # меньше слов с высотой — разброс не считается
MONOTONE_WORDS_MIN = 20  # меньше — монотонность не оценивается (короткий питч)
MONOTONE_SEMITONES = 1.5
OUTLIER_SEMITONES = 11.0  # слово почти на октаву выше или ниже медианы — ошибка определения высоты, не считаем
FADE_DB = 9.0  # последнее слово фразы тише медианы фразы на столько — затухание (естественное снижение 3–6 дБ)
FADE_MIN_WORDS = 5  # короткие фразы не оцениваются: слишком шумно
FADE_FLOOR_DB = -40.0  # фраза в целом тише — это шёпот или шум, затухание не считаем
_SENTENCE_END = (".", "!", "?", "…")


@dataclass(frozen=True, slots=True)
class Prosody:
    pitch_variation: float | None  # разброс высоты голоса по словам, полутоны; None — мало звонких слов
    monotone: bool | None  # None — слов мало, чтобы судить
    fades: list[tuple[int, float]]  # (номер последнего слова фразы, на сколько дБ оно тише фразы)


@dataclass(frozen=True, slots=True)
class WordVoice:
    f0: float | None  # медианная высота слова, Гц; None — не звонкое или слишком короткое
    db: float | None  # средняя громкость слова, дБ относительно полной шкалы


def _samples(wav: bytes):
    """WAV → (отсчёты float32 от −1 до 1, частота дискретизации). Стерео сводится в моно."""
    import numpy as np  # noqa: PLC0415 — тяжёлая зависимость только здесь

    with wave.open(io.BytesIO(wav)) as f:
        sr, channels, width = f.getframerate(), f.getnchannels(), f.getsampwidth()
        raw = f.readframes(f.getnframes())
    if width != 2:
        raise ValueError(f"prosody: ожидается 16-битный WAV, а не {width * 8}-битный")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x, sr


def _frames(x, sr: int):
    """Кадры по HOP_SEC: (время центра кадра, с; RMS; высота голоса, Гц, или nan для незвонких кадров)."""
    import numpy as np  # noqa: PLC0415

    frame, hop = int(FRAME_SEC * sr), int(HOP_SEC * sr)
    if len(x) < frame:
        x = np.pad(x, (0, frame - len(x)))
    frames = np.lib.stride_tricks.sliding_window_view(x, frame)[::hop]
    centers = (np.arange(len(frames)) * hop + frame / 2) / sr
    rms = np.sqrt((frames**2).mean(axis=1))
    floor = float(np.percentile(rms, 5))
    speech = rms > max(floor * NOISE_GAIN, RMS_MIN)

    lo, hi = int(sr / F0_MAX), int(sr / F0_MIN)
    n = 1 << (2 * frame - 1).bit_length()
    window = np.hanning(frame).astype(np.float32)
    f0 = np.full(len(frames), np.nan)
    for a in range(0, len(frames), CHUNK_FRAMES):
        idx = np.flatnonzero(speech[a : a + CHUNK_FRAMES]) + a
        if not len(idx):
            continue
        spec = np.fft.rfft(frames[idx] * window, n=n, axis=1)
        ac = np.fft.irfft(spec * np.conj(spec), n=n, axis=1)[:, : hi + 1]
        ac = ac / np.maximum(ac[:, :1], 1e-12)
        seg = ac[:, lo : hi + 1]
        lag = seg.argmax(axis=1) + lo
        voiced = seg.max(axis=1) >= VOICED_CORR
        f0[idx[voiced]] = sr / lag[voiced]
    return centers, rms, f0


def word_voices(wav: bytes, words: Sequence[Word]) -> list[WordVoice]:
    """Высота и громкость каждого слова по его таймкодам."""
    import numpy as np  # noqa: PLC0415

    x, sr = _samples(wav)
    centers, rms, f0 = _frames(x, sr)
    out: list[WordVoice] = []
    for w in words:
        if w.end - w.start < MIN_WORD_SEC:
            out.append(WordVoice(None, None))
            continue
        mask = (centers >= w.start) & (centers <= w.end)
        if not mask.any():
            out.append(WordVoice(None, None))
            continue
        db = 20 * math.log10(float(rms[mask].mean()) + 1e-9)
        voiced = f0[mask]
        voiced = voiced[~np.isnan(voiced)]
        pitch = float(np.median(voiced)) if len(voiced) >= MIN_VOICED_FRAMES else None
        out.append(WordVoice(pitch, round(db, 1)))
    return out


def pitch_variation(voices: Sequence[WordVoice]) -> tuple[float | None, bool | None]:
    """Разброс высоты по словам в полутонах и вердикт «монотонно» (None — слов мало)."""
    pitches = [v.f0 for v in voices if v.f0]
    if len(pitches) < PITCH_WORDS_MIN:
        return None, None
    ref = sorted(pitches)[len(pitches) // 2]
    semis = [12 * math.log2(p / ref) for p in pitches]
    semis = [s for s in semis if abs(s) < OUTLIER_SEMITONES]
    if len(semis) < PITCH_WORDS_MIN:
        return None, None
    mean = sum(semis) / len(semis)
    std = math.sqrt(sum((s - mean) ** 2 for s in semis) / len(semis))
    monotone = std < MONOTONE_SEMITONES if len(semis) >= MONOTONE_WORDS_MIN else None
    return round(std, 2), monotone


def _ends_sentence(text: str) -> bool:
    return text.rstrip(" \"'»)").endswith(_SENTENCE_END)


def fades(words: Sequence[Word], voices: Sequence[WordVoice]) -> list[tuple[int, float]]:
    """Фразы, последнее слово которых заметно тише остальных: (номер слова, на сколько дБ тише)."""
    found: list[tuple[int, float]] = []
    begin = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        if not (_ends_sentence(w.text) or last):
            continue
        phrase = range(begin, i + 1)
        begin = i + 1
        if len(phrase) < FADE_MIN_WORDS or voices[i].db is None:
            continue
        body = sorted(v.db for v in (voices[k] for k in phrase[:-1]) if v.db is not None)
        if len(body) < 3:
            continue
        level = body[len(body) // 2]
        drop = level - voices[i].db
        if level > FADE_FLOOR_DB and drop >= FADE_DB:
            found.append((i, round(drop, 1)))
    return found


def analyze_prosody(wav: bytes, words: Sequence[Word]) -> Prosody | None:
    """Голос по записи и словам; None — посчитать нельзя (нет numpy, битый WAV, нет слов)."""
    if not words:
        return None
    try:
        voices = word_voices(wav, words)
    except ImportError:
        logger.warning("prosody: numpy не установлен — голос не оценивается")
        return None
    except Exception:
        logger.exception("prosody: не удалось разобрать запись — голос не оценивается")
        return None
    variation, monotone = pitch_variation(voices)
    return Prosody(pitch_variation=variation, monotone=monotone, fades=fades(words, voices))
