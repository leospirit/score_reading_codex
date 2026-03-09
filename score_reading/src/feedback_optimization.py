from __future__ import annotations

import time
from typing import Any

from src.models import (
    Alignment,
    Analysis,
    Meta,
    PhonemeAlignment,
    PhonemeTag,
    Scores,
    ScoringResult,
    WordAlignment,
    WordTag,
)


def _to_word_tag(raw: Any) -> WordTag:
    tag = str(raw or "ok").strip().lower()
    if tag == "weak":
        return WordTag.WEAK
    if tag == "poor":
        return WordTag.POOR
    if tag == "missing":
        return WordTag.MISSING
    return WordTag.OK


def _to_phoneme_tag(raw: Any) -> PhonemeTag:
    tag = str(raw or "ok").strip().lower()
    if tag == "poor":
        return PhonemeTag.POOR
    if tag == "weak":
        return PhonemeTag.WEAK
    return PhonemeTag.OK


def hydrate_scoring_result_for_feedback(payload: dict[str, Any]) -> ScoringResult:
    payload = payload if isinstance(payload, dict) else {}
    meta_raw = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    scores_raw = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    alignment_raw = payload.get("alignment") if isinstance(payload.get("alignment"), dict) else {}
    analysis_raw = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    engine_raw = payload.get("engine_raw") if isinstance(payload.get("engine_raw"), dict) else {}

    result = ScoringResult(
        meta=Meta(
            task_id=str(meta_raw.get("task_id", "") or ""),
            student_id=str(meta_raw.get("student_id", "") or ""),
            student_name=str(meta_raw.get("student_name", "") or ""),
            submission_id=str(meta_raw.get("submission_id", "") or ""),
            timestamp=str(meta_raw.get("timestamp", "") or ""),
            engine_used=str(meta_raw.get("engine_used", "") or ""),
            fallback_chain=list(meta_raw.get("fallback_chain") or []),
            processing_time_ms=int(meta_raw.get("processing_time_ms", 0) or 0),
            is_auto_transcribed=bool(meta_raw.get("is_auto_transcribed", False)),
        ),
        script_text=str(payload.get("script_text", "") or ""),
        scores=Scores(
            overall_100=float(scores_raw.get("overall_100", 0.0) or 0.0),
            pronunciation_100=float(scores_raw.get("pronunciation_100", 0.0) or 0.0),
            fluency_100=float(scores_raw.get("fluency_100", 0.0) or 0.0),
            intonation_100=float(scores_raw.get("intonation_100", 0.0) or 0.0),
            completeness_100=float(scores_raw.get("completeness_100", 0.0) or 0.0),
        ),
        engine_raw=dict(engine_raw),
        analysis=Analysis(
            weak_words=list(analysis_raw.get("weak_words") or []),
            weak_phonemes=list(analysis_raw.get("weak_phonemes") or []),
            missing_words=list(analysis_raw.get("missing_words") or []),
            missing_indices=list(analysis_raw.get("missing_indices") or []),
            mistakes=list(analysis_raw.get("mistakes") or []),
        ),
    )

    words: list[WordAlignment] = []
    for item in list(alignment_raw.get("words") or []):
        if not isinstance(item, dict):
            continue
        words.append(
            WordAlignment(
                word=str(item.get("word", "") or ""),
                start=float(item.get("start", 0.0) or 0.0),
                end=float(item.get("end", 0.0) or 0.0),
                tag=_to_word_tag(item.get("tag")),
                score=float(item.get("score", 0.0) or 0.0),
                diagnosis=str(item.get("diagnosis", "") or ""),
            )
        )

    phonemes: list[PhonemeAlignment] = []
    for item in list(alignment_raw.get("phonemes") or []):
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("phoneme", "") or "").strip()
        if not symbol:
            continue
        phonemes.append(
            PhonemeAlignment(
                phoneme=symbol,
                start=float(item.get("start", 0.0) or 0.0),
                end=float(item.get("end", 0.0) or 0.0),
                tag=_to_phoneme_tag(item.get("tag")),
                score=float(item.get("score", 0.0) or 0.0),
                in_word=str(item.get("in_word", "") or ""),
            )
        )
    result.alignment = Alignment(words=words, phonemes=phonemes)

    fluency_data: dict[str, Any] = {}
    pause_count = engine_raw.get("pause_count")
    wpm = engine_raw.get("wpm")
    if pause_count is not None:
        fluency_data["pause_count"] = pause_count
    if wpm is not None:
        fluency_data["wpm"] = wpm
    setattr(result.analysis, "fluency", fluency_data)
    return result


def _current_feedback_text(payload: dict[str, Any]) -> str:
    override = payload.get("feedback_override") if isinstance(payload, dict) else {}
    if isinstance(override, dict):
        override_text = str(override.get("integrated_feedback_text", "") or "").strip()
        if override_text:
            return override_text
    engine_raw = payload.get("engine_raw") if isinstance(payload, dict) else {}
    integrated = engine_raw.get("integrated_feedback") if isinstance(engine_raw, dict) else {}
    if isinstance(integrated, dict):
        return str(integrated.get("overall_comment", "") or "").strip()
    return ""


def build_feedback_optimization_state(payload: dict[str, Any]) -> dict[str, Any]:
    current = payload.get("feedback_optimization")
    if isinstance(current, dict):
        state = dict(current)
    else:
        state = {}

    engine_raw = payload.get("engine_raw") if isinstance(payload, dict) else {}
    integrated = engine_raw.get("integrated_feedback") if isinstance(engine_raw, dict) else {}
    current_provider = str((integrated or {}).get("provider", "") or "azure_fallback").strip() or "azure_fallback"
    current_text = _current_feedback_text(payload)
    status = str(state.get("status", "") or "").strip().lower()
    if status not in {"pending", "optimizing", "frozen", "final"}:
        status = "final" if current_provider != "azure_fallback" and current_text else "pending"

    normalized = {
        "status": status,
        "version": int(state.get("version", 0) or 0),
        "current_provider": current_provider,
        "current_text": current_text,
        "updated_at": int(state.get("updated_at", 0) or 0),
        "last_error": str(state.get("last_error", "") or "").strip(),
        "freeze_reason": str(state.get("freeze_reason", "") or "").strip(),
    }
    payload["feedback_optimization"] = normalized
    return normalized


def freeze_feedback_optimization(payload: dict[str, Any], *, reason: str) -> dict[str, Any]:
    state = build_feedback_optimization_state(payload)
    state["status"] = "frozen"
    state["freeze_reason"] = str(reason or "").strip() or "manual"
    state["updated_at"] = int(time.time())
    state["current_text"] = _current_feedback_text(payload)
    payload["feedback_optimization"] = state
    return state


def begin_feedback_optimization(payload: dict[str, Any]) -> dict[str, Any]:
    state = build_feedback_optimization_state(payload)
    if state["status"] in {"frozen", "final"}:
        return state
    state["status"] = "optimizing"
    state["version"] = int(state.get("version", 0) or 0) + 1
    state["updated_at"] = int(time.time())
    state["last_error"] = ""
    payload["feedback_optimization"] = state
    return state


def mark_feedback_optimization_pending(payload: dict[str, Any], *, error: str = "") -> dict[str, Any]:
    state = build_feedback_optimization_state(payload)
    if state["status"] in {"frozen", "final"}:
        return state
    state["status"] = "pending"
    state["updated_at"] = int(time.time())
    state["last_error"] = str(error or "").strip()
    state["current_text"] = _current_feedback_text(payload)
    payload["feedback_optimization"] = state
    return state


def should_apply_feedback_optimization_result(payload: dict[str, Any], *, expected_version: int) -> bool:
    state = build_feedback_optimization_state(payload)
    if state["status"] in {"frozen"}:
        return False
    current_version = int(state.get("version", 0) or 0)
    return current_version == int(expected_version)


def _provider_tag(provider: str) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in {"volcengine", "ark", "doubao"}:
        return "db"
    if normalized in {"gemini", "zhipu", "llm_primary", "zhipu_primary"}:
        return "ge"
    return "az"


def apply_feedback_optimization_result(
    payload: dict[str, Any],
    advisor_feedback: dict[str, Any],
    *,
    expected_version: int,
) -> dict[str, Any]:
    if not should_apply_feedback_optimization_result(payload, expected_version=expected_version):
        return payload

    provider = str(advisor_feedback.get("_advisor_provider") or advisor_feedback.get("provider") or "azure_fallback").strip() or "azure_fallback"
    overall_comment = str(advisor_feedback.get("overall_comment", "") or "").strip()
    specific_feedback = advisor_feedback.get("specific_feedback")
    suggestions = []
    if isinstance(specific_feedback, list):
        for item in specific_feedback:
            if isinstance(item, dict):
                suggestion = str(item.get("suggestion", "") or "").strip()
                if suggestion:
                    suggestions.append(suggestion)
    practice_tips = advisor_feedback.get("practice_tips") if isinstance(advisor_feedback.get("practice_tips"), list) else []

    payload["advisor_feedback"] = dict(advisor_feedback)
    payload.setdefault("feedback", {})
    payload["feedback"]["cn_summary"] = overall_comment
    payload["feedback"]["cn_actions"] = suggestions[:1]
    payload["feedback"]["practice"] = [str(item or "").strip() for item in practice_tips if str(item or "").strip()]

    payload.setdefault("engine_raw", {})
    payload["engine_raw"]["feedback_source_tag"] = _provider_tag(provider)
    payload["engine_raw"]["integrated_feedback"] = {
        "overall_comment": overall_comment,
        "specific_suggestions": suggestions[:1],
        "practice_tips": [str(item or "").strip() for item in practice_tips if str(item or "").strip()],
        "provider": provider,
        "provider_chain": advisor_feedback.get("_advisor_chain") if isinstance(advisor_feedback.get("_advisor_chain"), list) else [],
    }

    state = build_feedback_optimization_state(payload)
    state["status"] = "final"
    state["current_provider"] = provider
    state["current_text"] = _current_feedback_text(payload)
    state["updated_at"] = int(time.time())
    state["last_error"] = ""
    payload["feedback_optimization"] = state
    return payload
