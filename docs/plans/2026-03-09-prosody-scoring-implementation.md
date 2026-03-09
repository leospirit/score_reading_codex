# Prosody Scoring Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the current heuristic stress/rhythm fallback with a prominence-driven scoring pipeline that supports fixed-script reading first and can later extend to free speaking.

**Architecture:** Introduce a new prosody analysis layer in backend scoring that computes per-word prominence and sentence rhythm metrics from acoustic features. Keep the existing visual components, but rewire bubble size/color and sentence selection to the new backend outputs. Phase 1 targets fixed-script tasks; phase 2 extends the same framework to free speaking.

**Tech Stack:** Python audio analysis, existing alignment pipeline, report JSON generation, React Report Builder.

---

### Task 1: Add feature extraction scaffolding

**Files:**
- Modify: `D:/score_reading_fresh/score_reading/src/pipeline/analyze.py`
- Modify: `D:/score_reading_fresh/score_reading/src/models.py`
- Test: new prosody feature tests under `D:/score_reading_fresh/score_reading/tests/`

**Step 1: Write failing tests**
Add tests for per-word prominence feature normalization:
- duration normalization
- missing-word handling
- function-word down-weighting behavior

**Step 2: Verify tests fail**
Run targeted pytest for the new test file.
Expected: FAIL before implementation.

**Step 3: Implement minimal code**
Add backend fields for:
- `prominence_score`
- optional confidence fields
- helper functions to compute normalized duration/energy/pitch placeholders

### Task 2: Build sentence rhythm scoring

**Files:**
- Modify: `D:/score_reading_fresh/score_reading/src/pipeline/analyze.py`
- Modify: `D:/score_reading_fresh/score_reading/src/pipeline/normalize.py`
- Test: new rhythm-score tests

**Step 1: Write failing tests**
Cover:
- content-vs-function contrast scoring
- flat rhythm penalty
- over-stressed function word penalty

**Step 2: Implement minimal code**
Compute:
- `sentence_rhythm_score`
- `prosodic_contrast_score`
- sentence-level issue extraction

### Task 3: Generate structured prosody analysis payload

**Files:**
- Modify: `D:/score_reading_fresh/score_reading/src/pipeline/analyze.py`
- Modify: `D:/score_reading_fresh/score_reading/src/report/render_html.py`

**Step 1: Write failing tests**
Assert generated JSON includes:
- best sentence
- needs-adjustment sentence
- word display classes or raw values needed for display mapping

**Step 2: Implement minimal code**
Emit backend-owned prosody payload rather than relying on UI fallback heuristics.

### Task 4: Rewire Report Builder to backend prosody payload

**Files:**
- Modify: `D:/score_reading_fresh/src/pages/ReportBuilder.tsx`
- Test: small wording/shape regression as needed

**Step 1: Remove fallback dominance**
Keep fallback only as emergency display logic.
Prefer backend-generated prosody analysis if present.

**Step 2: Map display**
- large bubble = relatively prominent
- small bubble = relatively light
- green/red/gray from backend judgment + confidence
- best sentence / needs-adjustment sentence from backend scores

### Task 5: Phase 2 extension for free speaking

**Files:**
- Modify later after phase 1 stabilizes

**Step 1: Reuse prominence layer**
Do not depend on exact script template.
Use acoustic + linguistic priors only.

**Step 2: Add guarded rollout**
Only enable free-speaking prosody analysis when alignment/confidence meets minimum thresholds.

### Task 6: Verification

**Run:**
- targeted pytest for new prosody tests
- `python -m py_compile` for changed backend files
- `npm --prefix D:/score_reading_fresh run build`
- regenerate one fixed-script sample and inspect prosody output

**Expected:**
- backend produces stable prosody payload
- frontend displays updated bubble logic from backend data
- no regressions to unrelated scoring modules
