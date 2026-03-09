from src.feedback_optimization import (
    apply_feedback_optimization_result,
    begin_feedback_optimization,
    build_feedback_optimization_state,
    freeze_feedback_optimization,
    hydrate_scoring_result_for_feedback,
    mark_feedback_optimization_pending,
    should_apply_feedback_optimization_result,
)
from src.models import PhonemeTag, WordTag


def test_build_feedback_optimization_state_keeps_current_feedback_visible():
    payload = {
        "engine_raw": {
            "integrated_feedback": {
                "overall_comment": "[az] current feedback",
                "provider": "azure_fallback",
            }
        }
    }

    state = build_feedback_optimization_state(payload)

    assert state["status"] == "pending"
    assert state["current_provider"] == "azure_fallback"
    assert state["current_text"] == "[az] current feedback"


def test_build_feedback_optimization_state_marks_non_azure_feedback_final_by_default():
    payload = {
        "engine_raw": {
            "integrated_feedback": {
                "overall_comment": "[db] improved feedback",
                "provider": "volcengine",
            }
        }
    }

    state = build_feedback_optimization_state(payload)

    assert state["status"] == "final"
    assert state["current_provider"] == "volcengine"


def test_freeze_feedback_optimization_marks_current_feedback_final():
    payload = {
        "engine_raw": {
            "integrated_feedback": {
                "overall_comment": "[az] current feedback",
                "provider": "azure_fallback",
            }
        }
    }

    freeze_feedback_optimization(payload, reason="single_export")

    state = payload["feedback_optimization"]
    assert state["status"] == "frozen"
    assert state["freeze_reason"] == "single_export"
    assert state["current_text"] == "[az] current feedback"


def test_should_not_apply_feedback_after_report_is_frozen():
    payload = {
        "feedback_optimization": {
            "status": "frozen",
            "version": 2,
        }
    }

    assert should_apply_feedback_optimization_result(payload, expected_version=2) is False


def test_begin_feedback_optimization_moves_pending_to_optimizing_with_new_version():
    payload = {
        "engine_raw": {
            "integrated_feedback": {
                "overall_comment": "[az] current feedback",
                "provider": "azure_fallback",
            }
        }
    }

    state = begin_feedback_optimization(payload)

    assert state["status"] == "optimizing"
    assert state["version"] == 1
    assert state["current_text"] == "[az] current feedback"


def test_mark_feedback_optimization_pending_keeps_current_feedback_and_error():
    payload = {
        "engine_raw": {
            "integrated_feedback": {
                "overall_comment": "[az] current feedback",
                "provider": "azure_fallback",
            }
        },
        "feedback_optimization": {
            "status": "optimizing",
            "version": 2,
        },
    }

    state = mark_feedback_optimization_pending(payload, error="timeout")

    assert state["status"] == "pending"
    assert state["version"] == 2
    assert state["current_text"] == "[az] current feedback"
    assert state["last_error"] == "timeout"


def test_apply_feedback_optimization_result_promotes_integrated_feedback():
    payload = {
        "feedback": {
            "cn_summary": "[az] old summary",
            "cn_actions": ["old suggestion"],
            "practice": [],
        },
        "engine_raw": {
            "feedback_source_tag": "az",
            "integrated_feedback": {
                "overall_comment": "[az] old summary",
                "specific_suggestions": ["old suggestion"],
                "practice_tips": [],
                "provider": "azure_fallback",
            },
        },
        "feedback_optimization": {
            "status": "optimizing",
            "version": 3,
        },
    }
    advisor_feedback = {
        "_advisor_provider": "volcengine",
        "_advisor_chain": ["volcengine", "gemini"],
        "overall_comment": "[db] praise. issue. action.",
        "specific_feedback": [
            {"target": "winter", "issue": "ending unstable", "suggestion": "Read winter slowly 3 times."}
        ],
        "practice_tips": [],
    }

    apply_feedback_optimization_result(payload, advisor_feedback, expected_version=3)

    assert payload["engine_raw"]["feedback_source_tag"] == "db"
    assert payload["engine_raw"]["integrated_feedback"]["provider"] == "volcengine"
    assert payload["feedback"]["cn_summary"] == "[db] praise. issue. action."
    assert payload["feedback_optimization"]["status"] == "final"


def test_hydrate_scoring_result_for_feedback_uses_existing_report_data():
    payload = {
        "meta": {"student_id": "S01 student", "submission_id": "sub-1"},
        "script_text": "My uncle's family live there.",
        "scores": {
            "overall_100": 84,
            "pronunciation_100": 81,
            "fluency_100": 86,
            "intonation_100": 82,
            "completeness_100": 90,
        },
        "alignment": {
            "words": [
                {"word": "live", "start": 0.3, "end": 0.7, "score": 58, "tag": "weak", "diagnosis": "linked"}
            ],
            "phonemes": [
                {"phoneme": "R", "start": 0.31, "end": 0.4, "score": 61, "tag": "weak", "in_word": "there"}
            ],
        },
        "analysis": {
            "missing_words": ["there"],
            "mistakes": [{"word": "live", "issue": "verb pronunciation"}],
        },
        "engine_raw": {"pause_count": 2, "wpm": 108},
    }

    result = hydrate_scoring_result_for_feedback(payload)

    assert result.meta.student_id == "S01 student"
    assert result.script_text == "My uncle's family live there."
    assert result.scores.overall_100 == 84
    assert result.alignment.words[0].word == "live"
    assert result.alignment.words[0].tag == WordTag.WEAK
    assert result.alignment.phonemes[0].phoneme == "R"
    assert result.alignment.phonemes[0].tag == PhonemeTag.WEAK
    assert result.analysis.missing_words == ["there"]
    assert result.analysis.mistakes == [{"word": "live", "issue": "verb pronunciation"}]
    assert getattr(result.analysis, "fluency") == {"pause_count": 2, "wpm": 108}
