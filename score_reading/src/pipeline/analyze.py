"""
鍙ｈ璇勫垎 CLI 妗嗘灦 - 鍒嗘瀽妯″潡

璐熻矗鎻愬彇 weak_words銆亀eak_phonemes銆乧onfusions 绛夊垎鏋愮粨鏋溿€?
"""
import logging
import math
import re
import json
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
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


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "enabled"}


def _clip_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def _append_jsonl_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _maybe_log_missing_debug_sample(
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any],
    stable_missing_indices: list[int],
    missing_source: str,
    completeness: CompletenessStats | None,
    context: dict[str, Any] | None = None,
) -> None:
    debug_cfg = config.get("analysis.missing_debug", {}) or {}
    if not isinstance(debug_cfg, dict):
        return
    if not _is_truthy(debug_cfg.get("enabled", False)):
        return

    path_raw = str(debug_cfg.get("path", "data/diagnostics/missing_debug.jsonl") or "").strip()
    if not path_raw:
        path_raw = "data/diagnostics/missing_debug.jsonl"
    try:
        max_script_chars = int(debug_cfg.get("max_script_chars", 360))
    except Exception:
        max_script_chars = 360
    try:
        max_transcript_chars = int(debug_cfg.get("max_transcript_chars", 360))
    except Exception:
        max_transcript_chars = 360

    script_tokens = re.findall(r"[A-Za-z']+", str(script_text or ""))
    stable_indices = sorted(set(i for i in stable_missing_indices if 0 <= i < len(script_tokens)))
    stable_words = [script_tokens[i] for i in stable_indices]
    alignment_indices = _alignment_missing_script_indices(alignment, script_tokens)
    alignment_words = [script_tokens[i] for i in alignment_indices if 0 <= i < len(script_tokens)]

    detected_transcript = str((engine_raw or {}).get("detected_transcript", "") or "").strip()
    gemini_transcript = str((engine_raw or {}).get("gemini_detected_transcript", "") or "").strip()

    payload: dict[str, Any] = {
        "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "submission_id": str((context or {}).get("submission_id", "") or ""),
        "student_id": str((context or {}).get("student_id", "") or ""),
        "task_id": str((context or {}).get("task_id", "") or ""),
        "source": str((engine_raw or {}).get("source", "") or ""),
        "annotation_source": str((engine_raw or {}).get("annotation_source", "") or ""),
        "missing_source": str(missing_source or ""),
        "script_word_count": len(script_tokens),
        "detected_word_count": len(_tokenize_for_compare(detected_transcript)),
        "gemini_detected_word_count": len(_tokenize_for_compare(gemini_transcript)),
        "stable_missing_indices": stable_indices,
        "stable_missing_words": stable_words,
        "alignment_missing_indices": alignment_indices,
        "alignment_missing_words": alignment_words,
        "completeness_coverage": int(getattr(completeness, "coverage", 0) or 0),
        "azure_completeness_score": (engine_raw or {}).get("completeness_score"),
        "script_preview": _clip_text(script_text, max_script_chars),
        "detected_preview": _clip_text(detected_transcript, max_transcript_chars),
        "gemini_detected_preview": _clip_text(gemini_transcript, max_transcript_chars),
    }

    raw_overlay = (engine_raw or {}).get("gemini_missing_indices")
    if isinstance(raw_overlay, list):
        payload["gemini_missing_indices_raw"] = raw_overlay[:32]
    raw_stable = (engine_raw or {}).get("stable_missing_indices")
    if isinstance(raw_stable, list):
        payload["stable_missing_indices_raw"] = raw_stable[:32]

    try:
        log_path = Path(path_raw).expanduser()
        if not log_path.is_absolute():
            log_path = Path.cwd() / log_path
        _append_jsonl_line(log_path, payload)
    except Exception as exc:
        logger.warning("Missing-debug sample log write failed: %s", exc)



def analyze_results(
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> Analysis:
    """
    鍒嗘瀽璇勫垎缁撴灉锛屾彁鍙栧叧閿俊鎭?
    
    Args:
        alignment: 瀵归綈淇℃伅
        script_text: 鏍囧噯鏂囨湰
        engine_raw: 寮曟搸鍘熷杈撳嚭
        context: Optional identifiers for diagnostics logging
        
    Returns:
        鍒嗘瀽缁撴灉
    """
    logger.info("Start analysis")
    
    analysis = Analysis()
    
    # 0. 寮哄姏瀵归綈锛氬己鍒?alignment.words 涓?script_text 缁撴瀯涓€鑷?(Reference Mode)
    # 杩欒В鍐充簡 Missing Words 涓嶆樉绀虹殑闂
    source = str((engine_raw or {}).get("source", "")).lower()
    gemini_hint = ("gemini" in source) or bool((engine_raw or {}).get("script_reference"))
    script_tokens = _tokenize_for_compare(script_text)
    len_ratio = (len(alignment.words) / max(1, len(script_tokens))) if script_tokens else 0.0
    is_azure_source = "azure" in source
    # Azure alignment already carries stable word timing; forced script realign can
    # inflate false missing labels when ASR transcript drops words.
    if is_azure_source:
        should_realign = False
    else:
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
    # Keep missing-word judgement on the Gemini/script-reference path only.
    # Azure-specific missing correction is intentionally disabled to avoid
    # overwriting real omissions (e.g. 1-3 truly missing words).
    # pause/linking detection runs after timeline repair

    # 1. 棰勫鐞嗭細鍋滈】妫€娴?涓?杩炶妫€娴嬶紙杩欏氨鍦颁慨鏀?alignment锛?    detect_pauses(alignment, script_text, engine_raw)
    
    feedback_error_words = extract_feedback_error_words(engine_raw)
    apply_feedback_top_errors_to_alignment(alignment, feedback_error_words)
    apply_script_reference_focus_rescore(alignment, engine_raw)
    repair_alignment_timeline(alignment, engine_raw)
    detect_pauses(alignment, script_text, engine_raw)
    detect_linking(alignment)
    generate_expected_stress(alignment)
    ensure_stress_signal(alignment)
    
    # 2. 鎻愬彇 weak words
    analysis.weak_words = extract_weak_words(alignment)
    analysis.weak_words = merge_feedback_words(analysis.weak_words, feedback_error_words)
    
    # 3. 鎻愬彇 weak phonemes (鐢ㄤ簬 AI 娣卞害鎸囧)
    analysis.weak_phonemes = extract_weak_phonemes(alignment)
    # Keep both missing words and missing indices for deterministic rendering.
    stable_missing_indices, missing_source = derive_stable_missing_indices(
        alignment,
        script_text,
        engine_raw,
    )
    script_tokens = re.findall(r"[A-Za-z']+", str(script_text or ""))
    reconcile_alignment_missing_tags(
        alignment,
        stable_missing_indices,
        source=missing_source,
        script_text=script_text,
    )
    analysis.missing_indices = [
        idx for idx in stable_missing_indices if 0 <= idx < len(script_tokens)
    ]
    analysis.missing_words = [script_tokens[idx] for idx in analysis.missing_indices]
    if isinstance(engine_raw, dict):
        engine_raw["stable_missing_source"] = missing_source
        engine_raw["stable_missing_indices"] = list(analysis.missing_indices)

    # 4. 鎻愬彇鍏蜂綋閿欒鎽樿 (Mistake Highlights)
    analysis.mistakes = detect_mistakes(alignment, engine_raw)
    
    
    # 3. 鎻愬彇 missing words (Already done above)
    # analysis.missing_words = extract_missing_words(alignment, script_text)
    
    # 4. 鎻愬彇 confusions锛堝鏋滃紩鎿庢彁渚涳級
    analysis.confusions = extract_confusions(engine_raw)
    
    # 5. 璇€熻秼鍔垮垎鏋?
    audio_duration_sec = _safe_float((engine_raw or {}).get("audio_duration_sec"), 0.0)
    analysis.pace_chart_data = calculate_pace_trend(
        alignment,
        audio_duration_sec=audio_duration_sec,
    )
    
    # 6. 瀹屾暣搴﹂珮绾у垎鏋?
    analysis.completeness = analyze_completeness(alignment, script_text, analysis.missing_words)
    _maybe_log_missing_debug_sample(
        alignment=alignment,
        script_text=script_text,
        engine_raw=engine_raw,
        stable_missing_indices=analysis.missing_indices,
        missing_source=missing_source,
        completeness=analysis.completeness,
        context=context,
    )
    
    # 7. 杩熺枒鍒嗘瀽 (Basic Text Matching)
    analysis.hesitations = analyze_hesitations(alignment)
    
    # 8. 璇皟鏇茬嚎鏁版嵁鎻愬彇
    if "pitch_contour" in engine_raw:
        analysis.pitch_contour = [
            PitchPoint(t=p["t"], f0=p["f"]) for p in engine_raw["pitch_contour"]
        ]

    audio_path = str((context or {}).get("audio_path", "") or "").strip()
    analysis.intonation_analysis = _build_prosody_intonation_analysis(
        alignment,
        script_text,
        engine_raw=engine_raw,
        audio_path=audio_path,
    )

    # 9. 鐢熸垚鏈熸湜閲嶉煶妯″紡 (Native Speaker 鍙傝€?
    generate_expected_stress(alignment)
    
    logger.info(
        f"鍒嗘瀽瀹屾垚: weak_words={len(analysis.weak_words)}, "
        f"weak_phonemes={len(analysis.weak_phonemes)}, "
        f"missing={len(analysis.missing_words)}, "
        f"confusions={len(analysis.confusions)}"
    )
    
    return analysis


# 甯歌铏氳瘝鍒楄〃锛堝急璇昏瘝锛?
FUNCTION_WORDS = {
    # 鍐犺瘝
    "a", "an", "the",
    # 浠嬭瘝
    "to", "of", "in", "on", "at", "for", "with", "by", "from", "up", "about",
    "into", "over", "after", "before", "between", "under", "without", "through",
    # 杩炶瘝
    "and", "or", "but", "so", "if", "because", "although", "while", "when",
    # 浠ｈ瘝
    "i", "me", "my", "you", "your", "he", "him", "his", "she", "her", "it", "its",
    "we", "us", "our", "they", "them", "their", "this", "that", "these", "those",
    # 鍔╁姩璇?
    "is", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    # 鍏朵粬
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
    Attach pronunciation-diagnostics hints to matching words.
    This linkage is advisory only: do not alter word score/tag to avoid false positives.
    """
    if not alignment.words or not feedback_words:
        return

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
    浣跨敤 difflib 灏嗚瘑鍒粨鏋滃己鍒跺榻愬埌鑴氭湰缁撴瀯銆?
    
    鐩殑锛?
    1. 纭繚 UI 鏄剧ず鐨勫崟璇嶅垪琛ㄤ笌鑴氭湰 1:1 瀵瑰簲锛圙host Words View 闇€瑕侊級銆?
    2. 鍙戠幇骞舵爣璁版紡璇荤殑璇嶏紙Missing锛夈€?
    3. 澶勭悊澶氳鐨勮瘝锛堝拷鐣ユ垨鏍囪锛夈€?
    
    绛栫暐锛?
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
    # 浣跨敤 \w+ 鍖呮嫭鏁板瓧鍜屽瓧姣嶏紝handle ' for contractions
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
    
    # 浣跨敤鏃堕棿娓告爣鏉ヤ负鎻掑叆鐨?Missing 璇嶄及绠楁椂闂?
    current_time_cursor = 0.0
    if hyp_words:
        current_time_cursor = hyp_words[0].start
        
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        # ref[i1:i2] vs hyp[j1:j2]
        
        if tag == 'equal':
            # 瀹屽叏鍖归厤锛氫繚鐣欒瘑鍒粨鏋?
            for k in range(j1, j2):
                w = hyp_words[k]
                # 寮哄埗淇鍗曡瘝鎷煎啓涓?Script 鐨勬牱瀛?(Case correction)
                ref_idx = i1 + (k - j1)
                if ref_idx < len(ref_tokens):
                    w.word = ref_tokens[ref_idx]
                new_words.append(w)
                current_time_cursor = w.end
                
        elif tag == 'delete':
            # Ref 鏈夛紝Hyp 娌℃湁 -> Missing
            # 鎻掑叆 Missing Words
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
            # Ref 鏈夛紝Hyp 涔熸湁浣嗕笉鍚?-> Mispronunciation (Wait, or just align error)
            # 閫昏緫锛氱敤鎴锋兂璇?Ref锛屼絾璇绘垚浜?Hyp銆?
            # 鎴戜滑淇濈暀 Ref 鐨勫崟璇嶆枃鏈紝浣嗙户鎵?Hyp 鐨勫垎鏁帮紙閫氬父杈冧綆锛夋垨鏍囪涓?WEAK
            
            # 杩欓噷鐨勬暟閲忓彲鑳戒笉涓€鑷?(e.g. Ref: "cat", Hyp: "bat mat")
            # 绠€鍗曠瓥鐣ワ細鎸?1:1 鏄犲皠锛屽浣欑殑蹇界暐/琛ュ叏
            len_ref = i2 - i1
            len_hyp = j2 - j1
            common_len = min(len_ref, len_hyp)
            
            for k in range(common_len):
                w_orig = hyp_words[j1 + k]
                ref_word = ref_tokens[i1 + k]
                hyp_word_before = w_orig.word
                
                # Keep script token text while preserving timing and acoustic score.
                w_orig.word = ref_word

                # Prevent replacement mismatches from being promoted as fully OK.
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
                
            # 澶勭悊鍓╀綑鐨?Ref (瑙嗕负 Missing)
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
                    
            # 澶勭悊鍓╀綑鐨?Hyp (瑙嗕负 Extra - 蹇界暐锛屽洜涓烘垜浠淇濇寔 Script 缁撴瀯)
            pass
            
        elif tag == 'insert':
            # Hyp 鏈?(extra)锛孯ef 娌℃湁 -> 蹇界暐
            pass
            
    # 鏇存柊 Alignment
    alignment.words = new_words
    
    # CRITICAL: Re-assign tags after reconstruction to ensure Missing/Weak/OK are set correctly
    assign_tags(alignment)
    
    logger.info(f"Alignment synced to script: {len(hyp_words)} -> {len(new_words)} words")


def _tokenize_for_compare(text: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z']+", text or "")]


def _expand_alignment_tokens_for_mapping(alignment: Alignment) -> tuple[list[str], list[int]]:
    tokens: list[str] = []
    parents: list[int] = []
    for idx, word in enumerate(alignment.words):
        raw = str(getattr(word, "word", "") or "")
        parts = re.findall(r"[A-Za-z']+", raw)
        if not parts:
            token = _normalize_token(raw)
            if token:
                parts = [token]
        for part in parts:
            token = _normalize_token(part)
            if not token:
                continue
            tokens.append(token)
            parents.append(idx)
    return tokens, parents


def _build_script_alignment_maps(
    script_tokens: list[str],
    alignment: Alignment,
) -> tuple[dict[int, int], dict[int, int]]:
    script_norm = [_normalize_token(t) for t in script_tokens]
    align_tokens, align_parents = _expand_alignment_tokens_for_mapping(alignment)
    if not script_norm or not align_tokens:
        return {}, {}

    script_to_align: dict[int, int] = {}
    align_to_script: dict[int, int] = {}
    matcher = SequenceMatcher(None, script_norm, align_tokens)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                s_idx = i1 + k
                a_idx = align_parents[j1 + k]
                script_to_align.setdefault(s_idx, a_idx)
                align_to_script.setdefault(a_idx, s_idx)
            continue
        if tag != "replace":
            continue
        left_len = i2 - i1
        right_len = j2 - j1
        if left_len <= 0 or right_len <= 0:
            continue
        for k in range(left_len):
            rel = int(k * right_len / max(left_len, 1))
            rel = min(max(rel, 0), right_len - 1)
            s_idx = i1 + k
            a_idx = align_parents[j1 + rel]
            script_to_align.setdefault(s_idx, a_idx)
            align_to_script.setdefault(a_idx, s_idx)

    return script_to_align, align_to_script


def _alignment_missing_script_indices(
    alignment: Alignment,
    script_tokens: list[str],
) -> list[int]:
    if not alignment.words or not script_tokens:
        return []
    _script_to_align, align_to_script = _build_script_alignment_maps(script_tokens, alignment)
    out: set[int] = set()
    for a_idx, word in enumerate(alignment.words):
        if word.tag != WordTag.MISSING:
            continue
        s_idx = align_to_script.get(a_idx)
        if s_idx is not None and 0 <= s_idx < len(script_tokens):
            out.add(s_idx)
    return sorted(out)


def _script_indices_to_alignment_indices(
    script_indices: list[int],
    script_tokens: list[str],
    alignment: Alignment,
) -> set[int]:
    if not script_indices:
        return set()
    script_to_align, _align_to_script = _build_script_alignment_maps(script_tokens, alignment)
    mapped: set[int] = set()
    for raw_idx in script_indices:
        try:
            s_idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        a_idx = script_to_align.get(s_idx)
        if a_idx is not None and 0 <= a_idx < len(alignment.words):
            mapped.add(a_idx)
        elif 0 <= s_idx < len(alignment.words):
            mapped.add(s_idx)
    return mapped


def _engine_transcript_tokens(engine_raw: dict[str, Any]) -> list[str]:
    detected = str((engine_raw or {}).get("detected_transcript", "") or "").strip()
    if detected:
        return _tokenize_for_compare(detected)

    source = str((engine_raw or {}).get("source", "")).lower()
    if "azure" in source:
        raw = (engine_raw or {}).get("json_raw") or {}
        nbest = raw.get("NBest") if isinstance(raw, dict) else None
        if isinstance(nbest, list) and nbest:
            first = nbest[0] if isinstance(nbest[0], dict) else {}
            display = str(first.get("Display", "") or first.get("Lexical", "") or "").strip()
            if display:
                return _tokenize_for_compare(display)
        display_text = str(raw.get("DisplayText", "") or "").strip() if isinstance(raw, dict) else ""
        if display_text:
            return _tokenize_for_compare(display_text)

    return []


def apply_azure_false_missing_correction(
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any],
) -> None:
    """
    Conservative guard for Azure path:
    if transcript evidence is present and overall missing ratio is not high,
    convert obvious false-missing words to non-missing.
    """
    if not alignment.words:
        return
    source = str((engine_raw or {}).get("source", "")).lower()
    if "azure" not in source:
        return

    ref_tokens = _tokenize_for_compare(script_text)
    if not ref_tokens:
        return

    missing_total = sum(1 for w in alignment.words if w.tag == WordTag.MISSING)
    if missing_total <= 0:
        return
    # Keep genuine small omissions (1-3 words) untouched.
    # Azure false-missing bursts usually appear as larger clusters.
    if missing_total < 4:
        return
    missing_ratio = missing_total / max(1, len(ref_tokens))
    if missing_ratio > 0.25:
        return

    transcript_tokens = _engine_transcript_tokens(engine_raw)
    if not transcript_tokens:
        return
    transcript_ratio = len(transcript_tokens) / max(1, len(ref_tokens))
    if transcript_ratio < 0.52:
        return

    likely_missing = _gemini_missing_indices(script_text, " ".join(transcript_tokens))
    word_ok = float(config.get("analysis.word_thresholds.ok", 65))
    word_weak = float(config.get("analysis.word_thresholds.weak", 40))
    accuracy = float((engine_raw or {}).get("accuracy_score") or (engine_raw or {}).get("pronunciation_score") or 70.0)

    corrected_score = max(word_weak + 16.0, min(82.0, accuracy - 2.0))
    promote_to_ok = transcript_ratio >= 0.68 and missing_ratio <= 0.18
    if promote_to_ok:
        corrected_score = max(corrected_score, word_ok + 1.0)

    corrected = 0
    for idx, word in enumerate(alignment.words):
        if word.tag != WordTag.MISSING:
            continue
        if idx in likely_missing:
            continue

        word.score = max(float(word.score or 0.0), corrected_score)
        word.tag = WordTag.OK if promote_to_ok and word.score >= word_ok else WordTag.WEAK
        note = "Azure transcript evidence indicates this word was spoken."
        word.diagnosis = f"{word.diagnosis} | {note}".strip(" |")
        corrected += 1

    if corrected and isinstance(engine_raw, dict):
        engine_raw["azure_false_missing_corrected_count"] = int(corrected)
        logger.info(
            "Azure false-missing correction adjusted %s words (missing_ratio=%.2f, transcript_ratio=%.2f, promote_to_ok=%s).",
            corrected,
            missing_ratio,
            transcript_ratio,
            promote_to_ok,
        )


def _token_similarity_ratio(a: str, b: str) -> float:
    import difflib

    left = str(a or "").strip().lower()
    right = str(b or "").strip().lower()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if len(left) >= 3 and len(right) >= 3 and left[:3] == right[:3]:
        return 0.82
    return float(difflib.SequenceMatcher(None, left, right).ratio())


def _compute_missing_indices_by_alignment(
    ref_tokens: list[str],
    hyp_tokens: list[str],
) -> set[int]:
    """
    Edit-path alignment with substitution-friendly costs.
    This avoids repeated-word drift from value-order matching and reduces
    false deletions when a token is substituted rather than omitted.
    """
    if not ref_tokens or not hyp_tokens:
        return set()

    n = len(ref_tokens)
    m = len(hyp_tokens)
    delete_cost = 1.0
    insert_cost = 1.0

    dp: list[list[float]] = [[0.0] * (m + 1) for _ in range(n + 1)]
    back: list[list[int]] = [[0] * (m + 1) for _ in range(n + 1)]  # 0=sub/match, 1=delete, 2=insert

    for i in range(1, n + 1):
        dp[i][0] = i * delete_cost
        back[i][0] = 1
    for j in range(1, m + 1):
        dp[0][j] = j * insert_cost
        back[0][j] = 2

    priority = {0: 0, 2: 1, 1: 2}  # prefer sub/match, then insert, then delete on ties
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            ref = ref_tokens[i - 1]
            hyp = hyp_tokens[j - 1]
            if ref == hyp:
                sub_cost = 0.0
            else:
                sim = _token_similarity_ratio(ref, hyp)
                if sim >= 0.86:
                    sub_cost = 0.35
                elif sim >= 0.70:
                    sub_cost = 0.65
                else:
                    sub_cost = 0.95

            candidates = (
                (dp[i - 1][j - 1] + sub_cost, 0),
                (dp[i][j - 1] + insert_cost, 2),
                (dp[i - 1][j] + delete_cost, 1),
            )
            best_cost, best_op = min(candidates, key=lambda item: (item[0], priority[item[1]]))
            dp[i][j] = best_cost
            back[i][j] = best_op

    missing: set[int] = set()
    i = n
    j = m
    while i > 0 or j > 0:
        op = back[i][j] if i >= 0 and j >= 0 else 0
        if i > 0 and j > 0 and op == 0:
            i -= 1
            j -= 1
            continue
        if i > 0 and (j == 0 or op == 1):
            missing.add(i - 1)
            i -= 1
            continue
        if j > 0:
            j -= 1
            continue
        break

    return missing


def _gemini_missing_indices(script_text: str, detected_transcript: str) -> set[int]:
    ref_tokens = _tokenize_for_compare(script_text)
    hyp_tokens = _tokenize_for_compare(detected_transcript)
    if not ref_tokens or not hyp_tokens:
        return set()
    return _compute_missing_indices_by_alignment(ref_tokens, hyp_tokens)


def _anchored_delete_indices(
    ref_tokens: list[str],
    hyp_tokens: list[str],
    *,
    max_delete_run: int = 3,
) -> set[int]:
    """
    Conservative delete detector:
    only keep short delete spans with exact anchors around the gap.
    """
    if not ref_tokens or not hyp_tokens:
        return set()

    out: set[int] = set()
    matcher = SequenceMatcher(None, ref_tokens, hyp_tokens)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "delete":
            continue
        run_len = i2 - i1
        if run_len <= 0 or run_len > max_delete_run:
            continue

        left_anchor = i1 > 0 and j1 > 0 and ref_tokens[i1 - 1] == hyp_tokens[j1 - 1]
        right_anchor = (
            i2 < len(ref_tokens)
            and j1 < len(hyp_tokens)
            and ref_tokens[i2] == hyp_tokens[j1]
        )

        if i1 > 0 and i2 < len(ref_tokens):
            if not (left_anchor and right_anchor):
                continue
        else:
            if not (left_anchor or right_anchor):
                continue

        out.update(range(i1, i2))

    return out


def _safe_transcript_missing_indices(
    script_text: str,
    engine_raw: dict[str, Any],
    *,
    transcript_field: str,
    min_cov: float = 0.72,
    max_cov: float = 1.30,
    max_missing_ratio: float = 0.16,
    max_missing_abs: int = 8,
) -> set[int]:
    ref_tokens = _tokenize_for_compare(script_text)
    if not ref_tokens:
        return set()

    transcript = str((engine_raw or {}).get(transcript_field, "")).strip()
    if not transcript:
        return set()

    hyp_tokens = _tokenize_for_compare(transcript)
    if not hyp_tokens:
        return set()

    coverage = len(hyp_tokens) / max(1, len(ref_tokens))
    if not (min_cov <= coverage <= max_cov):
        return set()

    missing = _gemini_missing_indices(script_text, transcript)
    max_missing = max(max_missing_abs, int(len(ref_tokens) * max_missing_ratio))
    if len(missing) > max_missing:
        return set()

    return {idx for idx in missing if 0 <= idx < len(ref_tokens)}


def _extract_ai_referee_missing_indices(
    script_text: str,
    engine_raw: dict[str, Any],
) -> list[int]:
    """
    Extract high-confidence explicit omissions from ai_referee conflict comments.
    Example supported forms:
      - missed 'the' before 'last'
      - missed 'a'
    We intentionally ignore suffix-only misses (plural/possessive/ending).
    """
    ref_tokens = _tokenize_for_compare(script_text)
    if not ref_tokens:
        return []

    ai_ref = (engine_raw or {}).get("ai_referee") or {}
    details = ai_ref.get("conflict_details") if isinstance(ai_ref, dict) else None
    if not isinstance(details, list):
        return []

    def _norm(token: str) -> str:
        return _normalize_token(token)

    explicit: set[int] = set()
    loose_words: list[str] = []

    # Gemini transcript hints help place repeated function words (e.g., multiple "a"/"you").
    transcript_hint_indices = _safe_transcript_missing_indices(
        script_text,
        engine_raw,
        transcript_field="gemini_detected_transcript",
        min_cov=0.70,
        max_cov=1.35,
        max_missing_ratio=0.22,
        max_missing_abs=12,
    )

    for item in details:
        if not isinstance(item, dict):
            continue
        comment = str(item.get("comment", "") or "")
        lowered = comment.lower()
        has_omit_hint = any(
            key in lowered for key in ("missed", "skipped", "omitted", "left out")
        )
        if not has_omit_hint:
            continue
        if any(k in lowered for k in ("plural", "possessive", "ending", "/", "sound")):
            continue

        m_before = re.search(
            r"(?:missed|skipped|omitted)\s+'([^']+)'\s+before\s+'([^']+)'",
            comment,
            flags=re.IGNORECASE,
        )
        if m_before:
            miss_word = _norm(m_before.group(1))
            anchor_word = _norm(m_before.group(2))
            if miss_word and anchor_word:
                for idx, tok in enumerate(ref_tokens):
                    if tok != anchor_word or idx <= 0:
                        continue
                    if ref_tokens[idx - 1] == miss_word:
                        explicit.add(idx - 1)
            continue

        m_plain = re.search(
            r"(?:missed|skipped|omitted)\s+'([^']+)'",
            comment,
            flags=re.IGNORECASE,
        )
        if m_plain:
            miss_word = _norm(m_plain.group(1))
            if miss_word:
                loose_words.append(miss_word)
            continue

        # Handle terse forms like "Word was skipped." where target is only in item["word"].
        word_field = _norm(str(item.get("word", "") or ""))
        if word_field:
            loose_words.append(word_field)

    # Conservative placement for plain omission words.
    # Prefer transcript-supported indices for repeated tokens; otherwise fallback sequentially.
    used: set[int] = set(explicit)
    search_start = 0
    for miss_word in loose_words:
        candidates = [
            idx
            for idx, token in enumerate(ref_tokens)
            if token == miss_word and idx not in used
        ]
        if not candidates:
            continue

        pick: int | None = None
        hinted = [idx for idx in candidates if idx in transcript_hint_indices]
        if hinted:
            pick = hinted[0]
        else:
            for idx in candidates:
                if idx >= search_start:
                    pick = idx
                    break
            if pick is None:
                pick = candidates[0]

        explicit.add(pick)
        used.add(pick)
        search_start = pick + 1

    return sorted(explicit)


def _consensus_missing_indices_from_transcripts(
    script_text: str,
    engine_raw: dict[str, Any],
) -> list[int]:
    """
    Conservative fallback: only keep missing indices agreed by both Gemini and Azure transcripts.
    """
    ref_tokens = _tokenize_for_compare(script_text)
    if not ref_tokens:
        return []

    gemini_transcript = str((engine_raw or {}).get("gemini_detected_transcript", "")).strip()
    azure_transcript = str((engine_raw or {}).get("detected_transcript", "")).strip()
    if not gemini_transcript or not azure_transcript:
        return []

    gem_tokens = _tokenize_for_compare(gemini_transcript)
    az_tokens = _tokenize_for_compare(azure_transcript)
    if not gem_tokens or not az_tokens:
        return []

    gem_cov = len(gem_tokens) / max(1, len(ref_tokens))
    az_cov = len(az_tokens) / max(1, len(ref_tokens))
    if not (0.78 <= gem_cov <= 1.22 and 0.78 <= az_cov <= 1.28):
        return []

    gem_missing = _gemini_missing_indices(script_text, gemini_transcript)
    az_missing = _gemini_missing_indices(script_text, azure_transcript)
    consensus = sorted(gem_missing.intersection(az_missing))
    if not consensus:
        return []

    max_missing = max(3, int(len(ref_tokens) * 0.08))
    if len(consensus) > max_missing:
        return []
    return consensus


def extract_gemini_overlay_missing_indices(
    script_text: str,
    engine_raw: dict[str, Any],
) -> list[int]:
    """
    For Azure+Gemini overlay mode, derive stable missing indices for script words.
    Priority:
    1) Explicit Gemini overlay indices from Pro engine (sequence mode),
    2) Gemini transcript alignment indices only when explicit indices are absent
       (legacy payload compatibility).
    """
    source = str((engine_raw or {}).get("source", "")).lower()
    annotation_source = str((engine_raw or {}).get("annotation_source", "")).lower()
    if "azure" not in source or annotation_source != "gemini":
        return []

    ref_tokens = _tokenize_for_compare(script_text)
    if not ref_tokens:
        return []

    max_index = len(ref_tokens) - 1
    raw_indices = (engine_raw or {}).get("gemini_missing_indices")
    if isinstance(raw_indices, list):
        # Explicit overlay indices are authoritative, including explicit empty list.
        explicit: set[int] = set()
        for raw in raw_indices:
            try:
                idx = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= idx <= max_index:
                explicit.add(idx)
        if explicit:
            ai_ref_missing = _extract_ai_referee_missing_indices(script_text, engine_raw)
            ai_ref_set = {i for i in ai_ref_missing if 0 <= i <= max_index}
            gemini_transcript_missing = _safe_transcript_missing_indices(
                script_text,
                engine_raw,
                transcript_field="gemini_detected_transcript",
                min_cov=0.72,
                max_cov=1.30,
                max_missing_ratio=0.16,
                max_missing_abs=8,
            )

            # Validate explicit overlay indices with extra evidence to avoid index-shift artifacts
            # (common around repeated function words such as "you", "a", "the").
            if ai_ref_set or gemini_transcript_missing:
                validated: set[int] = set()
                for idx in explicit:
                    token = ref_tokens[idx]
                    support = 1  # explicit overlay
                    if idx in ai_ref_set:
                        support += 1
                    if idx in gemini_transcript_missing:
                        support += 1
                    else:
                        has_near_same_token = any(
                            abs(cand - idx) <= 2 and ref_tokens[cand] == token
                            for cand in gemini_transcript_missing
                        )
                        if has_near_same_token:
                            support += 1
                    if support >= 2:
                        validated.add(idx)
                explicit = validated

            merged = set(explicit)
            if ai_ref_set:
                merged.update(ai_ref_set)
            if not merged and ai_ref_set:
                merged = set(ai_ref_set)

            consensus_missing = _consensus_missing_indices_from_transcripts(script_text, engine_raw)
            if consensus_missing:
                extras = [i for i in consensus_missing if i not in merged]
                if len(extras) <= 2:
                    merged.update(extras)
            return sorted(merged)

        # Explicit empty list: try high-confidence ai_referee omissions first.
        ai_ref_missing = _extract_ai_referee_missing_indices(script_text, engine_raw)
        if ai_ref_missing:
            return sorted(set(i for i in ai_ref_missing if 0 <= i <= max_index))

        # Last fallback for explicit-empty: dual-transcript consensus only.
        consensus_missing = _consensus_missing_indices_from_transcripts(script_text, engine_raw)
        if consensus_missing:
            return sorted(set(i for i in consensus_missing if 0 <= i <= max_index))
        return []

    # Legacy compatibility path: older payloads without gemini_missing_indices.
    detected_transcript = str((engine_raw or {}).get("gemini_detected_transcript", "")).strip()
    if detected_transcript:
        hyp_tokens = _tokenize_for_compare(detected_transcript)
        if hyp_tokens:
            coverage = len(hyp_tokens) / max(1, len(ref_tokens))
            if 0.78 <= coverage <= 1.22:
                transcript_missing = _gemini_missing_indices(script_text, detected_transcript)
                max_missing = max(6, int(len(ref_tokens) * 0.14))
                if len(transcript_missing) <= max_missing:
                    return sorted(transcript_missing)

    return []


def extract_gemini_overlay_missing_words(
    script_text: str,
    engine_raw: dict[str, Any],
) -> list[str]:
    missing_indices = extract_gemini_overlay_missing_indices(script_text, engine_raw)
    if not missing_indices:
        return []

    ref_tokens = _tokenize_for_compare(script_text)
    missing_words: list[str] = []
    for idx in missing_indices:
        if 0 <= idx < len(ref_tokens):
            missing_words.append(ref_tokens[idx])
    if missing_words:
        logger.info(
            "Using Gemini overlay missing indices for completeness: %s",
            len(missing_words),
        )
    return missing_words


def _prefer_transcript_anchor_over_shifted_alignment(
    script_tokens: list[str],
    alignment_missing: list[int],
    transcript_tokens: list[str],
) -> list[int] | None:
    """
    Correct common local shift artifacts where Azure alignment marks a neighboring
    word as missing but Azure transcript supports a cleaner short phrase omission.
    """
    if not alignment_missing or not transcript_tokens or not script_tokens:
        return None

    ref_norm = [t.lower() for t in script_tokens]
    hyp_norm = [t.lower() for t in transcript_tokens]
    anchored_missing = sorted(
        _anchored_delete_indices(
            ref_norm,
            hyp_norm,
            max_delete_run=3,
        )
    )
    if not anchored_missing:
        return None

    align_set = sorted(set(int(i) for i in alignment_missing if 0 <= int(i) < len(script_tokens)))
    anchor_set = sorted(set(int(i) for i in anchored_missing if 0 <= int(i) < len(script_tokens)))
    if not align_set or not anchor_set:
        return None

    def _is_pronoun_like(idx: int) -> bool:
        tok = ref_norm[idx]
        return tok in {"i'm", "im", "i", "you", "we", "we're", "they", "he", "she"}

    def _cluster(indices: list[int]) -> list[list[int]]:
        if not indices:
            return []
        items = sorted(indices)
        clusters: list[list[int]] = [[items[0]]]
        for idx in items[1:]:
            if idx == clusters[-1][-1] + 1:
                clusters[-1].append(idx)
            else:
                clusters.append([idx])
        return clusters

    align_clusters = _cluster(align_set)
    anchor_clusters = _cluster(anchor_set)

    # Prefer a short contiguous phrase omission when alignment points to an
    # adjacent pronoun/contraction but transcript keeps that token and skips the
    # following phrase, e.g. "I'm bring" -> missing "going to".
    replaced = False
    merged = list(align_set)
    for a_cluster in align_clusters:
        if len(a_cluster) != 1:
            continue
        a0 = a_cluster[0]
        if not _is_pronoun_like(a0):
            continue
        for t_cluster in anchor_clusters:
            if len(t_cluster) < 2:
                continue
            if t_cluster[0] == a0 + 1 and t_cluster[-1] <= a0 + 3:
                merged = [idx for idx in merged if idx != a0]
                merged.extend(t_cluster)
                replaced = True
                break
        if replaced:
            break

    if replaced:
        return sorted(set(merged))

    # More general local rule: if transcript-anchor produces a short contiguous
    # deletion near a shifted single-token alignment miss and explains more
    # tokens, prefer the anchored phrase locally.
    for a_cluster in align_clusters:
        if len(a_cluster) != 1:
            continue
        a0 = a_cluster[0]
        for t_cluster in anchor_clusters:
            if len(t_cluster) <= len(a_cluster):
                continue
            if t_cluster != list(range(t_cluster[0], t_cluster[-1] + 1)):
                continue
            if max(abs(t_cluster[0] - a0), abs(t_cluster[-1] - a0)) <= 2:
                merged = [idx for idx in align_set if idx != a0] + t_cluster
                return sorted(set(merged))

    return None


def derive_stable_missing_indices(
    alignment: Alignment,
    script_text: str,
    engine_raw: dict[str, Any],
) -> tuple[list[int], str]:
    """
    Derive robust missing-word indices for UI/completeness.
    Final missing-word output follows Azure evidence only:
    1) Azure alignment-based missing,
    2) Azure transcript-anchor fallback,
    3) Never use transcript-only missing without anchored alignment support.
    """
    script_tokens = re.findall(r"[A-Za-z']+", str(script_text or ""))
    if not script_tokens:
        return [], "none"
    alignment_missing = sorted(set(_alignment_missing_script_indices(alignment, script_tokens)))

    raw = engine_raw or {}
    source = str(raw.get("source", "")).lower()

    if "azure" in source:
        transcript_tokens = _engine_transcript_tokens(engine_raw)
        if alignment_missing and transcript_tokens:
            reanchored = _prefer_transcript_anchor_over_shifted_alignment(
                script_tokens,
                alignment_missing,
                transcript_tokens,
            )
            if reanchored and reanchored != alignment_missing:
                logger.info(
                    "Re-anchor Azure alignment missing to transcript phrase: %s -> %s",
                    alignment_missing,
                    reanchored,
                )
                return reanchored, "alignment_reanchor"

    if alignment_missing:
        return alignment_missing, "alignment"

    if "azure" in source:
        transcript_tokens = _engine_transcript_tokens(engine_raw)
        if transcript_tokens:
            ref_norm = [t.lower() for t in script_tokens]
            transcript_missing = sorted(_compute_missing_indices_by_alignment(ref_norm, transcript_tokens))
            coverage = len(transcript_tokens) / max(1, len(script_tokens))
            anchored_missing = sorted(
                _anchored_delete_indices(
                    ref_norm,
                    transcript_tokens,
                    max_delete_run=3,
                )
            )
            max_anchor_missing = max(2, int(len(script_tokens) * 0.03))
            if (
                not alignment_missing
                and 0.86 <= coverage <= 1.12
                and 0 < len(anchored_missing) <= max_anchor_missing
            ):
                logger.info(
                    "Apply Azure anchored-missing fallback: anchored=%s coverage=%.2f",
                    len(anchored_missing),
                    coverage,
                )
                return anchored_missing, "transcript_anchor"
            if transcript_missing:
                logger.info(
                    "Ignore Azure transcript-only missing (no Gemini evidence / no alignment evidence): transcript=%s",
                    len(transcript_missing),
                )
    return [], "alignment"


def reconcile_alignment_missing_tags(
    alignment: Alignment,
    stable_missing_indices: list[int],
    source: str,
    script_text: str,
) -> None:
    """
    Keep alignment missing tags consistent with stable missing indices so UI and
    downstream mistake summaries do not show inflated false-missing words.
    """
    if source not in {"overlay", "transcript", "transcript_anchor"}:
        return
    if not alignment.words:
        return
    script_tokens = re.findall(r"[A-Za-z']+", str(script_text or ""))
    if not script_tokens:
        return
    if abs(len(alignment.words) - len(script_tokens)) > 2:
        logger.info(
            "Skip missing-tag reconciliation: alignment/script length mismatch (%s vs %s).",
            len(alignment.words),
            len(script_tokens),
        )
        return

    script_to_align, _align_to_script = _build_script_alignment_maps(script_tokens, alignment)
    missing_set: set[int] = set()
    for raw_idx in stable_missing_indices:
        try:
            s_idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        if not (0 <= s_idx < len(script_tokens)):
            continue
        a_idx = script_to_align.get(s_idx)
        if a_idx is None or not (0 <= a_idx < len(alignment.words)):
            continue
        script_token = _normalize_token(script_tokens[s_idx])
        align_parts = [
            _normalize_token(tok)
            for tok in re.findall(r"[A-Za-z']+", str(getattr(alignment.words[a_idx], "word", "") or ""))
        ]
        # Only mark as missing when mapped alignment token still carries the same
        # lexical token. Otherwise it is likely a shift-to-neighbor artifact.
        if script_token and script_token in align_parts:
            missing_set.add(a_idx)
    word_ok = float(config.get("analysis.word_thresholds.ok", 65))
    word_weak = float(config.get("analysis.word_thresholds.weak", 40))
    recover_score = max(word_weak + 14.0, min(word_ok + 1.0, 66.0))

    adjusted = 0
    for idx, word in enumerate(alignment.words):
        if idx in missing_set:
            if word.tag != WordTag.MISSING:
                word.tag = WordTag.MISSING
                word.score = min(float(word.score or 0.0), max(0.0, word_weak - 2.0))
                adjusted += 1
            continue

        if word.tag == WordTag.MISSING:
            word.score = max(float(word.score or 0.0), recover_score)
            word.tag = WordTag.OK if word.score >= word_ok else WordTag.WEAK
            note = "Missing tag cleared by transcript evidence."
            word.diagnosis = f"{word.diagnosis} | {note}".strip(" |")
            adjusted += 1

    if adjusted:
        logger.info(
            "Reconciled %s missing tags using %s source.",
            adjusted,
            source,
        )


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
    # This correction is for Gemini-primary runs only.
    # In Azure+Gemini overlay mode, completeness missing words are derived via
    # extract_gemini_overlay_missing_words().
    if "gemini" not in source:
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

    # 涓嶅啀鎶娾€滆 Gemini 璇佹槑宸茶鍒扳€濈殑璇嶇粺涓€鍘嬪埌 45 鍒嗐€?    # 璇勫垎鏉ヨ嚜鍏ㄥ眬璐ㄩ噺锛岄槻姝㈤暱鏂囨湰鍑犱箮鍏ㄦ銆?    corrected_score = max(word_weak + 18.0, min(82.0, accuracy - 4.0))
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
    has_gemini_hint = "gemini" in source
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


def _timeline_wpm_scale(timeline_span: float, audio_duration_sec: float) -> float:
    """
    Return a duration scale for WPM/pace when alignment span is clearly off from audio span.
    """
    span = max(0.001, float(timeline_span or 0.0))
    audio_dur = max(0.0, float(audio_duration_sec or 0.0))
    if audio_dur < 1.5:
        return 1.0
    ratio = span / audio_dur
    if ratio < 0.65 or ratio > 1.45:
        return max(0.35, min(4.0, audio_dur / span))
    return 1.0


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
    gaps = [
        max(0.0, _safe_float(words[i + 1].start) - _safe_float(words[i].end))
        for i in range(len(words) - 1)
    ]

    if timeline_end > 500.0 and audio_duration > 0 and timeline_end > audio_duration * 5:
        for w in words:
            w.start = _safe_float(w.start) / 1000.0
            w.end = _safe_float(w.end) / 1000.0
        timeline_end = max((_safe_float(w.end) for w in words), default=0.0)
        timeline_start = min((_safe_float(w.start) for w in words), default=0.0)
        timeline_span = max(0.001, timeline_end - timeline_start)

    gap_std = 0.0
    fixed_gap_ratio = 0.0
    median_gap = 0.0
    if gaps:
        sorted_gaps = sorted(gaps)
        median_gap = float(sorted_gaps[len(sorted_gaps) // 2])
        around_median = [g for g in gaps if abs(g - median_gap) <= 0.02]
        fixed_gap_ratio = len(around_median) / max(1, len(gaps))
        mean_gap = sum(gaps) / len(gaps)
        gap_std = math.sqrt(sum((g - mean_gap) ** 2 for g in gaps) / max(1, len(gaps)))

    uniform_gap_signature = (
        len(gaps) >= 6
        and (
            (0.07 <= median_gap <= 0.18 and fixed_gap_ratio >= 0.65 and gap_std <= 0.03)
            or (median_gap <= 0.45 and fixed_gap_ratio >= 0.90 and gap_std <= 0.012)
        )
    )
    likely_synthetic = (
        audio_duration > 8.0
        and timeline_span < max(6.0, audio_duration * 0.35)
        and tiny_ratio >= 0.45
        and uniform_gap_signature
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
    涓烘瘡涓崟璇嶇敓鎴愭湡鏈涢噸闊冲€?(Native Speaker 鍙傝€?
    
    瑙勫垯锛?
    - 瀹炶瘝锛堝悕璇嶃€佸姩璇嶃€佸舰瀹硅瘝銆佸壇璇嶏級锛氶珮閲嶉煶 (0.7-0.9)
    - 铏氳瘝锛堝啝璇嶃€佷粙璇嶃€佷唬璇嶃€佸姪鍔ㄨ瘝锛夛細浣庨噸闊?(0.2-0.4)
    - 鍙ラ/鍙ュ熬璇嶉€氬父鐣ラ噸
    """
    words = alignment.words
    if not words:
        return
    
    for i, word in enumerate(words):
        clean_word = word.word.lower().strip(".,!?;:\"'")
        
        # 鍩虹鍒ゅ畾锛氬疄璇?vs 铏氳瘝
        if clean_word in FUNCTION_WORDS:
            base_stress = 0.3  # 铏氳瘝 - 寮辫
        else:
            base_stress = 0.8  # 瀹炶瘝 - 閲嶈
        
        # 鍙ラ鍔犳垚
        if i == 0:
            base_stress = min(1.0, base_stress + 0.1)
        
        # 鍙ュ熬鍔犳垚锛堟渶鍚庝竴涓垨鑰呭€掓暟绗簩涓疄璇嶏級
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


def _normalize_index_map(raw_map: dict[int, float]) -> dict[int, float]:
    finite = {idx: float(value) for idx, value in raw_map.items() if math.isfinite(float(value))}
    if not finite:
        return {}
    values = list(finite.values())
    low = min(values)
    high = max(values)
    if high - low <= 1e-6:
        return {idx: 0.5 for idx in finite}
    return {
        idx: _clamp((value - low) / (high - low), 0.0, 1.0)
        for idx, value in finite.items()
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    pos = max(0, min(len(sorted_vals) - 1, round((len(sorted_vals) - 1) * q)))
    return float(sorted_vals[pos])


def _extract_pitch_by_word(
    alignment: Alignment,
    engine_raw: dict[str, Any] | None,
) -> dict[int, float]:
    contour = (engine_raw or {}).get("pitch_contour")
    if not isinstance(contour, list) or not contour:
        return {}

    out: dict[int, float] = {}
    for idx, word in enumerate(alignment.words):
        start = _safe_float(getattr(word, "start", 0.0), 0.0)
        end = _safe_float(getattr(word, "end", 0.0), 0.0)
        if end <= start or getattr(word, "tag", WordTag.OK) == WordTag.MISSING:
            continue
        values = [
            _safe_float(point.get("f", point.get("f0", 0.0)), 0.0)
            for point in contour
            if start <= _safe_float(point.get("t", 0.0), 0.0) <= end
        ]
        values = [value for value in values if value > 50.0]
        if not values:
            continue
        out[idx] = max(values)
    return out


def _extract_energy_by_word(
    alignment: Alignment,
    audio_path: str = "",
) -> dict[int, float]:
    path = Path(str(audio_path or "").strip())
    if not path.exists():
        return {}
    try:
        import librosa
    except Exception:
        return {}

    try:
        y, sr = librosa.load(str(path), sr=16000)
        rms = librosa.feature.rms(y=y, hop_length=512)[0]
        times = librosa.times_like(rms, sr=sr, hop_length=512)
    except Exception as exc:
        logger.warning("Prosody energy extraction failed: %s", exc)
        return {}

    out: dict[int, float] = {}
    for idx, word in enumerate(alignment.words):
        start = _safe_float(getattr(word, "start", 0.0), 0.0)
        end = _safe_float(getattr(word, "end", 0.0), 0.0)
        if end <= start or getattr(word, "tag", WordTag.OK) == WordTag.MISSING:
            continue
        vals = [float(rms[i]) for i, t in enumerate(times) if start <= float(t) <= end]
        if vals:
            out[idx] = max(vals)
    return out


def _compute_word_prominence_scores(
    alignment: Alignment,
    *,
    pitch_by_index: dict[int, float] | None = None,
    energy_by_index: dict[int, float] | None = None,
) -> list[float]:
    duration_raw: dict[int, float] = {}
    for idx, word in enumerate(alignment.words):
        if getattr(word, "tag", WordTag.OK) == WordTag.MISSING:
            continue
        duration_raw[idx] = max(0.05, _safe_float(getattr(word, "end", 0.0), 0.0) - _safe_float(getattr(word, "start", 0.0), 0.0))

    duration_norm = _normalize_index_map(duration_raw)
    pitch_norm = _normalize_index_map(pitch_by_index or {})
    energy_norm = _normalize_index_map(energy_by_index or {})

    out: list[float] = []
    for idx, word in enumerate(alignment.words):
        if getattr(word, "tag", WordTag.OK) == WordTag.MISSING:
            word.prominence_score = 0.0
            out.append(0.0)
            continue

        duration_score = duration_norm.get(idx, 0.0)
        energy_score = energy_norm.get(idx, 0.0)
        pitch_score = pitch_norm.get(idx, 0.0)
        stress_score = _clamp(_safe_float(getattr(word, "stress", 0.0), 0.0), 0.0, 1.0)
        expected_score = _clamp(_safe_float(getattr(word, "expected_stress", 0.5), 0.5), 0.0, 1.0)

        prominence = (
            0.40 * duration_score
            + 0.25 * energy_score
            + 0.20 * pitch_score
            + 0.15 * max(stress_score, expected_score * 0.85)
        )

        token = _normalize_token(getattr(word, "word", ""))
        if token in FUNCTION_WORDS:
            prominence *= 0.86
        elif expected_score >= 0.62:
            prominence = min(1.0, prominence + 0.04)

        word.prominence_score = round(_clamp(prominence, 0.0, 1.0), 3)
        out.append(word.prominence_score)
    return out


def _split_prosody_sentences(
    alignment: Alignment,
    script_text: str,
) -> list[list[WordAlignment]]:
    words = [word for word in alignment.words if str(getattr(word, "word", "")).strip()]
    if len(words) < 4:
        return []

    script_words, puncts = _collect_script_words_and_punctuation(script_text)
    usable = min(len(words), len(script_words)) if script_words else len(words)
    chunks: list[list[WordAlignment]] = []
    current: list[WordAlignment] = []
    for idx, word in enumerate(words[:usable]):
        current.append(word)
        marks = puncts[idx] if idx < len(puncts) else ""
        should_cut = any(mark in marks for mark in ".!?")
        if not should_cut and len(current) >= 10:
            should_cut = True
        if should_cut:
            if len(current) >= 4:
                chunks.append(current)
            current = []
    if len(current) >= 4:
        chunks.append(current)
    if not chunks and len(words) >= 4:
        chunks.append(words[: min(len(words), 10)])
    return chunks


def _build_prosody_intonation_analysis(
    alignment: Alignment,
    script_text: str,
    *,
    engine_raw: dict[str, Any] | None = None,
    pitch_by_index: dict[int, float] | None = None,
    energy_by_index: dict[int, float] | None = None,
    audio_path: str = "",
) -> dict[str, Any] | None:
    if not alignment.words or len(alignment.words) < 4:
        return None

    computed_pitch = pitch_by_index if pitch_by_index is not None else _extract_pitch_by_word(alignment, engine_raw)
    computed_energy = energy_by_index if energy_by_index is not None else _extract_energy_by_word(alignment, audio_path)
    prominence_scores = _compute_word_prominence_scores(
        alignment,
        pitch_by_index=computed_pitch,
        energy_by_index=computed_energy,
    )
    if not prominence_scores:
        return None

    sentence_chunks = _split_prosody_sentences(alignment, script_text)
    if not sentence_chunks:
        return None

    evaluated: list[dict[str, Any]] = []
    for chunk in sentence_chunks:
        values = [float(getattr(word, "prominence_score", 0.0) or 0.0) for word in chunk]
        if len(values) < 4:
            continue
        cutoff = max(0.55, min(0.80, _quantile(values, 0.68)))

        targets = 0
        correct = 0
        issue_words: list[str] = []
        token_views: list[dict[str, Any]] = []
        strong_vals: list[float] = []
        light_vals: list[float] = []
        over_stress = 0

        for word in chunk:
            token = _normalize_token(getattr(word, "word", ""))
            expected = _safe_float(getattr(word, "expected_stress", 0.5), 0.5) >= 0.62
            actual = float(getattr(word, "prominence_score", 0.0) or 0.0) >= cutoff
            tag = str(getattr(word, "tag", WordTag.OK).value if hasattr(getattr(word, "tag", None), "value") else getattr(word, "tag", "ok")).lower()
            blocked = tag in {"missing", "poor"}
            if expected:
                targets += 1
                ok = actual and not blocked
                if ok:
                    correct += 1
                else:
                    issue_words.append(str(getattr(word, "word", "")))
                strong_vals.append(float(getattr(word, "prominence_score", 0.0) or 0.0))
                token_views.append({"word": word.word, "is_stressed": True, "stress_correct": ok})
            else:
                light_vals.append(float(getattr(word, "prominence_score", 0.0) or 0.0))
                over = actual and not blocked and token in FUNCTION_WORDS
                if over:
                    over_stress += 1
                    issue_words.append(str(getattr(word, "word", "")))
                token_views.append({"word": word.word, "is_stressed": False, "stress_correct": not over})

        if targets <= 0:
            continue

        base_accuracy = round((correct / max(1, targets)) * 100)
        contrast = _mean(strong_vals) - _mean(light_vals)
        spread = (max(values) - min(values)) if values else 0.0
        penalty = 0
        if contrast < 0.06:
            penalty += 22
        elif contrast < 0.10:
            penalty += 12
        elif contrast < 0.14:
            penalty += 6
        if spread < 0.05:
            penalty += 8
        penalty += min(18, over_stress * 5)
        score = int(round(_clamp(base_accuracy - penalty, 0.0, 100.0)))

        if issue_words:
            tip = f"重点改进：{' / '.join(list(dict.fromkeys(issue_words))[:3])}。做法：让关键词更突出，连接词更轻一些。"
        elif contrast < 0.12:
            tip = "这句的重弱对比还不够明显。做法：让关键词更突出，连接词更轻一些。"
        else:
            tip = "这句关键词比较突出，重弱分布较自然。继续保持这个节奏。"

        evaluated.append(
            {
                "sentence": " ".join(str(getattr(word, "word", "")).strip() for word in chunk).strip(),
                "words": token_views,
                "stress_accuracy": score,
                "tip": tip,
                "contrast": round(contrast, 3),
            }
        )

    if not evaluated:
        return None

    best = max(evaluated, key=lambda row: int(row.get("stress_accuracy", 0)))
    worst = min(evaluated, key=lambda row: int(row.get("stress_accuracy", 0)))
    best = {
        **best,
        "tip": "这句关键词比较突出，重弱分布较自然。继续保持这个节奏。",
    }
    worst = {
        **worst,
        "tip": str(worst.get("tip") or "这句的重弱对比还不够明显。"),
    }
    return {
        "best_sentence": best,
        "problem_sentences": [worst],
        "method": "prominence_v1",
    }


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


def _estimate_spoken_wpm(words: list[WordAlignment], audio_duration_sec: float = 0.0) -> float:
    spoken = [
        w for w in words
        if (_safe_float(w.end) - _safe_float(w.start)) > 0.02 and w.tag != WordTag.MISSING
    ]
    if len(spoken) < 2:
        return 0.0
    start_t = _safe_float(spoken[0].start)
    end_t = _safe_float(spoken[-1].end)
    span = max(0.2, end_t - start_t)
    span *= _timeline_wpm_scale(span, audio_duration_sec)
    return float(len(spoken) / span * 60.0)


def _pause_target_window(
    pause_type: str | None,
    *,
    wpm: float = 0.0,
    child_mode: bool = True,
) -> tuple[float, float]:
    p = str(pause_type or "").strip().lower()
    if p == "strong":
        base_min, base_max = 0.55, 1.05
    elif p == "medium":
        base_min, base_max = 0.22, 0.58
    elif p == "light":
        base_min, base_max = 0.10, 0.30
    elif p == "none":
        base_min, base_max = 0.00, 0.12
    else:
        base_min, base_max = 0.22, 0.58

    if not child_mode or wpm <= 0.0:
        return base_min, base_max

    # Child speech is naturally less stable in boundary timing.
    # We widen acceptance bands, especially for slower speaking rates.
    if wpm < 105.0:
        min_scale, max_scale = 0.82, 1.30
    elif wpm < 125.0:
        min_scale, max_scale = 0.86, 1.24
    elif wpm < 145.0:
        min_scale, max_scale = 0.90, 1.16
    elif wpm > 185.0:
        min_scale, max_scale = 1.04, 0.95
    else:
        min_scale, max_scale = 1.00, 1.00

    if p == "none":
        min_scale = 1.0
        if wpm < 125.0:
            max_scale = max(max_scale, 1.30)

    t_min = max(0.0, base_min * min_scale)
    t_max = max(t_min + 0.04, base_max * max_scale)
    return round(t_min, 3), round(t_max, 3)


def detect_pauses(alignment: Alignment, script_text: str, engine_raw: dict[str, Any] | None = None) -> None:
    """
    Detect pause boundaries and attach pause diagnostics to each word.
    """
    words = alignment.words
    if not words:
        return

    for w in words:
        w.pause = None

    script_words, script_punct = _collect_script_words_and_punctuation(script_text)
    ref_pause_targets = _build_reference_pause_targets(script_words, engine_raw)
    child_mode = bool(config.get("analysis.pause_diagnostics.child_voice_mode", True))
    audio_duration = _safe_float((engine_raw or {}).get("audio_duration_sec"), 0.0)
    spoken_wpm = _estimate_spoken_wpm(words, audio_duration_sec=audio_duration)
    if child_mode:
        long_slack_for = {"strong": 0.11, "medium": 0.10, "light": 0.09, "none": 0.11}
        short_slack_for = {"strong": 0.12, "medium": 0.10, "light": 0.08, "none": 0.0}
        too_long_min_for = {"strong": 0.16, "medium": 0.14, "light": 0.12, "none": 0.16}
        # Keep too-short diagnostics only for severe sentence-boundary misses in child mode.
        too_short_min_for = {"strong": 0.60, "medium": 999.0, "light": 999.0, "none": 999.0}
    else:
        long_slack_for = {"strong": 0.10, "medium": 0.09, "light": 0.08, "none": 0.10}
        short_slack_for = {"strong": 0.08, "medium": 0.07, "light": 0.06, "none": 0.0}
        too_long_min_for = {"strong": 0.18, "medium": 0.16, "light": 0.12, "none": 0.20}
        too_short_min_for = {"strong": 0.50, "medium": 999.0, "light": 999.0, "none": 999.0}

    punct_target: dict[int, str] = {}
    for idx, marks in enumerate(script_punct):
        if any(ch in marks for ch in ".!?"):
            punct_target[idx] = "strong"
        elif any(ch in marks for ch in ",;:"):
            punct_target[idx] = "medium"

    # Map alignment-word indices to script-word indices.
    # This avoids punctuation drift when ASR inserts/drops words.
    _script_to_align, align_to_script = _build_script_alignment_maps(script_words, alignment)
    mapped_script_idx: list[int | None] = [align_to_script.get(i) for i in range(len(words))]
    prev_mapped: list[int | None] = [None] * len(words)
    next_mapped: list[int | None] = [None] * len(words)
    last_seen: int | None = None
    for i, s_idx in enumerate(mapped_script_idx):
        if s_idx is not None:
            last_seen = s_idx
        prev_mapped[i] = last_seen
    last_seen = None
    for i in range(len(words) - 1, -1, -1):
        s_idx = mapped_script_idx[i]
        if s_idx is not None:
            last_seen = s_idx
        next_mapped[i] = last_seen

    mapping_coverage = sum(1 for s_idx in mapped_script_idx if s_idx is not None) / max(1, len(words))
    low_mapping_coverage = mapping_coverage < 0.35

    def _script_idx_for_alignment_idx(a_idx: int) -> int | None:
        if not (0 <= a_idx < len(words)):
            return None
        direct = mapped_script_idx[a_idx]
        if direct is not None and 0 <= direct < len(script_words):
            return direct

        left = prev_mapped[a_idx]
        right = next_mapped[a_idx]
        if left is not None and right is not None:
            if right - left >= 2:
                guess = left + 1
            else:
                guess = left
            return guess if 0 <= guess < len(script_words) else None
        if left is not None:
            return left if 0 <= left < len(script_words) else None
        if right is not None:
            return right if 0 <= right < len(script_words) else None
        if low_mapping_coverage and 0 <= a_idx < len(script_words):
            return a_idx
        return None

    # Detect synthetic/low-fidelity timelines (e.g. evenly spaced fallback timestamps).
    # On such timelines, do not emit hard good/bad labels.
    gaps: list[float] = []
    for gi in range(len(words) - 1):
        g = max(0.0, float(words[gi + 1].start) - float(words[gi].end))
        gaps.append(g)
    synthetic_timeline = False
    median_gap = 0.0
    gap_std = 0.0
    fixed_gap_ratio = 0.0
    timeline_start = min((_safe_float(w.start) for w in words), default=0.0)
    timeline_end = max((_safe_float(w.end) for w in words), default=0.0)
    timeline_span = max(0.001, timeline_end - timeline_start)
    if gaps:
        sorted_gaps = sorted(gaps)
        median_gap = float(sorted_gaps[len(sorted_gaps) // 2])
        around_median = [g for g in gaps if abs(g - median_gap) <= 0.02]
        fixed_gap_ratio = len(around_median) / max(1, len(gaps))
        mean_gap = sum(gaps) / len(gaps)
        gap_std = math.sqrt(sum((g - mean_gap) ** 2 for g in gaps) / max(1, len(gaps)))
        uniform_gap_signature = (
            len(gaps) >= 6
            and (
                (0.07 <= median_gap <= 0.18 and fixed_gap_ratio >= 0.65 and gap_std <= 0.03)
                or (median_gap <= 0.45 and fixed_gap_ratio >= 0.90 and gap_std <= 0.012)
            )
        )
        compressed_vs_audio = (
            audio_duration > 8.0 and timeline_span < max(6.0, audio_duration * 0.45)
        )
        synthetic_timeline = uniform_gap_signature and (
            compressed_vs_audio or audio_duration <= 0.1 or fixed_gap_ratio >= 0.92
        )

    # Additional low-confidence signal:
    # if most strong boundaries are near-zero gaps, timestamps are likely coarse.
    upper_probe = len(words) - 1
    strong_boundary_count = 0
    strong_zero_gap_count = 0
    for idx in range(max(0, upper_probe)):
        script_idx = _script_idx_for_alignment_idx(idx)
        exp_probe = (
            (ref_pause_targets.get(script_idx) if script_idx is not None else None)
            or (punct_target.get(script_idx) if script_idx is not None else None)
            or "none"
        ).strip().lower()
        if exp_probe != "strong":
            continue
        strong_boundary_count += 1
        gap_probe = float(gaps[idx]) if idx < len(gaps) else 0.0
        if gap_probe <= 0.03:
            strong_zero_gap_count += 1
    if strong_boundary_count >= 6:
        zero_ratio = strong_zero_gap_count / max(1, strong_boundary_count)
        if zero_ratio >= 0.70:
            synthetic_timeline = True

    expected_pause_targets = 0
    practice_focus_words: list[str] = []
    practice_focus_points: list[dict[str, Any]] = []
    boundary_candidates: list[dict[str, Any]] = []
    seen_focus: set[str] = set()
    upper = len(words) - 1
    for idx in range(max(0, upper)):
        script_idx = _script_idx_for_alignment_idx(idx)
        exp = (
            (ref_pause_targets.get(script_idx) if script_idx is not None else None)
            or (punct_target.get(script_idx) if script_idx is not None else None)
            or "none"
        ).strip().lower()
        if exp not in {"strong", "medium", "light", "none"}:
            exp = "medium"
        if exp != "none":
            expected_pause_targets += 1

        left_word = str(words[idx].word if idx < len(words) else "").strip()
        right_word = str(words[idx + 1].word if (idx + 1) < len(words) else "").strip()
        token = _normalize_token(left_word)
        if not token:
            continue

        gap = float(gaps[idx]) if idx < len(gaps) else 0.0
        target_min, target_max = _pause_target_window(
            exp,
            wpm=spoken_wpm,
            child_mode=child_mode,
        )
        long_slack = float(long_slack_for.get(exp, 0.09))
        short_slack = float(short_slack_for.get(exp, 0.05))
        too_long_min = float(too_long_min_for.get(exp, 0.16))
        too_short_min = float(too_short_min_for.get(exp, 0.12))
        if synthetic_timeline:
            # In low-confidence timelines, disable short-pause misses to avoid
            # over-flagging fluent readers based on coarse timestamps.
            too_short_min = 999.0
            too_long_min = max(0.15, too_long_min)
        issue = ""
        adjust_sec = 0.0
        obvious_long = synthetic_timeline and gap >= max(0.85, target_max + 0.10)
        if gap > target_max + long_slack or obvious_long:
            adjust_sec = max(0.0, gap - target_max)
            if adjust_sec >= too_long_min:
                issue = "too_long"
        elif gap < max(0.0, target_min - short_slack):
            adjust_sec = max(0.0, target_min - gap)
            if adjust_sec >= too_short_min:
                issue = "too_short"

        left_score = _safe_float(words[idx].score if idx < len(words) else 100.0, 100.0)
        right_score = _safe_float(words[idx + 1].score if (idx + 1) < len(words) else left_score, left_score)
        boundary_score = round((left_score + right_score) / 2.0, 2)
        boundary_candidates.append(
            {
                "left_word": left_word,
                "right_word": right_word,
                "pause_type": exp,
                "boundary_score": boundary_score,
                "is_content": 0 if (token in FUNCTION_WORDS or len(token) <= 2) else 1,
                "idx": idx,
                "issue": issue,
                "actual_gap": round(gap, 3),
                "target_min": round(target_min, 3),
                "target_max": round(target_max, 3),
                "adjust_sec": round(adjust_sec, 3),
            }
        )

    # Prioritize true issues first, then larger timing deviation.
    issue_priority = {"too_long": 0, "too_short": 1}
    boundary_candidates.sort(
        key=lambda c: (
            -int(bool(c.get("issue"))),
            int(issue_priority.get(str(c.get("issue", "")), 2)),
            -float(c.get("adjust_sec", 0.0)),
            float(c.get("boundary_score", 100.0)),
            -int(c.get("is_content", 0)),
            int(c.get("idx", 0)),
        )
    )
    for cand in boundary_candidates:
        token = _normalize_token(cand.get("left_word", ""))
        if not token or token in seen_focus:
            continue
        seen_focus.add(token)
        if not str(cand.get("issue", "")).strip():
            continue

        practice_focus_points.append(
            {
                "left_word": str(cand.get("left_word", "")),
                "right_word": str(cand.get("right_word", "")),
                "pause_type": str(cand.get("pause_type", "medium")),
                "boundary_score": float(cand.get("boundary_score", 100.0)),
                "idx": int(cand.get("idx", -1)),
                "issue": str(cand.get("issue", "")),
                "actual_gap": float(cand.get("actual_gap", 0.0)),
                "target_min": float(cand.get("target_min", 0.0)),
                "target_max": float(cand.get("target_max", 0.0)),
                "adjust_sec": float(cand.get("adjust_sec", 0.0)),
            }
        )
        if len(practice_focus_points) >= 3:
            break
    practice_focus_words = [str(p.get("left_word", "")).strip() for p in practice_focus_points if str(p.get("left_word", "")).strip()]

    if isinstance(engine_raw, dict):
        pause_profile = engine_raw.get("pause_profile")
        if not isinstance(pause_profile, dict):
            pause_profile = {}
        pause_profile["median_gap"] = round(float(median_gap), 4)
        pause_profile["gap_std"] = round(float(gap_std), 4)
        pause_profile["fixed_gap_ratio"] = round(float(fixed_gap_ratio), 4)
        pause_profile["synthetic_timeline"] = 1.0 if synthetic_timeline else 0.0
        pause_profile["expected_pause_targets"] = float(expected_pause_targets)
        pause_profile["practice_focus_words"] = practice_focus_words
        pause_profile["practice_focus_points"] = practice_focus_points
        if synthetic_timeline:
            pause_profile["timing_confidence"] = "low"
            pause_profile["low_confidence_timing"] = 1.0
        engine_raw["pause_profile"] = pause_profile

    for i in range(len(words)):
        curr_word = words[i]
        if i >= len(words) - 1:
            continue

        next_word = words[i + 1]
        gap = max(0.0, float(next_word.start) - float(curr_word.end))
        duration = round(gap, 2)
        pause_type = None
        issue = ""
        adjust_sec = 0.0

        script_idx = _script_idx_for_alignment_idx(i)
        expected = (
            (ref_pause_targets.get(script_idx) if script_idx is not None else None)
            or (punct_target.get(script_idx) if script_idx is not None else None)
            or "none"
        ).strip().lower()
        if expected not in {"strong", "medium", "light", "none"}:
            expected = "none"
        target_min, target_max = _pause_target_window(
            expected,
            wpm=spoken_wpm,
            child_mode=child_mode,
        )
        long_slack = float(long_slack_for.get(expected, 0.09))
        short_slack = float(short_slack_for.get(expected, 0.05))
        too_long_min = float(too_long_min_for.get(expected, 0.16))
        too_short_min = float(too_short_min_for.get(expected, 0.12))
        if synthetic_timeline:
            # In low-confidence timelines, disable short-pause misses to avoid
            # over-flagging fluent readers based on coarse timestamps.
            too_short_min = 999.0
            too_long_min = max(0.15, too_long_min)

        if expected == "none":
            obvious_long = synthetic_timeline and gap >= max(0.85, target_max + 0.10)
            if gap > target_max + long_slack or obvious_long:
                adjust_sec = max(0.0, gap - target_max)
                if adjust_sec >= too_long_min:
                    if not synthetic_timeline:
                        pause_type = "bad"
                    issue = "too_long"
                elif gap >= 0.22 and not synthetic_timeline:
                    pause_type = "optional"
            elif gap >= 0.22 and not synthetic_timeline:
                pause_type = "optional"
        else:
            obvious_long = synthetic_timeline and gap >= max(0.85, target_max + 0.10)
            if gap > target_max + long_slack or obvious_long:
                adjust_sec = max(0.0, gap - target_max)
                if adjust_sec >= too_long_min:
                    if not synthetic_timeline:
                        pause_type = "bad"
                    issue = "too_long"
                elif not synthetic_timeline:
                    pause_type = "optional"
            elif gap < max(0.0, target_min - short_slack):
                adjust_sec = max(0.0, target_min - gap)
                if adjust_sec >= too_short_min:
                    if not synthetic_timeline:
                        pause_type = "missed"
                    issue = "too_short"
                elif not synthetic_timeline:
                    pause_type = "optional"
            elif not synthetic_timeline:
                pause_type = "good"

        if pause_type:
            curr_word.pause = PauseInfo(
                type=pause_type,
                duration=duration,
                issue=issue,
                target_min=round(target_min, 3),
                target_max=round(target_max, 3),
                adjust_sec=round(adjust_sec, 3),
                expected_type=expected,
            )

def detect_linking(alignment: Alignment) -> None:
    """
    妫€娴嬭繛璇诲苟鏇存柊 Alignment
    
    杩炶瑙勫垯 (鍒濇瀹炵幇)锛?
    1. 鍓嶄竴璇嶇殑缁撴潫涓庡悗涓€璇嶇殑寮€濮嬫湁閲嶅彔锛屾垨闂撮殧鏋佸井灏?(< 0.02s)
    2. 鍚庣画鍙墿灞曢煶绱犵骇瑙勫垯 (C-V)
    """
    words = alignment.words
    if not words:
        return
    
    for i in range(len(words) - 1):
        curr_word = words[i]
        next_word = words[i+1]
        
        # 璁＄畻闂撮殭
        gap = next_word.start - curr_word.end
        
        # 杩炶鍒ゅ畾瑙勫垯锛?
        # 1. 閲嶅彔 (gap < 0)
        # 2. 鏋佸叾寰皬鐨勯棿闅?(gap < 0.03s)
        if gap < 0.03:
            curr_word.is_linked = True


def calculate_pace_trend(
    alignment: Alignment,
    window_size: float = 2.0,
    audio_duration_sec: float = 0.0,
) -> list[PacePoint]:
    """
    鐠侊紕鐣荤拠顓⑩偓鐔荤Ъ閸?(WPM)

    娴ｈ法鏁ゅ鎴濆З缁愭褰涢妴?
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
    time_scale = _timeline_wpm_scale(duration, audio_duration_sec)
    effective_duration = max(1.0, duration * time_scale)
    centers = [
        ((_safe_float(w.start) + _safe_float(w.end)) / 2 - start_time) * time_scale
        for w in spoken_words
    ]

    points = []

    logger.info(
        "Calculating Pace: words=%d raw_duration=%.2fs effective_duration=%.2fs scale=%.3f",
        len(alignment.words),
        duration,
        effective_duration,
        time_scale,
    )

    step = 0.5
    current_t = 0.0
    smooth: list[int] = []

    while current_t <= effective_duration:
        t_start = current_t - window_size / 2
        t_end = current_t + window_size / 2

        t_start_clip = max(0.0, t_start)
        t_end_clip = min(effective_duration, t_end)
        effective_window = max(0.35, t_end_clip - t_start_clip)

        count = sum(1 for c in centers if t_start_clip <= c < t_end_clip)

        wpm = int(round((count / effective_window) * 60))
        wpm = int(_clamp(float(wpm), 20.0, 240.0))
        smooth.append(wpm)
        if len(smooth) >= 3:
            local = smooth[-3:]
            wpm = int(round(sum(local) / len(local)))

        points.append(PacePoint(x=round(current_t, 1), y=wpm))
        current_t += step

    logger.info(f"Pace Points Generated: {len(points)}")
    return points


def analyze_completeness(
    alignment: Alignment, 
    script_text: str, 
    missing_words: list[str]
) -> CompletenessStats:
    """Completeness analysis."""
    FUNCTION_WORDS = {
        "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "by", 
        "is", "are", "was", "were", "be", "been", "has", "have", "had", 
        "and", "or", "but", "so", "as", "if", "that", "it", "this", "that"
    }
    
    # Token count from script text.
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
            
    # Generate tips.
    tips = []
    if key_missed > 0:
        tips.append("Read content words more clearly, especially nouns and verbs.")
    if func_missed > 2:
        tips.append("Do not drop short function words like 'a' and 'the'.")
    if coverage == 100:
        tips.append("Great job. No words were omitted.")
    elif coverage > 90 and key_missed == 0:
        tips.append("Great overall coverage. Only a few function words were missed.")
        
    return CompletenessStats(
        title="Completeness Analysis",
        score_label="Excellent" if coverage > 90 else ("Good" if coverage > 70 else "Needs Work"),
        coverage=coverage,
        missing_stats={
            "total": missing_count,
            "keywords": key_missed,
            "function_words": func_missed
        },
        insight=f"Coverage {coverage}% (missed {key_missed} key word(s))",
        tips=tips
    )


def analyze_hesitations(alignment: Alignment) -> HesitationStats | None:
    """
    鏉╃喓鏋?婵夘偄鍘栫拠宥呭瀻閺?

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
    鎻愬彇鍒嗘暟鏈€浣庣殑璇?
    
    Args:
        alignment: 瀵归綈淇℃伅
        
    Returns:
        寮辫瘝鍒楄〃锛堟寜鍒嗘暟鍗囧簭锛?
    """
    top_n = config.get("analysis.weak_words_top_n", 5)
    ok_threshold = config.get("analysis.word_thresholds.ok", 85)
    
    # 绛涢€夐潪 missing 鐨勪綆鍒嗚瘝
    weak_candidates = [
        (w.word, w.score)
        for w in alignment.words
        if w.tag != WordTag.MISSING and w.score < ok_threshold
    ]
    
    # 鎸夊垎鏁板崌搴忔帓搴忥紝鍙?top N
    weak_candidates.sort(key=lambda x: x[1])
    
    return [word for word, score in weak_candidates[:top_n]]


def extract_weak_phonemes(alignment: Alignment) -> list[str]:
    """
    鎻愬彇鍒嗘暟鏈€浣庣殑闊崇礌
    
    Args:
        alignment: 瀵归綈淇℃伅
        
    Returns:
        寮遍煶绱犲垪琛紙鍘婚噸锛屾寜鍑虹幇棰戠巼鎺掑簭锛?
    """
    top_n = config.get("analysis.weak_phonemes_top_n", 3)
    ok_threshold = config.get("analysis.phoneme_thresholds.ok", 85)
    
    # 鎯呭喌 1锛氬鏋滄湁璇︾粏闊崇礌锛屾寜鍑虹幇棰戠巼鎺掑簭
    if alignment.phonemes:
        weak_phoneme_counts: Counter[str] = Counter()
        for phoneme in alignment.phonemes:
            if phoneme.score < ok_threshold:
                # 鏍囧噯鍖栭煶绱犲悕绉帮紙鍘绘帀鏁板瓧鍚庣紑绛夛級
                phoneme_name = phoneme.phoneme.rstrip("012").upper()
                weak_phoneme_counts[phoneme_name] += 1
        
        # 鍙栧嚭鐜版渶澶氱殑 top N
        most_common = weak_phoneme_counts.most_common(top_n)
        return [phoneme for phoneme, count in most_common]
    
    # 鎯呭喌 2锛氬鏋滄病鏈夎缁嗛煶绱狅紙淇濆簳妯″紡锛夛紝灏濊瘯浠庡急璇嶄腑鍚堟垚闊崇礌寤鸿
    # 杩欐槸涓€绉嶁€滃惎鍙戝紡鈥濆垎鏋愶紝璁╂姤鍛婄湅璧锋潵鏇翠笓涓?
    else:
        # 鑾峰彇鎵€鏈夊急璇嶏紙涓嶉檺浜?top_n锛?
        all_weak = [w.word.lower() for w in alignment.words if w.tag != WordTag.OK and w.tag != WordTag.MISSING]
        
        # 绠€鍗曡鍒欐槧灏?(Spelling -> Phoneme)
        rules = [
            ("th", "胃"),
            ("v", "v"),
            ("r", "r"),
            ("l", "l"),
            ("ng", "艐"),
            ("w", "w"),
            ("ph", "f"),
            ("sh", "蕛"),
            ("ch", "t蕛"),
        ]
        
        synthesized: Counter[str] = Counter()
        for word in all_weak:
            for pattern, ph in rules:
                if pattern in word:
                    synthesized[ph] += 1
        
        # 鍙栧嚭鐜版渶澶氱殑 top N
        most_common = synthesized.most_common(top_n)
        return [ph for ph, count in most_common]


def extract_missing_words(alignment: Alignment, script_text: str) -> list[str]:
    """
    鎻愬彇缂哄け鐨勮瘝
    
    Args:
        alignment: 瀵归綈淇℃伅
        script_text: 鏍囧噯鏂囨湰
        
    Returns:
        缂哄け璇嶅垪琛?
    """
    # 浠庡榻愮粨鏋滀腑鑾峰彇 missing 鏍囩鐨勮瘝
    missing_from_alignment = [
        w.word for w in alignment.words if w.tag == WordTag.MISSING
    ]
    
    # 鍚屾牱浣跨敤姝ｅ垯鍒嗚瘝杩涜瀵规瘮
    script_words = set(
        word.lower()
        for word in re.findall(r"[a-zA-Z']+", script_text)
    )
    
    recognized_words = set(
        w.word.lower() for w in alignment.words if w.tag != WordTag.MISSING
    )
    
    missing_from_comparison = list(script_words - recognized_words)
    
    # 鍚堝苟鍘婚噸
    all_missing = list(set(missing_from_alignment + missing_from_comparison))
    
    return all_missing


def extract_confusions(engine_raw: dict[str, Any]) -> list[Confusion]:
    """
    鎻愬彇闊崇礌娣锋穯淇℃伅
    
    鏌愪簺寮曟搸浼氳緭鍑烘贩娣嗙煩闃碉紝鏍囪瘑瀛︾敓鎶婂摢涓煶绱犲彂鎴愪簡鍝釜銆?
    
    Args:
        engine_raw: 寮曟搸鍘熷杈撳嚭
        
    Returns:
        娣锋穯鍒楄〃
    """
    top_n = config.get("analysis.confusions_top_n", 2)
    
    confusions: list[Confusion] = []
    
    # 妫€鏌ュ紩鎿庢槸鍚︽彁渚涗簡娣锋穯淇℃伅
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
    
    # 鎸?count 闄嶅簭鎺掑簭锛屽彇 top N
    confusions.sort(key=lambda x: x.count, reverse=True)
    
    return confusions[:top_n]


def detect_mistakes(alignment: Alignment, engine_raw: dict[str, Any]) -> list[dict[str, Any]]:
    """
    妫€娴嬪叿浣撶殑閿欒妯″紡锛岀敓鎴愯缁嗘弿杩般€?
    """
    mistakes = []
    
    # 1. 妫€娴?AI 璇箟鍐茬獊 (鏉ヨ嚜 ProEngine)
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

    # 2. 妫€娴嬫紡璇?(Missing)
    for word in alignment.words:
        if word.tag == WordTag.MISSING:
            mistakes.append({
                "type": "missing",
                "target": word.word,
                "desc": "Forgot to pronounce",
                "severity": "high"
            })

    # 3. 妫€娴嬮煶绱犵骇鏄捐憲閿欒 (GOP Evidence)
    for word in alignment.words:
        # 鍗充娇 WordTag 鏄?OK锛屽鏋滄湁闊崇礌鍒嗘瀬浣庯紝涔熻鎸戝嚭鏉?
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
    涓哄榻愮粨鏋滃垎閰嶆爣绛?
    
    鏍规嵁鍒嗘暟闃堝€间负 words 鍜?phonemes 鍒嗛厤 ok/weak/poor 鏍囩銆?
    
    Args:
        alignment: 瀵归綈淇℃伅锛堜細琚師鍦颁慨鏀癸級
    """
    word_ok = config.get("analysis.word_thresholds.ok", 70)
    word_weak = config.get("analysis.word_thresholds.weak", 40)
    phoneme_ok = config.get("analysis.phoneme_thresholds.ok", 70)

    # Default threshold; may be relaxed for long text sessions.
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
    
    # Assign word tags.
    for word in alignment.words:
        if word.tag == WordTag.MISSING:
            continue
        elif float(word.score or 0) >= adaptive_word_ok:
            word.tag = WordTag.OK
        elif word.score >= word_weak:
            word.tag = WordTag.WEAK
        else:
            word.tag = WordTag.POOR
    
    # 鍒嗛厤闊崇礌鏍囩
    for phoneme in alignment.phonemes:
        if phoneme.score >= phoneme_ok:
            phoneme.tag = PhonemeTag.OK
        else:
            phoneme.tag = PhonemeTag.WEAK

