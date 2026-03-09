# Legacy Stress Rhythm Copy Softening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Soften legacy HTML report stress guidance copy so it reads as practice-oriented advice instead of hard verdicts.

**Architecture:** Keep the existing legacy `Stress & Rhythm Guide` structure and selection logic. Change only the wording strings in the template and lock the new copy with a small regression script.

**Tech Stack:** Python regression script, Jinja2 HTML template, legacy inline JavaScript.

---

### Task 1: Lock the new wording in a failing regression test

**Files:**
- Modify: `D:/score_reading_fresh/tests/test_prosody_copy.py`
- Test: `D:/score_reading_fresh/tests/test_prosody_copy.py`

**Step 1: Write the failing test**
Add assertions for the new wording and assertions that old verdict-style wording is absent.

**Step 2: Run test to verify it fails**
Run: `python D:\score_reading_fresh\tests\test_prosody_copy.py`
Expected: FAIL because legacy template still contains the old wording.

**Step 3: Write minimal implementation**
Update the legacy template wording only.

**Step 4: Run test to verify it passes**
Run: `python D:\score_reading_fresh\tests\test_prosody_copy.py`
Expected: PASS.

### Task 2: Verify build and restart API

**Files:**
- Modify: `D:/score_reading_fresh/score_reading/src/report/templates/report.html.j2`

**Step 1: Verify template regression**
Run: `python D:\score_reading_fresh\tests\test_prosody_copy.py`
Expected: PASS.

**Step 2: Restart backend container**
Run: `docker restart speech-scoring-api`
Expected: container restarts successfully.
