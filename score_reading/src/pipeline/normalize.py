"""
口语评分 CLI 框架 - 分数归一化模块

负责将各引擎的原始分数映射到统一的 0-100 分制。
"""
import logging
import re
from typing import Any

from src.config import config
from src.models import (
    Alignment,
    AudioMetrics,
    PhonemeTag,
    Scores,
    WordTag,
)

logger = logging.getLogger(__name__)


def _clamp_0_100(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except Exception:
        v = default
    return max(0.0, min(100.0, v))


def _is_azure_source(engine_raw: dict[str, Any]) -> bool:
    source = str((engine_raw or {}).get("source", "")).lower()
    profile = str((engine_raw or {}).get("scoring_profile", "")).lower()
    return ("azure" in source) or ("azure" in profile)


def _extract_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _count_stable_missing_words(
    script_text: str,
    alignment: Alignment,
    engine_raw: dict[str, Any],
) -> tuple[int, int, str]:
    script_tokens = re.findall(r"[A-Za-z']+", str(script_text or ""))
    total = len(script_tokens)
    if total <= 0:
        return 0, 0, "none"

    raw_indices = (engine_raw or {}).get("stable_missing_indices")
    if isinstance(raw_indices, list):
        stable: set[int] = set()
        for raw in raw_indices:
            try:
                idx = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < total:
                stable.add(idx)
        return len(stable), total, "stable_indices"

    missing_by_tag = 0
    for idx, word in enumerate(alignment.words):
        if idx >= total:
            break
        if word.tag == WordTag.MISSING:
            missing_by_tag += 1
    return missing_by_tag, total, "alignment_tags"


def _timeline_duration_for_wpm(
    timed_words: list[Any],
    audio_duration_sec: float = 0.0,
) -> tuple[float, float, float]:
    """
    Return (duration_used, raw_duration, timeline/audio ratio).
    """
    if not timed_words:
        return 0.2, 0.2, 0.0
    start_t = float(timed_words[0].start)
    end_t = float(timed_words[-1].end)
    raw_duration = max(0.2, end_t - start_t)
    audio_dur = max(0.0, float(audio_duration_sec or 0.0))
    ratio = (raw_duration / audio_dur) if audio_dur > 0 else 0.0
    if audio_dur >= 1.5 and (ratio < 0.65 or ratio > 1.45):
        return max(0.2, audio_dur), raw_duration, ratio
    return raw_duration, raw_duration, ratio


def _compute_pause_profile(
    alignment: Alignment,
    audio_duration_sec: float = 0.0,
) -> dict[str, float] | None:
    timed_words = [
        w for w in alignment.words
        if float(w.end or 0) > float(w.start or 0)
    ]
    if len(timed_words) < 3:
        return None

    duration, raw_duration, timeline_audio_ratio = _timeline_duration_for_wpm(
        timed_words,
        audio_duration_sec=audio_duration_sec,
    )
    duration_calibrated = 1.0 if abs(duration - raw_duration) > 1e-6 else 0.0
    wpm = (len(timed_words) / duration) * 60.0

    pause_count = 0
    total_pause = 0.0
    medium = 0
    long = 0
    extreme = 0
    all_gaps: list[float] = []
    for i in range(len(timed_words) - 1):
        gap = max(0.0, float(timed_words[i + 1].start) - float(timed_words[i].end))
        all_gaps.append(gap)
        if gap < 0.28:
            continue
        pause_count += 1
        total_pause += gap
        if gap >= 1.8:
            extreme += 1
        elif gap >= 0.9:
            long += 1
        else:
            medium += 1

    pause_ratio = total_pause / duration
    pause_per_min = pause_count / max(duration / 60.0, 1e-6)
    median_gap = 0.0
    gap_std = 0.0
    fixed_gap_ratio = 0.0
    synthetic_timeline = 0.0
    low_confidence_timing = 0.0
    if all_gaps:
        sorted_gaps = sorted(all_gaps)
        median_gap = float(sorted_gaps[len(sorted_gaps) // 2])
        around_median = [g for g in all_gaps if abs(g - median_gap) <= 0.02]
        fixed_gap_ratio = len(around_median) / max(1, len(all_gaps))
        mean_gap = sum(all_gaps) / len(all_gaps)
        gap_std = (sum((g - mean_gap) ** 2 for g in all_gaps) / max(1, len(all_gaps))) ** 0.5
        if len(all_gaps) >= 6:
            if 0.07 <= median_gap <= 0.16 and fixed_gap_ratio >= 0.65:
                synthetic_timeline = 1.0
                low_confidence_timing = 1.0
            elif median_gap <= 0.20 and gap_std <= 0.02 and fixed_gap_ratio >= 0.55:
                synthetic_timeline = 1.0
                low_confidence_timing = 1.0
            elif median_gap <= 0.45 and gap_std <= 0.012 and fixed_gap_ratio >= 0.90:
                synthetic_timeline = 1.0
                low_confidence_timing = 1.0
    return {
        "wpm": wpm,
        "pause_count": pause_count,
        "pause_ratio": pause_ratio,
        "pause_per_min": pause_per_min,
        "medium_pause_count": medium,
        "long_pause_count": long,
        "extreme_pause_count": extreme,
        "median_gap": median_gap,
        "gap_std": gap_std,
        "fixed_gap_ratio": fixed_gap_ratio,
        "synthetic_timeline": synthetic_timeline,
        "low_confidence_timing": low_confidence_timing,
        "timeline_duration": raw_duration,
        "duration_used": duration,
        "duration_calibrated": duration_calibrated,
        "timeline_audio_ratio": timeline_audio_ratio,
    }


def _is_low_confidence_timing(profile: dict[str, float] | None) -> bool:
    if not profile:
        return False
    if float(profile.get("synthetic_timeline", 0.0)) >= 0.5:
        return True
    if float(profile.get("low_confidence_timing", 0.0)) >= 0.5:
        return True
    median_gap = float(profile.get("median_gap", 0.0))
    gap_std = float(profile.get("gap_std", 0.0))
    fixed_gap_ratio = float(profile.get("fixed_gap_ratio", 0.0))
    if 0.07 <= median_gap <= 0.16 and fixed_gap_ratio >= 0.65:
        return True
    if median_gap <= 0.20 and gap_std <= 0.02 and fixed_gap_ratio >= 0.55:
        return True
    if median_gap <= 0.45 and gap_std <= 0.012 and fixed_gap_ratio >= 0.90:
        return True
    return False


def _collect_script_words_and_punctuation(script_text: str) -> tuple[list[str], list[str]]:
    tokens = re.findall(r"[A-Za-z']+|[.,!?;:]", script_text or "")
    words: list[str] = []
    punct_after: list[str] = []
    for tok in tokens:
        if re.fullmatch(r"[A-Za-z']+", tok):
            words.append(tok.lower())
            punct_after.append("")
        elif punct_after:
            punct_after[-1] += tok
    return words, punct_after


def _estimate_pausing_score(
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any] | None = None,
) -> dict[str, float] | None:
    timed_words = [
        w for w in alignment.words
        if float(w.end or 0) > float(w.start or 0)
    ]
    if len(timed_words) < 3:
        return None
    audio_duration = float(_extract_number((engine_raw or {}).get("audio_duration_sec")) or 0.0)
    timing_profile = _compute_pause_profile(alignment, audio_duration_sec=audio_duration)
    low_confidence_timing = _is_low_confidence_timing(timing_profile)

    script_words, script_punct = _collect_script_words_and_punctuation(script_text)
    transitions = max(1, len(timed_words) - 1)
    expected_targets = 0
    good = 0
    bad = 0
    missed = 0
    optional = 0
    long_bad = 0
    strong_target_count = 0
    strong_zero_gap_count = 0

    # Prefer analyzed pause labels if available to keep scoring consistent with UI.
    has_pause_marks = any(getattr(w, "pause", None) for w in timed_words[:-1])
    if has_pause_marks:
        for w in timed_words[:-1]:
            pause = getattr(w, "pause", None)
            if not pause:
                continue
            p_type = str(getattr(pause, "type", "") or "").lower()
            p_dur = float(getattr(pause, "duration", 0.0) or 0.0)
            p_expected = str(getattr(pause, "expected_type", "") or "").lower()
            if p_expected == "strong":
                strong_target_count += 1
                if p_dur <= 0.03:
                    strong_zero_gap_count += 1
            if p_type == "good":
                good += 1
                expected_targets += 1
            elif p_type == "missed":
                if low_confidence_timing:
                    optional += 1
                else:
                    missed += 1
                    expected_targets += 1
            elif p_type == "bad":
                bad += 1
                if p_dur >= 0.90:
                    long_bad += 1
            elif p_type == "optional":
                optional += 1
        expected_targets = max(expected_targets, good + missed)
        # Extra safeguard: if most strong boundaries have near-zero gaps,
        # the timestamp grid is likely too coarse for reliable pause scoring.
        if strong_target_count >= 6:
            strong_zero_ratio = strong_zero_gap_count / max(1, strong_target_count)
            if strong_zero_ratio >= 0.70:
                low_confidence_timing = True
    else:
        for i in range(len(timed_words) - 1):
            left = timed_words[i]
            right = timed_words[i + 1]
            gap = max(0.0, float(right.start) - float(left.end))
            marks = script_punct[i] if i < len(script_punct) else ""

            expected = "none"
            if any(ch in marks for ch in ".!?"):
                expected = "strong"
                expected_targets += 1
            elif any(ch in marks for ch in ",;:"):
                expected = "medium"
                expected_targets += 1

            if expected != "none":
                min_gap = 0.42 if expected == "strong" else 0.24
                if gap >= min_gap:
                    good += 1
                elif gap <= max(0.08, min_gap * 0.6):
                    if low_confidence_timing:
                        optional += 1
                    else:
                        missed += 1
                else:
                    optional += 1
            else:
                if gap >= 0.65:
                    bad += 1
                    if gap >= 0.90:
                        long_bad += 1
                elif gap >= 0.30:
                    optional += 1

    missing_words = sum(1 for w in alignment.words if w.tag == WordTag.MISSING)
    bad_rate = bad / transitions
    miss_rate = missed / max(1, expected_targets)
    good_rate = good / max(1, expected_targets)
    optional_rate = optional / transitions
    long_rate = long_bad / transitions
    missing_rate = missing_words / max(1, len(script_words) if script_words else len(timed_words))
    pause_per_min = float(timing_profile.get("pause_per_min", 0.0))
    pause_ratio = float(timing_profile.get("pause_ratio", 0.0))

    # Piecewise-friendly scoring:
    # - light penalty for small error counts
    # - stronger penalty only when errors accumulate
    score = 92.0
    score -= min(38.0, bad * 3.4 + long_bad * 1.8)

    miss_scale = 1.0
    if expected_targets >= 8:
        miss_scale = 1.2
    elif expected_targets <= 2:
        miss_scale = 0.7
    # Keep light tolerance for a few too-short misses, then penalize sharply.
    if missed <= 3:
        miss_penalty = missed * 1.1 * miss_scale
    else:
        miss_penalty = 3.0 * 1.1 * miss_scale + (missed - 3.0) * 3.0 * miss_scale
    score -= min(24.0, miss_penalty)

    score -= min(12.0, missing_words * 1.2)
    score += min(10.0, good * 0.8)
    # Optional pauses are not hard errors, but dense optional labels indicate broken rhythm.
    score -= min(18.0, max(0.0, optional - 12.0) * 0.35)
    if not low_confidence_timing:
        score -= min(10.0, max(0.0, pause_per_min - 14.0) * 0.9)
        score -= min(9.0, max(0.0, pause_ratio - 0.20) * 45.0)

    # ratio-based penalties for dense pausing errors in long texts
    if transitions >= 20:
        score -= max(0.0, bad_rate - 0.10) * 70.0
        miss_density_weight = 32.0
        if bad == 0 and long_bad == 0:
            # If there are almost no long/over pauses, treat dense too-short as a
            # moderate rhythm issue rather than a severe collapse.
            miss_density_weight = 14.0
        score -= max(0.0, miss_rate - 0.18) * miss_density_weight
        score -= max(0.0, long_rate - 0.06) * 50.0
        if low_confidence_timing:
            score -= max(0.0, optional_rate - 0.55) * 18.0
        else:
            score -= max(0.0, optional_rate - 0.30) * 45.0

    # Do not over-credit pausing when timing confidence is low.
    if low_confidence_timing:
        score = min(score, 78.0)
        if bad == 0 and missed == 0:
            # Tiered cap for low-confidence timelines:
            # allow strong readers with only light optional pauses to score higher,
            # but keep dense optional pauses conservative.
            if optional_rate <= 0.25 and pause_per_min <= 10.0 and pause_ratio <= 0.16:
                score = min(score, 78.0)
            elif optional_rate <= 0.40 and pause_per_min <= 16.0 and pause_ratio <= 0.22:
                score = min(score, 74.0)
            else:
                score = min(score, 70.0)
        if optional_rate >= 0.45 or pause_per_min >= 20.0 or pause_ratio >= 0.26:
            score = min(score, 62.0)
        if optional_rate >= 0.65 or pause_per_min >= 30.0 or pause_ratio >= 0.34:
            score = min(score, 55.0)

    score = max(0.0, min(100.0, score))

    # Hard caps to prevent disfluent samples getting unrealistically high Pausing.
    if bad >= 20:
        score = min(score, 58.0)
    elif bad >= 12:
        score = min(score, 68.0)
    if missed >= 8:
        score = min(score, 65.0)
    if optional >= 40:
        score = min(score, 62.0)
    if optional >= 80:
        score = min(score, 55.0)

    # Pausing should not greatly exceed hesitation quality for the same sample.
    hesitation_proxy = _hesitation_score_from_profile(timing_profile)
    if hesitation_proxy < 85.0:
        score = min(score, hesitation_proxy + 14.0)
    if hesitation_proxy < 65.0:
        score = min(score, hesitation_proxy + 9.0)
    if hesitation_proxy < 50.0:
        score = min(score, hesitation_proxy + 6.0)
    if low_confidence_timing and bad == 0 and missed == 0:
        score = max(score, 40.0)

    # Floors for clearly stable pausing behavior.
    if not low_confidence_timing:
        total_err = bad + missed
        if bad == 0 and long_bad == 0 and missed <= 3 and optional_rate <= 0.35:
            score = max(score, 68.0)
            if missed <= 2 and optional <= 8:
                score = max(score, 74.0)
        if bad == 0 and long_bad == 0 and missed <= 8 and optional_rate <= 0.35:
            score = max(score, 60.0)
        if total_err <= 1 and long_bad == 0:
            score = max(score, 84.0)
        elif total_err <= 2 and long_bad <= 1:
            score = max(score, 78.0)
        elif bad <= 2 and missed <= 1 and good_rate >= 0.60:
            score = max(score, 80.0)

    return {
        "pausing_score": score,
        "bad_pauses": float(bad),
        "missed_pauses": float(missed),
        "good_pauses": float(good),
        "optional_pauses": float(optional),
        "long_bad_pauses": float(long_bad),
        "expected_pause_targets": float(expected_targets),
        "bad_rate": bad_rate,
        "miss_rate": miss_rate,
        "good_rate": good_rate,
        "optional_rate": optional_rate,
        "long_rate": long_rate,
        "missing_rate": missing_rate,
        "pause_per_min": pause_per_min,
        "pause_ratio": pause_ratio,
        "low_confidence_timing": 1.0 if low_confidence_timing else 0.0,
    }


def _pace_score_from_wpm(wpm: float) -> float:
    if 95.0 <= wpm <= 155.0:
        return 92.0
    if 80.0 <= wpm < 95.0:
        return 80.0 + (wpm - 80.0) * 0.8
    if 155.0 < wpm <= 180.0:
        return 92.0 - (wpm - 155.0) * 1.0
    if 65.0 <= wpm < 80.0:
        return 66.0 + (wpm - 65.0) * 0.9
    return 52.0


def _hesitation_score_from_profile(profile: dict[str, float]) -> float:
    pause_per_min = float(profile.get("pause_per_min", 0.0))
    pause_ratio = float(profile.get("pause_ratio", 0.0))
    long_cnt = float(profile.get("long_pause_count", 0.0))
    extreme_cnt = float(profile.get("extreme_pause_count", 0.0))

    score = 100.0
    score -= min(35.0, max(0.0, pause_per_min - 8.0) * 1.6)
    score -= min(30.0, max(0.0, pause_ratio - 0.12) * 95.0)
    score -= min(25.0, long_cnt * 3.8 + extreme_cnt * 7.2)
    return max(0.0, min(100.0, score))


def _compose_fluency_from_components(
    *,
    base_fluency: float,
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any],
) -> float:
    audio_duration = float(_extract_number((engine_raw or {}).get("audio_duration_sec")) or 0.0)
    profile = _compute_pause_profile(alignment, audio_duration_sec=audio_duration)
    pausing = _estimate_pausing_score(alignment, script_text, engine_raw)
    if not profile or not pausing:
        return max(0.0, min(100.0, base_fluency))

    wpm = float(profile.get("wpm", 0.0))
    pace_score = _pace_score_from_wpm(wpm)
    hesitation_score = _hesitation_score_from_profile(profile)
    pausing_score = float(pausing.get("pausing_score", base_fluency))

    composed = (
        0.72 * pausing_score
        + 0.18 * pace_score
        + 0.10 * hesitation_score
    )
    # Keep only a tiny anchor to upstream engine fluency to reduce inversion risk.
    final = 0.95 * composed + 0.05 * float(base_fluency)

    # Monotonic cap: fluency cannot drift far above pausing quality.
    if pausing_score < 80.0:
        final = min(final, pausing_score + 8.0)
    if pausing_score < 65.0:
        final = min(final, pausing_score + 5.0)
    if pausing_score < 55.0:
        final = min(final, pausing_score + 3.0)
    final = max(0.0, min(100.0, final))

    engine_raw["fluency_components"] = {
        "pausing_score": round(pausing_score, 2),
        "pace_score": round(pace_score, 2),
        "hesitation_score": round(hesitation_score, 2),
        "base_fluency": round(float(base_fluency), 2),
        "composed_fluency": round(final, 2),
        **{k: round(float(v), 4) for k, v in pausing.items()},
    }
    return final


def _apply_fluency_guardrails(
    *,
    fluency: float,
    alignment: Alignment,
    engine_raw: dict[str, Any],
) -> float:
    """
    Keep fluency post-processing transparent and monotonic.

    We intentionally avoid hidden hard caps here so the final fluency score
    stays consistent with the visible Pausing/Pace/Hesitation components.
    """
    audio_duration = float(_extract_number((engine_raw or {}).get("audio_duration_sec")) or 0.0)
    profile = _compute_pause_profile(alignment, audio_duration_sec=audio_duration)
    if not profile:
        return max(0.0, min(100.0, fluency))

    existing_profile = (engine_raw or {}).get("pause_profile")
    if isinstance(existing_profile, dict):
        profile["synthetic_timeline"] = max(
            float(profile.get("synthetic_timeline", 0.0)),
            float(existing_profile.get("synthetic_timeline", 0.0)),
        )
        profile["low_confidence_timing"] = max(
            float(profile.get("low_confidence_timing", 0.0)),
            float(existing_profile.get("low_confidence_timing", 0.0)),
            1.0 if str(existing_profile.get("timing_confidence", "")).lower() == "low" else 0.0,
        )
        if "expected_pause_targets" in existing_profile:
            profile["expected_pause_targets"] = float(existing_profile.get("expected_pause_targets", 0.0) or 0.0)
        if isinstance(existing_profile.get("practice_focus_words"), list):
            profile["practice_focus_words"] = [
                str(w).strip() for w in existing_profile.get("practice_focus_words", []) if str(w).strip()
            ][:3]
        if isinstance(existing_profile.get("practice_focus_points"), list):
            points: list[dict[str, Any]] = []
            for row in existing_profile.get("practice_focus_points", []):
                if not isinstance(row, dict):
                    continue
                left_word = str(row.get("left_word", "")).strip()
                right_word = str(row.get("right_word", "")).strip()
                pause_type = str(row.get("pause_type", "")).strip().lower()
                if pause_type not in {"strong", "medium", "light", "none"}:
                    pause_type = "medium"
                issue = str(row.get("issue", "")).strip().lower()
                if issue not in {"too_long", "too_short"}:
                    issue = ""
                target_min = max(0.0, float(_extract_number(row.get("target_min")) or 0.0))
                target_max = max(target_min, float(_extract_number(row.get("target_max")) or target_min))
                actual_gap = max(0.0, float(_extract_number(row.get("actual_gap")) or 0.0))
                adjust_sec = max(0.0, float(_extract_number(row.get("adjust_sec")) or 0.0))
                if not left_word:
                    continue
                points.append(
                    {
                        "left_word": left_word,
                        "right_word": right_word,
                        "pause_type": pause_type,
                        "boundary_score": _clamp_0_100(row.get("boundary_score", 100.0), 100.0),
                        "idx": int(_extract_number(row.get("idx")) or -1),
                        "issue": issue,
                        "target_min": round(target_min, 3),
                        "target_max": round(target_max, 3),
                        "actual_gap": round(actual_gap, 3),
                        "adjust_sec": round(adjust_sec, 3),
                    }
                )
                if len(points) >= 3:
                    break
            if points:
                profile["practice_focus_points"] = points

    # Expose profile for downstream diagnostics/debug, but do not alter score.
    if _is_low_confidence_timing(profile):
        profile["timing_confidence"] = "low"
    else:
        profile["timing_confidence"] = "high"
    engine_raw["pause_profile"] = profile
    return max(0.0, min(100.0, float(fluency)))


def _calibrate_fluency_by_script_reference(
    *,
    base_fluency: float,
    alignment: Alignment,
    engine_raw: dict[str, Any],
) -> float:
    script_ref = ((engine_raw or {}).get("script_reference") or {})
    if not isinstance(script_ref, dict):
        return max(0.0, min(100.0, base_fluency))

    pace_norm = script_ref.get("pace_norm") or {}
    if not isinstance(pace_norm, dict) or not pace_norm:
        return max(0.0, min(100.0, base_fluency))

    timed_words = [w for w in alignment.words if float(w.end or 0) > float(w.start or 0)]
    if len(timed_words) < 4:
        return max(0.0, min(100.0, base_fluency))

    audio_duration = float(_extract_number((engine_raw or {}).get("audio_duration_sec")) or 0.0)
    duration, raw_duration, timeline_audio_ratio = _timeline_duration_for_wpm(
        timed_words,
        audio_duration_sec=audio_duration,
    )
    if duration <= 0.2:
        return max(0.0, min(100.0, base_fluency))
    observed_wpm = (len(timed_words) / duration) * 60.0

    target = _extract_number(pace_norm.get("target_wpm"))
    low = _extract_number(pace_norm.get("warn_below"))
    high = _extract_number(pace_norm.get("warn_above"))
    if target is None:
        target = 110.0
    if low is None:
        low = max(50.0, target - 30.0)
    if high is None:
        high = target + 30.0

    adjusted = float(base_fluency)
    if observed_wpm < low:
        adjusted -= min(14.0, (low - observed_wpm) * 0.45)
    elif observed_wpm > high:
        adjusted -= min(14.0, (observed_wpm - high) * 0.35)
    else:
        adjusted += 1.5

    # Optional pause-rule calibration: penalize systemic missed/over pauses.
    pause_rules = script_ref.get("pause_rules") or []
    if isinstance(pause_rules, list) and pause_rules:
        expected_by_word: dict[str, list[str]] = {}
        for row in pause_rules:
            if not isinstance(row, dict):
                continue
            key = re.sub(r"[^a-z']+", "", str(row.get("after_word", "")).lower())
            p_type = str(row.get("pause_type", "")).strip().lower()
            if not key:
                continue
            if p_type not in {"strong", "medium", "light", "none"}:
                p_type = "medium"
            expected_by_word.setdefault(key, []).append(p_type)

        min_gap = {"strong": 0.42, "medium": 0.24, "light": 0.12, "none": 0.0}
        expected_hits = 0
        violations = 0
        consumed: dict[str, int] = {}
        for i in range(len(timed_words) - 1):
            left = timed_words[i]
            right = timed_words[i + 1]
            key = re.sub(r"[^a-z']+", "", str(left.word or "").lower())
            if not key or key not in expected_by_word:
                continue
            idx = consumed.get(key, 0)
            variants = expected_by_word[key]
            if idx >= len(variants):
                continue
            exp = variants[idx]
            consumed[key] = idx + 1
            gap = max(0.0, float(right.start) - float(left.end))
            expected_hits += 1
            if exp == "none":
                if gap >= 0.50:
                    violations += 1
            elif gap < min_gap.get(exp, 0.24) * 0.6:
                violations += 1

        if expected_hits >= 3:
            ratio = violations / expected_hits
            adjusted -= min(10.0, ratio * 12.0)

    adjusted = max(0.0, min(100.0, adjusted))
    logger.info(
        "Fluency calibrated by script reference: base=%.1f observed_wpm=%.1f range=[%.1f, %.1f] final=%.1f raw_dur=%.2f used_dur=%.2f ratio=%.3f",
        base_fluency,
        observed_wpm,
        low,
        high,
        adjusted,
        raw_duration,
        duration,
        timeline_audio_ratio,
    )
    return adjusted


def normalize_gop_score(raw_score: float) -> float:
    """
    归一化 GOP 分数
    
    采用 Sigmoid (S型曲线) 函数进行非线性归一化，以提高不同水平学生的区分度。
    
    Args:
        raw_score: 原始 GOP 分数（通常为负数）
        
    Returns:
        0-100 分制分数
    """
    mode = config.get("normalization.gop.mode", "linear")
    
    if mode == "sigmoid":
        import math
        # Sigmoid 公式: 100 / (1 + exp(-k * (raw_score - center)))
        k = config.get("normalization.gop.sigmoid.k", 1.5)
        center = config.get("normalization.gop.sigmoid.center", -4.0)
        
        # GOP 越接近 0 越好，所以 raw_score - center 正值代表优秀
        score = 100 / (1 + math.exp(-k * (raw_score - center)))
        return max(0, min(100, score))
    else:
        # 传统线性映射 - 中性平衡
        # 范围扩大到 -10，使极差的分数也不至于掉到 10 分左右
        gop_min = config.get("normalization.gop.min", -10.0)
        gop_max = config.get("normalization.gop.max", -1.0)
        clamped = max(gop_min, min(gop_max, raw_score))
        normalized = (clamped - gop_min) / (gop_max - gop_min) * 100
        return max(0, min(100, normalized))


def calculate_fluency_score(
    audio_metrics: AudioMetrics,
    alignment: Alignment,
) -> float:
    """
    计算流利度分数
    
    基于语速(WPM)和异常停顿计算流利度。
    
    语速评分标准（小学生朗读）：
    - 80-120 WPM: 优秀（90-100分）
    - 60-80 或 120-150 WPM: 良好（70-90分）
    - 40-60 或 150-180 WPM: 一般（50-70分）
    - <40 或 >180 WPM: 较差（<50分）
    
    Args:
        audio_metrics: 音频质量指标
        alignment: 对齐信息
        
    Returns:
        0-100 分制流利度分数
    """
    # 计算实际语速（WPM = Words Per Minute）
    word_count = len([w for w in alignment.words if w.end > 0])  # 只计有时间戳的词
    duration_sec = audio_metrics.duration_sec
    
    if duration_sec <= 0 or word_count == 0:
        logger.warning("无法计算语速：时长或词数为 0")
        return 50.0  # 返回中等分数
    
    wpm = (word_count / duration_sec) * 60
    logger.info(f"语速计算: {word_count} 词 / {duration_sec:.1f}s = {wpm:.1f} WPM")
    
    # 语速评分（核心指标）
    # NOTE: 小学生朗读最佳语速约 80-120 WPM
    if 80 <= wpm <= 120:
        wpm_score = 98  # 最佳区间，给予高分
    elif 60 <= wpm <= 150:
        # 增加衰减斜率，让低于 80 WPM 的分数下降更快
        if wpm < 80:
            # 60 WPM 从原来的 70 调低到 60
            wpm_score = 60 + (wpm - 60) / 20 * 30
        else:
            wpm_score = 98 - (wpm - 120) / 30 * 28
    elif 40 <= wpm <= 180:
        if wpm < 60:
            # 40 WPM 降到 30
            wpm_score = 30 + (wpm - 40) / 20 * 30
        else:
            wpm_score = 60 - (wpm - 150) / 30 * 20
    else:
        wpm_score = 25  # 极端语速，大幅降分
    
    # 停顿分析（辅助指标）
    # 从配置读取惩罚权重
    pause_weight = config.get("normalization.fluency.pause_penalty_weight", 20)
    pause_penalty = 0
    if alignment.words and len(alignment.words) > 1:
        # 降低长停顿阈值（从 2.0 降到 1.2）
        long_pause_threshold = 1.2
        very_long_pause_threshold = 3.0
        
        for i in range(1, len(alignment.words)):
            prev_word = alignment.words[i - 1]
            curr_word = alignment.words[i]
            
            if prev_word.end > 0 and curr_word.start > 0:
                gap = curr_word.start - prev_word.end
                
                if gap > very_long_pause_threshold:
                    pause_penalty += 8  # 增加惩罚力度
                elif gap > long_pause_threshold:
                    pause_penalty += 3
        
        pause_penalty = min(30, pause_penalty) # 上限 30 分
    
    # 综合分数
    score = wpm_score - pause_penalty
    
    logger.info(f"流利度计算: WPM分={wpm_score:.1f}, 停顿惩罚={pause_penalty}, 最终={score:.1f}")
    
    return max(0, min(100, score))


def calculate_intonation_score(audio_metrics: AudioMetrics, pitch_contour: list = None) -> float:
    """
    计算语调分数（稳定、可复现）
    """
    import numpy as np
    
    base_score = 88.0
    
    # 1. 维度：音高起伏度 (Pitch Variation)
    if pitch_contour and len(pitch_contour) > 10:
        # 获取 F0 数组
        # 注意：engine_raw 中 pitch_contour 里的 key 是 'f' (Standard) 或 'f' (Whisper)
        f_list = [p.get("f", p.get("f0", 0)) for p in pitch_contour]
        pitches = [f for f in f_list if f > 50]
        
        if len(pitches) > 10:
            # 计算变异系数 (CV)
            cv = (np.std(pitches) / np.mean(pitches)) * 100
            if cv < 12: # 太单调
                base_score -= 15
            elif cv < 18:
                base_score -= 6
            elif 18 <= cv <= 35: # 很丰富
                base_score += 6
            elif cv > 50:
                base_score -= 6
    
    # 2. 维度：能量质量 (RMS)
    if audio_metrics.rms_db < -25:
        base_score -= 18
    elif audio_metrics.rms_db < -18:
        base_score -= 8
    
    # 3. 维度：静音率惩罚
    if audio_metrics.silence_ratio > 0.5:
        base_score -= 22
    elif audio_metrics.silence_ratio > 0.3:
        base_score -= 10
    
    return max(0, min(100, base_score))


def calculate_completeness_score(
    script_text: str,
    alignment: Alignment,
) -> float:
    """
    计算完整度分数
    """
    # 使用正则解析标准文本中的词，确保与识别引擎分词逻辑一致
    import re
    script_words = [
        word.lower()
        for word in re.findall(r"[a-zA-Z']+", script_text)
    ]
    
    # 获取识别到的词（同样使用正则清理，以防万一）
    recognized_words = [
        w.word.lower() for w in alignment.words if w.tag != WordTag.MISSING
    ]
    
    if not script_words:
        return 100.0
    
    # 使用简单匹配计数，允许重复词
    from collections import Counter
    s_counter = Counter(script_words)
    r_counter = Counter(recognized_words)
    
    matches = 0
    for w, count in s_counter.items():
        matches += min(count, r_counter.get(w, 0))
    
    score = (matches / len(script_words)) * 100
    
    return max(0, min(100, score))


def calculate_overall_score(scores: Scores) -> float:
    """
    计算综合分数 - 提高区分度
    """
    # 权重配置 (调整：提高发音权重，降低完整度偏移)
    weights = {
        "pronunciation": 0.55,   # 0.4 -> 0.55
        "fluency": 0.25,        # 保持
        "intonation": 0.15,      # 保持
        "completeness": 0.05,    # 0.2 -> 0.05 (降低完整度带来的底分效应)
    }
    
    raw_overall = (
        scores.pronunciation_100 * weights["pronunciation"]
        + scores.fluency_100 * weights["fluency"]
        + scores.intonation_100 * weights["intonation"]
        + scores.completeness_100 * weights["completeness"]
    )
    
    # 移除过度严苛的非线性惩罚 (Remove math.pow 1.25)
    # 使 80 分就是真实的 80 分，不再被强行降至 70+
    return round(raw_overall, 1)


def normalize_scores(
    engine_raw: dict[str, Any],
    audio_metrics: AudioMetrics,
    alignment: Alignment,
    script_text: str,
) -> Scores:
    """
    归一化所有分数
    
    将引擎原始分数和分析结果转换为统一的 0-100 分制。
    
    Args:
        engine_raw: 引擎原始输出
        audio_metrics: 音频质量指标
        alignment: 对齐信息
        script_text: 标准文本
        
    Returns:
        归一化后的分数
    """
    logger.info("开始分数归一化")
    azure_native = _is_azure_source(engine_raw)
    if azure_native:
        logger.info("Using Azure-native normalization profile.")
    
    if azure_native:
        # Azure path: trust Azure sub-scores as canonical signals.
        pronunciation = _clamp_0_100(
            engine_raw.get("accuracy_score", engine_raw.get("pronunciation_score", 0.0))
        )

        fluency_num = _extract_number(engine_raw.get("fluency_score"))
        fluency = (
            _clamp_0_100(fluency_num)
            if fluency_num is not None
            else calculate_fluency_score(audio_metrics, alignment)
        )

        prosody_num = _extract_number(engine_raw.get("prosody_score", engine_raw.get("intonation_score")))
        if prosody_num is not None and prosody_num > 0:
            intonation = _clamp_0_100(prosody_num)
        else:
            pitch_contour = engine_raw.get("pitch_contour", [])
            intonation = calculate_intonation_score(audio_metrics, pitch_contour)

        completeness_num = _extract_number(engine_raw.get("completeness_score"))
        completeness = (
            _clamp_0_100(completeness_num)
            if completeness_num is not None
            else calculate_completeness_score(script_text, alignment)
        )
        engine_raw["fluency_components"] = {
            "source": "azure_native",
            "pausing_score": round(fluency, 2),
            "pace_score": round(fluency, 2),
            "hesitation_score": round(fluency, 2),
            "base_fluency": round(fluency, 2),
            "composed_fluency": round(fluency, 2),
        }
    else:
        # 发音分数
        if "pronunciation_score" in engine_raw:
            pronunciation = engine_raw["pronunciation_score"]
            logger.info(f"使用引擎直接提供的发音分: {pronunciation}")
        else:
            gop_mean = engine_raw.get("gop_mean", -5.0)
            pronunciation = normalize_gop_score(gop_mean)
        
        # 流利度分数
        if "fluency_score" in engine_raw:
            fluency = engine_raw["fluency_score"]
            logger.info(f"使用引擎直接提供的流利分: {fluency}")
        else:
            fluency = calculate_fluency_score(audio_metrics, alignment)
        fluency = _calibrate_fluency_by_script_reference(
            base_fluency=float(fluency),
            alignment=alignment,
            engine_raw=engine_raw,
        )
        fluency = _compose_fluency_from_components(
            base_fluency=float(fluency),
            alignment=alignment,
            script_text=script_text,
            engine_raw=engine_raw,
        )
        fluency = _apply_fluency_guardrails(
            fluency=float(fluency),
            alignment=alignment,
            engine_raw=engine_raw,
        )
        
        # 语调分数
        if "intonation_score" in engine_raw:
            intonation = engine_raw["intonation_score"]
            logger.info(f"使用引擎直接提供的语调分: {intonation}")
        else:
            pitch_contour = engine_raw.get("pitch_contour", [])
            intonation = calculate_intonation_score(audio_metrics, pitch_contour)
        
        # 完整度分数
        if "completeness_score" in engine_raw:
            completeness = engine_raw["completeness_score"]
            logger.info(f"使用引擎直接提供的完整分: {completeness}")
        else:
            completeness = calculate_completeness_score(script_text, alignment)

    missing_count, script_total, missing_source = _count_stable_missing_words(
        script_text=script_text,
        alignment=alignment,
        engine_raw=engine_raw,
    )
    if script_total > 0:
        current = _clamp_0_100(completeness)
        raw_source = str((engine_raw or {}).get("source", "")).lower()
        annotation_source = str((engine_raw or {}).get("annotation_source", "")).lower()
        allow_force_100 = (
            "gemini" in raw_source
            or ("azure" in raw_source and annotation_source == "gemini")
        )
        if missing_count <= 0:
            if allow_force_100 and current < 99.9:
                completeness = 100.0
                engine_raw["completeness_adjusted_for_missing"] = {
                    "from": round(current, 2),
                    "to": completeness,
                    "missing_count": 0,
                    "script_word_count": int(script_total),
                    "missing_source": missing_source,
                    "rule": "no_missing_force_100",
                }
                logger.info(
                    "Completeness forced to 100.0 because no missing words were detected (source=%s).",
                    missing_source,
                )
        else:
            max_consistent = max(0.0, 100.0 * (1.0 - missing_count / script_total))
            if allow_force_100:
                # Gemini-involved path: completeness should reflect missing-word count directly.
                # This avoids mismatches like "2 missing words but completeness still ~85".
                if abs(current - max_consistent) >= 0.6:
                    completeness = round(max_consistent, 1)
                    engine_raw["completeness_adjusted_for_missing"] = {
                        "from": round(current, 2),
                        "to": completeness,
                        "missing_count": int(missing_count),
                        "script_word_count": int(script_total),
                        "missing_source": missing_source,
                        "rule": "missing_ratio_consistent",
                    }
                    logger.info(
                        "Completeness normalized to missing-ratio consistency: %.1f -> %.1f (missing=%s/%s, source=%s).",
                        current,
                        completeness,
                        missing_count,
                        script_total,
                        missing_source,
                    )
            elif current > max_consistent + 0.6:
                completeness = round(max_consistent, 1)
                engine_raw["completeness_adjusted_for_missing"] = {
                    "from": round(current, 2),
                    "to": completeness,
                    "missing_count": int(missing_count),
                    "script_word_count": int(script_total),
                    "missing_source": missing_source,
                }
                logger.info(
                    "Completeness adjusted for missing-word consistency: %.1f -> %.1f (missing=%s/%s, source=%s).",
                    current,
                    completeness,
                    missing_count,
                    script_total,
                    missing_source,
                )

    scores = Scores(
        pronunciation_100=pronunciation,
        fluency_100=fluency,
        intonation_100=intonation,
        completeness_100=completeness,
    )
    
    # 统一口径：所有引擎都使用同一套四维加权公式计算综合分。
    # Azure 原生 overall_score 仅作为参考值保留，不覆盖最终综合分。
    azure_overall_ref = _extract_number(engine_raw.get("overall_score"))
    if azure_native and azure_overall_ref is not None:
        engine_raw["azure_overall_reference"] = round(_clamp_0_100(azure_overall_ref), 2)
        logger.info(
            "Azure overall_score kept as reference: %s (final overall still uses unified weighted formula)",
            engine_raw["azure_overall_reference"],
        )
    scores.overall_100 = calculate_overall_score(scores)
    
    logger.info(
        f"分数归一化完成: 综合={scores.overall_100}, "
        f"发音={pronunciation:.1f}, 流利={fluency:.1f}, "
        f"语调={intonation:.1f}, 完整={completeness:.1f}"
    )
    
    return scores


def assign_word_tags(alignment: Alignment) -> None:
    """
    为对齐结果中的词分配标签
    
    根据分数阈值分配 ok/weak/poor 标签。
    
    Args:
        alignment: 对齐信息（会被原地修改）
    """
    # 全面放宽：OK 阈值由 75 降至 60，WEAK 阈值由 45 降至 35
    ok_threshold = config.get("analysis.word_thresholds.ok", 60)
    weak_threshold = config.get("analysis.word_thresholds.weak", 35)
    
    for word in alignment.words:
        if word.tag == WordTag.MISSING:
            continue
        elif word.score >= ok_threshold:
            word.tag = WordTag.OK
        elif word.score >= weak_threshold:
            word.tag = WordTag.WEAK
        else:
            word.tag = WordTag.POOR


def assign_phoneme_tags(alignment: Alignment) -> None:
    """
    为对齐结果中的音素分配标签
    
    Args:
        alignment: 对齐信息（会被原地修改）
    """
    ok_threshold = config.get("analysis.phoneme_thresholds.ok", 70)
    
    for phoneme in alignment.phonemes:
        if phoneme.score >= ok_threshold:
            phoneme.tag = PhonemeTag.OK
        else:
            phoneme.tag = PhonemeTag.WEAK
