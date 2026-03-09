from __future__ import annotations

import re
from typing import Any


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z']+", text or "")


def build_semantic_pronunciation_priors(script_text: str) -> list[dict[str, str]]:
    """
    Build deterministic pronunciation priors for lexically ambiguous words.
    These priors are local heuristics and do not depend on the LLM.
    """
    tokens = _tokenize(script_text)
    lowers = [token.lower() for token in tokens]
    priors: list[dict[str, str]] = []

    live_residence_next = {"there", "here", "in", "with", "at", "near"}
    for idx, token in enumerate(lowers):
        if token != "live":
            continue
        next_token = lowers[idx + 1] if idx + 1 < len(lowers) else ""
        next_pair = " ".join(part for part in lowers[idx + 1: idx + 3] if part).strip()
        if next_token in live_residence_next:
            priors.append(
                {
                    "word": "live",
                    "context": f"live {next_pair}".strip(),
                    "meaning": "reside",
                    "ipa": "lɪv",
                    "rule": "In residence contexts such as 'live there' or 'live in', pronounce 'live' as /lɪv/, not /laɪv/.",
                }
            )

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in priors:
        key = (
            str(item.get("word", "")).strip().lower(),
            str(item.get("context", "")).strip().lower(),
            str(item.get("ipa", "")).strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def summarize_semantic_pronunciation_priors(priors: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in priors:
        if not isinstance(item, dict):
            continue
        word = str(item.get("word", "")).strip()
        context = str(item.get("context", "")).strip()
        meaning = str(item.get("meaning", "")).strip()
        ipa = str(item.get("ipa", "")).strip()
        rule = str(item.get("rule", "")).strip()
        if not word or not ipa:
            continue
        line = f"SemanticPron: {word}"
        if context:
            line += f" | context={context}"
        if meaning:
            line += f" | meaning={meaning}"
        line += f" | ipa=/{ipa}/"
        if rule:
            line += f" | rule={rule}"
        lines.append(line)
    return lines


def apply_semantic_priors_to_reference(
    word_pronunciations: list[dict[str, str]],
    pronunciation_rules: list[dict[str, Any]],
    priors: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    if not priors:
        return word_pronunciations, pronunciation_rules

    updated_words = [dict(row) for row in word_pronunciations]
    updated_rules = [dict(row) for row in pronunciation_rules]
    seen_rules = {
        str(row.get("rule", "")).strip().lower()
        for row in updated_rules
        if isinstance(row, dict) and str(row.get("rule", "")).strip()
    }

    for prior in priors:
        word = str(prior.get("word", "")).strip().lower()
        ipa = str(prior.get("ipa", "")).strip()
        context = str(prior.get("context", "")).strip()
        rule = str(prior.get("rule", "")).strip()
        meaning = str(prior.get("meaning", "")).strip()
        if not word or not ipa:
            continue

        for row in updated_words:
            if str(row.get("word", "")).strip().lower() != word:
                continue
            row["ipa"] = ipa
            tip = str(row.get("tip", "")).strip()
            semantic_tip = f"{word} here means '{meaning}', so read it as /{ipa}/."
            row["tip"] = semantic_tip if not tip else f"{tip} {semantic_tip}".strip()

        rule_key = rule.lower()
        if rule and rule_key not in seen_rules:
            examples = [context] if context else [word]
            updated_rules.insert(0, {"rule": rule, "examples": examples})
            seen_rules.add(rule_key)

    return updated_words, updated_rules
