from src.report.render_html import _build_reading_words_from_script


def test_reading_words_does_not_leak_stale_alignment_missing_tag():
    script_text = "You too I'm going to bring back"
    alignment_words = [
        {"word": "you", "tag": "ok", "score": 97.0, "start": 0.0, "end": 0.1},
        {"word": "too", "tag": "ok", "score": 97.0, "start": 0.1, "end": 0.2},
        {"word": "i'm", "tag": "missing", "score": 0.0, "start": 0.2, "end": 0.3, "diagnosis": "Transcript evidence: omitted word."},
        {"word": "bring", "tag": "ok", "score": 97.0, "start": 0.3, "end": 0.4},
        {"word": "back", "tag": "ok", "score": 100.0, "start": 0.4, "end": 0.5},
    ]

    reading_words = _build_reading_words_from_script(
        script_text=script_text,
        alignment_words=alignment_words,
        missing_indices=[3, 4],
        stable_missing_indices=[3, 4],
    )

    assert reading_words[2]["word"] == "I'm"
    assert reading_words[2]["tag"] != "missing"
    assert reading_words[3]["tag"] == "missing"
    assert reading_words[4]["tag"] == "missing"
