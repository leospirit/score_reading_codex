"""
Azure pronunciation scoring engine using Microsoft Speech SDK.
"""
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from src.config import config
from src.models import (
    Alignment,
    PhonemeAlignment,
    PhonemeTag,
    WordAlignment,
    WordTag,
)

logger = logging.getLogger(__name__)

_speech_sdk = None


def get_speech_sdk():
    global _speech_sdk
    if _speech_sdk is None:
        try:
            import azure.cognitiveservices.speech as sdk
            _speech_sdk = sdk
        except ImportError:
            logger.error("azure-cognitiveservices-speech is not installed")
            raise ImportError("Please install azure-cognitiveservices-speech")
    return _speech_sdk


class AzureEngine:
    def __init__(self) -> None:
        self.api_key = config.get("engines.azure.api_key")
        self.region = config.get("engines.azure.region", "eastus")
        self.language = config.get("engines.azure.language", "en-US")

        if not self.api_key:
            logger.warning("Azure API key is not configured; Azure engine will fail.")

    def run(
        self,
        wav_path: Path,
        script_text: str,
        work_dir: Optional[Path] = None,
    ) -> tuple[Alignment, dict[str, Any]]:
        sdk = get_speech_sdk()

        if not self.api_key:
            raise ValueError("Azure API key is required for AzureEngine")

        logger.info("Azure engine start: %s", wav_path.name)

        speech_config = sdk.SpeechConfig(subscription=self.api_key, region=self.region)
        audio_config = sdk.audio.AudioConfig(filename=str(wav_path))

        # Relax segmentation so pauses do not truncate long recordings too early.
        try:
            speech_config.set_property(
                sdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
                str(config.get("engines.azure.segmentation_silence_timeout_ms", 2000)),
            )
        except Exception:
            pass

        pronunciation_config = sdk.PronunciationAssessmentConfig(
            reference_text=script_text,
            grading_system=sdk.PronunciationAssessmentGradingSystem.HundredMark,
            granularity=sdk.PronunciationAssessmentGranularity.Phoneme,
            enable_miscue=True,
        )
        try:
            pronunciation_config.enable_prosody_assessment()
        except Exception:
            logger.info("Azure SDK does not support explicit prosody toggle.")

        speech_recognizer = sdk.SpeechRecognizer(
            speech_config=speech_config,
            language=self.language,
            audio_config=audio_config,
        )
        pronunciation_config.apply_to(speech_recognizer)

        # Primary: continuous recognition for long files.
        try:
            merged_data = self._recognize_continuous(speech_recognizer, sdk)
            return self._parse_result(merged_data, script_text)
        except Exception as e:
            logger.warning("Azure continuous recognition failed; fallback to recognize_once: %s", e)

        # Fallback: one-shot recognition.
        result = speech_recognizer.recognize_once()
        if result.reason == sdk.ResultReason.RecognizedSpeech:
            return self._parse_result(result, script_text)
        if result.reason == sdk.ResultReason.NoMatch:
            raise RuntimeError("Azure: no valid speech recognized")
        if result.reason == sdk.ResultReason.Canceled:
            details = result.cancellation_details
            raise RuntimeError(f"Azure canceled: {details.reason}. details: {details.error_details}")
        raise RuntimeError(f"Azure unknown error reason={result.reason}")

    def _recognize_continuous(self, speech_recognizer: Any, sdk: Any) -> dict[str, Any]:
        segment_payloads: list[dict[str, Any]] = []
        done = threading.Event()
        cancel_error: str | None = None

        def on_recognized(evt: Any) -> None:
            res = getattr(evt, "result", None)
            if res is None or res.reason != sdk.ResultReason.RecognizedSpeech:
                return
            raw = res.properties.get(sdk.PropertyId.SpeechServiceResponse_JsonResult)
            if not raw:
                return
            try:
                segment_payloads.append(json.loads(raw))
            except Exception as parse_err:
                logger.warning("Azure segment JSON parse failed: %s", parse_err)

        def on_canceled(evt: Any) -> None:
            nonlocal cancel_error
            res = getattr(evt, "result", None)
            if res is not None and res.reason == sdk.ResultReason.Canceled:
                details = res.cancellation_details
                cancel_error = f"{details.reason}: {details.error_details}"
            done.set()

        def on_session_stopped(evt: Any) -> None:
            done.set()

        speech_recognizer.recognized.connect(on_recognized)
        speech_recognizer.canceled.connect(on_canceled)
        speech_recognizer.session_stopped.connect(on_session_stopped)

        speech_recognizer.start_continuous_recognition()
        if not done.wait(timeout=float(config.get("engines.azure.continuous_timeout_sec", 300))):
            speech_recognizer.stop_continuous_recognition()
            raise RuntimeError("Azure continuous recognition timed out")
        speech_recognizer.stop_continuous_recognition()

        if not segment_payloads:
            if cancel_error:
                raise RuntimeError(f"Azure continuous canceled: {cancel_error}")
            raise RuntimeError("Azure continuous returned no recognized segments")

        return self._merge_segment_payloads(segment_payloads)

    def _merge_segment_payloads(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        merged_words: list[dict[str, Any]] = []
        displays: list[str] = []
        lexicals: list[str] = []
        weighted_scores = {
            "PronScore": 0.0,
            "AccuracyScore": 0.0,
            "FluencyScore": 0.0,
            "CompletenessScore": 0.0,
            "ProsodyScore": 0.0,
        }
        total_weight = 0.0

        for payload in payloads:
            nbest = (payload.get("NBest") or [{}])[0]
            seg_words = nbest.get("Words") or []
            seg_weight = float(len(seg_words) or 1)
            merged_words.extend(seg_words)
            total_weight += seg_weight

            display = str(nbest.get("Display") or "").strip()
            lexical = str(nbest.get("Lexical") or "").strip()
            if display:
                displays.append(display)
            if lexical:
                lexicals.append(lexical)

            pa = nbest.get("PronunciationAssessment") or {}
            for key in weighted_scores:
                try:
                    weighted_scores[key] += float(pa.get(key, 0.0)) * seg_weight
                except Exception:
                    continue

        if total_weight <= 0:
            total_weight = 1.0

        merged_pa = {k: (v / total_weight) for k, v in weighted_scores.items()}
        return {
            "NBest": [
                {
                    "Display": " ".join(displays).strip(),
                    "Lexical": " ".join(lexicals).strip(),
                    "Words": merged_words,
                    "PronunciationAssessment": merged_pa,
                }
            ],
            "segment_count": len(payloads),
        }

    def _parse_result(self, result_or_data: Any, script_text: str) -> tuple[Alignment, dict[str, Any]]:
        sdk = get_speech_sdk()

        if isinstance(result_or_data, dict):
            data = result_or_data
        else:
            raw = result_or_data.properties.get(sdk.PropertyId.SpeechServiceResponse_JsonResult)
            data = json.loads(raw)

        nbest = (data.get("NBest") or [{}])[0]
        pron_assessment = nbest.get("PronunciationAssessment", {})

        alignment = Alignment()
        words = nbest.get("Words", [])

        for w in words:
            w_text = w.get("Word", "")
            w_assessment = w.get("PronunciationAssessment", {})
            w_error = w_assessment.get("ErrorType", "None")
            w_score = float(w_assessment.get("AccuracyScore", 0.0) or 0.0)

            if w_error == "Omission":
                tag = WordTag.MISSING
                w_score = 0.0
            elif w_error == "Mispronunciation" or w_score < 75:
                tag = WordTag.WEAK
            else:
                tag = WordTag.OK

            word_align = WordAlignment(
                word=w_text,
                start=float(w.get("Offset", 0) or 0) / 10000000.0,
                end=(float(w.get("Offset", 0) or 0) + float(w.get("Duration", 0) or 0)) / 10000000.0,
                score=w_score,
                tag=tag,
            )
            alignment.words.append(word_align)

            for p in (w.get("Phonemes") or []):
                p_text = p.get("Phoneme", "")
                p_assessment = p.get("PronunciationAssessment", {})
                p_score = float(p_assessment.get("AccuracyScore", 0.0) or 0.0)
                alignment.phonemes.append(
                    PhonemeAlignment(
                        phoneme=p_text,
                        start=word_align.start,
                        end=word_align.end,
                        score=p_score,
                        tag=PhonemeTag.OK if p_score >= 75 else PhonemeTag.WEAK,
                        in_word=w_text,
                    )
                )

        display_text = str(nbest.get("Display") or "").strip()
        lexical_text = str(nbest.get("Lexical") or "").strip()

        engine_raw = {
            "source": "Azure",
            "overall_score": float(pron_assessment.get("PronScore", 0) or 0),
            "accuracy_score": float(pron_assessment.get("AccuracyScore", 0) or 0),
            "fluency_score": float(pron_assessment.get("FluencyScore", 0) or 0),
            "completeness_score": float(pron_assessment.get("CompletenessScore", 0) or 0),
            "prosody_score": float(pron_assessment.get("ProsodyScore", 0) or 0),
            "pronunciation_score": float(pron_assessment.get("AccuracyScore", 0) or 0),
            "intonation_score": float(pron_assessment.get("ProsodyScore", 0) or 0),
            "scoring_profile": "azure_native_v1",
            "json_raw": data,
            "detected_transcript": display_text or lexical_text,
        }
        engine_raw["gop_mean"] = (engine_raw["accuracy_score"] - 50.0) / 10.0

        return alignment, engine_raw


def run_azure_engine(
    wav_path: Path,
    script_text: str,
    work_dir: Path,
) -> tuple[Alignment, dict[str, Any]]:
    engine = AzureEngine()
    return engine.run(wav_path, script_text, work_dir)
