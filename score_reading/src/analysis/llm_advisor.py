import json
import logging
import os
import time
from typing import Any, Dict

from src.analysis.openai_provider import OpenAIProvider
from src.advice.playbook import build_playbook_runtime_hints
from src.config import load_config
from src.models import Feedback, PhonemeTag, ScoringResult, WordTag
from src.semantic_pronunciation import build_semantic_pronunciation_priors

logger = logging.getLogger(__name__)


class LLMAdvisor:
    """
    AI teacher advisor.
    Generates natural language coaching feedback from scoring data.
    """

    def __init__(self):
        self.provider = OpenAIProvider()

    @staticmethod
    def _provider_ready(provider: OpenAIProvider | None) -> bool:
        if provider is None:
            return False
        return bool(
            (getattr(provider, "client_type", "") and getattr(provider, "client_type", "") != "none")
            or getattr(provider, "client", None)
            or getattr(provider, "genai_model", None)
        )

    @staticmethod
    def _provider_signature(provider: OpenAIProvider) -> tuple[str, str, str]:
        api_keys = getattr(provider, "api_keys", None) or []
        first_key = str(api_keys[0]) if api_keys else ""
        return (
            str(getattr(provider, "client_type", "") or ""),
            str(getattr(provider, "model", "") or ""),
            first_key[:12],
        )

    def _build_volcengine_provider(self) -> OpenAIProvider | None:
        """Build deterministic Volcengine Ark advisor provider."""
        try:
            cfg = load_config()
        except Exception:
            cfg = {}

        ark_key = (
            cfg.get("llm.ark_api_key")
            or os.getenv("ARK_API_KEY")
        )
        if not ark_key:
            return None

        ark_model = (
            cfg.get("llm.ark_model")
            or os.getenv("ARK_MODEL")
            or "doubao-seed-2-0-lite-260215"
        )
        ark_base_url = (
            cfg.get("llm.ark_base_url")
            or os.getenv("ARK_BASE_URL")
            or "https://ark.cn-beijing.volces.com/api/v3"
        )
        provider = OpenAIProvider(
            api_key=ark_key,
            model=ark_model,
            base_url=ark_base_url,
            provider_name="volcengine",
        )
        return provider if self._provider_ready(provider) else None

    def _build_provider_chain(self) -> list[tuple[str, OpenAIProvider]]:
        """
        Fixed fallback chain for integrated feedback:
        1) Volcengine Ark
        2) Gemini
        3) Azure fallback
        """
        chain: list[tuple[str, OpenAIProvider]] = []
        volcengine = self._build_volcengine_provider()
        if volcengine and self._provider_ready(volcengine):
            chain.append(("volcengine", volcengine))

        if self._provider_ready(self.provider):
            primary_type = str(getattr(self.provider, "client_type", "") or "").lower()
            if primary_type in {"gemini", "gemini_rest"}:
                primary_label = "gemini"
            elif primary_type == "volcengine":
                primary_label = "volcengine"
            elif primary_type == "zhipu":
                primary_label = "zhipu_primary"
            else:
                primary_label = "llm_primary"
            primary_sig = self._provider_signature(self.provider)
            duplicate = any(self._provider_signature(provider) == primary_sig for _, provider in chain)
            if not duplicate:
                chain.append((primary_label, self.provider))
        return chain

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

        try:
            cfg = load_config()
            raw_budget = cfg.get("llm.advisor_budget_sec", cfg.get("llm.max_total_wait_sec", 12.0))
            try:
                advisor_budget_sec = max(2.0, float(raw_budget))
            except Exception:
                advisor_budget_sec = 12.0
            raw_fallback_reserve = cfg.get("llm.fallback_min_budget_sec", 3.0)
            try:
                fallback_min_budget_sec = max(1.0, float(raw_fallback_reserve))
            except Exception:
                fallback_min_budget_sec = 3.0

            provider_chain = self._build_provider_chain()
            if not provider_chain:
                logger.warning("No LLM provider available. Skipping AI feedback.")
                return result.feedback, None

            logger.info(
                "Calling LLM for advisor feedback generation (chain=%s).",
                " -> ".join(label for label, _ in provider_chain),
            )
            feedback_data: Dict[str, Any] = {}
            provider_errors: list[str] = []
            started_at = time.monotonic()
            chain_labels = [label for label, _ in provider_chain]

            for idx, (label, provider) in enumerate(provider_chain):
                elapsed_sec = time.monotonic() - started_at
                remaining_sec = advisor_budget_sec - elapsed_sec
                if remaining_sec <= 0.6:
                    logger.info(
                        "Skipping advisor provider %s due to budget exhaustion (remaining=%.2fs).",
                        label,
                        max(0.0, remaining_sec),
                    )
                    continue

                remaining_providers = max(0, len(provider_chain) - idx - 1)
                reserved_for_fallback = fallback_min_budget_sec * remaining_providers
                provider_budget_sec = remaining_sec
                if remaining_providers > 0:
                    provider_budget_sec = max(1.0, remaining_sec - reserved_for_fallback)

                original_wait = getattr(provider, "max_total_wait_sec", remaining_sec)
                try:
                    provider.max_total_wait_sec = min(float(original_wait), float(provider_budget_sec))
                    prompt_data = self._prepare_prompt_data(result, provider_label=label)
                    system_prompt = self._get_system_prompt(provider_label=label)
                    user_prompt = json.dumps(prompt_data, ensure_ascii=False, indent=2)
                    response_json = provider.generate_response(system_prompt, user_prompt)
                    parsed = self._parse_response(response_json)
                    if parsed:
                        feedback_data = parsed
                        feedback_data["_advisor_provider"] = label
                        feedback_data["_advisor_chain"] = chain_labels
                        logger.info("Advisor provider succeeded: %s", label)
                        break
                    provider_errors.append(f"{label}: empty_response")
                    logger.warning("Advisor provider %s returned empty/invalid JSON.", label)
                except Exception as err:
                    provider_errors.append(f"{label}: {err}")
                    logger.warning("Advisor provider %s failed: %s", label, err)
                finally:
                    try:
                        provider.max_total_wait_sec = original_wait
                    except Exception:
                        pass

            if not feedback_data:
                if provider_errors:
                    logger.error(
                        "Advisor feedback unavailable after chain fallback: %s",
                        " | ".join(provider_errors),
                    )
                return result.feedback, {
                    "provider": "azure_fallback",
                    "_advisor_chain": chain_labels,
                    "_advisor_errors": provider_errors,
                }

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

            # Keep rubric scores source-driven. LLM style score is advisory only.
            ai_naturalness = feedback_data.get("ai_naturalness_score")
            if ai_naturalness is not None:
                try:
                    feedback_data["ai_naturalness_score"] = float(ai_naturalness)
                except Exception:
                    feedback_data.pop("ai_naturalness_score", None)

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

    def _prepare_prompt_data(self, result: ScoringResult, provider_label: str = "") -> Dict[str, Any]:
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

        playbook_hints = build_playbook_runtime_hints(
            script_text=result.script_text or "",
            weak_words=[str(item.get("word", "")) for item in weak_words_data[:20]],
            phoneme_symbols=[str(item.get("phoneme", "")) for item in phoneme_issues[:20]],
            max_items=5,
        )
        semantic_pronunciation_priors = build_semantic_pronunciation_priors(result.script_text or "")

        payload = {
            "instruction": (
                f"Start with '{nickname}' and provide precise, actionable coaching in Chinese. "
                "Do not use generic address like the generic Chinese term for classmate."
            ),
            "student_nickname": nickname,
            "playbook_hints": playbook_hints,
            "semantic_pronunciation_priors": semantic_pronunciation_priors,
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

        if str(provider_label or "").strip().lower() == "volcengine":
            ordered_scores = [
                ("completeness", round(result.scores.completeness_100, 1)),
                ("fluency", round(result.scores.fluency_100, 1)),
                ("pronunciation", round(result.scores.pronunciation_100, 1)),
                ("intonation", round(result.scores.intonation_100, 1)),
            ]
            focus_strength = max(ordered_scores, key=lambda item: item[1])
            focus_issue_word = weak_words_data[0] if weak_words_data else {}
            focus_issue_phoneme = phoneme_issues[0] if phoneme_issues else {}
            compact_payload = {
                "instruction": (
                    f"Address {nickname} directly in Chinese. Write exactly 3 short sentences: praise, issue, action."
                ),
                "student_nickname": nickname,
                "text": result.script_text,
                "focus_strength": {
                    "dimension": focus_strength[0],
                    "score": focus_strength[1],
                },
                "focus_issue": {
                    "word": str(focus_issue_word.get('word', '') or ''),
                    "word_score": float(focus_issue_word.get("score", 0) or 0),
                    "diagnosis": str(focus_issue_word.get("diagnosis", "") or ""),
                    "phoneme": str(focus_issue_phoneme.get("phoneme", "") or ""),
                    "phoneme_score": float(focus_issue_phoneme.get("score", 0) or 0),
                },
                "scores": payload["scores"],
                "fluency_details": payload["fluency_details"],
                "missing_words": list((result.analysis.missing_words or [])[:3]),
                "semantic_pronunciation_priors": semantic_pronunciation_priors[:3],
            }
            return compact_payload
        return payload

    def _get_system_prompt(self, provider_label: str = "") -> str:
        if str(provider_label or "").strip().lower() == "volcengine":
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
- overall_comment must be exactly 3 Chinese sentences.
- Sentence 1: one factual praise based on provided evidence only (fluency/completeness/pronunciation/intonation).
- Sentence 2: point out exactly one core problem.
- Sentence 3: give exactly one concrete, easy-to-practice action.
- Do not mention any numeric score/points.
- Keep wording simple and student-facing; avoid mouth/tongue anatomy wording.
- specific_feedback must contain at most 1 item.
- practice_tips must contain at most 1 item.
- top_errors should contain at most 1 item.
- If semantic_pronunciation_priors are provided, obey them for ambiguous word readings.
"""
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
- Keep language simple and student-facing; avoid technical mouth/tongue anatomy wording.
- If semantic_pronunciation_priors are provided, obey them for ambiguous word readings and do not contradict them.
- Do not mention any numeric score/points in overall_comment, specific_feedback, or practice_tips.
- Prefer playbook_hints when they match evidence; ignore irrelevant entries.
- overall_comment must be exactly 3 Chinese sentences.
- Sentence 1: one factual praise.
- Sentence 2: one key issue.
- Sentence 3: one actionable suggestion.
- specific_feedback must contain at most 1 item.
- practice_tips must contain at most 1 item.
- top_errors should include 1-3 concrete items when evidence exists.
- Each top_errors[i].improvement should include one drill + one vivid mnemonic, suitable for Chinese Grade-6 daily life.
- Mnemonic can be playful/humorous, but must remain positive and executable.
- Avoid repeating the same wording across top_errors improvements.
- Keep tone encouraging but evidence-based.
- Prohibited words/ideas in final text: 舌尖, 舌中, 口型, 卷舌, 长音短音对比, x音发音器官说明.
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
