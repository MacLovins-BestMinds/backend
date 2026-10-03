from app.ai.delivery_metrics import analyze, fillers_score, find_fillers, gaze_on_ratio, pace_score, timing_score
from app.ai.schemas import GazePoint
from app.ai.stt import Transcript, Word


def _words(*texts: str, step: float = 0.4) -> list[Word]:
    return [Word(t, i * step, i * step + 0.3) for i, t in enumerate(texts)]


def test_scores_follow_spec_points() -> None:
    assert (fillers_score(1), fillers_score(5), fillers_score(12)) == (100, 40, 0)
    assert pace_score(140) == 100 and pace_score(80) == 0
    assert timing_score(120, 60, 180) == 100 and timing_score(30, 60, 180) == 50


def test_fillers_include_bigrams_and_hesitations() -> None:
    found = find_fillers(_words("Ну,", "это", "как", "бы", "ээээ", "продукт", "Вот."))
    assert [w for _, w in found] == ["ну", "как бы", "ээээ", "вот"]


def test_gaze_ratio_uses_change_points() -> None:
    gaze = [GazePoint(t=0, on=True), GazePoint(t=6, on=False), GazePoint(t=8, on=True)]
    assert gaze_on_ratio(gaze, 10) == 0.8


def test_analyze_detects_long_pause_and_total() -> None:
    words = _words("Представьте", "бабушку") + [Word("утром", 5.0, 5.3)]
    result = analyze(Transcript("…", words, duration=90), gaze=[], min_sec=60, max_sec=180)
    assert result.metrics.long_pauses == 1
    assert [e.type for e in result.events] == ["long_pause"]
    assert 0 <= result.score.total <= 100
