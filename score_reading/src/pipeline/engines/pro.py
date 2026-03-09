"""
Speech scoring - Pro engine.

Gemini/Azure are preferred. When cloud engines fail, default to fast fallback
for low latency. Local wav2vec2+whisper path is still available as a backup.
"""
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

from src.config import config
from src.models import Alignment, WordTag

logger = logging.getLogger(__name__)


class ProEngine:
    def __init__(self) -> None:
        self.whisper = None

    def _get_whisper(self):
        if self.whisper is None:
            from src.pipeline.engines.whisper_engine import WhisperEngine
            self.whisper = WhisperEngine()
        return self.whisper

    @staticmethod
    def _normalize_token(token: str) -> str:
        norm = re.sub(r"[^a-z0-9']+", "", str(token or "").lower())
        return norm.strip("'")

    def _overlay_gemini_annotations(
        self,
        azure_alignment: Alignment,
        gemini_alignment: Alignment,
    ) -> tuple[Alignment, dict[str, Any]]:
        """
        Keep Azure timing/scoring basis, but let Gemini own word-level error labels.
        This is a display/diagnostic overlay only; factor scores remain Azure-native.
        """
        from difflib import SequenceMatcher

        if not azure_alignment.words:
            return azure_alignment, {"mode": "empty_azure"}
        if not gemini_alignment.words:
            return azure_alignment, {"mode": "empty_gemini"}

        word_ok = float(config.get("analysis.word_thresholds.ok", 65))
        word_weak = float(config.get("analysis.word_thresholds.weak", 40))
        baseline_floor = max(word_weak + 10.0, 55.0)

        # Reset word tags first so reading highlights only reflect Gemini judgement.
        for w in azure_alignment.words:
            if float(getattr(w, "score", 0.0) or 0.0) <= 0.0:
                w.score = baseline_floor
            w.tag = WordTag.OK
            w.diagnosis = ""

        base_tokens = [self._normalize_token(w.word) for w in azure_alignment.words]
        gem_tokens = [self._normalize_token(w.word) for w in gemini_alignment.words]
        base_nonempty = [t for t in base_tokens if t]
        gem_nonempty = [t for t in gem_tokens if t]
        coverage_ratio = len(gem_nonempty) / max(1, len(base_nonempty))

        applied = 0
        missing_applied = 0
        weak_applied = 0
        poor_applied = 0
        skipped_placeholder = 0
        missing_indices: set[int] = set()
        track_missing_indices = False

        def _is_placeholder_judgement(gem_word: Any) -> bool:
            text = str(getattr(gem_word, "diagnosis", "") or "").lower()
            if not text:
                return False
            placeholder_markers = (
                "sparse token coverage",
                "kept acoustic judgement",
                "alignment gap only",
                "pending further evidence",
            )
            return any(marker in text for marker in placeholder_markers)

        def _apply(base_idx: int, gem_word: Any) -> None:
            nonlocal applied, missing_applied, weak_applied, poor_applied, skipped_placeholder
            base_word = azure_alignment.words[base_idx]
            if _is_placeholder_judgement(gem_word):
                skipped_placeholder += 1
                return
            g_tag = getattr(gem_word, "tag", WordTag.OK)
            base_word.tag = g_tag
            if g_tag == WordTag.MISSING:
                base_word.score = 0.0
                missing_applied += 1
                if track_missing_indices:
                    missing_indices.add(base_idx)
            elif g_tag == WordTag.WEAK:
                base_word.score = min(float(base_word.score or 0.0), max(word_weak + 1.0, word_ok - 1.0))
                weak_applied += 1
            elif g_tag == WordTag.POOR:
                base_word.score = min(float(base_word.score or 0.0), max(0.0, word_weak - 2.0))
                poor_applied += 1
            diag = str(getattr(gem_word, "diagnosis", "") or "").strip()
            if diag:
                base_word.diagnosis = diag
            applied += 1

        # Sparse output: Gemini likely returned issue words only.
        if coverage_ratio < 0.70:
            mode = "sparse_issue_overlay"
            gem_issue_pool: dict[str, list[Any]] = {}
            for gw in gemini_alignment.words:
                key = self._normalize_token(gw.word)
                if not key:
                    continue
                has_issue = (getattr(gw, "tag", WordTag.OK) != WordTag.OK) or bool(str(getattr(gw, "diagnosis", "") or "").strip())
                if not has_issue:
                    continue
                gem_issue_pool.setdefault(key, []).append(gw)

            for idx, bw in enumerate(azure_alignment.words):
                key = self._normalize_token(bw.word)
                candidates = gem_issue_pool.get(key) or []
                if not candidates:
                    continue
                _apply(idx, candidates.pop(0))
        else:
            mode = "sequence_overlay"
            track_missing_indices = True
            matcher = SequenceMatcher(None, base_tokens, gem_tokens)
            for op, i1, i2, j1, j2 in matcher.get_opcodes():
                if op == "equal":
                    for k in range(min(i2 - i1, j2 - j1)):
                        _apply(i1 + k, gemini_alignment.words[j1 + k])
                elif op == "replace":
                    base_len = i2 - i1
                    gem_len = j2 - j1
                    if gem_len <= 0:
                        continue
                    for k in range(base_len):
                        mapped = min(gem_len - 1, max(0, int(k * gem_len / max(base_len, 1))))
                        _apply(i1 + k, gemini_alignment.words[j1 + mapped])
                elif op == "insert":
                    continue
                elif op == "delete":
                    continue

        stats = {
            "mode": mode,
            "gemini_coverage_ratio": round(coverage_ratio, 3),
            "applied_words": int(applied),
            "skipped_placeholder": int(skipped_placeholder),
            "missing_applied": int(missing_applied),
            "missing_indices": sorted(missing_indices) if track_missing_indices else [],
            "weak_applied": int(weak_applied),
            "poor_applied": int(poor_applied),
        }
        return azure_alignment, stats

    def run(
        self,
        wav_path: Path,
        script_text: str,
        work_dir: Path,
    ) -> tuple[Alignment, dict[str, Any]]:
        logger.info("Pro engine starting")
        cloud_errors: list[str] = []

        # 1) Run cloud engines in parallel to reduce end-to-end latency.
        azure_result: tuple[Alignment, dict[str, Any]] | None = None
        gemini_result: tuple[Alignment, dict[str, Any]] | None = None
        azure_key = config.get("engines.azure.api_key") or os.getenv("AZURE_API_KEY")
        gemini_key = config.get("engines.gemini.api_key") or os.getenv("GEMINI_API_KEY")
        azure_future = None
        gemini_future = None

        pool = ThreadPoolExecutor(max_workers=2)
        try:
            if azure_key:
                from src.pipeline.engines.azure import run_azure_engine
                logger.info("Pro engine: starting Azure baseline scoring")
                azure_future = pool.submit(run_azure_engine, wav_path, script_text, work_dir)
            if gemini_key:
                from src.pipeline.engines.gemini import run_gemini_engine
                logger.info("Pro engine: starting Gemini annotation pass")
                gemini_future = pool.submit(run_gemini_engine, wav_path, script_text, work_dir)

            if azure_future is not None:
                try:
                    azure_result = azure_future.result()
                except Exception as e:
                    cloud_errors.append(f"azure: {e}")
                    logger.warning("Azure failed inside Pro; trying Gemini fallback: %s", e)

            if gemini_future is not None:
                try:
                    if azure_result is not None:
                        raw_wait = config.get("engines.pro.gemini_overlay_wait_sec", 10.0)
                        try:
                            overlay_wait_sec = max(0.0, float(raw_wait))
                        except Exception:
                            overlay_wait_sec = 10.0
                        gemini_result = gemini_future.result(timeout=overlay_wait_sec)
                    else:
                        gemini_result = gemini_future.result()
                except FutureTimeoutError:
                    cloud_errors.append("gemini: overlay_wait_timeout")
                    logger.warning(
                        "Gemini overlay timed out after Azure baseline; continue with Azure-only annotation."
                    )
                except Exception as e:
                    cloud_errors.append(f"gemini: {e}")
                    logger.warning("Gemini failed inside Pro annotation pass: %s", e)
        finally:
            if gemini_future is not None and gemini_result is None and not gemini_future.done():
                gemini_future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

        # Azure baseline exists: keep Azure scores unchanged, overlay Gemini labels if available.
        if azure_result is not None:
            alignment, engine_raw = azure_result
            engine_raw["engine_type"] = "pro_azure_scoring"
            raw_prefer_gemini = config.get("engines.gemini.prefer_gemini", True)
            prefer_gemini = str(raw_prefer_gemini).strip().lower() not in ("0", "false", "no", "off")
            raw_overlay_retry_count = config.get("engines.pro.gemini_overlay_retry_count", 1)
            try:
                overlay_retry_count = int(raw_overlay_retry_count)
            except Exception:
                overlay_retry_count = 1
            if overlay_retry_count < 0:
                overlay_retry_count = 0

            # GE priority: if Azure is ready but Gemini annotation missed, retry Gemini overlay once more.
            if gemini_result is None and prefer_gemini and gemini_key and overlay_retry_count > 0:
                from src.pipeline.engines.gemini import run_gemini_engine
                for retry_idx in range(overlay_retry_count):
                    try:
                        logger.info(
                            "Gemini overlay retry %d/%d on Azure baseline",
                            retry_idx + 1,
                            overlay_retry_count,
                        )
                        gemini_result = run_gemini_engine(wav_path, script_text, work_dir)
                        logger.info("Gemini overlay retry succeeded.")
                        break
                    except Exception as e:
                        cloud_errors.append(f"gemini_retry_{retry_idx + 1}: {e}")
                        logger.warning(
                            "Gemini overlay retry %d/%d failed: %s",
                            retry_idx + 1,
                            overlay_retry_count,
                            e,
                        )
                        time.sleep(0.25)

            if gemini_result is not None:
                gemini_alignment, gemini_raw = gemini_result
                alignment, anno_stats = self._overlay_gemini_annotations(alignment, gemini_alignment)
                engine_raw["annotation_source"] = "gemini"
                engine_raw["gemini_annotation_stats"] = anno_stats
                engine_raw["gemini_missing_indices"] = list(anno_stats.get("missing_indices") or [])
                if isinstance(gemini_raw, dict):
                    if gemini_raw.get("ai_referee"):
                        engine_raw["ai_referee"] = gemini_raw.get("ai_referee")
                    if gemini_raw.get("integrated_feedback"):
                        engine_raw["integrated_feedback"] = gemini_raw.get("integrated_feedback")
                    if gemini_raw.get("script_reference"):
                        engine_raw["script_reference"] = gemini_raw.get("script_reference")
                    if gemini_raw.get("detected_transcript"):
                        engine_raw["gemini_detected_transcript"] = gemini_raw.get("detected_transcript")
                logger.info("Applied Gemini annotation overlay on Azure baseline: %s", anno_stats)
            else:
                engine_raw["annotation_source"] = "azure_only"

            if cloud_errors:
                engine_raw["pro_cloud_errors"] = cloud_errors
            return alignment, engine_raw

        raw_strict_azure = config.get("engines.pro.strict_azure_scoring", True)
        strict_azure_scoring = str(raw_strict_azure).strip().lower() not in ("0", "false", "no", "off")
        if strict_azure_scoring:
            detail = " | ".join(cloud_errors) if cloud_errors else "Azure baseline unavailable."
            raise RuntimeError(f"Azure scoring unavailable (strict_azure_scoring=true): {detail}")

        # Azure unavailable but Gemini succeeded: optional fallback when strict mode is disabled.
        if gemini_result is not None:
            alignment, engine_raw = gemini_result
            engine_raw["engine_type"] = "pro_gemini_only_fallback"
            engine_raw["score_fallback"] = "gemini"
            if cloud_errors:
                engine_raw["pro_cloud_errors"] = cloud_errors
            return alignment, engine_raw

        # Accuracy-first mode: never output local fallback scores as final results.
        raw_require_cloud = config.get("engines.pro.require_cloud_success", True)
        require_cloud_success = str(raw_require_cloud).strip().lower() not in ("0", "false", "no", "off")
        if require_cloud_success:
            detail = " | ".join(cloud_errors) if cloud_errors else "No cloud engine available."
            raise RuntimeError(f"Cloud scoring unavailable (accuracy-first mode): {detail}")

        # 3) Prefer fast fallback for latency
        local_fallback_engine = str(config.get("engines.pro.local_fallback_engine", "fast")).strip().lower()
        if local_fallback_engine == "fast":
            try:
                from src.pipeline.engines.fast import run_fast_engine
                logger.warning("Pro fallback: cloud unavailable, switching to fast engine")
                alignment, engine_raw = run_fast_engine(wav_path, script_text, work_dir)
                engine_raw["engine_type"] = "pro_fast_fallback"
                if cloud_errors:
                    engine_raw["pro_cloud_errors"] = cloud_errors
                return alignment, engine_raw
            except Exception as e:
                cloud_errors.append(f"fast: {e}")
                logger.warning("Fast fallback failed; switching to local Pro path: %s", e)

        # 4) Local enhanced fallback: wav2vec2 + whisper semantic cross-check
        from src.pipeline.engines.wav2vec2 import Wav2Vec2Engine

        wav2vec2 = Wav2Vec2Engine()
        alignment, engine_raw = wav2vec2.run(wav_path, script_text, work_dir)

        try:
            whisper_alignment, _ = self._get_whisper().run(wav_path, script_text, work_dir)
            transcript_words = [
                w.word.lower().strip(".,!?;:'\\\"")
                for w in whisper_alignment.words
                if w.tag != WordTag.MISSING
            ]

            semantic_conflicts = 0
            conflict_details = []

            for i, word_align in enumerate(alignment.words):
                if word_align.tag == WordTag.MISSING:
                    continue

                target = word_align.word.lower().strip(".,!?;:'\\\"")
                found_word = None
                search_window = transcript_words[max(0, i - 2): min(len(transcript_words), i + 3)]

                if target in search_window:
                    found_word = target
                else:
                    for sw in search_window:
                        if sw.startswith(target[:3]) or target.startswith(sw[:3]):
                            found_word = sw
                            break

                if found_word != target and (word_align.tag == WordTag.OK or word_align.score > 70):
                    logger.warning(
                        "AI semantic mismatch: expected '%s', heard '%s'",
                        target,
                        found_word or "Nothing",
                    )
                    word_align.tag = WordTag.WEAK
                    word_align.score = min(word_align.score, 68.5)
                    semantic_conflicts += 1
                    conflict_details.append(
                        {
                            "word": word_align.word,
                            "expected": target,
                            "got": found_word or "???",
                        }
                    )

            engine_raw["ai_referee"] = {
                "conflicts": semantic_conflicts,
                "conflict_details": conflict_details,
                "whisper_transcript": transcript_words,
                "status": "completed",
            }
            logger.info("Pro semantic check done; conflicts=%d", semantic_conflicts)

        except Exception as e:
            logger.error("Pro semantic referee failed: %s", e)
            engine_raw["ai_referee"] = {"status": "failed", "error": str(e)}

        engine_raw["engine_type"] = "pro_v1"
        if cloud_errors:
            engine_raw["pro_cloud_errors"] = cloud_errors
        return alignment, engine_raw


_engine_instance: ProEngine | None = None


def get_pro_engine() -> ProEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = ProEngine()
    return _engine_instance


def run_pro_engine(
    wav_path: Path,
    script_text: str,
    work_dir: Path,
) -> tuple[Alignment, dict[str, Any]]:
    engine = get_pro_engine()
    return engine.run(wav_path, script_text, work_dir)
