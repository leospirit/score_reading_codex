"""
Phoneme alignment fallback builder.

Guarantees non-empty, per-word phoneme alignment data for UI breakdown
when upstream engines do not provide dense phoneme timestamps/scores.
"""
from __future__ import annotations

import re
from typing import Iterable

from src.models import Alignment, PhonemeAlignment, PhonemeTag, WordTag


def _normalize_token(text: str) -> str:
    return re.sub(r"[^a-z']+", "", str(text or "").lower())


def _score_to_tag(score: float) -> PhonemeTag:
    if score >= 80:
        return PhonemeTag.OK
    if score >= 60:
        return PhonemeTag.WEAK
    return PhonemeTag.POOR


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _tokenize_to_pseudo_phonemes(word: str) -> list[str]:
    """
    Lightweight grapheme -> pseudo-phoneme split.
    Deterministic and dependency-free.
    """
    text = _normalize_token(word)
    if not text:
        return []

    units: list[str] = []
    i = 0
    # Prioritize common English digraph/trigraph patterns.
    long_units = (
        "tch", "dge", "igh", "eigh",
        "tion", "sion",
        "th", "sh", "ch", "ph", "wh", "ng", "ck", "qu",
        "ee", "oo", "ea", "ai", "ay", "oa", "ow", "ou", "oi", "oy", "au",
        "er", "ir", "ur", "ar", "or",
    )
    while i < len(text):
        matched = None
        for pat in long_units:
            if text.startswith(pat, i):
                matched = pat
                break
        if matched:
            units.append(matched)
            i += len(matched)
        else:
            units.append(text[i])
            i += 1

    # Keep tooltip readable: cap to a reasonable width.
    if len(units) > 12:
        # Merge tail units to avoid too many columns.
        head = units[:11]
        tail = "".join(units[11:])
        return head + ([tail] if tail else [])
    return units


def _flatten_word_phonemes(words: Iterable) -> list[PhonemeAlignment]:
    flat: list[PhonemeAlignment] = []
    for w in words:
        for p in list(getattr(w, "phonemes", []) or []):
            if not isinstance(p, PhonemeAlignment):
                continue
            if not getattr(p, "in_word", ""):
                p.in_word = str(getattr(w, "word", "") or "")
            flat.append(p)
    return flat


def ensure_dense_phoneme_alignment(alignment: Alignment) -> None:
    """
    Mutates `alignment` in-place:
    - Consolidates any existing word-level phonemes into `alignment.phonemes`
    - Fills uncovered words with deterministic pseudo-phoneme alignments
    """
    words = list(alignment.words or [])
    if not words:
        alignment.phonemes = []
        return

    existing = list(alignment.phonemes or [])
    if not existing:
        existing = _flatten_word_phonemes(words)

    # Track per-word coverage from existing phonemes.
    coverage: dict[int, int] = {}
    for idx, w in enumerate(words):
        coverage[idx] = 0
        w_token = _normalize_token(getattr(w, "word", ""))
        if not w_token:
            continue
        for p in existing:
            if _normalize_token(getattr(p, "in_word", "")) == w_token:
                coverage[idx] += 1

    out = list(existing)

    for idx, w in enumerate(words):
        # Missing words stay visually marked as missing; no synthetic phonemes needed.
        if getattr(w, "tag", None) == WordTag.MISSING:
            continue

        # Already covered.
        if coverage.get(idx, 0) > 0:
            continue

        word_text = str(getattr(w, "word", "") or "").strip()
        units = _tokenize_to_pseudo_phonemes(word_text)
        if not units:
            continue

        try:
            start = float(getattr(w, "start", 0.0))
            end = float(getattr(w, "end", 0.0))
        except Exception:
            start, end = 0.0, 0.0
        if end <= start:
            # Conservative fallback duration when timing is absent.
            start = max(0.0, float(idx) * 0.10)
            end = start + 0.10

        duration = max(0.02, end - start)
        slot = duration / max(1, len(units))

        base_score = float(getattr(w, "score", 0.0) or 0.0)
        w_tag = str(getattr(w, "tag", "") or "").lower()
        if w_tag == "poor":
            base_score = min(base_score, 62.0)
        elif w_tag == "weak":
            base_score = min(base_score, 78.0)

        generated: list[PhonemeAlignment] = []
        for j, unit in enumerate(units):
            # Deterministic small spread to avoid identical columns.
            offset = ((j % 5) - 2) * 1.8
            p_score = _clip(base_score + offset, 0.0, 100.0)
            p_start = round(start + j * slot, 3)
            p_end = round(min(end, start + (j + 1) * slot), 3)
            generated.append(
                PhonemeAlignment(
                    phoneme=unit,
                    start=p_start,
                    end=max(p_start + 0.005, p_end),
                    tag=_score_to_tag(p_score),
                    score=p_score,
                    in_word=word_text,
                )
            )

        # Attach to both containers for compatibility.
        try:
            w.phonemes = list(generated)
        except Exception:
            pass
        out.extend(generated)

    alignment.phonemes = out

