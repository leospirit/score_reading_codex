# Prosody Copy Update Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Update the stress/rhythm module wording in Report Builder and the legacy report page so it reads as a guidance module rather than a strict right/wrong judgment, without changing any scoring or classification logic.

**Architecture:** Keep this as a pure copy change. Update titles, legends, section headers, and explanatory tips in the React Report Builder and the legacy Jinja report template. Add a small regression test that checks old hard-judgment wording is removed and the new guidance wording is present.

**Tech Stack:** React + TypeScript, Jinja2 template, Python unittest for copy regression.

---

### Task 1: Add wording regression test

**Files:**
- Create: `D:/score_reading_fresh/tests/test_prosody_copy.py`
- Modify: `D:/score_reading_fresh/src/pages/ReportBuilder.tsx`
- Modify: `D:/score_reading_fresh/score_reading/src/report/templates/report.html.j2`

**Step 1: Write the failing test**

Add a simple file-content regression test that asserts:
1. New wording exists in `ReportBuilder.tsx` and `report.html.j2`.
2. Old wording such as `Correct stress`, `Incorrect stress`, `Unstressed`, and `重读准确率` is removed from the relevant sections.

**Step 2: Run test to verify it fails**

Run: `python D:/score_reading_fresh/tests/test_prosody_copy.py`
Expected: FAIL because the old wording is still in the files.

### Task 2: Update Report Builder copy

**Files:**
- Modify: `D:/score_reading_fresh/src/pages/ReportBuilder.tsx`

**Step 1: Replace wording only**

Update:
1. Module title to `重弱与节奏提示`.
2. Legend labels to `自然 / 需调整 / 轻读`.
3. Add a small explanatory note that large balls are relatively prominent and red means needs adjustment.
4. Best-sentence title to `节奏自然的一句`.
5. Problem-sentence title to `最值得调整的一句`.
6. Remove `准确率` wording from the visible UI in this module.
7. Keep existing data fields and rendering structure unchanged.

### Task 3: Update legacy report template copy

**Files:**
- Modify: `D:/score_reading_fresh/score_reading/src/report/templates/report.html.j2`

**Step 1: Replace wording only**

Update:
1. Section title from `Prosody Analysis (Stress & Rhythm)` to a guidance-style title.
2. Legend labels to `Natural / Needs Adjustment / Light` or matching Chinese wording if already localized there.
3. Issue titles/actions so they read as guidance rather than strict stress errors where possible.

### Task 4: Verify

**Files:**
- Mention modified files only.

**Step 1: Run regression test**

Run: `python D:/score_reading_fresh/tests/test_prosody_copy.py`
Expected: PASS

**Step 2: Run frontend build**

Run: `npm --prefix D:/score_reading_fresh run build`
Expected: PASS

**Step 3: Restart services if needed**

Because `web` serves built assets, rebuild/restart `web`. Because `api` bind-mounts `score_reading`, restart `api` only if needed to ensure fresh template usage.
