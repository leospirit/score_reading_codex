from src.models import Alignment, PauseInfo, WordAlignment
from src.pipeline.analyze import calculate_pace_trend, detect_pauses, repair_alignment_timeline


def test_synthetic_timeline_does_not_flood_optional_without_expected_boundaries():
    # Nearly fixed inter-word gaps to simulate low-confidence synthetic timeline.
    words = [
        WordAlignment(word="one", start=0.00, end=0.40, score=90),
        WordAlignment(word="two", start=0.70, end=1.10, score=90),
        WordAlignment(word="three", start=1.40, end=1.80, score=90),
        WordAlignment(word="four", start=2.10, end=2.50, score=90),
        WordAlignment(word="five", start=2.80, end=3.20, score=90),
        WordAlignment(word="six", start=3.50, end=3.90, score=90),
        WordAlignment(word="seven", start=4.20, end=4.60, score=90),
    ]
    alignment = Alignment(words=words, phonemes=[])
    engine_raw = {}

    detect_pauses(alignment, "one two three four five six seven", engine_raw)

    optional_count = sum(
        1 for w in alignment.words if w.pause and w.pause.type == "optional"
    )
    assert optional_count == 0
    assert engine_raw.get("pause_profile", {}).get("timing_confidence") == "low"


def test_detect_pauses_clears_stale_pause_labels():
    words = [
        WordAlignment(word="hello", start=0.0, end=0.4, score=90, pause=PauseInfo(type="optional", duration=0.3)),
        WordAlignment(word="world", start=0.41, end=0.8, score=90, pause=PauseInfo(type="optional", duration=0.3)),
    ]
    alignment = Alignment(words=words, phonemes=[])

    detect_pauses(alignment, "hello world", {})

    assert alignment.words[0].pause is None
    assert alignment.words[1].pause is None


def test_detect_pauses_emits_too_long_and_too_short_diagnostics():
    words = [
        WordAlignment(word="hello", start=0.0, end=0.4, score=90),
        WordAlignment(word="world", start=0.40, end=0.80, score=90),
        WordAlignment(word="again", start=1.50, end=1.90, score=90),
    ]
    alignment = Alignment(words=words, phonemes=[])
    engine_raw = {}

    detect_pauses(alignment, "hello. world again", engine_raw)

    first_pause = alignment.words[0].pause
    second_pause = alignment.words[1].pause
    assert first_pause is not None
    assert second_pause is not None

    assert first_pause.type == "missed"
    assert first_pause.issue == "too_short"
    assert first_pause.target_min >= 0.40
    assert first_pause.adjust_sec > 0.30

    assert second_pause.type == "bad"
    assert second_pause.issue == "too_long"
    assert second_pause.target_max <= 0.17
    assert second_pause.adjust_sec > 0.40

    points = engine_raw.get("pause_profile", {}).get("practice_focus_points", [])
    assert any(str(p.get("issue", "")) == "too_short" for p in points)
    assert any(str(p.get("issue", "")) == "too_long" for p in points)


def test_repair_alignment_does_not_retime_non_uniform_short_span_timeline():
    words = [
        WordAlignment(word="w1", start=0.00, end=0.09, score=90),
        WordAlignment(word="w2", start=0.35, end=0.44, score=90),
        WordAlignment(word="w3", start=0.72, end=0.81, score=90),
        WordAlignment(word="w4", start=1.35, end=1.44, score=90),
        WordAlignment(word="w5", start=1.65, end=1.74, score=90),
        WordAlignment(word="w6", start=2.40, end=2.49, score=90),
        WordAlignment(word="w7", start=2.65, end=2.74, score=90),
    ]
    alignment = Alignment(words=words, phonemes=[])
    before = [(w.start, w.end) for w in alignment.words]

    repair_alignment_timeline(alignment, {"audio_duration_sec": 10.0})

    after = [(w.start, w.end) for w in alignment.words]
    assert after == before


def test_pace_trend_uses_audio_duration_when_timeline_span_is_obviously_too_short():
    words = []
    t = 0.0
    for i in range(10):
        words.append(WordAlignment(word=f"w{i}", start=round(t, 3), end=round(t + 0.25, 3), score=90))
        t += 0.30
    alignment = Alignment(words=words, phonemes=[])

    raw_points = calculate_pace_trend(alignment)
    cal_points = calculate_pace_trend(alignment, audio_duration_sec=9.0)

    assert raw_points and cal_points
    raw_avg = sum(p.y for p in raw_points) / len(raw_points)
    cal_avg = sum(p.y for p in cal_points) / len(cal_points)

    assert raw_avg > 160.0
    assert cal_avg < 110.0
