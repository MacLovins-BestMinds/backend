"""Голос по записи: монотонность и затухание фраз — на синтетическом звуке с известной высотой и громкостью."""

import io
import math
import struct
import wave

from app.ai.delivery_metrics import MONOTONE_COST, analyze
from app.ai.prosody import Prosody, analyze_prosody, fades, pitch_variation, word_voices
from app.ai.stt import Transcript, Word

SR = 16_000


def _wav(spoken: list[tuple[str, float, float, float, float]]) -> tuple[bytes, list[Word]]:
    """spoken — (слово, начало, конец, высота в Гц, амплитуда). Голос — основной тон с двумя обертонами."""
    total = int((spoken[-1][2] + 0.3) * SR)
    samples = [0.0] * total
    words = []
    for text, start, end, f0, amp in spoken:
        for n in range(int(start * SR), int(end * SR)):
            t = n / SR
            samples[n] = amp * (math.sin(2 * math.pi * f0 * t) + 0.5 * math.sin(4 * math.pi * f0 * t) + 0.25 * math.sin(6 * math.pi * f0 * t))
        words.append(Word(text, start, end))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes(struct.pack(f"<{total}h", *(int(max(-1.0, min(1.0, s)) * 32767) for s in samples)))
    return buf.getvalue(), words


def _sentence(n: int, at: float, pitches: list[float], amps: list[float], end_mark: str = ".") -> list[tuple[str, float, float, float, float]]:
    out = []
    for k in range(n):
        start = at + k * 0.35
        text = f"w{k}" + (end_mark if k == n - 1 else "")
        out.append((text, start, start + 0.25, pitches[k % len(pitches)], amps[k % len(amps)]))
    return out


def test_pitch_of_each_word_is_measured_from_the_audio() -> None:
    wav, words = _wav(_sentence(6, 0.0, [110.0, 165.0, 220.0], [0.3]))
    voices = word_voices(wav, words)
    # высота ищется по целому числу отсчётов периода, поэтому точность — около процента
    assert all(abs(v.f0 - f0) / f0 < 0.02 for v, f0 in zip(voices, [110, 165, 220, 110, 165, 220], strict=True))
    assert all(v.db is not None and -20 < v.db < 0 for v in voices)


def test_flat_pitch_over_many_words_is_monotone_and_a_lively_voice_is_not() -> None:
    flat = _sentence(12, 0.0, [130.0], [0.3]) + _sentence(12, 5.0, [130.0], [0.3])
    wav, words = _wav(flat)
    result = analyze_prosody(wav, words)
    assert result is not None and result.monotone is True and result.pitch_variation < 0.5
    lively = _sentence(12, 0.0, [110.0, 150.0, 200.0], [0.3]) + _sentence(12, 5.0, [120.0, 180.0], [0.3])
    wav, words = _wav(lively)
    result = analyze_prosody(wav, words)
    assert result is not None and result.monotone is False and result.pitch_variation > 3
    # короткий питч: монотонность не оценивается, но разброс известен
    wav, words = _wav(_sentence(8, 0.0, [130.0], [0.3]))
    short = analyze_prosody(wav, words)
    assert short is not None and short.monotone is None and short.pitch_variation is not None


def test_a_phrase_whose_last_word_is_much_quieter_fades() -> None:
    faded = _sentence(6, 0.0, [140.0], [0.3, 0.3, 0.3, 0.3, 0.3, 0.03])  # последнее слово тише на 20 дБ
    even = _sentence(6, 3.0, [140.0], [0.3])
    wav, words = _wav(faded + even)
    voices = word_voices(wav, words)
    assert fades(words, voices) == [(5, fades(words, voices)[0][1])] and fades(words, voices)[0][1] >= 15
    # фраза короче FADE_MIN_WORDS не оценивается, естественное снижение на пару децибел — не затухание
    soft = _sentence(6, 0.0, [140.0], [0.3, 0.3, 0.3, 0.3, 0.3, 0.2])
    wav, words = _wav(soft)
    assert fades(words, word_voices(wav, words)) == []


def test_pitch_variation_needs_enough_voiced_words() -> None:
    from app.ai.prosody import WordVoice

    assert pitch_variation([WordVoice(120.0, -10.0)] * 3) == (None, None)
    assert pitch_variation([WordVoice(None, -10.0)] * 30) == (None, None)
    # ошибка на октаву у одного слова не делает голос «живым»
    voices = [WordVoice(120.0, -10.0)] * 24 + [WordVoice(240.0, -10.0)]
    assert pitch_variation(voices) == (0.0, True)


def test_voice_findings_land_in_events_metrics_and_score() -> None:
    text = "one two three four five six. seven eight nine ten eleven twelve."
    words = [Word(w, i * 0.5, i * 0.5 + 0.4) for i, w in enumerate(text.split())]
    transcript = Transcript(text, words, duration=90)
    plain = analyze(transcript, gaze=[], min_sec=60, max_sec=180)
    voice = Prosody(pitch_variation=0.4, monotone=True, fades=[(5, 12.0), (11, 10.5)])
    result = analyze(transcript, gaze=[], min_sec=60, max_sec=180, prosody=voice)
    energy = [(text[e.start : e.end], e.text) for e in result.events if e.type == "energy"]
    assert energy == [("six.", "The voice fades at the end of the phrase (−12 dB)"), ("twelve.", "The voice fades at the end of the phrase (−10 dB)")]
    assert (result.metrics.monotone, result.metrics.pitch_variation, result.metrics.fades) == (True, 0.4, 2)
    assert result.score.total == plain.score.total - MONOTONE_COST  # два затухания — норма, монотонность — минус
    assert plain.metrics.monotone is None and plain.metrics.fades == 0


def test_broken_audio_leaves_the_review_without_voice() -> None:
    assert analyze_prosody(b"not a wav", [Word("hi", 0.0, 0.5)]) is None
    assert analyze_prosody(b"", []) is None
