# Teacher Phrase Delete Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow teachers to delete their own custom phrase-bank entries from Report Builder, while preventing deletion of built-in phrases.

**Architecture:** Add a small server-side delete helper plus a `DELETE /api/teacher-phrases/{phrase_id}` endpoint that only removes `builtin=false` entries from `data/teacher_phrase_bank.json`. In the Report Builder phrase suggestion area, render a compact delete control only for custom phrases and remove the item from local state after a successful delete.

**Tech Stack:** FastAPI, JSON file persistence, React + TypeScript.

---

### Task 1: Add backend regression tests for phrase deletion

**Files:**
- Create: `D:/score_reading_fresh/tests/test_teacher_phrase_delete.py`
- Modify: `D:/score_reading_fresh/server.py`

**Step 1: Write the failing tests**

Add tests that prove:
1. Deleting a custom phrase removes it from the phrase bank and returns the deleted item.
2. Deleting a built-in phrase is rejected and leaves the phrase bank unchanged.

Use a temporary JSON file and monkeypatch `server.TEACHER_PHRASE_BANK_PATH` so the test does not touch real data.

**Step 2: Run test to verify it fails**

Run: `pytest D:/score_reading_fresh/tests/test_teacher_phrase_delete.py -q`
Expected: FAIL because the helper/endpoint behavior does not exist yet.

### Task 2: Implement minimal backend delete path

**Files:**
- Modify: `D:/score_reading_fresh/server.py`

**Step 1: Write minimal implementation**

Add:
1. A helper that deletes one teacher phrase from the loaded bank.
2. Validation that rejects unknown ids with `404`.
3. Validation that rejects `builtin=true` items with `400`.
4. Persistence back to `data/teacher_phrase_bank.json`.
5. `DELETE /api/teacher-phrases/{phrase_id}` endpoint returning the deleted item.

**Step 2: Run test to verify it passes**

Run: `pytest D:/score_reading_fresh/tests/test_teacher_phrase_delete.py -q`
Expected: PASS

### Task 3: Add Report Builder delete affordance

**Files:**
- Modify: `D:/score_reading_fresh/src/pages/ReportBuilder.tsx`

**Step 1: Add UI state and handler**

Implement a delete handler that:
1. Calls `DELETE /api/teacher-phrases/{phrase_id}`.
2. Removes the item from `teacherPhrases` state on success.
3. Shows a concise success/error notice.
4. Tracks `isDeletingTeacherPhraseId` so only the clicked item shows a busy state.

**Step 2: Render delete control**

In the teacher phrase suggestion chips:
1. Keep built-in phrases unchanged.
2. For custom phrases (`builtin === false`), render a small `删除` button/icon on the chip edge.
3. Ensure clicking delete does not also insert the phrase into the editor.

**Step 3: Verify build**

Run: `npm --prefix D:/score_reading_fresh run build`
Expected: PASS

### Task 4: Verify end-to-end behavior

**Files:**
- Mention modified files only.

**Step 1: Restart or reload as needed**

Because `api` uses a bind mount for `./score_reading`, restart `speech-scoring-api` after the backend change.

**Step 2: Final verification**

Run:
- `pytest D:/score_reading_fresh/tests/test_teacher_phrase_delete.py -q`
- `npm --prefix D:/score_reading_fresh run build`

Expected:
- Backend delete guard works
- Frontend builds cleanly
- Only custom phrases are deletable
