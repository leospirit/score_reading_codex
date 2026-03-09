import sys
import types


fake_openai = types.ModuleType("openai")
fake_openai.OpenAI = object
sys.modules["openai"] = fake_openai

from src.analysis.llm_advisor import LLMAdvisor
from src.models import Meta, ScoringResult, Scores, Analysis, Alignment, WordAlignment, WordTag, PhonemeAlignment, PhonemeTag


class _FakeProvider:
    def __init__(self, client_type: str, model: str, key: str) -> None:
        self.client_type = client_type
        self.model = model
        self.api_keys = [key]
        self.client = None
        self.genai_model = None


def test_llm_advisor_chain_prefers_volcengine_then_gemini(monkeypatch):
    advisor = LLMAdvisor.__new__(LLMAdvisor)
    advisor.provider = _FakeProvider("gemini_rest", "gemini-3-flash-preview", "AIza-test")

    monkeypatch.setattr(
        LLMAdvisor,
        "_build_volcengine_provider",
        lambda self: _FakeProvider("volcengine", "doubao-seed-2-0-lite-260215", "ark-key"),
    )

    chain = advisor._build_provider_chain()

    assert [label for label, _ in chain] == ["volcengine", "gemini"]


def test_volcengine_prompt_uses_compact_payload_and_three_sentence_rule():
    advisor = LLMAdvisor.__new__(LLMAdvisor)
    result = ScoringResult(
        meta=Meta(student_id="蒋淇悦6单元背诵"),
        script_text="My uncle's family live there.",
        scores=Scores(
            pronunciation_100=86,
            fluency_100=91,
            intonation_100=83,
            completeness_100=97,
            overall_100=88,
        ),
        analysis=Analysis(missing_words=[]),
        alignment=Alignment(
            words=[
                WordAlignment(word="winter", start=0.0, end=0.2, score=61, tag=WordTag.POOR, diagnosis="ending unclear"),
                WordAlignment(word="fine", start=0.2, end=0.4, score=72, tag=WordTag.WEAK, diagnosis="nasalized"),
            ],
            phonemes=[
                PhonemeAlignment(phoneme="R", start=0.0, end=0.1, score=70, tag=PhonemeTag.POOR, in_word="winter"),
                PhonemeAlignment(phoneme="N", start=0.1, end=0.2, score=76, tag=PhonemeTag.WEAK, in_word="fine"),
            ],
        ),
    )

    payload = advisor._prepare_prompt_data(result, provider_label="volcengine")
    system_prompt = advisor._get_system_prompt(provider_label="volcengine")

    assert "playbook_hints" not in payload
    assert "weak_words" not in payload
    assert payload["focus_strength"]["dimension"] == "completeness"
    assert payload["focus_issue"]["word"] == "winter"
    assert payload["focus_issue"]["phoneme"] == "R"
    assert "exactly 3 Chinese sentences" in system_prompt
    assert "Sentence 1" in system_prompt
