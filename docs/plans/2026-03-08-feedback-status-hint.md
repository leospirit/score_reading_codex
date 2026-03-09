# Feedback Status Hint Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep Azure Integrated Feedback visible at all times while showing a small non-blocking optimization status hint in the report page.

**Architecture:** Extract a tiny pure helper that maps `feedback_optimization` state plus current provider tag into a UI hint. Reuse existing Integrated Feedback body text unchanged, and render only a short status line in the module header. This keeps Azure content visible and avoids layout churn.

**Tech Stack:** React, TypeScript, Node built-in test runner

---

### Task 1: Add a failing status-hint test

**Files:**
- Create: `D:\score_reading_fresh\src\pages\reportFeedbackStatus.ts`
- Create: `D:\score_reading_fresh\src\pages\reportFeedbackStatus.test.ts`

**Step 1: Write the failing test**

Cover these behaviors:
- `pending/optimizing` with Azure provider returns a hint that Azure is being shown now
- `pending` with `last_error` returns a failure hint while preserving Azure wording
- non-Azure or final states return no hint

**Step 2: Run test to verify it fails**

Run: `node --test D:\score_reading_fresh\src\pages\reportFeedbackStatus.test.ts`

Expected: FAIL because helper does not exist yet.

**Step 3: Write minimal implementation**

Implement a pure exported function that returns either `null` or `{ tone, text }`.

**Step 4: Run test to verify it passes**

Run: `node --test D:\score_reading_fresh\src\pages\reportFeedbackStatus.test.ts`

Expected: PASS

### Task 2: Render the hint in ReportBuilder

**Files:**
- Modify: `D:\score_reading_fresh\src\pages\ReportBuilder.tsx`

**Step 1: Import the helper and derive the hint**

Add one memoized value near the existing `feedbackSourceTag` and `activeIntegratedFeedbackText`.

**Step 2: Render a small status line**

Show the hint in the `ai_feedback` header area only.
- Do not replace body text
- Do not hide `[az]`
- Keep styles subdued

**Step 3: Verify build**

Run: `npm.cmd run build`

Expected: PASS
