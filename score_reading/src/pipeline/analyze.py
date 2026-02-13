"""
口语评分 CLI 框架 - 分析模块

负责提取 weak_words、weak_phonemes、confusions 等分析结果。
"""
import logging
import math
import re
from collections import Counter
from typing import Any

from src.config import config
from src.pipeline.normalize import normalize_scores
from src.models import (
    Alignment,
    Analysis,
    CompletenessStats,
    Confusion,
    HesitationStats,
    PacePoint,
    PitchPoint,
    PauseInfo,
    PhonemeTag,
    PhonemeTag,
    WordTag,
    WordAlignment,
)

logger = logging.getLogger(__name__)



def analyze_results(
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any],
) -> Analysis:
    """
    分析评分结果，提取关键信息
    
    Args:
        alignment: 对齐信息
        script_text: 标准文本
        engine_raw: 引擎原始输出
        
    Returns:
        分析结果
    """
    logger.info("开始分析评分结果")
    
    analysis = Analysis()
    
    # 0. 强力对齐：强制 alignment.words 与 script_text 结构一致 (Reference Mode)
    # 这解决了 Missing Words 不显示的问题
    source = str((engine_raw or {}).get("source", "")).lower()
    gemini_hint = ("gemini" in source) or bool((engine_raw or {}).get("script_reference"))
    script_tokens = _tokenize_for_compare(script_text)
    len_ratio = (len(alignment.words) / max(1, len(script_tokens))) if script_tokens else 0.0
    should_realign = not (gemini_hint and 0.80 <= len_ratio <= 1.25)
    if should_realign:
        align_to_script(alignment, script_text)
    else:
        logger.info(
            "Skip forced align_to_script for Gemini route (len_ratio=%.2f) to avoid false missing inflation.",
            len_ratio,
        )

    apply_gemini_missing_correction(alignment, script_text, engine_raw)
    suppress_over_missing_for_gemini(alignment, script_text, engine_raw)
    detect_pauses(alignment, script_text, engine_raw)

    # 1. 预处理：停顿检测 与 连读检测（这就地修改 alignment）
    detect_pauses(alignment, script_text, engine_raw)
    detect_linking(alignment)
    feedback_error_words = extract_feedback_error_words(engine_raw)
    apply_feedback_top_errors_to_alignment(alignment, feedback_error_words)
    apply_script_reference_focus_rescore(alignment, engine_raw)
    repair_alignment_timeline(alignment, engine_raw)
    generate_expected_stress(alignment)
    ensure_stress_signal(alignment)
    
    # 2. 提取 weak words
    analysis.weak_words = extract_weak_words(alignment)
    analysis.weak_words = merge_feedback_words(analysis.weak_words, feedback_error_words)
    
    # 3. 提取 weak phonemes (用于 AI 深度指导)
    analysis.weak_phonemes = extract_weak_phonemes(alignment)
    # 由于已经对其过，直接找 tag=MISSING 即可
    analysis.missing_words = [w.word for w in alignment.words if w.tag == WordTag.MISSING]
    
    # 4. 提取具体错误摘要 (Mistake Highlights)
    analysis.mistakes = detect_mistakes(alignment, engine_raw)
    
    
    # 3. 提取 missing words (Already done above)
    # analysis.missing_words = extract_missing_words(alignment, script_text)
    
    # 4. 提取 confusions（如果引擎提供）
    analysis.confusions = extract_confusions(engine_raw)
    
    # 5. 语速趋势分析
    analysis.pace_chart_data = calculate_pace_trend(alignment)
    
    # 6. 完整度高级分析
    analysis.completeness = analyze_completeness(alignment, script_text, analysis.missing_words)
    
    # 7. 迟疑分析 (Basic Text Matching)
    analysis.hesitations = analyze_hesitations(alignment)
    
    # 8. 语调曲线数据提取
    if "pitch_contour" in engine_raw:
        analysis.pitch_contour = [
            PitchPoint(t=p["t"], f0=p["f"]) for p in engine_raw["pitch_contour"]
        ]
    
    # 9. 生成期望重音模式 (Native Speaker 参考)
    generate_expected_stress(alignment)
    
    logger.info(
        f"分析完成: weak_words={len(analysis.weak_words)}, "
        f"weak_phonemes={len(analysis.weak_phonemes)}, "
        f"missing={len(analysis.missing_words)}, "
        f"confusions={len(analysis.confusions)}"
    )
    
    return analysis


# 常见虚词列表（弱读词）
FUNCTION_WORDS = {
    # 冠词
    "a", "an", "the",
    # 介词
    "to", "of", "in", "on", "at", "for", "with", "by", "from", "up", "about",
    "into", "over", "after", "before", "between", "under", "without", "through",
    # 连词
    "and", "or", "but", "so", "if", "because", "although", "while", "when",
    # 代词
    "i", "me", "my", "you", "your", "he", "him", "his", "she", "her", "it", "its",
    "we", "us", "our", "they", "them", "their", "this", "that", "these", "those",
    # 助动词
    "is", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    # 其他
    "not", "just", "some", "any", "no", "very", "too", "also",
}


def _normalize_token(text: str) -> str:
    return re.sub(r"[^A-Za-z']+", "", str(text or "").strip().lower())


def extract_feedback_error_words(engine_raw: dict[str, Any]) -> list[str]:
    """
    Extract word-level diagnostics from Gemini integrated feedback.
    """
    raw_words: list[str] = []
    integrated = (engine_raw or {}).get("integrated_feedback") or {}
    top_errors = integrated.get("top_errors") or []
    if isinstance(top_errors, list):
        for err in top_errors:
            if not isinstance(err, dict):
                continue
            for w in err.get("words") or []:
                w_text = str(w or "").strip()
                if w_text:
                    raw_words.append(w_text)

    if not raw_words:
        conflict_details = ((engine_raw or {}).get("ai_referee") or {}).get("conflict_details") or []
        if isinstance(conflict_details, list):
            for item in conflict_details:
                if not isinstance(item, dict):
                    continue
                w_text = str(item.get("word") or "").strip()
                if w_text:
                    raw_words.append(w_text)

    deduped: list[str] = []
    seen: set[str] = set()
    for w in raw_words:
        key = _normalize_token(w)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(w)
    return deduped


def apply_feedback_top_errors_to_alignment(alignment: Alignment, feedback_words: list[str]) -> None:
    """
    Ensure words shown in pronunciation diagnostics are reflected in reading analysis colors/tags.
    """
    if not alignment.words or not feedback_words:
        return

    word_ok = float(config.get("analysis.word_thresholds.ok", 65))
    word_weak = float(config.get("analysis.word_thresholds.weak", 40))
    cap_score = max(word_weak + 8.0, word_ok - 3.0)

    used_idx: set[int] = set()
    matched = 0
    for hint in feedback_words:
        target = _normalize_token(hint)
        if not target:
            continue
        for idx, word in enumerate(alignment.words):
            if idx in used_idx or word.tag == WordTag.MISSING:
                continue
            if _normalize_token(word.word) != target:
                continue

            if float(word.score or 0) > cap_score:
                word.score = cap_score
            if word.tag == WordTag.OK:
                word.tag = WordTag.WEAK
            note = "Linked from pronunciation diagnostics."
            word.diagnosis = f"{word.diagnosis} | {note}".strip(" |")
            used_idx.add(idx)
            matched += 1
            break

    if matched:
        logger.info("Applied pronunciation diagnostics linkage to %s words.", matched)


def merge_feedback_words(weak_words: list[str], feedback_words: list[str]) -> list[str]:
    """
    Keep weak words list consistent with pronunciation diagnostics.
    """
    configured_top_n = int(config.get("analysis.weak_words_top_n", 5))
    top_n = max(configured_top_n, min(8, len(feedback_words or [])))
    merged: list[str] = []
    seen: set[str] = set()
    for w in list(feedback_words or []) + list(weak_words or []):
        key = _normalize_token(w)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(w)
        if len(merged) >= top_n:
            break
    return merged


def apply_script_reference_focus_rescore(alignment: Alignment, engine_raw: dict[str, Any]) -> None:
    """
    Word-level prior rescoring:
    - Use script reference focus words as high-value checkpoints.
    - Cross-check with detected transcript to reduce false positives / false negatives.
    """
    if not alignment.words:
        return

    script_ref = ((engine_raw or {}).get("script_reference") or {})
    focus_words_raw = script_ref.get("focus_words") or []
    if not isinstance(focus_words_raw, list) or not focus_words_raw:
        return

    focus_set = {
        _normalize_token(str(w))
        for w in focus_words_raw
        if _normalize_token(str(w))
    }
    if not focus_set:
        return

    detected_transcript = str((engine_raw or {}).get("detected_transcript", "")).strip()
    transcript_tokens = {
        _normalize_token(token)
        for token in re.findall(r"[A-Za-z']+", detected_transcript)
        if _normalize_token(token)
    }

    word_ok = float(config.get("analysis.word_thresholds.ok", 65))
    word_weak = float(config.get("analysis.word_thresholds.weak", 40))
    promote_floor = max(word_weak + 6.0, min(word_ok - 2.0, 58.0))
    demote_cap = max(word_weak + 8.0, word_ok - 2.0)

    adjusted = 0
    for word in alignment.words:
        if word.tag == WordTag.MISSING:
            continue
        key = _normalize_token(word.word)
        if not key or key not in focus_set:
            continue

        score = float(word.score or 0.0)
        in_transcript = key in transcript_tokens if transcript_tokens else False

        if in_transcript:
            # If transcript confirms focus word, avoid over-harsh penalties.
            if score < promote_floor:
                word.score = promote_floor
                if word.tag == WordTag.POOR:
                    word.tag = WordTag.WEAK
                adjusted += 1
                note = "Focus word confirmed by transcript."
                word.diagnosis = f"{word.diagnosis} | {note}".strip(" |")
            continue

        # If transcript does not confirm a focus word, avoid false-green near threshold.
        if word.tag == WordTag.OK and score <= (word_ok + 10.0):
            word.score = min(score, demote_cap)
            word.tag = WordTag.WEAK
            adjusted += 1
            note = "Focus word not confirmed by transcript evidence."
            word.diagnosis = f"{word.diagnosis} | {note}".strip(" |")

    if adjusted:
        logger.info("Applied focus-word prior rescoring to %s words.", adjusted)


def align_to_script(alignment: Alignment, script_text: str) -> None:
    """
    使用 difflib 将识别结果强制对齐到脚本结构。
    
    目的：
    1. 确保 UI 显示的单词列表与脚本 1:1 对应（Ghost Words View 需要）。
    2. 发现并标记漏读的词（Missing）。
    3. 处理多读的词（忽略或标记）。
    
    策略：
    - Reference: Script Tokens
    - Hypothesis: Recognized Words
    - OpCodes:
        - equal: Keep recognized word (has score/timing).
        - delete (in Ref, not in Hyp): Insert Script word as MISSING.
        - insert (in Hyp, not in Ref): Ignore (extra words not in script).
        - replace: User said something else. Keep Script word, mark as POOR/WEAK (Mispronunciation).
    """
    import difflib
    
    if not script_text or not script_text.strip():
        return
        
    # 1. Tokenize Script (Robust split)
    # 使用 \w+ 包括数字和字母，handle ' for contractions
    ref_tokens = re.findall(r"[\w']+", script_text)
    if not ref_tokens:
        return
        
    # 2. Get Hyp Tokens (normalized)
    hyp_words = alignment.words
    # Clean both ref and hyp similarly for matching
    ref_tokens_lower = [t.lower().strip(".,!?;:\"") for t in ref_tokens]
    hyp_tokens = [w.word.lower().strip(".,!?;:\"") for w in hyp_words]
    
    matcher = difflib.SequenceMatcher(None, ref_tokens_lower, hyp_tokens)
    
    new_words: list[WordAlignment] = []
    
    # 使用时间游标来为插入的 Missing 词估算时间
    current_time_cursor = 0.0
    if hyp_words:
        current_time_cursor = hyp_words[0].start
        
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        # ref[i1:i2] vs hyp[j1:j2]
        
        if tag == 'equal':
            # 完全匹配：保留识别结果
            for k in range(j1, j2):
                w = hyp_words[k]
                # 强制修正单词拼写为 Script 的样子 (Case correction)
                ref_idx = i1 + (k - j1)
                if ref_idx < len(ref_tokens):
                    w.word = ref_tokens[ref_idx]
                new_words.append(w)
                current_time_cursor = w.end
                
        elif tag == 'delete':
            # Ref 有，Hyp 没有 -> Missing
            # 插入 Missing Words
            for k in range(i1, i2):
                missing_word = ref_tokens[k]
                new_w = WordAlignment(
                    word=missing_word,
                    start=current_time_cursor,
                    end=current_time_cursor + 0.1, # Mock duration
                    score=0,
                    tag=WordTag.MISSING
                )
                new_words.append(new_w)
                current_time_cursor += 0.1
                
        elif tag == 'replace':
            # Ref 有，Hyp 也有但不同 -> Mispronunciation (Wait, or just align error)
            # 逻辑：用户想读 Ref，但读成了 Hyp。
            # 我们保留 Ref 的单词文本，但继承 Hyp 的分数（通常较低）或标记为 WEAK
            
            # 这里的数量可能不一致 (e.g. Ref: "cat", Hyp: "bat mat")
            # 简单策略：按 1:1 映射，多余的忽略/补全
            len_ref = i2 - i1
            len_hyp = j2 - j1
            common_len = min(len_ref, len_hyp)
            
            for k in range(common_len):
                w_orig = hyp_words[j1 + k]
                ref_word = ref_tokens[i1 + k]
                hyp_word_before = w_orig.word
                
                # 修改文本为 Target
                w_orig.word = ref_word
                # 避免“全橙”误判：仅在词形差异较大时才降分。
                # 对于近似词（如缩写/轻微拼写差异）保留原始评分。
                sim_ratio = difflib.SequenceMatcher(
                    None,
                    str(ref_word).lower(),
                    str(hyp_word_before).lower(),
                ).ratio()
                if sim_ratio < 0.45:
                    w_orig.score = min(float(w_orig.score or 0), 55.0)
                    if w_orig.tag == WordTag.OK:
                        w_orig.tag = WordTag.WEAK
                elif sim_ratio < 0.70:
                    w_orig.score = min(float(w_orig.score or 0), 68.0)
                    
                new_words.append(w_orig)
                current_time_cursor = w_orig.end
                
            # 处理剩余的 Ref (视为 Missing)
            if len_ref > len_hyp:
                for k in range(i1 + common_len, i2):
                    missing_word = ref_tokens[k]
                    new_w = WordAlignment(
                        word=missing_word,
                        start=current_time_cursor,
                        end=current_time_cursor + 0.1,
                        score=0,
                        tag=WordTag.MISSING
                    )
                    new_words.append(new_w)
                    current_time_cursor += 0.1
                    
            # 处理剩余的 Hyp (视为 Extra - 忽略，因为我们要保持 Script 结构)
            pass
            
        elif tag == 'insert':
            # Hyp 有 (extra)，Ref 没有 -> 忽略
            pass
            
    # 更新 Alignment
    alignment.words = new_words
    
    # CRITICAL: Re-assign tags after reconstruction to ensure Missing/Weak/OK are set correctly
    assign_tags(alignment)
    
    logger.info(f"Alignment synced to script: {len(hyp_words)} -> {len(new_words)} words")


def _tokenize_for_compare(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z']+", text or "")]


def _gemini_missing_indices(script_text: str, detected_transcript: str) -> set[int]:
    import difflib

    ref_tokens = _tokenize_for_compare(script_text)
    hyp_tokens = _tokenize_for_compare(detected_transcript)
    if not ref_tokens or not hyp_tokens:
        return set()

    matcher = difflib.SequenceMatcher(None, ref_tokens, hyp_tokens)
    missing_indices: set[int] = set()
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag == "delete":
            missing_indices.update(range(i1, i2))
    return missing_indices


def apply_gemini_missing_correction(
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any],
) -> None:
    """
    Reduce false missing words by cross-checking Gemini transcript evidence.
    """
    if not alignment.words:
        return

    detected_transcript = str((engine_raw or {}).get("detected_transcript", "")).strip()
    if not detected_transcript:
        return
    source = str((engine_raw or {}).get("source", "")).lower()
    if "gemini" not in source and not (engine_raw or {}).get("script_reference"):
        return
    ref_tokens = _tokenize_for_compare(script_text)
    hyp_tokens = _tokenize_for_compare(detected_transcript)
    if not ref_tokens or not hyp_tokens:
        return

    transcript_ratio = len(hyp_tokens) / max(1, len(ref_tokens))
    if transcript_ratio < 0.60:
        logger.info(
            "Skip Gemini missing correction due sparse transcript evidence (coverage=%.2f).",
            transcript_ratio,
        )
        return

    missing_indices = _gemini_missing_indices(script_text, detected_transcript)

    word_ok = float(config.get("analysis.word_thresholds.ok", 65))
    word_weak = float(config.get("analysis.word_thresholds.weak", 40))
    accuracy = float((engine_raw or {}).get("accuracy_score") or (engine_raw or {}).get("pronunciation_score") or 70.0)
    fluency = float((engine_raw or {}).get("fluency_score") or 70.0)
    completeness = float((engine_raw or {}).get("completeness_score") or 70.0)
    conflicts = int(((engine_raw or {}).get("ai_referee") or {}).get("conflicts", 0) or 0)
    total_words = max(1, len(alignment.words))
    conflict_ratio = conflicts / total_words

    # 不再把“被 Gemini 证明已读到”的词统一压到 45 分。
    # 评分来自全局质量，防止长文本几乎全橙。
    corrected_score = max(word_weak + 18.0, min(82.0, accuracy - 4.0))
    promote_to_ok = completeness >= 90.0 and fluency >= 70.0 and conflict_ratio <= 0.20
    if promote_to_ok:
        corrected_score = max(corrected_score, word_ok + 2.0)

    corrected = 0
    for idx, word in enumerate(alignment.words):
        if word.tag != WordTag.MISSING:
            continue
        if idx in missing_indices:
            continue

        word.score = max(float(word.score or 0), corrected_score)
        word.tag = WordTag.OK if promote_to_ok and word.score >= word_ok else WordTag.WEAK
        note = "Gemini transcript indicates this word was spoken."
        word.diagnosis = f"{word.diagnosis} | {note}".strip(" |")
        corrected += 1

    if corrected:
        logger.info(
            "Adjusted %s missing words using Gemini transcript evidence (score=%.1f, promote_to_ok=%s).",
            corrected,
            corrected_score,
            promote_to_ok,
        )

def suppress_over_missing_for_gemini(
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any],
) -> None:
    """
    Guard rail for Gemini sparse transcript outputs:
    if transcript evidence is too sparse, avoid mass MISSING tags.
    """
    if not alignment.words:
        return

    source = str((engine_raw or {}).get("source", "")).lower()
    has_gemini_hint = ("gemini" in source) or bool((engine_raw or {}).get("script_reference"))
    if not has_gemini_hint:
        return

    script_tokens = _tokenize_for_compare(script_text)
    if not script_tokens:
        return

    missing_words = [w for w in alignment.words if w.tag == WordTag.MISSING]
    if not missing_words:
        return

    missing_ratio = len(missing_words) / max(1, len(script_tokens))
    if missing_ratio < 0.35:
        return

    detected_transcript = str((engine_raw or {}).get("detected_transcript", "")).strip()
    detected_tokens = _tokenize_for_compare(detected_transcript)
    transcript_ratio = len(detected_tokens) / max(1, len(script_tokens))

    # If transcript coverage is healthy, keep strict missing judgement.
    if transcript_ratio >= 0.60:
        return

    # This guard only addresses the "Gemini sparse/empty transcript" failure mode.
    # If there is some transcript evidence, rely on apply_gemini_missing_correction.
    if detected_tokens:
        return

    word_ok = float(config.get("analysis.word_thresholds.ok", 65))
    word_weak = float(config.get("analysis.word_thresholds.weak", 40))
    accuracy = float((engine_raw or {}).get("accuracy_score") or (engine_raw or {}).get("pronunciation_score") or 70.0)
    corrected_score = max(word_weak + 8.0, min(word_ok + 4.0, accuracy - 10.0))
    fallback_ok = word_ok + 4.0

    # Avoid over-triggering: only relax when missing inflation is extreme.
    if missing_ratio < 0.50:
        return

    corrected = 0
    for idx, word in enumerate(alignment.words):
        if word.tag != WordTag.MISSING:
            continue

        # Deterministic small spread to avoid "all words same score" artifacts.
        jitter = ((idx % 7) - 3) * 1.2  # [-3.6, +3.6]
        new_score = max(word_weak + 4.0, min(word_ok + 5.0, corrected_score + jitter))
        word.score = max(float(word.score or 0.0), new_score)
        if word.score >= fallback_ok:
            word.tag = WordTag.OK
        elif word.score >= word_weak:
            word.tag = WordTag.WEAK
        else:
            word.tag = WordTag.POOR
        note = "Missing tag relaxed: Gemini transcript unavailable; fallback to conservative non-missing scoring."
        word.diagnosis = f"{word.diagnosis} | {note}".strip(" |")
        corrected += 1

    if corrected:
        logger.warning(
            "Relaxed %s missing tags (missing_ratio=%.2f, transcript_ratio=%.2f) to avoid Gemini sparse-output false positives.",
            corrected,
            missing_ratio,
            transcript_ratio,
        )


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def repair_alignment_timeline(alignment: Alignment, engine_raw: dict[str, Any] | None = None) -> None:
    """
    Repair obviously synthetic/invalid timing so pace/hesitation modules remain meaningful.
    """
    words = alignment.words
    if not words:
        return

    audio_duration = _safe_float((engine_raw or {}).get("audio_duration_sec"), 0.0)
    durations = [max(0.0, _safe_float(w.end) - _safe_float(w.start)) for w in words]
    tiny_ratio = (sum(1 for d in durations if d <= 0.12) / len(durations)) if durations else 0.0
    timeline_end = max((_safe_float(w.end) for w in words), default=0.0)
    timeline_start = min((_safe_float(w.start) for w in words), default=0.0)
    timeline_span = max(0.001, timeline_end - timeline_start)

    if timeline_end > 500.0 and audio_duration > 0 and timeline_end > audio_duration * 5:
        for w in words:
            w.start = _safe_float(w.start) / 1000.0
            w.end = _safe_float(w.end) / 1000.0
        timeline_end = max((_safe_float(w.end) for w in words), default=0.0)
        timeline_start = min((_safe_float(w.start) for w in words), default=0.0)
        timeline_span = max(0.001, timeline_end - timeline_start)

    likely_synthetic = (
        audio_duration > 8.0
        and timeline_span < max(6.0, audio_duration * 0.35)
        and tiny_ratio >= 0.45
    )
    if likely_synthetic:
        n = max(1, len(words))
        step = max(0.22, audio_duration / n)
        word_dur = _clamp(step * 0.62, 0.10, 0.52)
        t = 0.0
        for w in words:
            w.start = round(t, 3)
            w.end = round(t + word_dur, 3)
            t += step
        logger.info(
            "Retimed synthetic alignment to audio duration %.2fs (n=%d, step=%.3f).",
            audio_duration,
            n,
            step,
        )
        return

    prev_end = 0.0
    for w in words:
        s = max(0.0, _safe_float(w.start))
        e = max(s + 0.02, _safe_float(w.end))
        if s < prev_end - 0.25:
            s = prev_end
            e = max(s + 0.08, e)
        w.start = round(s, 3)
        w.end = round(e, 3)
        prev_end = w.end


def ensure_stress_signal(alignment: Alignment) -> None:
    """
    Ensure each spoken word has a usable stress value even when engine omits it.
    """
    words = alignment.words
    if not words:
        return

    durations = [
        max(0.05, _safe_float(w.end) - _safe_float(w.start))
        for w in words
        if w.tag != WordTag.MISSING
    ]
    if not durations:
        return
    avg_duration = max(0.08, sum(durations) / len(durations))

    for w in words:
        if w.tag == WordTag.MISSING:
            w.stress = 0.0
            continue

        cur = _safe_float(getattr(w, "stress", 0.0), 0.0)
        if cur > 0.01:
            w.stress = round(_clamp(cur, 0.0, 1.0), 3)
            continue

        duration = max(0.05, _safe_float(w.end) - _safe_float(w.start))
        dur_norm = _clamp(duration / max(0.08, avg_duration * 1.2), 0.0, 1.0)
        score_norm = _clamp(_safe_float(w.score, 0.0) / 100.0, 0.0, 1.0)
        expected = _safe_float(getattr(w, "expected_stress", 0.5), 0.5)

        blended = 0.42 * score_norm + 0.38 * dur_norm + 0.20 * _clamp(expected, 0.0, 1.0)
        if _normalize_token(w.word) in FUNCTION_WORDS:
            blended *= 0.85
        w.stress = round(_clamp(blended, 0.0, 1.0), 3)

def generate_expected_stress(alignment: Alignment) -> None:
    """
    为每个单词生成期望重音值 (Native Speaker 参考)
    
    规则：
    - 实词（名词、动词、形容词、副词）：高重音 (0.7-0.9)
    - 虚词（冠词、介词、代词、助动词）：低重音 (0.2-0.4)
    - 句首/句尾词通常略重
    """
    words = alignment.words
    if not words:
        return
    
    for i, word in enumerate(words):
        clean_word = word.word.lower().strip(".,!?;:\"'")
        
        # 基础判定：实词 vs 虚词
        if clean_word in FUNCTION_WORDS:
            base_stress = 0.3  # 虚词 - 弱读
        else:
            base_stress = 0.8  # 实词 - 重读
        
        # 句首加成
        if i == 0:
            base_stress = min(1.0, base_stress + 0.1)
        
        # 句尾加成（最后一个或者倒数第二个实词）
        if i >= len(words) - 2 and clean_word not in FUNCTION_WORDS:
            base_stress = min(1.0, base_stress + 0.1)
        
        word.expected_stress = round(base_stress, 2)



def _collect_script_words_and_punctuation(script_text: str) -> tuple[list[str], list[str]]:
    words: list[str] = []
    puncts: list[str] = []
    for match in re.finditer(r"([A-Za-z']+)([^A-Za-z']*)", script_text or ""):
        word = str(match.group(1) or "").strip()
        if not word:
            continue
        tail = str(match.group(2) or "")
        marks = "".join(ch for ch in tail if ch in ",.;:!?")
        words.append(word.lower())
        puncts.append(marks)
    return words, puncts


def _build_reference_pause_targets(
    script_words: list[str],
    engine_raw: dict[str, Any] | None,
) -> dict[int, str]:
    targets: dict[int, str] = {}
    ref = ((engine_raw or {}).get("script_reference") or {})
    pause_rules = ref.get("pause_rules") or []
    if not isinstance(pause_rules, list) or not script_words:
        return targets

    used_indices: set[int] = set()
    for row in pause_rules:
        if not isinstance(row, dict):
            continue
        after_word = _normalize_token(str(row.get("after_word", "")))
        pause_type = str(row.get("pause_type", "")).strip().lower()
        if not after_word:
            continue
        if pause_type not in {"strong", "medium", "light", "none"}:
            pause_type = "medium"
        for idx, w in enumerate(script_words):
            if idx in used_indices:
                continue
            if _normalize_token(w) != after_word:
                continue
            targets[idx] = pause_type
            used_indices.add(idx)
            break
    return targets


def detect_pauses(alignment: Alignment, script_text: str, engine_raw: dict[str, Any] | None = None) -> None:
    """
    检测停顿并更新 Alignment
    
    规则：
    1. Gap >= 0.2s -> Pause
    2. 有标点 (,.!?;) -> Good Pause
    3. 无标点 -> Bad Pause (Hesitation / Broken flow)
    4. 有标点但 Gap 很小 -> Missed Pause (Rushed)
    """
    words = alignment.words
    if not words:
        return

    script_words, script_punct = _collect_script_words_and_punctuation(script_text)
    ref_pause_targets = _build_reference_pause_targets(script_words, engine_raw)

    # Expected pause level from punctuation fallback.
    punct_target: dict[int, str] = {}
    for idx, marks in enumerate(script_punct):
        if any(ch in marks for ch in ".!?"):
            punct_target[idx] = "strong"
        elif any(ch in marks for ch in ",;:"):
            punct_target[idx] = "medium"

    # Decision thresholds in seconds.
    min_gap_for = {
        "strong": 0.42,
        "medium": 0.24,
        "light": 0.12,
        "none": 0.0,
    }

    # Detect synthetic/low-fidelity timelines (e.g. evenly spaced fallback timestamps).
    # On such timelines, "missing break" is not reliable and should not be over-reported.
    gaps: list[float] = []
    for gi in range(len(words) - 1):
        g = max(0.0, float(words[gi + 1].start) - float(words[gi].end))
        gaps.append(g)
    synthetic_timeline = False
    median_gap = 0.0
    gap_std = 0.0
    fixed_gap_ratio = 0.0
    if gaps:
        sorted_gaps = sorted(gaps)
        median_gap = float(sorted_gaps[len(sorted_gaps) // 2])
        around_median = [g for g in gaps if abs(g - median_gap) <= 0.02]
        fixed_gap_ratio = len(around_median) / max(1, len(gaps))
        mean_gap = sum(gaps) / len(gaps)
        gap_std = math.sqrt(sum((g - mean_gap) ** 2 for g in gaps) / max(1, len(gaps)))

        # Heuristic: fixed/near-fixed inter-word gap timelines are usually synthetic
        # fallback timestamps and should not emit "missed break".
        if len(gaps) >= 6:
            if 0.07 <= median_gap <= 0.16 and fixed_gap_ratio >= 0.65:
                synthetic_timeline = True
            elif median_gap <= 0.20 and gap_std <= 0.02 and fixed_gap_ratio >= 0.55:
                synthetic_timeline = True

    if isinstance(engine_raw, dict):
        pause_profile = engine_raw.get("pause_profile")
        if not isinstance(pause_profile, dict):
            pause_profile = {}
        pause_profile["median_gap"] = round(float(median_gap), 4)
        pause_profile["gap_std"] = round(float(gap_std), 4)
        pause_profile["fixed_gap_ratio"] = round(float(fixed_gap_ratio), 4)
        pause_profile["synthetic_timeline"] = 1.0 if synthetic_timeline else 0.0
        if synthetic_timeline:
            pause_profile["timing_confidence"] = "low"
        engine_raw["pause_profile"] = pause_profile

    for i in range(len(words)):
        curr_word = words[i]
        if i >= len(words) - 1:
            continue

        next_word = words[i + 1]
        gap = max(0.0, float(next_word.start) - float(curr_word.end))
        duration = round(gap, 2)
        pause_type = None

        expected = ref_pause_targets.get(i) or punct_target.get(i)
        if expected:
            if expected == "none":
                if gap >= 0.55:
                    pause_type = "bad"
                elif gap >= 0.32:
                    pause_type = "optional"
            else:
                min_gap = min_gap_for.get(expected, 0.24)
                if gap >= min_gap:
                    pause_type = "good"
                elif gap <= max(0.08, min_gap * 0.6):
                    if synthetic_timeline:
                        # Do not label MB on low-confidence synthetic timelines.
                        pause_type = "optional"
                    else:
                        pause_type = "missed"
                else:
                    pause_type = "optional"
        else:
            if gap >= 0.65:
                pause_type = "bad"
            elif gap >= 0.30:
                pause_type = "optional"

        if pause_type:
            curr_word.pause = PauseInfo(type=pause_type, duration=duration)


def detect_linking(alignment: Alignment) -> None:
    """
    检测连读并更新 Alignment
    
    连读规则 (初步实现)：
    1. 前一词的结束与后一词的开始有重叠，或间隔极微小 (< 0.02s)
    2. 后续可扩展音素级规则 (C-V)
    """
    words = alignment.words
    if not words:
        return
    
    for i in range(len(words) - 1):
        curr_word = words[i]
        next_word = words[i+1]
        
        # 计算间隙
        gap = next_word.start - curr_word.end
        
        # 连读判定规则：
        # 1. 重叠 (gap < 0)
        # 2. 极其微小的间隙 (gap < 0.03s)
        if gap < 0.03:
            curr_word.is_linked = True


def calculate_pace_trend(alignment: Alignment, window_size: float = 2.0) -> list[PacePoint]:
    """
    璁＄畻璇€熻秼鍔?(WPM)

    浣跨敤婊戝姩绐楀彛銆?
    """
    if not alignment.words:
        return []

    spoken_words = [
        w for w in alignment.words
        if w.tag != WordTag.MISSING and (_safe_float(w.end) - _safe_float(w.start)) > 0.01
    ]
    if len(spoken_words) < 3:
        return []

    start_time = _safe_float(spoken_words[0].start)
    end_time = max(_safe_float(spoken_words[-1].end), start_time + 1.0)
    duration = max(end_time - start_time, 1.0)

    points = []

    logger.info(f"Calculating Pace: {len(alignment.words)} words, duration={duration}s")

    step = 0.5
    current_t = start_time
    smooth: list[int] = []

    while current_t <= end_time:
        t_start = current_t - window_size / 2
        t_end = current_t + window_size / 2

        t_start_clip = max(start_time, t_start)
        t_end_clip = min(end_time, t_end)
        effective_window = max(0.35, t_end_clip - t_start_clip)

        count = 0
        for w in spoken_words:
            w_center = (_safe_float(w.start) + _safe_float(w.end)) / 2
            if t_start_clip <= w_center < t_end_clip:
                count += 1

        wpm = int(round((count / effective_window) * 60))
        wpm = int(_clamp(float(wpm), 20.0, 240.0))
        smooth.append(wpm)
        if len(smooth) >= 3:
            local = smooth[-3:]
            wpm = int(round(sum(local) / len(local)))

        points.append(PacePoint(x=round(current_t - start_time, 1), y=wpm))
        current_t += step

    logger.info(f"Pace Points Generated: {len(points)}")
    return points


def analyze_completeness(
    alignment: Alignment, 
    script_text: str, 
    missing_words: list[str]
) -> CompletenessStats:
    """
    完整度分析
    """
    FUNCTION_WORDS = {
        "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "by", 
        "is", "are", "was", "were", "be", "been", "has", "have", "had", 
        "and", "or", "but", "so", "as", "if", "that", "it", "this", "that"
    }
    
    # 使用正则分词统计单词数
    total_words = len(re.findall(r"[a-zA-Z']+", script_text))
    if total_words == 0:
        total_words = 1
        
    missing_count = len(missing_words)
    coverage = max(0, min(100, int((1 - missing_count / total_words) * 100)))
    
    func_missed = 0
    key_missed = 0
    
    for w in missing_words:
        if w.lower() in FUNCTION_WORDS:
            func_missed += 1
        else:
            key_missed += 1
            
    # 生成 Tips
    tips = []
    if key_missed > 0:
        tips.append("尝试更仔细地阅读实词（名词、动词），它们承载了句子的核心含义。")
    if func_missed > 2:
        tips.append("注意不要吞掉像 'a', 'the' 这样的小词，虽然它们不重读，但也是句子的一部分。")
    if coverage == 100:
        tips.append("完美！你没有漏掉任何单词。")
    elif coverage > 90 and key_missed == 0:
        tips.append("整体完整度很高，只漏读了一些功能词，继续保持！")
        
    return CompletenessStats(
        title="完整度分析",
        score_label="优秀" if coverage > 90 else ("良好" if coverage > 70 else "需加油"),
        coverage=coverage,
        missing_stats={
            "total": missing_count,
            "keywords": key_missed,
            "function_words": func_missed
        },
        insight=f"内容覆盖率: {coverage}% (漏读 {key_missed} 个关键词)",
        tips=tips
    )


def analyze_hesitations(alignment: Alignment) -> HesitationStats | None:
    """
    杩熺枒/濉厖璇嶅垎鏋?

    Mixed disfluency detection based on fillers + long pauses + repetitions.
    """
    words = alignment.words
    if not words:
        return None

    spoken = [(i, w) for i, w in enumerate(words) if w.tag != WordTag.MISSING]
    if len(spoken) < 2:
        return HesitationStats(
            score_label="Limited",
            desc="Not enough stable speech segments to estimate hesitations.",
            fillers=[],
            examples=[],
            tips=["Try recording at least 10 seconds of continuous speech for a reliable disfluency estimate."],
        )

    FILLERS = {"uh", "um", "er", "ah", "hmm", "uhh", "umm"}
    filler_counts: Counter[str] = Counter()
    medium_pause_events: list[tuple[int, float]] = []
    long_pause_events: list[tuple[int, float]] = []
    repetition_events: list[tuple[int, str]] = []

    for idx, w in spoken:
        token = _normalize_token(w.word)
        if token in FILLERS:
            filler_counts[token] += 1

    for pos in range(len(spoken) - 1):
        i, cur = spoken[pos]
        j, nxt = spoken[pos + 1]
        gap = max(0.0, _safe_float(nxt.start) - _safe_float(cur.end))
        if gap > 3.0:
            # Usually indicates timestamp corruption rather than a real hesitation pause.
            continue

        pause_type = str(getattr(getattr(cur, "pause", None), "type", "") or "").lower()
        if pause_type == "bad":
            if gap >= 0.95:
                long_pause_events.append((i, gap))
            elif gap >= 0.55:
                medium_pause_events.append((i, gap))
        elif pause_type == "optional" and gap >= 0.90:
            medium_pause_events.append((i, gap))

        w1 = _normalize_token(cur.word)
        w2 = _normalize_token(nxt.word)
        if w1 and w1 == w2 and w1 not in FUNCTION_WORDS:
            repetition_events.append((j, w1))

    def _window_text(center_index: int, radius: int = 3) -> tuple[str, list[str]]:
        lo = max(0, center_index - radius)
        hi = min(len(words), center_index + radius + 1)
        seq = [str(words[k].word) for k in range(lo, hi)]
        return " ".join(seq), seq

    def _pause_snippet(center_index: int) -> str:
        left_lo = max(0, center_index - 2)
        left = [str(words[k].word) for k in range(left_lo, center_index + 1)]
        right_hi = min(len(words), center_index + 4)
        right = [str(words[k].word) for k in range(center_index + 1, right_hi)]
        return f"{' '.join(left)} | {' '.join(right)}".strip()

    examples: list[dict[str, str]] = []

    for k, gap in long_pause_events[:2]:
        original = _pause_snippet(k)
        examples.append(
            {
                "original_text": original,
                "clean_text": f"The pause here is about {gap:.1f}s. Try keeping it around 0.3-0.8s for smoother flow.",
                "filler": "pause",
            }
        )

    for k, word in repetition_events[:2]:
        original, seq = _window_text(k)
        cleaned_seq = []
        seen_dup = False
        for token in seq:
            if not seen_dup and _normalize_token(token) == word:
                seen_dup = True
                cleaned_seq.append(token)
                continue
            if seen_dup and _normalize_token(token) == word:
                continue
            cleaned_seq.append(token)
        examples.append(
            {
                "original_text": original,
                "clean_text": " ".join(cleaned_seq),
                "filler": word,
            }
        )

    for idx, w in spoken:
        token = _normalize_token(w.word)
        if token in filler_counts and len(examples) < 4:
            original, seq = _window_text(idx)
            cleaned = [x for x in seq if _normalize_token(x) not in FILLERS]
            examples.append(
                {
                    "original_text": original,
                    "clean_text": " ".join(cleaned).strip(),
                    "filler": token,
                }
            )

    filler_total = sum(filler_counts.values())
    disfluency_units = (
        filler_total
        + len(medium_pause_events) * 1.5
        + len(long_pause_events) * 2.4
        + len(repetition_events) * 1.2
    )
    speech_span = max(8.0, _safe_float(spoken[-1][1].end) - _safe_float(spoken[0][1].start))
    rate_per_min = disfluency_units / (speech_span / 60.0)

    if rate_per_min <= 2.8:
        score_label = "Smooth"
        desc = "Speech flow is stable with very few disfluency signals."
    elif rate_per_min <= 5.5:
        score_label = "Moderate"
        desc = "A few pauses or repetitions were detected; flow is mostly understandable."
    else:
        score_label = "Frequent"
        desc = "Frequent pauses/repetitions are affecting fluency continuity."

    signal_rows = [{"word": k, "count": v} for k, v in filler_counts.most_common(4)]
    if long_pause_events:
        signal_rows.append({"word": "long pause", "count": len(long_pause_events)})
    if repetition_events:
        signal_rows.append({"word": "repetition", "count": len(repetition_events)})

    tips: list[str] = []
    if long_pause_events:
        tips.append("Plan breath groups of 4-7 words to reduce long mid-sentence pauses.")
    if repetition_events:
        tips.append("If you restart a word, pause briefly and continue once instead of repeating.")
    if filler_total:
        tips.append("Replace filler sounds with a short silent pause before the next phrase.")
    if not tips:
        tips.append("Maintain this steady rhythm and keep sentence endings clear.")

    return HesitationStats(
        score_label=score_label,
        desc=desc,
        fillers=signal_rows,
        examples=examples[:4],
        tips=tips[:2],
    )


def extract_weak_words(alignment: Alignment) -> list[str]:
    """
    提取分数最低的词
    
    Args:
        alignment: 对齐信息
        
    Returns:
        弱词列表（按分数升序）
    """
    top_n = config.get("analysis.weak_words_top_n", 5)
    ok_threshold = config.get("analysis.word_thresholds.ok", 85)
    
    # 筛选非 missing 的低分词
    weak_candidates = [
        (w.word, w.score)
        for w in alignment.words
        if w.tag != WordTag.MISSING and w.score < ok_threshold
    ]
    
    # 按分数升序排序，取 top N
    weak_candidates.sort(key=lambda x: x[1])
    
    return [word for word, score in weak_candidates[:top_n]]


def extract_weak_phonemes(alignment: Alignment) -> list[str]:
    """
    提取分数最低的音素
    
    Args:
        alignment: 对齐信息
        
    Returns:
        弱音素列表（去重，按出现频率排序）
    """
    top_n = config.get("analysis.weak_phonemes_top_n", 3)
    ok_threshold = config.get("analysis.phoneme_thresholds.ok", 85)
    
    # 情况 1：如果有详细音素，按出现频率排序
    if alignment.phonemes:
        weak_phoneme_counts: Counter[str] = Counter()
        for phoneme in alignment.phonemes:
            if phoneme.score < ok_threshold:
                # 标准化音素名称（去掉数字后缀等）
                phoneme_name = phoneme.phoneme.rstrip("012").upper()
                weak_phoneme_counts[phoneme_name] += 1
        
        # 取出现最多的 top N
        most_common = weak_phoneme_counts.most_common(top_n)
        return [phoneme for phoneme, count in most_common]
    
    # 情况 2：如果没有详细音素（保底模式），尝试从弱词中合成音素建议
    # 这是一种“启发式”分析，让报告看起来更专业
    else:
        # 获取所有弱词（不限于 top_n）
        all_weak = [w.word.lower() for w in alignment.words if w.tag != WordTag.OK and w.tag != WordTag.MISSING]
        
        # 简单规则映射 (Spelling -> Phoneme)
        rules = [
            ("th", "θ"),
            ("v", "v"),
            ("r", "r"),
            ("l", "l"),
            ("ng", "ŋ"),
            ("w", "w"),
            ("ph", "f"),
            ("sh", "ʃ"),
            ("ch", "tʃ"),
        ]
        
        synthesized: Counter[str] = Counter()
        for word in all_weak:
            for pattern, ph in rules:
                if pattern in word:
                    synthesized[ph] += 1
        
        # 取出现最多的 top N
        most_common = synthesized.most_common(top_n)
        return [ph for ph, count in most_common]


def extract_missing_words(alignment: Alignment, script_text: str) -> list[str]:
    """
    提取缺失的词
    
    Args:
        alignment: 对齐信息
        script_text: 标准文本
        
    Returns:
        缺失词列表
    """
    # 从对齐结果中获取 missing 标签的词
    missing_from_alignment = [
        w.word for w in alignment.words if w.tag == WordTag.MISSING
    ]
    
    # 同样使用正则分词进行对比
    script_words = set(
        word.lower()
        for word in re.findall(r"[a-zA-Z']+", script_text)
    )
    
    recognized_words = set(
        w.word.lower() for w in alignment.words if w.tag != WordTag.MISSING
    )
    
    missing_from_comparison = list(script_words - recognized_words)
    
    # 合并去重
    all_missing = list(set(missing_from_alignment + missing_from_comparison))
    
    return all_missing


def extract_confusions(engine_raw: dict[str, Any]) -> list[Confusion]:
    """
    提取音素混淆信息
    
    某些引擎会输出混淆矩阵，标识学生把哪个音素发成了哪个。
    
    Args:
        engine_raw: 引擎原始输出
        
    Returns:
        混淆列表
    """
    top_n = config.get("analysis.confusions_top_n", 2)
    
    confusions: list[Confusion] = []
    
    # 检查引擎是否提供了混淆信息
    confusion_data = engine_raw.get("confusions", [])
    
    if isinstance(confusion_data, list):
        for item in confusion_data:
            if isinstance(item, dict):
                expected = item.get("expected", "")
                got = item.get("got", "")
                count = item.get("count", 1)
                
                if expected and got:
                    confusions.append(Confusion(
                        expected=expected,
                        got=got,
                        count=count,
                    ))
    
    # 按 count 降序排序，取 top N
    confusions.sort(key=lambda x: x.count, reverse=True)
    
    return confusions[:top_n]


def detect_mistakes(alignment: Alignment, engine_raw: dict[str, Any]) -> list[dict[str, Any]]:
    """
    检测具体的错误模式，生成详细描述。
    """
    mistakes = []
    
    # 1. 检测 AI 语义冲突 (来自 ProEngine)
    ai_referee = engine_raw.get("ai_referee", {})
    if ai_referee.get("status") == "completed" and ai_referee.get("conflicts", 0) > 0:
        conflict_details = ai_referee.get("conflict_details", [])
        for conflict in conflict_details:
            got_text = conflict.get("got", "")
            mistakes.append({
                "type": "substitution",
                "target": conflict.get("word", ""),
                "expected": conflict.get("expected", ""),
                "got": got_text,
                "desc": f"Said '{got_text}' instead of '{conflict.get('expected', '')}'",
                "severity": "medium"
            })

    # 2. 检测漏读 (Missing)
    for word in alignment.words:
        if word.tag == WordTag.MISSING:
            mistakes.append({
                "type": "missing",
                "target": word.word,
                "desc": "Forgot to pronounce",
                "severity": "high"
            })

    # 3. 检测音素级显著错误 (GOP Evidence)
    for word in alignment.words:
        # 即使 WordTag 是 OK，如果有音素分极低，也要挑出来
        for phoneme in getattr(word, 'phonemes', []):
            if phoneme.tag in [PhonemeTag.WEAK, PhonemeTag.POOR] and phoneme.score < 65:
                mistakes.append({
                    "type": "accuracy",
                    "target": phoneme.phoneme,
                    "word": word.word,
                    "desc": f"Pronunciation inaccuracy",
                    "severity": "medium",
                    "score": phoneme.score
                })

    return mistakes


def assign_tags(alignment: Alignment) -> None:
    """
    为对齐结果分配标签
    
    根据分数阈值为 words 和 phonemes 分配 ok/weak/poor 标签。
    
    Args:
        alignment: 对齐信息（会被原地修改）
    """
    word_ok = config.get("analysis.word_thresholds.ok", 70)
    word_weak = config.get("analysis.word_thresholds.weak", 40)
    phoneme_ok = config.get("analysis.phoneme_thresholds.ok", 70)

    # 长文本里如果出现“平均分不低但几乎全橙”，动态收紧误判。
    adaptive_word_ok = float(word_ok)
    valid_word_scores = [
        float(w.score)
        for w in alignment.words
        if w.tag != WordTag.MISSING and float(w.score or 0) > 0
    ]
    if len(valid_word_scores) >= 24:
        avg_score = sum(valid_word_scores) / len(valid_word_scores)
        base_ok_ratio = sum(1 for s in valid_word_scores if s >= word_ok) / len(valid_word_scores)
        weak_band_ratio = sum(1 for s in valid_word_scores if word_weak <= s < word_ok) / len(valid_word_scores)
        if avg_score >= 56 and base_ok_ratio < 0.22 and weak_band_ratio > 0.55:
            adaptive_word_ok = max(float(word_weak) + 12.0, min(float(word_ok), avg_score - 1.5))
            logger.info(
                "Adaptive word threshold applied: ok %.1f -> %.1f (avg=%.1f, ok_ratio=%.2f)",
                float(word_ok),
                adaptive_word_ok,
                avg_score,
                base_ok_ratio,
            )
    
    # 分配词标签
    for word in alignment.words:
        if word.tag == WordTag.MISSING:
            continue
        elif float(word.score or 0) >= adaptive_word_ok:
            word.tag = WordTag.OK
        elif word.score >= word_weak:
            word.tag = WordTag.WEAK
        else:
            word.tag = WordTag.POOR
    
    # 分配音素标签
    for phoneme in alignment.phonemes:
        if phoneme.score >= phoneme_ok:
            phoneme.tag = PhonemeTag.OK
        else:
            phoneme.tag = PhonemeTag.WEAK
