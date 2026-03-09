# Completeness Re-Normalization Bug Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure completeness is re-normalized from Azure-derived `stable_missing_indices` after analysis, so a prior `force_100` result cannot survive when missing words are later detected.

**Architecture:** Keep the existing Azure-only missing detection unchanged. Fix the inconsistency at the normalization layer by adding a regression test for the observed mismatch and then making the smallest possible change in `normalize.py` so `stable_missing_indices` always drives the second completeness pass.

**Tech Stack:** Python, pytest, existing scoring pipeline in `score_reading/src/pipeline`

---

### Task 1: Add regression test for the observed mismatch

**Files:**
- Create: `D:\score_reading_fresh\score_reading\tests\test_normalize_completeness.py`
- Read: `D:\score_reading_fresh\score_reading\src\pipeline\normalize.py`

**Step 1: Write the failing test**

```python
def test_completeness_uses_stable_missing_indices_after_analysis():
    ...
```

Build a minimal alignment and `engine_raw` payload where:
- engine source is Azure with `annotation_source = gemini`
- first-pass completeness score is high
- `stable_missing_indices = [108]`
- expected second-pass completeness is below 100 and `missing_count == 1`

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=D:\score_reading_fresh\score_reading pytest D:\score_reading_fresh\score_reading\tests\test_normalize_completeness.py -q`
Expected: FAIL showing completeness still resolves to `100.0` or keeps `missing_count = 0`

**Step 3: Commit**

Skip commit for now; user asked for direct fix in current workspace.

### Task 2: Implement the minimal normalization fix

**Files:**
- Modify: `D:\score_reading_fresh\score_reading\src\pipeline\normalize.py`
- Test: `D:\score_reading_fresh\score_reading\tests\test_normalize_completeness.py`

**Step 1: Write minimal implementation**

Adjust the completeness normalization path so that when `engine_raw['stable_missing_indices']` exists and contains valid indices, the computed `missing_count` from `_count_stable_missing_words()` takes precedence over any earlier `force_100` outcome.

The fix must:
- leave Azure-only missing derivation unchanged
- leave overall weighting unchanged
- only affect completeness consistency after analysis writes stable missing indices

**Step 2: Run targeted test to verify it passes**

Run: `PYTHONPATH=D:\score_reading_fresh\score_reading pytest D:\score_reading_fresh\score_reading\tests\test_normalize_completeness.py -q`
Expected: PASS

**Step 3: Run nearby regression tests**

Run: `PYTHONPATH=D:\score_reading_fresh\score_reading pytest D:\score_reading_fresh\score_reading\tests\test_analyze.py -q`
Expected: PASS

### Task 3: Verify behavior against real output shape

**Files:**
- Read: latest `D:\score_reading_fresh\data\out\...\*.json`

**Step 1: Re-check the mismatch fields**

Inspect:
- `engine_raw.stable_missing_indices`
- `engine_raw.completeness_adjusted_for_missing`
- `scores.completeness_100`
- `analysis.missing_words`

**Step 2: Confirm expected consistency**

Expected after fix:
- if `stable_missing_indices` contains valid indices, completeness is no longer left at `100.0`
- `completeness_adjusted_for_missing.missing_count` matches the stable indices count

**Step 3: Report exact files and commands used**

Summarize the root cause and verification evidence for the user.
