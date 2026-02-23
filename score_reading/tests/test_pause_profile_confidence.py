from src.models import Alignment, WordAlignment
from src.pipeline.normalize import (
    _apply_fluency_guardrails,
    _compute_pause_profile,
    _is_low_confidence_timing,
)


def _build_uniform_gap_alignment(gap: float = 0.31, n_words: int = 12) -> Alignment:
    words = []
    t = 0.0
    dur = 0.4
    for i in range(n_words):
        words.append(WordAlignment(word=f"w{i}", start=round(t, 3), end=round(t + dur, 3), score=90))
        t += dur + gap
    return Alignment(words=words, phonemes=[])


def test_uniform_gap_timeline_is_low_confidence():
    alignment = _build_uniform_gap_alignment(gap=0.31, n_words=12)
    profile = _compute_pause_profile(alignment)
    assert profile is not None
    assert float(profile.get("synthetic_timeline", 0.0)) >= 0.5
    assert float(profile.get("low_confidence_timing", 0.0)) >= 0.5
    assert _is_low_confidence_timing(profile) is True


def test_guardrails_keep_existing_low_confidence_pause_profile_flags():
    alignment = _build_uniform_gap_alignment(gap=0.31, n_words=12)
    engine_raw = {
        "pause_profile": {
            "timing_confidence": "low",
            "low_confidence_timing": 1.0,
            "practice_focus_words": ["today", "school", "vacation"],
            "practice_focus_points": [
                {"left_word": "today", "right_word": "is", "pause_type": "medium", "boundary_score": 68.0, "idx": 2},
                {"left_word": "school", "right_word": "where", "pause_type": "strong", "boundary_score": 65.0, "idx": 7},
            ],
            "expected_pause_targets": 24.0,
        }
    }

    _apply_fluency_guardrails(fluency=80.0, alignment=alignment, engine_raw=engine_raw)
    profile = engine_raw.get("pause_profile", {})
    assert str(profile.get("timing_confidence", "")).lower() == "low"
    assert float(profile.get("low_confidence_timing", 0.0)) >= 0.5
    assert profile.get("practice_focus_words") == ["today", "school", "vacation"]
    points = profile.get("practice_focus_points")
    assert isinstance(points, list) and len(points) == 2
    assert points[0]["left_word"] == "today"
    assert points[0]["right_word"] == "is"
    assert points[0]["pause_type"] == "medium"
    assert points[0]["idx"] == 2
    assert float(profile.get("expected_pause_targets", 0.0)) == 24.0


def test_pause_profile_calibrates_wpm_when_timeline_duration_is_compressed():
    words = []
    t = 0.0
    for i in range(10):
        words.append(WordAlignment(word=f"w{i}", start=round(t, 3), end=round(t + 0.25, 3), score=90))
        t += 0.30
    alignment = Alignment(words=words, phonemes=[])

    raw_profile = _compute_pause_profile(alignment)
    cal_profile = _compute_pause_profile(alignment, audio_duration_sec=9.0)

    assert raw_profile is not None and cal_profile is not None
    assert float(raw_profile.get("wpm", 0.0)) > 160.0
    assert float(cal_profile.get("wpm", 0.0)) < 110.0
    assert float(cal_profile.get("duration_calibrated", 0.0)) >= 0.5
