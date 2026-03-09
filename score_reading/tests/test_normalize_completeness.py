import math

from src.models import Alignment, AudioMetrics, WordAlignment, WordTag
from src.pipeline.normalize import normalize_scores


def test_completeness_uses_stable_missing_indices_after_analysis():
    script_text = "one two three four five six seven eight nine ten"
    alignment = Alignment(
        words=[
            WordAlignment(word="one", start=0.0, end=0.1, tag=WordTag.OK, score=95),
            WordAlignment(word="two", start=0.1, end=0.2, tag=WordTag.OK, score=95),
            WordAlignment(word="three", start=0.2, end=0.3, tag=WordTag.OK, score=95),
            WordAlignment(word="four", start=0.3, end=0.4, tag=WordTag.OK, score=95),
            WordAlignment(word="five", start=0.4, end=0.5, tag=WordTag.OK, score=95),
            WordAlignment(word="six", start=0.5, end=0.6, tag=WordTag.OK, score=95),
            WordAlignment(word="seven", start=0.6, end=0.7, tag=WordTag.OK, score=95),
            WordAlignment(word="eight", start=0.7, end=0.8, tag=WordTag.OK, score=95),
            WordAlignment(word="nine", start=0.8, end=0.9, tag=WordTag.OK, score=95),
            WordAlignment(word="ten", start=0.9, end=1.0, tag=WordTag.OK, score=95),
        ],
        phonemes=[],
    )
    audio = AudioMetrics(duration_sec=1.0, silence_ratio=0.0, rms_db=-20.0)
    engine_raw = {
        "source": "Azure",
        "annotation_source": "gemini",
        "completeness_score": 98.0,
        "accuracy_score": 95.0,
        "fluency_score": 95.0,
        "prosody_score": 95.0,
        "pronunciation_score": 95.0,
        "intonation_score": 95.0,
        "stable_missing_indices": [9],
    }

    scores = normalize_scores(
        engine_raw=engine_raw,
        audio_metrics=audio,
        alignment=alignment,
        script_text=script_text,
    )

    assert scores.completeness_100 == 90.0
    assert engine_raw["completeness_adjusted_for_missing"]["missing_count"] == 1
    assert engine_raw["completeness_adjusted_for_missing"]["rule"] == "missing_ratio_consistent"
