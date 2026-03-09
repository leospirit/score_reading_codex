# Missing Phrase Realignment Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Improve missing-word localization so Azure evidence can re-anchor phrase omissions like `going to` instead of mislabeling adjacent words such as `I'm`.

**Architecture:** Keep Azure as the sole source of missing-word evidence. Add a small phrase-level realignment pass inside `derive_stable_missing_indices()` that only activates when Azure alignment-based missing conflicts with a more coherent local transcript anchor in a short window.

**Tech Stack:** Python, existing Azure transcript/alignment pipeline, pytest

---

### Task 1: Add failing regression tests for phrase-drift cases

**Files:**
- Modify: `D:\score_reading_fresh\score_reading\tests\test_analyze.py`
- Read: `D:\score_reading_fresh\score_reading\src\pipeline\analyze.py`

**Step 1: Write the failing tests**

Add focused tests for:
- `You too. I'm bring back ...` should localize missing words to `going`, `to`, not `I'm`
- `Are you going ski?` should localize missing word to `to`

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=D:\score_reading_fresh\score_reading pytest D:\score_reading_fresh\score_reading\tests\test_analyze.py -q`
Expected: FAIL because current logic returns alignment-shifted indices.

### Task 2: Implement minimal phrase realignment in analyze.py

**Files:**
- Modify: `D:\score_reading_fresh\score_reading\src\pipeline\analyze.py`
- Test: `D:\score_reading_fresh\score_reading\tests\test_analyze.py`

**Step 1: Add a local realignment helper**

Implement a narrow helper that:
- compares alignment-based missing indices with transcript-anchor candidates
- works only in a short local window
- replaces alignment indices only when transcript-anchor gives a more coherent contiguous phrase omission
- prefers multi-token phrase candidates over a shifted single-token miss when both are Azure-derived and locally aligned

**Step 2: Call helper from derive_stable_missing_indices()**

Keep current ordering, but before returning `alignment_missing`, allow the helper to replace obviously shifted alignment misses.

**Step 3: Run tests to verify they pass**

Run: `PYTHONPATH=D:\score_reading_fresh\score_reading pytest D:\score_reading_fresh\score_reading\tests\test_analyze.py -q`
Expected: PASS

### Task 3: Re-verify completeness behavior remains stable

**Files:**
- Reuse existing tests

**Step 1: Run nearby completeness regression tests**

Run: `PYTHONPATH=D:\score_reading_fresh\score_reading pytest D:\score_reading_fresh\score_reading\tests\test_normalize_completeness.py -q`
Expected: PASS

**Step 2: Summarize what changed**

Report exact missing indices before/after for the phrase-drift examples.
