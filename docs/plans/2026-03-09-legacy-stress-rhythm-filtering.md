# Legacy Stress Rhythm Filtering Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the legacy report page's `Stress & Rhythm Guide` avoid omission zones and half-sentences when choosing its best and worst example sentences.

**Architecture:** Keep the current client-side sentence evaluation in `report.html.j2`, but add three minimal filters: punctuation-aware splitting, no-missing best-sentence selection, and completeness gating for worst-sentence selection. Validate with template regression assertions rather than introducing a larger JS test harness.

**Tech Stack:** Jinja2 template, inline JavaScript, Python regression script

---

### Task 1: Add failing regression assertions

**Files:**
- Modify: `D:\score_reading_fresh\tests\test_prosody_copy.py`
- Read: `D:\score_reading_fresh\score_reading\src\report\templates\report.html.j2`

**Step 1: Write failing assertions**

Assert the legacy template contains markers for:
- reading words as the data source
- skipping missing-containing sentences for best selection
- filtering incomplete fragments for worst selection
- punctuation-aware splitting support

**Step 2: Run regression to verify it fails**

Run: `python D:\score_reading_fresh\tests\test_prosody_copy.py`
Expected: FAIL because the new markers are not present yet.

### Task 2: Implement minimal template filtering

**Files:**
- Modify: `D:\score_reading_fresh\score_reading\src\report\templates\report.html.j2`

**Step 1: Improve sentence splitting**

Add light punctuation-aware boundary logic to `splitIntoIntonationSentences(words)`.

**Step 2: Filter best-sentence candidates**

In `pickBestAndWorst(sentenceStats)`, exclude sentences containing missing tokens from the best-sentence pool.

**Step 3: Filter worst-sentence candidates**

Reject incomplete tail fragments and very short low-information spans from worst-sentence selection.

**Step 4: Keep current guide copy and scoring behavior otherwise unchanged**

### Task 3: Verify

**Files:**
- Test: `D:\score_reading_fresh\tests\test_prosody_copy.py`

**Step 1: Run regression**

Run: `python D:\score_reading_fresh\tests\test_prosody_copy.py`
Expected: PASS

**Step 2: Restart API**

Run: `docker restart speech-scoring-api`
Expected: container restarts successfully so freshly generated reports use the updated template.
