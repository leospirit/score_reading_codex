"""
Speech scoring - Pro engine.

Gemini/Azure are preferred. When cloud engines fail, default to fast fallback
for low latency. Local wav2vec2+whisper path is still available as a backup.
"""
import logging
import os
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

    def run(
        self,
        wav_path: Path,
        script_text: str,
        work_dir: Path,
    ) -> tuple[Alignment, dict[str, Any]]:
        logger.info("Pro engine starting")
        cloud_errors: list[str] = []

        # 1) Gemini first
        gemini_key = config.get("engines.gemini.api_key") or os.getenv("GEMINI_API_KEY")
        if gemini_key:
            try:
                from src.pipeline.engines.gemini import run_gemini_engine
                logger.info("Pro engine: using Gemini first")
                return run_gemini_engine(wav_path, script_text, work_dir)
            except Exception as e:
                cloud_errors.append(f"gemini: {e}")
                logger.warning("Gemini failed inside Pro; trying next option: %s", e)

        # 2) Azure second
        azure_key = config.get("engines.azure.api_key") or os.getenv("AZURE_API_KEY")
        if azure_key:
            try:
                from src.pipeline.engines.azure import run_azure_engine
                logger.info("Pro engine: trying Azure")
                return run_azure_engine(wav_path, script_text, work_dir)
            except Exception as e:
                cloud_errors.append(f"azure: {e}")
                logger.warning("Azure failed inside Pro; trying local fallback: %s", e)

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
