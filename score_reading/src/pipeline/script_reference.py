import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.analysis.openai_provider import OpenAIProvider
from src.config import load_config
from src.semantic_pronunciation import (
    apply_semantic_priors_to_reference,
    build_semantic_pronunciation_priors,
    summarize_semantic_pronunciation_priors,
)

logger = logging.getLogger(__name__)

_REF_VERSION = 6
_REF_DIR = Path("data/script_references")
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_HASHES: set[str] = set()


def _tokenize_script(script_text: str) -> list[str]:
    return re.findall(r"[A-Za-z']+", script_text or "")


def _normalize_script(script_text: str) -> str:
    tokens = _tokenize_script(script_text)
    return " ".join(token.lower() for token in tokens).strip()


def _ordinal(n: int) -> str:
    if n % 100 in (11, 12, 13):
        return f"{n}th"
    if n % 10 == 1:
        return f"{n}st"
    if n % 10 == 2:
        return f"{n}nd"
    if n % 10 == 3:
        return f"{n}rd"
    return f"{n}th"


def _normalize_stress_hint(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    lower = text.lower()

    base = ""
    if "primary" in lower:
        base = "primary"
    elif "secondary" in lower:
        base = "secondary"

    idx = None
    m_num = re.search(r"([1-9])\s*(?:st|nd|rd|th)?\s*syll", lower)
    if m_num:
        try:
            idx = int(m_num.group(1))
        except Exception:
            idx = None
    else:
        word_to_num = {"first": 1, "second": 2, "third": 3, "fourth": 4}
        for k, v in word_to_num.items():
            if k in lower and "syll" in lower:
                idx = v
                break

    if base and idx is not None:
        return f"{base}: {_ordinal(idx)} syllable"
    if idx is not None:
        return f"{_ordinal(idx)} syllable"
    if base:
        return base

    # Drop non-informative short fragments like "ca" from malformed outputs.
    if len(text) < 3:
        return ""
    return text[:40]


def script_reference_hash(script_text: str) -> str:
    normalized = _normalize_script(script_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _reference_path(script_hash: str) -> Path:
    return _REF_DIR / f"{script_hash}.json"


def _is_modern_reference(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    version = int(data.get("version", 0) or 0)
    if version < _REF_VERSION:
        return False
    # Must include the preprocessed policy pieces we rely on at runtime.
    if not isinstance(data.get("pronunciation_rules"), list):
        return False
    if not isinstance(data.get("pause_rules"), list):
        return False
    if not isinstance(data.get("pace_norm"), dict):
        return False
    if not isinstance(data.get("semantic_pronunciation_priors"), list):
        return False
    return True


def _load_reference_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning("Failed to read script reference %s: %s", path, exc)
    return None


def load_script_reference(script_text: str) -> Optional[dict[str, Any]]:
    if not script_text or not script_text.strip():
        return None

    ref_hash = script_reference_hash(script_text)
    path = _reference_path(ref_hash)
    if not path.exists():
        return None

    return _load_reference_file(path)


def ensure_script_reference_async(script_text: str) -> Optional[Path]:
    if not script_text or not script_text.strip():
        return None

    normalized = _normalize_script(script_text)
    if not normalized:
        return None

    ref_hash = script_reference_hash(script_text)
    out_path = _reference_path(ref_hash)
    if out_path.exists():
        current = _load_reference_file(out_path)
        if _is_modern_reference(current):
            return out_path
        # Old cache exists but lacks preprocessed policy; rebuild it.
        try:
            out_path.unlink(missing_ok=True)
            logger.info("Removed outdated script reference cache: %s", out_path)
        except Exception as exc:
            logger.warning("Failed to remove outdated script reference %s: %s", out_path, exc)

    with _INFLIGHT_LOCK:
        if ref_hash in _INFLIGHT_HASHES:
            return out_path
        _INFLIGHT_HASHES.add(ref_hash)

    thread = threading.Thread(
        target=_build_reference_file,
        args=(script_text, ref_hash, out_path),
        daemon=True,
        name=f"script-ref-{ref_hash[:8]}",
    )
    thread.start()
    logger.info("Scheduled script reference prebuild: %s", out_path)
    return out_path


def wait_for_script_reference(script_text: str, timeout_sec: float = 1.5) -> Optional[dict[str, Any]]:
    if not script_text or not script_text.strip():
        return None

    timeout_sec = max(0.0, float(timeout_sec))
    ref_hash = script_reference_hash(script_text)
    path = _reference_path(ref_hash)

    if path.exists():
        current = load_script_reference(script_text)
        if _is_modern_reference(current):
            return current

    ensure_script_reference_async(script_text)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if path.exists():
            current = load_script_reference(script_text)
            if _is_modern_reference(current):
                return current
        time.sleep(0.08)
    return None


def summarize_script_reference(reference_data: dict[str, Any], max_words: int = 40) -> str:
    if not isinstance(reference_data, dict):
        return ""

    lines: list[str] = []
    focus_words = [str(w).strip().lower() for w in (reference_data.get("focus_words") or []) if str(w).strip()]
    if focus_words:
        lines.append("FocusWords: " + ", ".join(focus_words[:24]))

    words = reference_data.get("word_pronunciations") or []
    prioritized: list[dict[str, Any]] = []
    remainder: list[dict[str, Any]] = []
    focus_set = set(focus_words)
    for item in words:
        if not isinstance(item, dict):
            continue
        key = str(item.get("word", "")).strip().lower()
        if focus_set and key in focus_set:
            prioritized.append(item)
        else:
            remainder.append(item)

    for item in (prioritized + remainder)[:max_words]:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word", "")).strip()
        if not word:
            continue
        ipa = str(item.get("ipa", "")).strip()
        stress = str(item.get("stress", "")).strip()
        tip = str(item.get("tip", "")).strip()
        lines.append(f"WordRef: {word} | ipa={ipa} | stress={stress} | tip={tip}")

    pronunciation_rules = reference_data.get("pronunciation_rules") or []
    for item in pronunciation_rules[:4]:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule", "")).strip()
        examples = ", ".join(str(x).strip() for x in (item.get("examples") or []) if str(x).strip())
        if rule:
            lines.append(f"PronRule: {rule} | examples={examples}")

    pause_rules = reference_data.get("pause_rules") or []
    for item in pause_rules[:8]:
        if not isinstance(item, dict):
            continue
        after_word = str(item.get("after_word", "")).strip()
        pause_type = str(item.get("pause_type", "")).strip()
        pause_ms = str(item.get("pause_ms", "")).strip()
        reason = str(item.get("reason", "")).strip()
        if after_word:
            lines.append(
                f"PauseRule: after={after_word} | type={pause_type} | ms={pause_ms} | reason={reason}"
            )

    pace_norm = reference_data.get("pace_norm") or {}
    if isinstance(pace_norm, dict) and pace_norm:
        target_wpm = str(pace_norm.get("target_wpm", "")).strip()
        warn_below = str(pace_norm.get("warn_below", "")).strip()
        warn_above = str(pace_norm.get("warn_above", "")).strip()
        breath_group_words = str(pace_norm.get("breath_group_words", "")).strip()
        notes = ", ".join(str(x).strip() for x in (pace_norm.get("notes") or []) if str(x).strip())
        lines.append(
            "PaceNorm: "
            f"target_wpm={target_wpm} | warn_below={warn_below} | warn_above={warn_above} "
            f"| breath_group_words={breath_group_words} | notes={notes}"
        )

    rhythm = reference_data.get("sentence_rhythm") or []
    if isinstance(rhythm, list):
        for sentence in rhythm[:3]:
            if isinstance(sentence, str) and sentence.strip():
                lines.append(f"Rhythm: {sentence.strip()}")

    linking = reference_data.get("common_linking") or []
    if isinstance(linking, list):
        compact = [str(x).strip() for x in linking if str(x).strip()][:4]
        if compact:
            lines.append("Linking: " + " | ".join(compact))

    semantic_priors = reference_data.get("semantic_pronunciation_priors") or []
    if isinstance(semantic_priors, list):
        lines.extend(summarize_semantic_pronunciation_priors(semantic_priors[:6]))

    return "\n".join(lines).strip()


def render_preheat_text(script_text: str, reference_data: Optional[dict[str, Any]]) -> str:
    """
    Render an editable preheat note with pronunciation / pause / linking / stress hints.
    This is for UI display only and must not replace the scoring script text.
    """
    raw_script = (script_text or "").strip()
    if not raw_script:
        return ""
    if not isinstance(reference_data, dict):
        return raw_script

    def _clean_ipa(value: str) -> str:
        ipa = str(value or "").strip()
        ipa = ipa.strip("/")
        return ipa[:80]

    def _clean_stress(value: str) -> str:
        return _normalize_stress_hint(str(value or ""))

    def _normalize_display_script(text: str) -> str:
        out = re.sub(r"\s+", " ", text or "").strip()
        out = re.sub(r"\s*([,;:!?])\s*", r"\1 ", out)
        out = re.sub(r"\s*([.])\s*", r". ", out)
        out = re.sub(r"\s+", " ", out).strip()
        return out

    def _format_pause_marker(pause_type: str, pause_ms: str) -> str:
        p_type = (pause_type or "medium").strip().lower()
        if p_type not in {"strong", "medium", "light", "none"}:
            p_type = "medium"
        marker = f"[PAUSE:{p_type}"
        if pause_ms:
            marker += f" {pause_ms}"
        marker += "]"
        return marker

    word_map: dict[str, dict[str, str]] = {}
    for item in (reference_data.get("word_pronunciations") or []):
        if not isinstance(item, dict):
            continue
        word = str(item.get("word", "")).strip().lower()
        if not word:
            continue
        word_map[word] = {
            "ipa": _clean_ipa(str(item.get("ipa", ""))),
            "stress": _clean_stress(str(item.get("stress", ""))),
            "tip": str(item.get("tip", "")).strip(),
        }

    pause_map: dict[str, dict[str, str]] = {}
    for item in (reference_data.get("pause_rules") or []):
        if not isinstance(item, dict):
            continue
        after_word = str(item.get("after_word", "")).strip().lower()
        if not after_word:
            continue
        # Keep the first rule per word; repeated words may have conflicting reasons.
        if after_word in pause_map:
            continue
        pause_map[after_word] = {
            "type": str(item.get("pause_type", "medium")).strip(),
            "ms": str(item.get("pause_ms", "")).strip(),
            "reason": str(item.get("reason", "")).strip(),
        }

    normalized_script = _normalize_display_script(raw_script)
    tokens = re.findall(r"[A-Za-z']+|[.,!?;:]", normalized_script)
    punctuation_pause = {
        ",": ("medium", "350-500"),
        ";": ("medium", "450-650"),
        ":": ("medium", "450-650"),
        ".": ("strong", "700-900"),
        "!": ("strong", "700-900"),
        "?": ("strong", "700-900"),
    }
    annotated_tokens: list[str] = []
    stressed_words: list[str] = []
    last_word_clean = ""
    for idx, token in enumerate(tokens):
        if re.fullmatch(r"[A-Za-z']+", token):
            clean = token.lower()
            item = token
            last_word_clean = clean
            if clean in word_map:
                ipa = word_map[clean].get("ipa", "")
                stress = word_map[clean].get("stress", "")
                if ipa:
                    item += f"{{/{ipa}/}}"
                if stress:
                    item += f"{{stress:{stress}}}"
                    stressed_words.append(clean)
            annotated_tokens.append(item)

            # Add lexical pause markers only when the next token is not punctuation.
            next_token = tokens[idx + 1] if idx + 1 < len(tokens) else ""
            if clean in pause_map and not re.fullmatch(r"[.,!?;:]", next_token or ""):
                pause = pause_map[clean]
                annotated_tokens.append(
                    _format_pause_marker(pause.get("type", "medium"), pause.get("ms", ""))
                )
            continue

        if annotated_tokens:
            annotated_tokens[-1] = annotated_tokens[-1] + token
        else:
            annotated_tokens.append(token)

        if token in punctuation_pause:
            p_type, p_ms = punctuation_pause[token]
            if last_word_clean and last_word_clean in pause_map:
                pause = pause_map[last_word_clean]
                p_type = pause.get("type", p_type) or p_type
                p_ms = pause.get("ms", p_ms) or p_ms
            annotated_tokens.append(_format_pause_marker(p_type, p_ms))

    linking = [str(x).strip() for x in (reference_data.get("common_linking") or []) if str(x).strip()]
    pron_rules = reference_data.get("pronunciation_rules") or []
    pause_rules = reference_data.get("pause_rules") or []
    pace = reference_data.get("pace_norm") or {}
    focus_words = [str(w).strip().lower() for w in (reference_data.get("focus_words") or []) if str(w).strip()]
    semantic_priors = reference_data.get("semantic_pronunciation_priors") or []

    lines: list[str] = []
    lines.append("[Annotated Script]")
    lines.append(" ".join(annotated_tokens))
    lines.append("")
    lines.append("[Pronunciation Rules]")
    if pron_rules:
        for idx, row in enumerate(pron_rules[:6], start=1):
            if isinstance(row, dict):
                rule = str(row.get("rule", "")).strip()
                examples = ", ".join(str(x).strip() for x in (row.get("examples") or []) if str(x).strip())
                if rule:
                    lines.append(f"{idx}. {rule}" + (f" | examples: {examples}" if examples else ""))
    else:
        lines.append("No explicit pronunciation rule.")

    lines.append("")
    lines.append("[Pause Rules]")
    if pause_rules:
        for idx, row in enumerate(pause_rules[:12], start=1):
            if isinstance(row, dict):
                after_word = str(row.get("after_word", "")).strip()
                pause_type = str(row.get("pause_type", "")).strip()
                pause_ms = str(row.get("pause_ms", "")).strip()
                if after_word:
                    lines.append(
                        f"{idx}. after '{after_word}' => {pause_type}"
                        + (f" {pause_ms}" if pause_ms else "")
                    )
    else:
        lines.append("No explicit pause rule.")

    lines.append("")
    lines.append("[Linking Notes]")
    if linking:
        for idx, row in enumerate(linking[:8], start=1):
            lines.append(f"{idx}. {row}")
    else:
        lines.append("No explicit linking note.")

    lines.append("")
    lines.append("[Stress Focus]")
    if stressed_words:
        unique_stress = []
        seen = set()
        for w in stressed_words:
            if w in seen:
                continue
            seen.add(w)
            unique_stress.append(w)
        lines.append(", ".join(unique_stress[:20]))
    else:
        lines.append("No explicit stress target.")

    lines.append("")
    lines.append("[Focus Words]")
    if focus_words:
        lines.append(", ".join(focus_words[:24]))
    else:
        lines.append("No explicit focus words.")
    lines.append("")
    lines.append("[Semantic Pronunciation Priors]")
    semantic_lines = summarize_semantic_pronunciation_priors(semantic_priors[:8]) if isinstance(semantic_priors, list) else []
    if semantic_lines:
        lines.extend(semantic_lines)
    else:
        lines.append("No explicit semantic pronunciation prior.")

    lines.append("")
    lines.append("[Pace Norm]")
    if isinstance(pace, dict) and pace:
        target = pace.get("target_wpm", "")
        low = pace.get("warn_below", "")
        high = pace.get("warn_above", "")
        breath = pace.get("breath_group_words", "")
        notes = ", ".join(str(x).strip() for x in (pace.get("notes") or []) if str(x).strip())
        lines.append(f"target_wpm={target}; warn_below={low}; warn_above={high}; breath_group_words={breath}")
        if notes:
            lines.append(f"notes: {notes}")
    else:
        lines.append("No pace norm.")

    return "\n".join(lines).strip()


def _parse_json_text(text: str) -> Optional[dict[str, Any]]:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    if "```json" in cleaned:
        cleaned = cleaned.split("```json")[-1].split("```")[0].strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned.split("```")[-1].split("```")[0].strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def _build_reference_file(script_text: str, script_hash: str, out_path: Path) -> None:
    try:
        data = _generate_reference(script_text, script_hash)
        if not data:
            return

        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(out_path)
        logger.info("Script reference saved: %s", out_path)
    except Exception as exc:
        logger.warning("Script reference build failed: %s", exc)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT_HASHES.discard(script_hash)


def _clean_text_list(value: Any, limit: int = 50) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text[:120])
        if len(out) >= limit:
            break
    return out


def _clean_word_pronunciations(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word", "")).strip()
        if not word:
            continue
        rows.append(
            {
                "word": word,
                "ipa": str(item.get("ipa", "")).strip()[:80],
                "stress": _normalize_stress_hint(str(item.get("stress", ""))),
                "tip": str(item.get("tip", "")).strip()[:180],
            }
        )
        if len(rows) >= 180:
            break
    return rows


def _clean_pronunciation_rules(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule", "")).strip()
        if not rule:
            continue
        examples = _clean_text_list(item.get("examples"), limit=6)
        rows.append({"rule": rule[:140], "examples": examples})
        if len(rows) >= 20:
            break
    return rows


def _extract_focus_words(
    word_pronunciations: list[dict[str, str]],
    pronunciation_rules: list[dict[str, Any]],
    *,
    limit: int = 28,
) -> list[str]:
    """
    Derive a compact high-priority word list for runtime rescoring.
    Priority sources:
    1) rule examples (strong signal)
    2) words with explicit tip/stress/ipa metadata
    """
    out: list[str] = []
    seen: set[str] = set()
    valid_words = {
        re.sub(r"[^a-z']+", "", str(row.get("word", "")).lower())
        for row in word_pronunciations
        if isinstance(row, dict)
    }
    valid_words = {w for w in valid_words if w}

    def _push(word: str) -> None:
        token = str(word or "").strip()
        if not token:
            return
        key = re.sub(r"[^a-z']+", "", token.lower())
        if valid_words and key not in valid_words:
            return
        if not key or key in seen:
            return
        seen.add(key)
        out.append(key)

    for row in pronunciation_rules:
        if not isinstance(row, dict):
            continue
        for ex in row.get("examples") or []:
            for token in re.findall(r"[A-Za-z']+", str(ex or "")):
                if len(token) >= 3:
                    _push(token)
                if len(out) >= limit:
                    return out

    for row in word_pronunciations:
        if not isinstance(row, dict):
            continue
        word = str(row.get("word", "")).strip()
        if not word:
            continue
        tip = str(row.get("tip", "")).strip()
        stress = str(row.get("stress", "")).strip()
        ipa = str(row.get("ipa", "")).strip()
        if tip or stress or ipa:
            _push(word)
        if len(out) >= limit:
            return out

    return out


def _clean_pause_rules(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    allowed_types = {"strong", "medium", "light", "none"}
    for item in value:
        if not isinstance(item, dict):
            continue
        after_word = re.sub(r"[^A-Za-z']+", "", str(item.get("after_word", "")).strip().lower())
        if not after_word:
            continue
        pause_type = str(item.get("pause_type", "")).strip().lower()
        if pause_type not in allowed_types:
            pause_type = "medium"
        pause_ms = str(item.get("pause_ms", "")).strip()[:32]
        reason = str(item.get("reason", "")).strip()[:140]
        rows.append(
            {
                "after_word": after_word[:48],
                "pause_type": pause_type,
                "pause_ms": pause_ms,
                "reason": reason,
            }
        )
        if len(rows) >= 80:
            break
    return rows


def _script_word_stream(script_text: str) -> list[tuple[str, str]]:
    stream: list[tuple[str, str]] = []
    for match in re.finditer(r"([A-Za-z']+)([^A-Za-z']*)", script_text or ""):
        word = re.sub(r"[^A-Za-z']+", "", str(match.group(1) or "").lower())
        if not word:
            continue
        tail = str(match.group(2) or "")
        punct = "".join(ch for ch in tail if ch in ",.;:!?")
        stream.append((word, punct))
    return stream


def _build_template_pause_rules(script_text: str) -> list[dict[str, str]]:
    """
    Deterministic pause template from punctuation.
    This is the stable baseline; Gemini rules are merged as refinement.
    """
    stream = _script_word_stream(script_text)
    template: list[dict[str, str]] = []
    seen: set[str] = set()

    for word, punct in stream:
        if not punct or word in seen:
            continue
        seen.add(word)
        if any(ch in punct for ch in ".!?"):
            template.append(
                {
                    "after_word": word,
                    "pause_type": "strong",
                    "pause_ms": "800-1000",
                    "reason": "Template sentence boundary",
                }
            )
        elif any(ch in punct for ch in ",;:"):
            template.append(
                {
                    "after_word": word,
                    "pause_type": "medium",
                    "pause_ms": "350-500",
                    "reason": "Template clause boundary",
                }
            )
    return template


def build_local_script_reference(script_text: str, script_hash: str = "") -> dict[str, Any]:
    """
    Build a deterministic local-only script reference.
    This is the immediate fallback used by the preheat UI when Gemini data
    is unavailable or still building in the background.
    """
    raw_text = str(script_text or "").strip()
    words = _tokenize_script(raw_text)
    unique_word_count = len({word.lower() for word in words if str(word).strip()})
    resolved_hash = str(script_hash or "").strip() or script_reference_hash(raw_text)
    semantic_priors = build_semantic_pronunciation_priors(raw_text)

    word_pronunciations: list[dict[str, str]] = []
    for prior in semantic_priors:
        word = str(prior.get("word", "")).strip()
        ipa = str(prior.get("ipa", "")).strip()
        meaning = str(prior.get("meaning", "")).strip()
        if not word or not ipa:
            continue
        tip = f"{word} here means '{meaning}', so read it as /{ipa}/.".strip()
        word_pronunciations.append(
            {
                "word": word,
                "ipa": ipa,
                "stress": "",
                "tip": tip,
            }
        )

    word_pronunciations, pronunciation_rules = apply_semantic_priors_to_reference(
        word_pronunciations,
        [],
        semantic_priors,
    )
    pause_rules = _build_template_pause_rules(raw_text)
    pace_norm = _default_pace_norm(len(words))
    focus_words = _extract_focus_words(word_pronunciations, pronunciation_rules)

    return {
        "version": _REF_VERSION,
        "script_hash": resolved_hash,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "model": "local_fallback",
        "word_pronunciations": word_pronunciations,
        "pronunciation_rules": pronunciation_rules,
        "pause_rules": pause_rules,
        "pace_norm": pace_norm,
        "sentence_rhythm": [],
        "common_linking": [],
        "semantic_pronunciation_priors": semantic_priors,
        "focus_words": focus_words,
        "script_token_count": int(len(words)),
        "unique_word_count": int(unique_word_count),
    }


def _merge_pause_rules_with_template(
    *,
    script_text: str,
    gemini_pause_rules: list[dict[str, str]],
) -> list[dict[str, str]]:
    """
    Template-first merge:
    - Keep deterministic punctuation rules as primary.
    - Add Gemini-only refinements for non-template words (mainly light/none).
    """
    stream = _script_word_stream(script_text)
    order_index: dict[str, int] = {}
    for i, (word, _) in enumerate(stream):
        if word not in order_index:
            order_index[word] = i
    script_words = set(order_index.keys())

    merged: dict[str, dict[str, str]] = {}
    for row in _build_template_pause_rules(script_text):
        key = row.get("after_word", "")
        if key:
            merged[key] = row

    for row in gemini_pause_rules:
        if not isinstance(row, dict):
            continue
        key = re.sub(r"[^A-Za-z']+", "", str(row.get("after_word", "")).lower())
        if not key or key not in script_words:
            continue
        if key in merged:
            # Template has priority on punctuation anchors.
            continue
        p_type = str(row.get("pause_type", "")).strip().lower()
        if p_type not in {"light", "none", "medium"}:
            continue
        merged[key] = {
            "after_word": key,
            "pause_type": p_type,
            "pause_ms": str(row.get("pause_ms", "")).strip()[:32],
            "reason": ("Gemini refinement: " + str(row.get("reason", "")).strip())[:140].strip(),
        }

    ordered = sorted(
        merged.values(),
        key=lambda x: order_index.get(str(x.get("after_word", "")).lower(), 10**9),
    )
    return ordered[:80]


def _default_pace_norm(token_count: int) -> dict[str, Any]:
    if token_count >= 120:
        target = "105-140"
    elif token_count >= 70:
        target = "115-150"
    else:
        target = "120-160"
    return {
        "target_wpm": target,
        "warn_below": 95,
        "warn_above": 185,
        "breath_group_words": "4-8",
        "notes": [
            "Keep phrase-level rhythm stable.",
            "Avoid rushing in long clauses.",
        ],
    }


def _clean_pace_norm(value: Any, token_count: int) -> dict[str, Any]:
    default = _default_pace_norm(token_count)
    if not isinstance(value, dict):
        return default

    target_wpm = str(value.get("target_wpm", "")).strip() or default["target_wpm"]
    warn_below = value.get("warn_below", default["warn_below"])
    warn_above = value.get("warn_above", default["warn_above"])
    breath_group_words = str(value.get("breath_group_words", "")).strip() or default["breath_group_words"]
    notes = _clean_text_list(value.get("notes"), limit=6) or default["notes"]

    try:
        warn_below_num = int(float(warn_below))
    except Exception:
        warn_below_num = int(default["warn_below"])
    try:
        warn_above_num = int(float(warn_above))
    except Exception:
        warn_above_num = int(default["warn_above"])

    return {
        "target_wpm": target_wpm[:24],
        "warn_below": max(60, min(180, warn_below_num)),
        "warn_above": max(90, min(260, warn_above_num)),
        "breath_group_words": breath_group_words[:20],
        "notes": notes,
    }


def _normalize_reference(
    parsed: dict[str, Any],
    *,
    script_text: str,
    script_hash: str,
    model: str,
    script_token_count: int,
    unique_word_count: int,
) -> dict[str, Any]:
    word_pronunciations = _clean_word_pronunciations(parsed.get("word_pronunciations"))
    pronunciation_rules = _clean_pronunciation_rules(parsed.get("pronunciation_rules"))
    semantic_priors = build_semantic_pronunciation_priors(script_text)
    word_pronunciations, pronunciation_rules = apply_semantic_priors_to_reference(
        word_pronunciations,
        pronunciation_rules,
        semantic_priors,
    )
    pause_rules = _merge_pause_rules_with_template(
        script_text=script_text,
        gemini_pause_rules=_clean_pause_rules(parsed.get("pause_rules")),
    )
    pace_norm = _clean_pace_norm(parsed.get("pace_norm"), script_token_count)
    sentence_rhythm = _clean_text_list(parsed.get("sentence_rhythm"), limit=20)
    common_linking = _clean_text_list(parsed.get("common_linking"), limit=20)
    focus_words = _extract_focus_words(word_pronunciations, pronunciation_rules)

    return {
        "version": _REF_VERSION,
        "script_hash": script_hash,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "model": model,
        "word_pronunciations": word_pronunciations,
        "pronunciation_rules": pronunciation_rules,
        "pause_rules": pause_rules,
        "pace_norm": pace_norm,
        "sentence_rhythm": sentence_rhythm,
        "common_linking": common_linking,
        "semantic_pronunciation_priors": semantic_priors,
        "focus_words": focus_words,
        "script_token_count": int(script_token_count),
        "unique_word_count": int(unique_word_count),
    }


def _generate_reference(script_text: str, script_hash: str) -> Optional[dict[str, Any]]:
    cfg = load_config()
    gemini_key = cfg.get("engines.gemini.api_key") or os.getenv("GEMINI_API_KEY")
    model = cfg.get("engines.gemini.model") or "gemini-3-flash-preview"
    if "gemini-3" not in str(model).lower():
        model = "gemini-3-flash-preview"

    provider = OpenAIProvider(api_key=gemini_key, model=model)
    provider_ready = bool(
        getattr(provider, "client", None)
        or getattr(provider, "genai_model", None)
        or getattr(provider, "client_type", "") in {"gemini", "gemini_rest"}
    )
    if not provider_ready:
        logger.warning("Gemini provider unavailable, skip script reference generation")
        return None

    words = _tokenize_script(script_text)
    unique_words: list[str] = []
    seen: set[str] = set()
    for word in words:
        normalized = word.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_words.append(word)
    unique_words = unique_words[:180]

    system_prompt = (
        "You are an English speaking exam preprocessor. "
        "Build a reusable script policy profile to reduce online scoring work for audio-time inference. "
        "Return strict JSON only."
    )
    payload = {
        "task": "build_script_policy_profile_for_reading_assessment",
        "script": script_text,
        "words": unique_words,
        "semantic_pronunciation_priors": build_semantic_pronunciation_priors(script_text),
        "goal": "Precompute pronunciation focus, pause policy, and pace norms before audio arrives.",
        "schema": {
            "word_pronunciations": [{"word": "string", "ipa": "string", "stress": "string", "tip": "string"}],
            "pronunciation_rules": [{"rule": "string", "examples": ["string"]}],
            "pause_rules": [
                {
                    "after_word": "string",
                    "pause_type": "strong|medium|light|none",
                    "pause_ms": "string",
                    "reason": "string",
                }
            ],
            "pace_norm": {
                "target_wpm": "string",
                "warn_below": 0,
                "warn_above": 0,
                "breath_group_words": "string",
                "notes": ["string"],
            },
            "sentence_rhythm": ["string"],
            "common_linking": ["string"],
        },
        "rules": [
            "Use General American pronunciation baseline.",
            "When semantic_pronunciation_priors are provided, obey them for ambiguous words.",
            "Pause rules must be concrete and script-specific.",
            "Pace norm should be practical for elementary reading tasks.",
            "Keep tips short and directly actionable.",
            "Return JSON only, no markdown.",
        ],
    }

    raw = provider.generate_response(
        system_prompt=system_prompt,
        user_prompt=json.dumps(payload, ensure_ascii=False),
        temperature=0.0,
    )
    parsed = _parse_json_text(raw)
    if not parsed:
        logger.warning("Script reference generation returned invalid JSON")
        return None

    return _normalize_reference(
        parsed,
        script_text=script_text,
        script_hash=script_hash,
        model=str(getattr(provider, "model", model)),
        script_token_count=len(words),
        unique_word_count=len(unique_words),
    )
