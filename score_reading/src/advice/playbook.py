from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Iterable

PLAYBOOK_PATH = Path(__file__).resolve().parents[2] / "advice" / "western_pronunciation_playbook.md"
_WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")


def _csv_tokens(raw: str) -> list[str]:
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def _load_runtime_rows() -> list[dict[str, object]]:
    if not PLAYBOOK_PATH.exists():
        return []
    text = PLAYBOOK_PATH.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    in_table = False
    rows: list[dict[str, object]] = []
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "## runtime lookup table":
            in_table = True
            continue
        if in_table and stripped.startswith("## "):
            break
        if not in_table or not stripped.startswith("|"):
            continue

        cols = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cols) < 6:
            continue
        if cols[0].lower() == "key" or set(cols[0]) == {"-"}:
            continue

        rows.append(
            {
                "key": cols[0],
                "triggers": _csv_tokens(cols[1]),
                "focus_words": _csv_tokens(cols[2]),
                "technique": cols[3],
                "drill": cols[4],
                "mnemonic": cols[5],
            }
        )
    return rows


def _tokenize_script(script_text: str) -> set[str]:
    return set(_WORD_RE.findall((script_text or "").lower()))


def _clean_set(values: Iterable[str] | None) -> set[str]:
    if not values:
        return set()
    out = set()
    for value in values:
        token = str(value or "").strip().lower()
        if token:
            out.add(token)
            out.add(token.strip("/"))
    return out


def build_playbook_runtime_hints(
    script_text: str,
    weak_words: Iterable[str] | None = None,
    phoneme_symbols: Iterable[str] | None = None,
    *,
    max_items: int = 5,
) -> str:
    rows = _load_runtime_rows()
    if not rows:
        return "- No playbook hints loaded."

    max_items = max(1, min(8, int(max_items)))
    script_tokens = _tokenize_script(script_text)
    weak_word_set = _clean_set(weak_words)
    phoneme_set = _clean_set(phoneme_symbols)

    ranked: list[tuple[int, dict[str, object]]] = []
    for row in rows:
        triggers = set(row.get("triggers", []) or [])
        focus_words = set(row.get("focus_words", []) or [])
        row_tokens = triggers | focus_words

        score = 0
        score += len(script_tokens & row_tokens)
        score += 2 * len(weak_word_set & focus_words)

        if phoneme_set:
            joined = " ".join(str(x) for x in (row.get("key"), row.get("technique"), *row_tokens)).lower()
            for p in phoneme_set:
                if p and p in joined:
                    score += 2

        if score > 0:
            ranked.append((score, row))

    if ranked:
        ranked.sort(key=lambda item: (-item[0], str(item[1].get("key", ""))))
        selected = [row for _, row in ranked[:max_items]]
    else:
        selected = rows[:max_items]

    lines = []
    for row in selected:
        technique = str(row.get("technique") or "Technique")
        drill = str(row.get("drill") or "")
        mnemonic = str(row.get("mnemonic") or "")
        lines.append(f"- {technique} | Drill: {drill} | Memory hook: {mnemonic}")

    return "\n".join(lines)
