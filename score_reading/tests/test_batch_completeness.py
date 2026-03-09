import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.modules.setdefault("pydub", types.SimpleNamespace(AudioSegment=object))

from src.batch import Submission, process_single_submission
from src.models import Analysis, AudioMetrics, CompletenessStats, EngineMode, Feedback, Alignment, WordAlignment, WordTag


class BatchCompletenessTests(unittest.TestCase):
    def test_batch_re_normalizes_completeness_after_analysis(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            audio_path = tmp_path / "sample.wav"
            audio_path.write_bytes(b"fake")
            output_dir = tmp_path / "out"
            output_dir.mkdir()

            script_text = "one two three four five six seven eight nine ten"
            alignment = Alignment(
                words=[
                    WordAlignment(word="one", start=0.0, end=0.1, tag=WordTag.OK, score=95),
                    WordAlignment(word="two", start=0.1, end=0.2, tag=WordTag.OK, score=95),
                    WordAlignment(word="three", start=0.2, end=0.3, tag=WordTag.OK, score=95),
                    WordAlignment(word="four", start=0.3, end=0.4, tag=WordTag.OK, score=95),
                    WordAlignment(word="five", start=0.4, end=0.5, tag=WordTag.OK, score=95),
                    WordAlignment(word="six", start=0.5, end=0.6, tag=WordTag.OK, score=95),
                    WordAlignment(word="seven", start=0.6, end=0.7, tag=WordTag.OK, score=95),
                    WordAlignment(word="eight", start=0.7, end=0.8, tag=WordTag.OK, score=95),
                    WordAlignment(word="nine", start=0.8, end=0.9, tag=WordTag.OK, score=95),
                    WordAlignment(word="ten", start=0.9, end=1.0, tag=WordTag.OK, score=95),
                ],
                phonemes=[],
            )
            engine_raw = {
                "source": "Azure",
                "annotation_source": "gemini",
                "completeness_score": 98.0,
                "accuracy_score": 95.0,
                "fluency_score": 95.0,
                "prosody_score": 95.0,
                "pronunciation_score": 95.0,
                "intonation_score": 95.0,
            }

            def fake_preprocess_audio(path, work_path):
                return path, AudioMetrics(duration_sec=1.0, silence_ratio=0.0, rms_db=-20.0)

            def fake_run_with_fallback(**kwargs):
                return alignment, engine_raw, "pro", []

            def fake_analyze_results(alignment_arg, script_text_arg, engine_raw_arg, context=None):
                engine_raw_arg["stable_missing_source"] = "transcript_anchor"
                engine_raw_arg["stable_missing_indices"] = [9]
                return Analysis(
                    missing_words=["ten"],
                    missing_indices=[9],
                    completeness=CompletenessStats(coverage=90),
                )

            def fake_render_html_report(result, html_path):
                html_path.write_text("ok", encoding="utf-8")

            submission = Submission(
                task_id="task1",
                student_id="student1",
                student_name="student1",
                audio_path=audio_path,
                script_text=script_text,
            )

            with patch("src.batch.preprocess_audio", fake_preprocess_audio),                  patch("src.batch.run_with_fallback", fake_run_with_fallback),                  patch("src.batch.analyze_results", fake_analyze_results),                  patch("src.batch.generate_feedback", lambda analysis: Feedback()),                  patch("src.batch.render_html_report", fake_render_html_report):
                submission_id, success, error = process_single_submission(submission, output_dir, EngineMode.PRO)

            self.assertTrue(success)
            self.assertIsNone(error)

            json_path = next(output_dir.rglob(f"{submission_id}.json"))
            data = json.loads(json_path.read_text(encoding="utf-8"))

            self.assertEqual(data["engine_raw"]["stable_missing_indices"], [9])
            self.assertEqual(data["scores"]["completeness_100"], 90.0)
            self.assertEqual(data["engine_raw"]["completeness_adjusted_for_missing"]["missing_count"], 1)
            self.assertEqual(data["engine_raw"]["completeness_adjusted_for_missing"]["rule"], "missing_ratio_consistent")


if __name__ == "__main__":
    unittest.main()
