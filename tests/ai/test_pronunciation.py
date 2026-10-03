import array

from app.ai.pronunciation import BYTES_PER_SEC, aggregate, split_on_silence


def _pcm(seconds: float, loud: bool) -> bytes:
    n = int(seconds * BYTES_PER_SEC / 2)
    return array.array("h", [3000 if loud and i % 2 else -3000 if loud else 0 for i in range(n)]).tobytes()


def test_split_cuts_in_silence_and_keeps_short_audio_whole() -> None:
    assert len(split_on_silence(_pcm(10, loud=True), max_parallel=8)) == 1

    pcm = _pcm(16, loud=True) + _pcm(1, loud=False) + _pcm(16, loud=True)
    chunks = split_on_silence(pcm, max_parallel=8)
    assert len(chunks) == 2
    assert 16 <= chunks[1].offset_sec <= 17  # разрез внутри тихой секунды
    assert sum(len(c.pcm) for c in chunks) == len(pcm)


def _word(text: str, offset: int, accuracy: float, error: str = "None", syllables=()) -> dict:
    return {
        "Word": text,
        "Offset": offset,
        "PronunciationAssessment": {"AccuracyScore": accuracy, "ErrorType": error},
        "Syllables": [
            {"Syllable": s, "Grapheme": g, "PronunciationAssessment": {"AccuracyScore": a}} for s, g, a in syllables
        ],
    }


def test_aggregate_weights_scores_and_lists_problem_words() -> None:
    seg = {
        "NBest": [
            {
                "PronunciationAssessment": {
                    "AccuracyScore": 80,
                    "FluencyScore": 90,
                    "ProsodyScore": 70,
                    "PronScore": 80,
                },
                "Words": [
                    _word("we", 0, 95),
                    _word("pharmacy", 5_000_000, 37, "Mispronunciation", [("faar", "phar", 30), ("mah", "ma", 90)]),
                ],
            }
        ]
    }
    result = aggregate([(20.0, [seg])])
    assert result is not None
    assert (result.overall_score, result.prosody_score, result.words_total) == (80, 70, 2)
    issue = result.words[0]
    assert (issue.word, issue.t, issue.error, issue.weak_syllables) == ("pharmacy", 20.5, "mispronunciation", ["phar"])
    assert "pharmacy" in result.tips[0]
