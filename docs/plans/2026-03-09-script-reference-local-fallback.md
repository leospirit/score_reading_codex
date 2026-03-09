# Script Reference Local Fallback Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the preheat text API return an immediate deterministic local preheat note when Gemini script-reference generation is unavailable or still pending.

**Architecture:** Add a local fallback script-reference builder in `script_reference.py` that derives pause template, pace norm, and semantic pronunciation priors without any LLM call. Update the `/api/script-reference` route to schedule Gemini enhancement in the background but return the local fallback payload immediately when no modern cache is ready.

**Tech Stack:** FastAPI, Python, existing script reference pipeline, pytest.

---
