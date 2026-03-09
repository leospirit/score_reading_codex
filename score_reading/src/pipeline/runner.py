
import json
import logging
import re
import tempfile
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional, Tuple

from src.advice.generator import generate_feedback
from src.analysis.llm_advisor import get_llm_advisor
from src.models import (
    EngineMode,
    Meta,
    ScoringResult,
    WordTag,
)
from src.pipeline.analyze import analyze_results, assign_tags
from src.pipeline.normalize import normalize_scores
from src.pipeline.engines.whisper_engine import WhisperEngine
from src.pipeline.phoneme_fallback import ensure_dense_phoneme_alignment
from src.pipeline.preprocess import preprocess_audio
from src.pipeline.router import run_with_fallback
from src.pipeline.script_reference import ensure_script_reference_async
from src.report.render_html import render_html_report

logger = logging.getLogger("score_reading")

def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _normalize_script_for_scoring(text: str) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    # Ensure punctuation boundaries don't merge adjacent words (e.g. "school.Where").
    normalized = re.sub(r"([,.;:!?])([A-Za-z'])", r"\1 \2", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n[ \t]+", "\n", normalized)
    return normalized.strip()


def _normalize_word_token(text: str) -> str:
    return re.sub(r"[^a-z']+", "", str(text or "").lower())


def _is_generic_improvement(text: str) -> bool:
    t = re.sub(r"\s+", "", str(text or "").lower())
    if not t:
        return True
    generic_markers = [
        "slowread",
        "repeat",
        "slowdown",
        "finishwordendings",
        "stressedvowels",
    ]
    hits = sum(1 for m in generic_markers if m in t)
    return hits >= 2 or len(t) < 10


def _diagnosis_issue_kind(diagnosis: str) -> str:
    d = str(diagnosis or "").lower()

    ending_tokens = (
        "omission", "dropped", "missing ending", "ending sound", "word ending",
        "final consonant", "plural ending", "not pronounced", "missing /s", "missing /z",
        "final /s", "final /z",
    )
    stress_tokens = ("stress", "intonation", "prosody", "emphasis")
    linking_tokens = ("link", "linking", "break", "pause", "hesitation", "disfluency")
    consonant_tokens = ("consonant", "th sound", "initial sound", "final sound")
    vowel_tokens = ("vowel", "diphthong", "schwa", "unstressed vowel", "ai", "ei")

    if any(t in d for t in ending_tokens):
        return "ending"
    if any(t in d for t in stress_tokens):
        return "stress"
    if any(t in d for t in linking_tokens):
        return "linking"
    if any(t in d for t in consonant_tokens):
        return "consonant"
    if any(t in d for t in vowel_tokens):
        return "vowel"
    return "general"


def _creative_improvement_for_word(word_text: str, diagnosis: str) -> str:
    # Kept function name for compatibility; output is concise and actionable.
    w = str(word_text or "").strip() or "this word"
    d = str(diagnosis or "")
    issue = _diagnosis_issue_kind(d)
    seed = sum(ord(c) for c in (w.lower() + "|" + d.lower()))

    templates = {
        "ending": [
            f'"{w}" final sound: slow x3, then sentence x3.',
            f'Keep "{w}" ending clear; compare two recordings.',
        ],
        "vowel": [
            f'For "{w}", hold the key vowel clearly, then speed up.',
            f'Read "{w}" slowly x3, then back to full sentence.',
        ],
        "consonant": [
            f'Anchor consonants in "{w}" first, then connect naturally.',
            f'Practice "{w}" in isolation x3 before sentence reading.',
        ],
        "stress": [
            f'Put stress on the key syllable in "{w}" and keep rhythm.',
            f'Clap rhythm once, then read "{w}" inside the sentence.',
        ],
        "linking": [
            f'Avoid break around "{w}"; read phrase in one breath.',
            f'Connect words around "{w}" smoothly, no extra pause.',
        ],
        "general": [
            f'Read "{w}" slowly x3, then sentence x3.',
            f'Keep "{w}" clear first, then return to natural speed.',
        ],
    }

    options = templates.get(issue, templates["general"])
    return options[seed % len(options)]


def _personalize_top_errors_from_alignment(result: ScoringResult) -> None:
    """
    Make pronunciation top-errors student-specific.
    """
    if not result or not result.alignment or not result.alignment.words:
        return

    stop_words = {
        "lily", "today", "you", "your", "i", "we", "the", "a", "an",
        "is", "are", "to", "of", "in", "and", "it", "my", "me",
    }

    advisor = result.advisor_feedback if isinstance(result.advisor_feedback, dict) else {}
    existing = advisor.get("top_errors")
    existing_list = existing if isinstance(existing, list) else []

    ranked_words = sorted(
        [w for w in result.alignment.words if str(getattr(w, "word", "")).strip()],
        key=lambda w: float(getattr(w, "score", 100.0)),
    )
    low_tokens: set[str] = set()
    for w in ranked_words:
        token = _normalize_word_token(getattr(w, "word", ""))
        if not token or token in stop_words:
            continue
        if float(getattr(w, "score", 100.0)) <= 82 or str(getattr(w, "diagnosis", "")).strip():
            low_tokens.add(token)
        if len(low_tokens) >= 8:
            break

    def _clean_words(raw_words: Any) -> list[str]:
        out: list[str] = []
        if not isinstance(raw_words, list):
            return out
        for item in raw_words:
            if isinstance(item, dict):
                text = str(item.get("text", "")).strip()
            else:
                text = str(item).strip()
            if text:
                out.append(text)
        return out

    kept: list[dict[str, Any]] = []
    used_tokens: set[str] = set()
    for row in existing_list:
        if not isinstance(row, dict):
            continue
        words = _clean_words(row.get("words"))
        matched = False
        for w in words:
            tok = _normalize_word_token(w)
            if tok and tok in low_tokens:
                matched = True
                used_tokens.add(tok)
        if not matched:
            continue
        kept.append(
            {
                "phoneme": str(row.get("phoneme", "") or "Key Sound").strip(),
                "type": str(row.get("type", "") or "word").strip(),
                "description": str(row.get("description", "") or "Detected pronunciation instability on key words.").split("|")[0].strip(),
                "words": words[:4],
                "improvement": _creative_improvement_for_word(
                    words[0] if words else "",
                    str(row.get("description", "") or ""),
                ),
            }
        )
        if len(kept) >= 3:
            break

    generated: list[dict[str, Any]] = []
    for w in ranked_words:
        word_text = str(getattr(w, "word", "") or "").strip()
        token = _normalize_word_token(word_text)
        if not token or token in stop_words or token in used_tokens:
            continue

        score = float(getattr(w, "score", 100.0))
        diagnosis = str(getattr(w, "diagnosis", "") or "").strip()
        if score > 82 and not diagnosis:
            continue

        phoneme = "Key Sound"
        m = re.search(r"/([^/]{1,12})/", diagnosis)
        if m:
            phoneme = f"/{m.group(1)}/"

        description = diagnosis.split("|")[0].strip() if diagnosis else ""
        if not description:
            description = "Pronunciation is not stable on this word."

        improvement = _creative_improvement_for_word(word_text, diagnosis)

        generated.append(
            {
                "phoneme": phoneme,
                "type": "word",
                "description": description,
                "words": [word_text],
                "improvement": improvement,
            }
        )
        used_tokens.add(token)
        if len(generated) >= 3:
            break

    personalized = (kept + generated)[:3]
    if not personalized:
        return

    advisor["top_errors"] = personalized
    result.advisor_feedback = advisor

    if isinstance(result.engine_raw, dict):
        integrated = result.engine_raw.get("integrated_feedback")
        if isinstance(integrated, dict):
            integrated["top_errors"] = personalized






def _extract_display_name(student_id: str) -> str:
    raw = str(student_id or "").strip()
    default_name = "".join([chr(0x540C), chr(0x5B66)])
    if not raw:
        return default_name

    stem = Path(raw).stem
    stem = re.sub(r"(_v|_new)\d+$", "", stem, flags=re.IGNORECASE)

    def _is_cjk(ch: str) -> bool:
        code = ord(ch)
        return 0x4E00 <= code <= 0x9FFF

    leading = []
    for ch in stem:
        if _is_cjk(ch):
            leading.append(ch)
            continue
        if leading:
            break

    if len(leading) >= 2:
        return "".join(leading[:4])

    parts = [part for part in re.split(r"[_\-\s]+", stem) if part]
    for part in parts:
        run = []
        for ch in part:
            if _is_cjk(ch):
                run.append(ch)
            elif run:
                break
        if len(run) >= 2:
            return "".join(run[:4])

    if any(_is_cjk(ch) for ch in stem):
        only_cn = "".join(ch for ch in stem if _is_cjk(ch))
        if len(only_cn) >= 2:
            return only_cn[:4]
        if only_cn:
            return only_cn

    fallback = (parts[0] if parts else stem)[:16]
    return fallback or default_name


def _pick_focus_word(result: ScoringResult) -> str:
    advisor = result.advisor_feedback if isinstance(result.advisor_feedback, dict) else {}
    top_errors = advisor.get("top_errors")
    if isinstance(top_errors, list):
        for item in top_errors:
            if isinstance(item, dict):
                w = str(item.get("word", "")).strip()
                if not w:
                    words = item.get("words")
                    if isinstance(words, list) and words:
                        w = str(words[0]).strip()
            else:
                w = str(item).strip()
            if w:
                return w

    details = advisor.get("word_details")
    if isinstance(details, list):
        low = []
        for d in details:
            if not isinstance(d, dict):
                continue
            w = str(d.get("word", "")).strip()
            s = _safe_float(d.get("score"), 100.0)
            diagnosis = str(d.get("diagnosis", "") or "").strip()
            if w and s < 78 and diagnosis:
                low.append((s, w))
        if low:
            low.sort(key=lambda x: x[0])
            return low[0][1]

    weak_words = getattr(result.analysis, "weak_words", []) if result.analysis else []
    stop_words = {"lily", "today", "you", "your", "i", "we", "the", "a", "an"}
    if isinstance(weak_words, list):
        for w in weak_words:
            t = str(w).strip()
            if not t:
                continue
            key = re.sub(r"[^a-z']+", "", t.lower())
            if key and key not in stop_words:
                return t

    return ""


def _normalize_cn_sentence(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    s = re.sub(r"\s+", "", s)
    if s[-1] not in "\u3002\uff01\uff1f!?":
        s += "\u3002"
    return s


def _feedback_source_tag(result: ScoringResult) -> str:
    raw = result.engine_raw if isinstance(result.engine_raw, dict) else {}
    source = str(raw.get("source", "")).lower()
    annotation_source = str(raw.get("annotation_source", "")).lower()
    integrated = raw.get("integrated_feedback")
    has_integrated = isinstance(integrated, dict) and str(integrated.get("overall_comment", "")).strip()
    integrated_provider = str((integrated or {}).get("provider", "")).strip().lower() if isinstance(integrated, dict) else ""
    advisor = result.advisor_feedback if isinstance(result.advisor_feedback, dict) else {}
    advisor_provider = str(advisor.get("_advisor_provider") or advisor.get("provider") or "").strip().lower()

    llm_provider_markers = {"gemini", "zhipu", "llm_primary", "zhipu_primary"}
    volcengine_markers = {"volcengine", "ark", "doubao"}
    if integrated_provider in volcengine_markers or advisor_provider in volcengine_markers:
        return "db"
    if integrated_provider in llm_provider_markers or advisor_provider in llm_provider_markers:
        return "ge"
    if integrated_provider in {"azure_fallback", "azure"} or advisor_provider in {"azure_fallback", "azure"}:
        return "az"

    if has_integrated:
        if "gemini" in source:
            return "ge"
        if "azure" in source:
            return "ge" if annotation_source == "gemini" else "az"
    if "gemini" in source:
        return "ge"
    if "azure" in source:
        return "az"
    return "az"


def _with_feedback_tag(text: str, tag: str) -> str:
    t = str(text or "").strip()
    normalized = str(tag or "").strip().lower()
    if normalized == "db":
        normalized_tag = "db"
    elif normalized == "ge":
        normalized_tag = "ge"
    else:
        normalized_tag = "az"
    if not t:
        return f"[{normalized_tag}]"
    t = re.sub(r"^\[(?:ge|az|db)\]\s*", "", t, flags=re.IGNORECASE)
    return f"[{normalized_tag}] {t}"


def _advisor_specific_suggestions(advisor_payload: dict[str, Any]) -> list[str]:
    suggestions: list[str] = []
    raw = advisor_payload.get("specific_suggestions")
    if isinstance(raw, list):
        suggestions.extend(str(item or "").strip() for item in raw if str(item or "").strip())
    if suggestions:
        return suggestions

    specific_feedback = advisor_payload.get("specific_feedback")
    if isinstance(specific_feedback, list):
        for row in specific_feedback:
            if not isinstance(row, dict):
                continue
            text = str(row.get("suggestion", "") or "").strip()
            if text:
                suggestions.append(text)
    return suggestions


def _strip_score_mentions(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    # Remove score tokens like "（98分）" / "(98.5分)".
    s = re.sub(r"[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]", "", s)
    # Remove "得分98分" while keeping sentence flow.
    s = re.sub(r"得分\s*\d+(?:\.\d+)?\s*分", "表现", s)
    # Remove English score labels if they leak into final Chinese feedback.
    s = re.sub(r"\bscore\s*[:：]?\s*\d+(?:\.\d+)?\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s{2,}", " ", s).strip(" ，,。;；")
    return s


def _build_integrated_feedback_from_advisor(
    advisor_payload: dict[str, Any] | None,
    *,
    provider_default: str = "azure_fallback",
) -> dict[str, Any]:
    payload = advisor_payload if isinstance(advisor_payload, dict) else {}
    provider = str(payload.get("_advisor_provider") or payload.get("provider") or provider_default).strip() or provider_default
    chain = payload.get("_advisor_chain")
    if not isinstance(chain, list):
        chain = []

    overall_comment = _strip_score_mentions(str(payload.get("overall_comment", "") or "").strip())
    suggestions = [_strip_score_mentions(item) for item in _advisor_specific_suggestions(payload)]
    suggestions = [item for item in suggestions if item]
    practice_raw = payload.get("practice_tips")
    practice_tips = (
        [str(item or "").strip() for item in practice_raw if str(item or "").strip()]
        if isinstance(practice_raw, list)
        else []
    )

    out = {
        "overall_comment": overall_comment,
        "specific_suggestions": suggestions,
        "practice_tips": practice_tips,
        "provider": provider,
    }
    if chain:
        out["provider_chain"] = chain
    advisor_errors = payload.get("_advisor_errors")
    if isinstance(advisor_errors, list) and advisor_errors:
        out["advisor_errors"] = [str(item or "").strip() for item in advisor_errors if str(item or "").strip()]
    if payload.get("top_errors") is not None:
        out["top_errors"] = payload.get("top_errors")
    return out


def _strip_feedback_salutation(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    s = re.sub(r"^\[(?:ge|az)\]\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^\s*亲爱的[^，。!?！？]{0,24}同学[，,]?\s*", "", s, count=1)
    s = re.sub(r"^\s*(?:同学|小朋友)[，,、:：]?\s*", "", s, count=1)
    return s.strip(" ，,。:：")


def _split_feedback_sentences(text: str) -> list[str]:
    clean = re.sub(r"\s+", "", str(text or "")).strip()
    if not clean:
        return []
    clean = re.sub(r"^\[(?:ge|az)\]\s*", "", clean, flags=re.IGNORECASE)
    chunks = [c.strip() for c in re.split(r"[。！？!?]+", clean) if c.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        normalized = _strip_feedback_salutation(chunk)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _is_praise_line(text: str) -> bool:
    s = str(text or "")
    praise_markers = (
        "进步",
        "不错",
        "较好",
        "稳定",
        "清晰",
        "流畅",
        "自然",
        "准确",
        "表现",
        "很棒",
        "有提升",
    )
    return any(marker in s for marker in praise_markers)


def _is_issue_line(text: str) -> bool:
    s = str(text or "")
    issue_markers = ("关键问题", "需要改进", "要注意", "问题在", "不足", "不稳定", "仍需", "偏重", "不够", "but")
    return any(marker in s for marker in issue_markers)


def _is_action_line(text: str) -> bool:
    s = str(text or "")
    action_markers = ("建议", "练习", "慢读", "连读", "回听", "自检", "跟读", "重复")
    return any(marker in s for marker in action_markers)


def _is_low_value_issue_line(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True

    low_value_markers = (
        "建议重点练习以下发音",
        "重点练习以下发音",
        "以下发音",
    )
    if any(marker in s for marker in low_value_markers):
        tail = s
        if ":" in s:
            tail = s.split(":", 1)[1].strip()
        if "：" in tail:
            tail = tail.split("：", 1)[1].strip()
        if re.fullmatch(r"[A-Z]{1,4}(?:\s*,\s*[A-Z]{1,4})*", tail):
            return True

    # Pure phoneme-code fragments are not student-facing issues.
    if re.fullmatch(r"[A-Z]{1,4}(?:\s*,\s*[A-Z]{1,4})*", s):
        return True

    return False


def _is_low_value_action_line(text: str, focus_word: str = "") -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if any(marker in s for marker in ("最核心需要改进", "关键问题", "需要改进")):
        return True

    low_value_markers = (
        "卷舌动作",
        "r...r...r",
        "r…r…r",
        "舌尖向上卷",
        "嘴唇略微圆",
        "但不触碰任何部位",
    )
    if any(marker in s for marker in low_value_markers):
        return True

    # Skip very generic action lines that are not tied to the current focus word.
    if focus_word:
        focus = str(focus_word).strip().lower()
        action_like = any(k in s for k in ("慢读", "连读", "建议", "练习"))
        if action_like and focus and focus not in s.lower():
            if "该词" not in s and "这个词" not in s:
                return True
    return False


def _extract_focus_word_from_text(text: str) -> str:
    s = str(text or "")
    m = re.search(r"[“\"]([A-Za-z']{1,32})[”\"]", s)
    if m:
        return m.group(1)
    return ""


def _strip_embedded_action_from_issue(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    parts = re.split(r"(?:建议|练习|请先|可以先)", s, maxsplit=1)
    head = parts[0].strip(" ，,。:：")
    return head or s


def _build_issue_and_action(result: ScoringResult) -> tuple[str, str]:
    p_score = _safe_float(getattr(result.scores, "pronunciation_100", 0.0), 0.0)
    f_score = _safe_float(getattr(result.scores, "fluency_100", 0.0), 0.0)
    i_score = _safe_float(getattr(result.scores, "intonation_100", 0.0), 0.0)
    c_score = _safe_float(getattr(result.scores, "completeness_100", 0.0), 0.0)
    metric_pairs = [
        ("\u53d1\u97f3", p_score),
        ("\u6d41\u5229\u5ea6", f_score),
        ("\u8bed\u8c03", i_score),
        ("\u5b8c\u6574\u5ea6", c_score),
    ]
    weakest_metric, _weakest_score = sorted(metric_pairs, key=lambda x: x[1])[0]

    focus_word = _pick_focus_word(result)
    if focus_word:
        issue = _normalize_cn_sentence(
            f"\u5173\u952e\u95ee\u9898\u662f\u201c{focus_word}\u201d\u7684\u53d1\u97f3\u7a33\u5b9a\u6027\u4e0d\u8db3"
        )
        action = _normalize_cn_sentence(
            f"\u5efa\u8bae\uff1a\u628a\u201c{focus_word}\u201d\u6162\u8bfb3\u904d\uff0c\u518d\u653e\u56de\u539f\u53e5\u8fde\u8bfb3\u904d\uff0c\u6bcf\u6b21\u5f55\u97f3\u56de\u542c\u81ea\u68c0"
        )
        return issue, action

    issue = _normalize_cn_sentence(
        f"\u5173\u952e\u95ee\u9898\u5728{weakest_metric}\uff0c\u8fd8\u6709\u63d0\u5347\u7a7a\u95f4"
    )
    if weakest_metric == "\u6d41\u5229\u5ea6":
        action = _normalize_cn_sentence(
            "\u5efa\u8bae\uff1a\u6bcf\u53e5\u5148\u6162\u8bfb1\u904d\uff0c\u518d\u6309\u81ea\u7136\u8bed\u901f\u8bfb1\u904d\uff0c\u4e2d\u95f4\u4e0d\u8981\u65ad\u53e5"
        )
    elif weakest_metric == "\u5b8c\u6574\u5ea6":
        action = _normalize_cn_sentence(
            "\u5efa\u8bae\uff1a\u8ddf\u7740\u6587\u672c\u9010\u53e5\u8bfb\uff0c\u6bcf\u53e5\u8bfb\u5b8c\u7acb\u5373\u81ea\u67e5\u51a0\u8bcd\u548c\u8fde\u8bcd\u662f\u5426\u6f0f\u8bfb"
        )
    elif weakest_metric == "\u8bed\u8c03":
        action = _normalize_cn_sentence(
            "\u5efa\u8bae\uff1a\u5148\u6807\u51fa\u91cd\u8bfb\u8bcd\uff0c\u518d\u8fdb\u884c\u8ddf\u8bfb\uff0c\u8bfb\u51fa\u53e5\u5b50\u8d77\u4f0f"
        )
    else:
        action = _normalize_cn_sentence(
            "\u5efa\u8bae\uff1a\u805a\u7126\u6613\u9519\u8bcd\u505a\u201c\u6162\u8bfb3\u904d+\u539f\u53e5\u8fde\u8bfb3\u904d\u201d\u5faa\u73af\uff0c\u6bcf\u6b21\u5f55\u97f3\u5bf9\u6bd4"
        )
    return issue, action


def _build_fact_praise(result: ScoringResult) -> str:
    p_score = _safe_float(getattr(result.scores, "pronunciation_100", 0.0), 0.0)
    f_score = _safe_float(getattr(result.scores, "fluency_100", 0.0), 0.0)
    i_score = _safe_float(getattr(result.scores, "intonation_100", 0.0), 0.0)
    c_score = _safe_float(getattr(result.scores, "completeness_100", 0.0), 0.0)
    metric_pairs = [
        ("\u53d1\u97f3", p_score),
        ("\u6d41\u5229\u5ea6", f_score),
        ("\u8bed\u8c03", i_score),
        ("\u5b8c\u6574\u5ea6", c_score),
    ]
    best_metric, best_score = sorted(metric_pairs, key=lambda x: x[1], reverse=True)[0]
    if best_metric == "\u5b8c\u6574\u5ea6" and best_score >= 95.0:
        return _normalize_cn_sentence(
            "\u4f60\u8fd9\u6b21\u6717\u8bfb\u4e2d\u5b8c\u6574\u5ea6\u8868\u73b0\u66f4\u7a81\u51fa\uff0c\u662f\u6700\u7a33\u5b9a\u7684\u4e00\u9879"
        )
    return _normalize_cn_sentence(
        f"\u4f60\u8fd9\u6b21\u6717\u8bfb\u4e2d{best_metric}\u8868\u73b0\u7a33\u5b9a\uff0c\u662f\u6700\u7a81\u51fa\u7684\u4e00\u9879"
    )


def _enforce_feedback_style(result: ScoringResult) -> None:
    # Keep Gemini wording when available and only enforce a compact structure:
    # 1-2 praise lines + 1 issue line + 1 actionable suggestion line.
    if result.feedback is None:
        return

    student_id = result.meta.student_id if result.meta else ""
    nickname = _extract_display_name(student_id)
    salutation = f"\u4eb2\u7231\u7684{nickname}\u540c\u5b66"

    integrated = (result.engine_raw or {}).get("integrated_feedback")
    advisor = result.advisor_feedback if isinstance(result.advisor_feedback, dict) else {}

    summary_candidates: list[str] = []
    if isinstance(integrated, dict):
        summary_candidates.append(str(integrated.get("overall_comment", "")).strip())
    summary_candidates.append(str(getattr(result.feedback, "cn_summary", "") or "").strip())
    summary_candidates.append(str(advisor.get("overall_comment", "")).strip())

    summary_sentences: list[str] = []
    for block in summary_candidates:
        summary_sentences.extend(_split_feedback_sentences(_strip_score_mentions(block)))

    issue_fallback, action_fallback = _build_issue_and_action(result)
    issue_fallback = _strip_feedback_salutation(issue_fallback)
    action_fallback = _strip_feedback_salutation(action_fallback)
    issue_fallback = _strip_score_mentions(issue_fallback)
    action_fallback = _strip_score_mentions(action_fallback)

    praise_lines = [s for s in summary_sentences if _is_praise_line(s)][:2]
    if not praise_lines:
        praise_lines = [_strip_feedback_salutation(_build_fact_praise(result))]

    issue_line = next((s for s in summary_sentences if _is_issue_line(s) and not _is_low_value_issue_line(s)), "")
    if not issue_line and summary_sentences:
        # If no explicit issue marker exists, pick the last meaningful sentence.
        for candidate in reversed(summary_sentences):
            if not _is_low_value_issue_line(candidate):
                issue_line = candidate
                break
    raw_issue_line = _strip_score_mentions(_strip_feedback_salutation(issue_line)) or issue_fallback
    issue_focus_word = _extract_focus_word_from_text(raw_issue_line)
    issue_line = _strip_embedded_action_from_issue(raw_issue_line) or issue_fallback
    if issue_focus_word:
        action_fallback = _normalize_cn_sentence(
            f"\u5efa\u8bae\uff1a\u628a\u201c{issue_focus_word}\u201d\u6162\u8bfb3\u904d\uff0c\u518d\u653e\u56de\u539f\u53e5\u8fde\u8bfb3\u904d\uff0c\u6bcf\u6b21\u5f55\u97f3\u56de\u542c\u81ea\u68c0"
        )

    action_candidates: list[str] = []
    if isinstance(integrated, dict):
        raw = integrated.get("specific_suggestions")
        if isinstance(raw, list):
            action_candidates.extend(str(item or "").strip() for item in raw)
    raw_actions = getattr(result.feedback, "cn_actions", []) or []
    if isinstance(raw_actions, list):
        action_candidates.extend(str(item or "").strip() for item in raw_actions)
    raw_advisor_actions = advisor.get("specific_suggestions")
    if isinstance(raw_advisor_actions, list):
        action_candidates.extend(str(item or "").strip() for item in raw_advisor_actions)
    action_candidates.extend(s for s in summary_sentences if _is_action_line(s))

    focus_word = issue_focus_word or _pick_focus_word(result)
    action_line = ""
    for item in action_candidates:
        clean = _strip_score_mentions(_strip_feedback_salutation(item))
        if not clean:
            continue
        if _is_low_value_action_line(clean, focus_word=focus_word):
            continue
        action_line = clean
        if _is_action_line(clean):
            break
    if not action_line:
        action_line = action_fallback
    elif action_line in raw_issue_line:
        action_line = action_fallback
    if not _is_action_line(action_line):
        action_line = f"\u5efa\u8bae\uff1a{action_line}"

    summary_lines = praise_lines[:2] + [issue_line]
    normalized_summary_lines: list[str] = []
    for line in summary_lines:
        normalized = _normalize_cn_sentence(_strip_score_mentions(_strip_feedback_salutation(line)))
        if normalized and normalized not in normalized_summary_lines:
            normalized_summary_lines.append(normalized)
    if not normalized_summary_lines:
        normalized_summary_lines = [
            _normalize_cn_sentence(_build_fact_praise(result)),
            _normalize_cn_sentence(issue_fallback),
        ]
    summary = _strip_score_mentions(f"{salutation}\uff0c{''.join(normalized_summary_lines)}")
    feedback_tag = _feedback_source_tag(result)
    summary = _with_feedback_tag(summary, feedback_tag)

    action_line = _normalize_cn_sentence(_strip_score_mentions(action_line))
    result.feedback.cn_summary = summary
    result.feedback.cn_actions = [action_line]
    result.feedback.practice = []

    if advisor:
        advisor["overall_comment"] = summary
        advisor["specific_suggestions"] = result.feedback.cn_actions
        advisor["practice_tips"] = []
        result.advisor_feedback = advisor

    if isinstance(integrated, dict):
        integrated["overall_comment"] = summary
        integrated["specific_suggestions"] = result.feedback.cn_actions
        integrated["practice_tips"] = []

    if isinstance(result.engine_raw, dict):
        result.engine_raw["feedback_source_tag"] = feedback_tag
        if not isinstance(result.engine_raw.get("integrated_feedback"), dict):
            result.engine_raw["integrated_feedback"] = {
                "overall_comment": summary,
                "specific_suggestions": list(result.feedback.cn_actions),
                "practice_tips": [],
            }


def _expand_alignment_tokens_for_mapping(words: list[Any]) -> tuple[list[str], list[int]]:
    tokens: list[str] = []
    parents: list[int] = []
    for idx, word in enumerate(words):
        raw = str(getattr(word, "word", "") or "")
        parts = re.findall(r"[A-Za-z']+", raw)
        if not parts:
            token = _normalize_word_token(raw)
            if token:
                parts = [token]
        for part in parts:
            token = _normalize_word_token(part)
            if not token:
                continue
            tokens.append(token)
            parents.append(idx)
    return tokens, parents


def _map_script_indices_to_alignment_indices(
    script_tokens: list[str],
    words: list[Any],
    script_indices: list[int],
) -> set[int]:
    if not script_tokens or not words or not script_indices:
        return set()

    script_norm = [_normalize_word_token(t) for t in script_tokens]
    align_tokens, align_parents = _expand_alignment_tokens_for_mapping(words)
    if not align_tokens:
        return {
            idx for idx in script_indices
            if isinstance(idx, int) and 0 <= idx < len(words)
        }

    mapped: set[int] = set()
    matcher = SequenceMatcher(None, script_norm, align_tokens)
    script_to_align: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                script_to_align[i1 + k] = align_parents[j1 + k]
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
            script_to_align[i1 + k] = align_parents[j1 + rel]

    for raw_idx in script_indices:
        try:
            idx = int(raw_idx)
        except (TypeError, ValueError):
            continue
        mapped_idx = script_to_align.get(idx)
        if mapped_idx is not None and 0 <= mapped_idx < len(words):
            mapped.add(mapped_idx)
        elif 0 <= idx < len(words):
            mapped.add(idx)
    return mapped


def _collect_low_factor_word_indices(
    words: list[Any],
    phonemes: list[Any],
    floor: float,
) -> set[int]:
    """
    Collect low-score phoneme evidence at word-instance level.
    Avoid token-level bleed across repeated words (e.g. one "going" affecting all "going").
    """
    if not words or not phonemes:
        return set()

    indices: set[int] = set()
    token_slots: dict[str, list[int]] = {}
    for wi, w in enumerate(words):
        if getattr(w, "tag", None) == WordTag.MISSING:
            continue
        token = _normalize_word_token(getattr(w, "word", ""))
        if token:
            token_slots.setdefault(token, []).append(wi)

    token_cursor: dict[str, int] = {}
    for phoneme in phonemes:
        try:
            score = float(getattr(phoneme, "score", 100.0) or 100.0)
        except Exception:
            score = 100.0
        if score >= floor:
            continue

        chosen_idx: int | None = None
        try:
            p_start = float(getattr(phoneme, "start", -1.0) or -1.0)
            p_end = float(getattr(phoneme, "end", -1.0) or -1.0)
        except Exception:
            p_start = -1.0
            p_end = -1.0

        if p_end > p_start >= 0.0:
            best_overlap = 0.0
            for wi, w in enumerate(words):
                if getattr(w, "tag", None) == WordTag.MISSING:
                    continue
                try:
                    ws = float(getattr(w, "start", 0.0) or 0.0)
                    we = float(getattr(w, "end", 0.0) or 0.0)
                except Exception:
                    continue
                overlap = min(we, p_end) - max(ws, p_start)
                if overlap > best_overlap:
                    best_overlap = overlap
                    chosen_idx = wi
            if best_overlap <= 0.003:
                chosen_idx = None

        if chosen_idx is None:
            token = _normalize_word_token(getattr(phoneme, "in_word", ""))
            slots = token_slots.get(token) or []
            if slots:
                cursor = token_cursor.get(token, 0)
                slot_pos = min(cursor, len(slots) - 1)
                chosen_idx = slots[slot_pos]
                token_cursor[token] = cursor + 1

        if chosen_idx is not None:
            indices.add(chosen_idx)

    return indices


def _tune_reading_analysis_highlights(result: ScoringResult) -> None:
    """
    Keep Reading Analysis red highlights focused on high-confidence core issues.
    """
    if not result or not result.alignment or not result.alignment.words:
        return

    words = result.alignment.words
    analysis = result.analysis
    advisor = result.advisor_feedback if isinstance(result.advisor_feedback, dict) else {}
    engine_raw = result.engine_raw if isinstance(result.engine_raw, dict) else {}
    source = str(engine_raw.get("source", "") or "").lower()
    azure_source = "azure" in source
    word_ok = 65.0
    word_weak = 40.0
    azure_low_word_floor = 50.0

    # Preserve a hard visibility floor for very low Azure word scores.
    # These are real factor-level signals and should never appear as black "ok".
    low_score_indices: set[int] = set()
    if azure_source:
        low_score_indices.update(
            _collect_low_factor_word_indices(
                words=words,
                phonemes=list(getattr(result.alignment, "phonemes", None) or []),
                floor=azure_low_word_floor,
            )
        )

        for idx, word in enumerate(words):
            if word.tag == WordTag.MISSING:
                continue
            has_low_factor = False
            for ph in (getattr(word, "phonemes", None) or []):
                try:
                    if float(getattr(ph, "score", 100.0) or 100.0) < azure_low_word_floor:
                        has_low_factor = True
                        break
                except Exception:
                    continue
            if float(word.score or 0.0) < azure_low_word_floor or has_low_factor:
                low_score_indices.add(idx)

    focus_tokens: set[str] = set()
    top_errors = advisor.get("top_errors")
    if isinstance(top_errors, list):
        for item in top_errors:
            if not isinstance(item, dict):
                continue
            raw_words = item.get("words")
            if isinstance(raw_words, list):
                for raw in raw_words:
                    token = _normalize_word_token(str(raw or ""))
                    if token:
                        focus_tokens.add(token)
    for raw in (getattr(analysis, "weak_words", None) or [])[:6]:
        token = _normalize_word_token(str(raw or ""))
        if token:
            focus_tokens.add(token)

    missing_indices = set()
    if analysis and isinstance(getattr(analysis, "missing_indices", None), list):
        script_tokens = re.findall(r"[A-Za-z']+", str(getattr(result, "script_text", "") or ""))
        if script_tokens:
            missing_indices = _map_script_indices_to_alignment_indices(
                script_tokens=script_tokens,
                words=words,
                script_indices=list(analysis.missing_indices),
            )

    demoted_missing = 0
    for idx, word in enumerate(words):
        if word.tag != WordTag.MISSING:
            continue
        if missing_indices and idx in missing_indices:
            continue
        word.score = max(float(word.score or 0.0), word_weak + 8.0)
        word.tag = WordTag.WEAK if word.score < word_ok else WordTag.OK
        word.diagnosis = f"{word.diagnosis} | Display tuning: low-confidence missing hidden.".strip(" |")
        demoted_missing += 1

    poor_candidates: list[tuple[int, float, int]] = []
    for idx, word in enumerate(words):
        if word.tag != WordTag.POOR:
            continue
        token = _normalize_word_token(getattr(word, "word", ""))
        diagnosis = str(getattr(word, "diagnosis", "") or "").strip()
        evidence = 0
        if token and token in focus_tokens:
            evidence += 2
        if diagnosis:
            evidence += 1
        poor_candidates.append((evidence, float(word.score or 0.0), idx))

    poor_candidates.sort(key=lambda row: (-row[0], row[1], row[2]))
    keep_limit = 3
    kept_indices = {idx for evidence, _score, idx in poor_candidates[:keep_limit] if evidence > 0}

    demoted_poor = 0
    for _evidence, _score, idx in poor_candidates:
        if idx in kept_indices:
            continue
        word = words[idx]
        if idx in low_score_indices:
            word.tag = WordTag.WEAK
            word.diagnosis = f"{word.diagnosis} | Display tuning: kept orange by Azure low-score floor.".strip(" |")
            continue
        word.score = max(float(word.score or 0.0), word_weak + 6.0)
        word.tag = WordTag.WEAK if word.score < word_ok else WordTag.OK
        word.diagnosis = f"{word.diagnosis} | Display tuning: non-core red downgraded.".strip(" |")
        demoted_poor += 1

    core_weak_candidates: list[tuple[float, int]] = []
    demoted_weak = 0
    for idx, word in enumerate(words):
        if word.tag != WordTag.WEAK:
            continue
        token = _normalize_word_token(getattr(word, "word", ""))
        if idx in low_score_indices:
            continue
        if not token or token not in focus_tokens:
            word.score = max(float(word.score or 0.0), word_ok + 1.0)
            word.tag = WordTag.OK
            word.diagnosis = f"{word.diagnosis} | Display tuning: non-core orange hidden.".strip(" |")
            demoted_weak += 1
            continue
        core_weak_candidates.append((float(word.score or 0.0), idx))

    core_weak_candidates.sort(key=lambda row: (row[0], row[1]))
    keep_weak_limit = 5
    kept_weak = {idx for _score, idx in core_weak_candidates[:keep_weak_limit]}
    for _score, idx in core_weak_candidates[keep_weak_limit:]:
        word = words[idx]
        if idx in low_score_indices:
            continue
        word.score = max(float(word.score or 0.0), word_ok + 1.0)
        word.tag = WordTag.OK
        word.diagnosis = f"{word.diagnosis} | Display tuning: non-core orange hidden.".strip(" |")
        demoted_weak += 1

    if low_score_indices:
        for idx in low_score_indices:
            word = words[idx]
            if word.tag == WordTag.MISSING:
                continue
            if word.tag == WordTag.OK:
                word.score = min(float(word.score or 0.0), max(word_weak + 10.0, word_ok - 1.0))
                word.tag = WordTag.WEAK
                word.diagnosis = f"{word.diagnosis} | Display tuning: forced orange by Azure low-score floor.".strip(" |")

    if demoted_missing or demoted_poor or demoted_weak:
        logger.info(
            "Reading highlight tuned: demoted_missing=%s, demoted_poor=%s, demoted_weak=%s, kept_red=%s",
            demoted_missing,
            demoted_poor,
            demoted_weak,
            len(kept_indices),
        )


def run_scoring_pipeline(
    mp3_path: Path,
    text: str,
    output_dir: Path,
    student_id: str = "unknown",
    task_id: str = "default",
    submission_id: Optional[str] = None,
    engine_mode: EngineMode = EngineMode.AUTO,
    progress_callback = None
) -> Tuple[ScoringResult, Path, Path]:
    """
    杩愯瀹屾暣鐨勮瘎鍒?Pipeline
    
    Returns:
        (result, json_path, html_path)
    """
    start_time = time.time()
    
    if not submission_id:
        import hashlib
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        random_hash = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
        submission_id = f"sub_{timestamp}_{random_hash}"
        
    # Ensure output dir exists
    final_output_dir = output_dir / task_id / student_id / submission_id
    final_output_dir.mkdir(parents=True, exist_ok=True)
    
    # Init result
    result = ScoringResult()
    result.meta = Meta(
        task_id=task_id,
        student_id=student_id,
        student_name=student_id,
        submission_id=submission_id,
        timestamp=datetime.now().isoformat(),
    )
    result.script_text = text
    result.engine_raw = {}  # Initialize to empty dict to avoid undefined errors

    def update_progress(desc: str):
        if progress_callback:
            progress_callback(desc)
        logger.info(desc)

    try:
        # Use a stable work directory under data for sibling container (DooD) support
        work_base = Path("data/work")
        work_base.mkdir(parents=True, exist_ok=True)
        
        with tempfile.TemporaryDirectory(dir=work_base) as work_dir:
            work_path = Path(work_dir)
            
            # 1. Preprocess
            update_progress("棰勫鐞嗛煶棰?..")
            wav_path, audio_metrics = preprocess_audio(mp3_path, work_path)
            result.audio = audio_metrics
            
            # 1.1 Auto-Transcribe if text is empty
            if not text or not text.strip():
                update_progress("姝ｅ湪鑷姩璇嗗埆鏈楄鏂囨湰...")
                whisper = WhisperEngine()
                transcript_words = whisper._transcribe(wav_path)
                text = " ".join([w["word"] for w in transcript_words])
                result.script_text = text
                result.meta.is_auto_transcribed = True
                logger.info(f"鑷姩璇嗗埆缁撴灉: {text}")
            else:
                normalized_text = _normalize_script_for_scoring(text)
                if normalized_text and normalized_text != text:
                    logger.info("Script text normalized for scoring (punctuation boundary spacing).")
                text = normalized_text or text
                result.script_text = text
            
            # 2. Run Engine
            update_progress("杩愯璇勫垎寮曟搸...")
            if text and text.strip():
                # Warm up reference pronunciation profile in background cache.
                ensure_script_reference_async(text)
            alignment, engine_raw, engine_used, fallback_chain = run_with_fallback(
                wav_path=wav_path,
                script_text=text,
                work_dir=work_path,
                engine_mode=engine_mode,
                audio_metrics=audio_metrics,
            )
            
            result.alignment = alignment
            # Guarantee per-word phoneme alignment for UI factor breakdown.
            ensure_dense_phoneme_alignment(result.alignment)
            result.engine_raw = engine_raw
            if isinstance(result.engine_raw, dict):
                result.engine_raw["audio_duration_sec"] = float(audio_metrics.duration_sec)
            result.meta.engine_used = engine_used
            result.meta.fallback_chain = fallback_chain
            
            # 3. Normalize
            update_progress("璁＄畻鍒嗘暟...")
            result.scores = normalize_scores(
                engine_raw=engine_raw,
                audio_metrics=audio_metrics,
                alignment=alignment,
                script_text=text,
            )
            assign_tags(alignment)
            
            # 4. Analyze
            update_progress("鍒嗘瀽缁撴灉...")
            result.analysis = analyze_results(
                alignment,
                text,
                engine_raw,
                context={
                    "submission_id": result.meta.submission_id,
                    "student_id": result.meta.student_id,
                    "task_id": result.meta.task_id,
                    "audio_path": str(wav_path),
                },
            )

            # 4.1 Re-normalize after analysis so fluency uses finalized pause labels.
            result.scores = normalize_scores(
                engine_raw=engine_raw,
                audio_metrics=audio_metrics,
                alignment=alignment,
                script_text=text,
            )
            assign_tags(alignment)
            if result.analysis and result.analysis.completeness:
                try:
                    result.analysis.completeness.coverage = int(round(float(result.scores.completeness_100)))
                except Exception:
                    pass
            
            # 5. Feedback
            update_progress("鐢熸垚寤鸿...")
            result.feedback = generate_feedback(result.analysis)
            
            # 5.1 LLM Feedback (Priority: Engine-Native Multimodal Feedback)
            update_progress("AI 鑰佸笀鐐硅瘎涓?..")
            try:
                # 妫€鏌ュ紩鎿庢槸鍚﹀凡缁忔彁渚涗簡娣卞害鍙嶉 (濡?Gemini 2.0 鍘熺敓澶氭ā鎬佸弽棣?
                integrated = (result.engine_raw or {}).get("integrated_feedback")
                integrated_source = str((result.engine_raw or {}).get("source", "")).lower()
                annotation_source = str((result.engine_raw or {}).get("annotation_source", "")).lower()
                integrated_from_gemini = (
                    "gemini" in integrated_source
                    or ("azure" in integrated_source and annotation_source == "gemini")
                )
                
                # 濡傛灉寮曟搸宸茬粡鎻愪緵浜嗗畬鏁寸偣璇?(鍗?multimodal path)锛屽垯鐩存帴浣跨敤锛岄伩鍏嶄簩娆¤皟鐢?LLM 閫犳垚璐ㄩ噺鎽婅杽
                # Only trust engine-native integrated feedback when it is truly from Gemini path.
                # Wav2Vec2 fallback may include a generic template and should still go through advisor.
                if (
                    integrated
                    and integrated.get("overall_comment")
                    and integrated_from_gemini
                ):
                    logger.info("Using engine-native multimodal feedback (High Fidelity Path)")
                    from src.models import Feedback
                    result.feedback = Feedback(
                        cn_summary=integrated.get("overall_comment"),
                        cn_actions=integrated.get("specific_suggestions", []),
                        practice=integrated.get("practice_tips", [])
                    )
                    # 纭繚 advisor_feedback 涔熻濉厖锛岀敤浜?UI 灞曠幇
                    result.advisor_feedback = integrated
                    if isinstance(result.engine_raw, dict) and isinstance(result.engine_raw.get("integrated_feedback"), dict):
                        result.engine_raw["integrated_feedback"].setdefault("provider", "gemini")
                else:
                    # 濡傛灉寮曟搸娌℃湁闆嗘垚鐐硅瘎 (濡?Wav2Vec2)锛屽垯鎸夐渶璋冪敤 Advisor (Slow Path)
                    logger.info("No integrated feedback found, calling LLM Advisor (Standard Path)")
                    advisor = get_llm_advisor()
                    result.feedback, result.advisor_feedback = advisor.generate_feedback(result)
                    if isinstance(result.engine_raw, dict):
                        advisor_integrated = _build_integrated_feedback_from_advisor(
                            result.advisor_feedback,
                            provider_default="azure_fallback",
                        )
                        if advisor_integrated.get("overall_comment"):
                            result.engine_raw["integrated_feedback"] = advisor_integrated
                        else:
                            fallback_integrated = {
                                "overall_comment": str(getattr(result.feedback, "cn_summary", "") or "").strip(),
                                "specific_suggestions": list(getattr(result.feedback, "cn_actions", []) or []),
                                "practice_tips": list(getattr(result.feedback, "practice", []) or []),
                                "provider": "azure_fallback",
                            }
                            if isinstance(advisor_integrated.get("provider_chain"), list):
                                fallback_integrated["provider_chain"] = list(advisor_integrated.get("provider_chain") or [])
                            if isinstance(advisor_integrated.get("advisor_errors"), list):
                                fallback_integrated["advisor_errors"] = list(advisor_integrated.get("advisor_errors") or [])
                            result.engine_raw["integrated_feedback"] = fallback_integrated
            except Exception as e:
                logger.warning(f"AI 鐐硅瘎澶辫触: {e}")
                # Fallback to a basic message if everything fails
                if not result.feedback:
                     from src.models import Feedback
                     result.feedback = Feedback(cn_summary="评分分析完成，请查看建议。", cn_actions=[], practice=[])
            
            # Ensure top errors reflect this student's actual weak words.
            _personalize_top_errors_from_alignment(result)

            # Enforce a stable final feedback style across all paths.
            _enforce_feedback_style(result)
            _tune_reading_analysis_highlights(result)

            # Finalize
            result.meta.processing_time_ms = int((time.time() - start_time) * 1000)
            
            # 6. Save
            update_progress("淇濆瓨缁撴灉...")
            json_path = final_output_dir / f"{submission_id}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
                
            html_path = final_output_dir / f"{submission_id}.html"
            render_html_report(result, html_path, audio_path=mp3_path)
            
            return result, json_path, html_path

    except Exception as e:
        result.error = str(e)
        result.meta.processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Save error result
        json_path = final_output_dir / f"{submission_id}.json"
        final_output_dir.mkdir(parents=True, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
            
        raise e

