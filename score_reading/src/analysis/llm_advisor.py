import json
import logging
import os
from typing import Any, Dict

from src.analysis.openai_provider import OpenAIProvider
from src.config import load_config
from src.models import Feedback, PhonemeTag, ScoringResult, WordTag

logger = logging.getLogger(__name__)


class LLMAdvisor:
    """
    AI teacher advisor.
    Generates natural language coaching feedback from scoring data.
    """

    def __init__(self):
        self.provider = OpenAIProvider()

    def _build_fallback_provider(self) -> OpenAIProvider | None:
        """Optional backup advisor provider (e.g. Zhipu) when primary LLM fails."""
        try:
            cfg = load_config()
        except Exception:
            cfg = {}

        fallback_key = (
            cfg.get("llm.fallback_api_key")
            or cfg.get("llm.zhipu_api_key")
            or os.getenv("ZHIPU_API_KEY")
        )
        if not fallback_key:
            return None

        fallback_model = (
            cfg.get("llm.fallback_model")
            or cfg.get("llm.zhipu_model")
            or "glm-4-flash"
        )
        provider = OpenAIProvider(api_key=fallback_key, model=fallback_model)
        provider_ready = bool(
            (getattr(provider, "client_type", "") and getattr(provider, "client_type", "") != "none")
            or getattr(provider, "client", None)
            or getattr(provider, "genai_model", None)
        )
        return provider if provider_ready else None

    def generate_feedback(self, result: ScoringResult) -> tuple[Feedback, dict[str, Any] | None]:
        """
        Returns: (Feedback object, advisor JSON payload)
        """
        # Guardrail for clearly abnormal recordings.
        num_missing = len([w for w in result.alignment.words if w.score < 10])
        total_words = len(result.alignment.words)
        missing_ratio = (num_missing / total_words) if total_words > 0 else 0
        if result.scores.overall_100 < 30 or missing_ratio > 0.7:
            abnormal_comment = (
                "检测到录音可能存在较大噪音或设备问题，当前评分可能不能准确反映真实水平，"
                "建议在更安静环境下重试。"
            )
            logger.warning(
                "Abnormal score guard triggered (overall=%.1f, missing_ratio=%.2f)",
                result.scores.overall_100,
                missing_ratio,
            )
            abnormal_feedback = Feedback(
                cn_summary=abnormal_comment,
                cn_actions=["确保录音环境安静", "检查麦克风权限", "适当增大朗读音量"],
                practice=["重新录制并保持稳定语速", "尽量避免句中被打断"],
            )
            return abnormal_feedback, {
                "overall_comment": abnormal_comment,
                "specific_feedback": [],
                "is_abnormal": True,
            }

        provider_ready = bool(
            (getattr(self.provider, "client_type", "") and getattr(self.provider, "client_type", "") != "none")
            or getattr(self.provider, "client", None)
            or getattr(self.provider, "genai_model", None)
        )
        if not provider_ready:
            logger.warning("LLM provider not available. Skipping AI feedback.")
            return result.feedback, None

        try:
            prompt_data = self._prepare_prompt_data(result)
            system_prompt = self._get_system_prompt()
            user_prompt = json.dumps(prompt_data, ensure_ascii=False, indent=2)

            logger.info("Calling LLM for advisor feedback generation...")
            feedback_data: Dict[str, Any] = {}
            primary_error: Exception | None = None

            try:
                response_json = self.provider.generate_response(system_prompt, user_prompt)
                feedback_data = self._parse_response(response_json)
            except Exception as e:
                primary_error = e
                logger.warning("Primary advisor LLM failed: %s", e)

            if not feedback_data:
                fallback_provider = self._build_fallback_provider()
                if fallback_provider:
                    try:
                        logger.info("Trying fallback advisor provider...")
                        response_json = fallback_provider.generate_response(system_prompt, user_prompt)
                        feedback_data = self._parse_response(response_json)
                        if feedback_data:
                            logger.info("Fallback advisor provider succeeded.")
                    except Exception as fallback_err:
                        logger.warning("Fallback advisor provider failed: %s", fallback_err)
                if not feedback_data:
                    if primary_error:
                        logger.error("Advisor feedback unavailable after fallback: %s", primary_error)
                    return result.feedback, None

            new_feedback = Feedback(
                cn_summary=feedback_data.get("overall_comment", ""),
                cn_actions=[
                    item.get("suggestion", "")
                    for item in feedback_data.get("specific_feedback", [])
                    if isinstance(item, dict)
                ],
                practice=feedback_data.get("practice_tips", []),
            )

            result.advisor_feedback = feedback_data

            # Optional style score from model.
            ai_naturalness = feedback_data.get("ai_naturalness_score")
            if ai_naturalness is not None:
                score = float(ai_naturalness)
                result.scores.intonation_100 = (result.scores.intonation_100 * 0.4) + (score * 0.6)
                result.scores.overall_100 = (result.scores.overall_100 * 0.8) + (score * 0.2)

            return new_feedback, feedback_data
        except Exception as e:
            logger.error("Failed to generate LLM feedback: %s", e)
            return result.feedback, None

    def _extract_nickname(self, student_id: str) -> str:
        """
        Extract display nickname from student_id.
        """
        if not student_id:
            return "".join([chr(0x540C), chr(0x5B66)])

        stem = student_id.split(".")[0]
        if len(stem) < 2:
            return stem
        prefix = stem[:3]
        if len(prefix) < 3:
            return prefix

        third_char = prefix[2]
        is_chinese = "\u4e00" <= third_char <= "\u9fff"
        if is_chinese:
            return prefix[1:3]
        return prefix[:2]

    def _prepare_prompt_data(self, result: ScoringResult) -> Dict[str, Any]:
        """
        Build compact structured payload for advisor model.
        """
        student_id = result.meta.student_id if result.meta else ""
        nickname = self._extract_nickname(student_id)
        logger.info("Extracted nickname '%s' from student_id '%s'", nickname, student_id)

        weak_words_data = []
        for w in result.alignment.words:
            if w.tag != WordTag.OK or w.score < 80:
                weak_words_data.append(
                    {
                        "word": w.word,
                        "score": round(float(w.score), 1),
                        "tag": w.tag.value,
                        "diagnosis": (w.diagnosis or "").strip(),
                    }
                )
        weak_words_data.sort(key=lambda x: x["score"])

        phoneme_issues = []
        for p in result.alignment.phonemes:
            if p.tag != PhonemeTag.OK or p.score < 85:
                phoneme_issues.append(
                    {
                        "phoneme": p.phoneme,
                        "in_word": p.in_word,
                        "score": round(float(p.score), 1),
                        "tag": p.tag.value,
                    }
                )
        phoneme_issues.sort(key=lambda x: x["score"])

        hesitations = []
        if result.analysis.hesitations and result.analysis.hesitations.fillers:
            hesitations = result.analysis.hesitations.fillers

        return {
            "instruction": (
                f"Start with '{nickname}' and provide precise, actionable coaching in Chinese. "
                "Do not use generic address like the generic Chinese term for classmate."
            ),
            "student_nickname": nickname,
            "text": result.script_text,
            "scores": {
                "pronunciation": round(result.scores.pronunciation_100, 1),
                "fluency": round(result.scores.fluency_100, 1),
                "intonation": round(result.scores.intonation_100, 1),
                "completeness": round(result.scores.completeness_100, 1),
                "overall": round(result.scores.overall_100, 1),
            },
            "weak_words": weak_words_data[:15],
            "phoneme_issues": phoneme_issues[:15],
            "hesitations": hesitations,
            "missing_words": result.analysis.missing_words or [],
            "mistakes_evidence": result.analysis.mistakes or [],
            "fluency_details": {
                "wpm": round((result.analysis.fluency or {}).get("wpm", 0), 1)
                if hasattr(result.analysis, "fluency")
                else 0,
                "pause_count": (result.analysis.fluency or {}).get("pause_count", 0)
                if hasattr(result.analysis, "fluency")
                else 0,
            },
        }

    def _get_system_prompt(self) -> str:
        return """
You are an expert English speaking coach for children.
Return ONLY valid JSON using this schema:
{
  "overall_comment": "string",
  "top_errors": [{"phoneme":"string","type":"string","description":"string","words":["string"],"improvement":"string"}],
  "specific_feedback": [{"target":"string","issue":"string","suggestion":"string"}],
  "practice_tips": ["string"],
  "fluency_diagnosis": {"status":"string","advice":"string","motto":"string"},
  "ai_naturalness_score": 0
}

Rules:
- Respond in Chinese.
- Always address the student with provided student_nickname.
- Use concrete, physical pronunciation instructions.
- Keep overall_comment to 1-2 sentences and <= 45 Chinese chars.
- Mention exactly one strongest point and one key fix.
- specific_feedback must contain at most 1 item.
- practice_tips must contain at most 1 item.
- Keep tone encouraging but evidence-based.
"""

    def _parse_response(self, response_str: str) -> Dict[str, Any]:
        try:
            clean_str = response_str.strip()
            if clean_str.startswith("```json"):
                clean_str = clean_str[7:]
            if clean_str.endswith("```"):
                clean_str = clean_str[:-3]
            return json.loads(clean_str)
        except json.JSONDecodeError:
            logger.error("Failed to parse LLM JSON response")
            return {}


def get_llm_advisor() -> LLMAdvisor:
    # Always instantiate to pick latest config and env.
    return LLMAdvisor()
