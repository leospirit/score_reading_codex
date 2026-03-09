# Azure-Only Missing Detection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make final missing-word detection and the completeness missing adjustment rely only on Azure evidence, while leaving pronunciation, fluency, intonation, diagnosis, and overall scoring behavior unchanged.

**Architecture:** Keep the change local to the missing-word derivation path in `src/pipeline/analyze.py`. Final `stable_missing_indices` will come only from Azure alignment evidence and Azure transcript-anchor fallback. Gemini-derived missing evidence remains available in raw engine data for debugging, but it no longer influences final missing indices.

**Tech Stack:** Python, pytest, existing `src.models.Alignment` / `WordAlignment` helpers.

---

### Task 1: Add regression tests for Azure-only missing routing

**Files:**
- Modify: `D:/score_reading_fresh/score_reading/tests/test_analyze.py`
- Reference: `D:/score_reading_fresh/score_reading/src/pipeline/analyze.py`

**Step 1: Write the failing tests**

Add tests that prove:
1. When `annotation_source="gemini"` but Azure transcript-anchor detects one missing token, `derive_stable_missing_indices()` still returns that Azure anchored result.
2. When Gemini overlay indices exist but Azure alignment identifies a different missing token, the final result follows Azure alignment and ignores Gemini.

**Step 2: Run tests to verify they fail**

Run: `pytest D:/score_reading_fresh/score_reading/tests/test_analyze.py -k "azure_only_missing or ignores_gemini" -q`
Expected: FAIL because current logic still prioritizes Gemini and blocks Azure fallback under `annotation_source=gemini`.

**Step 3: Commit**

Do not commit yet; continue after implementation.

### Task 2: Implement the minimal missing-source change

**Files:**
- Modify: `D:/score_reading_fresh/score_reading/src/pipeline/analyze.py`

**Step 1: Write minimal implementation**

Update `derive_stable_missing_indices()` so it:
1. Computes Azure alignment-based missing indices first.
2. Returns Azure alignment evidence when present.
3. Allows Azure transcript-anchor fallback regardless of `annotation_source`.
4. Ignores Gemini overlay and Gemini transcript evidence for final missing-word output.
5. Keeps return source values consistent (`alignment`, `transcript_anchor`, `none/alignment` as appropriate).

**Step 2: Keep scope tight**

Do not modify:
- word/phoneme scoring
- diagnosis generation
- feedback generation
- `normalize.py` formulas

Only the final missing index source changes.

### Task 3: Verify behavior

**Files:**
- Test: `D:/score_reading_fresh/score_reading/tests/test_analyze.py`
- Reference: `D:/score_reading_fresh/score_reading/src/pipeline/normalize.py`

**Step 1: Run targeted tests**

Run: `pytest D:/score_reading_fresh/score_reading/tests/test_analyze.py -q`
Expected: PASS

**Step 2: Run focused normalization regression**

Run: `pytest D:/score_reading_fresh/score_reading/tests/test_normalize.py -q`
Expected: PASS

**Step 3: Optional syntax check**

Run: `python -m py_compile D:/score_reading_fresh/score_reading/src/pipeline/analyze.py`
Expected: no output

### Task 4: Summarize exact impact

**Files:**
- Mention only modified files and test evidence.

**Step 1: Report user-visible effect**

Document that missing-word detection now follows Azure only, while all other scores and diagnostics keep existing behavior.
