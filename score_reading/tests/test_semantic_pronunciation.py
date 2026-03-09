import sys
import types


fake_openai_provider = types.ModuleType("src.analysis.openai_provider")


class _FakeOpenAIProvider:
    def __init__(self, *args, **kwargs) -> None:
        self.client_type = "none"
        self.client = None
        self.genai_model = None
        self.model = kwargs.get("model", "")


fake_openai_provider.OpenAIProvider = _FakeOpenAIProvider
sys.modules.setdefault("src.analysis.openai_provider", fake_openai_provider)

from src.analysis.llm_advisor import LLMAdvisor
from src.models import Meta, ScoringResult
from src.pipeline.script_reference import _normalize_reference, build_local_script_reference, render_preheat_text
from src.semantic_pronunciation import build_semantic_pronunciation_priors


def test_build_semantic_pronunciation_priors_marks_live_there_as_reside() -> None:
    priors = build_semantic_pronunciation_priors("My uncle's family live there.")

    assert priors
    assert priors[0]["word"] == "live"
    assert priors[0]["meaning"] == "reside"
    assert priors[0]["ipa"] == "l\u026av"


def test_normalize_reference_overrides_live_pronunciation_from_semantic_prior() -> None:
    normalized = _normalize_reference(
        {
            "word_pronunciations": [
                {"word": "live", "ipa": "la\u026av", "stress": "", "tip": "old tip"},
                {"word": "there", "ipa": "\u00f0er", "stress": "", "tip": ""},
            ],
            "pronunciation_rules": [],
            "pause_rules": [],
            "pace_norm": {},
            "sentence_rhythm": [],
            "common_linking": [],
        },
        script_text="My uncle's family live there.",
        script_hash="demo",
        model="gemini-test",
        script_token_count=5,
        unique_word_count=5,
    )

    live_row = next(row for row in normalized["word_pronunciations"] if row["word"].lower() == "live")
    assert live_row["ipa"] == "l\u026av"
    assert "reside" in live_row["tip"]
    assert normalized["semantic_pronunciation_priors"]
    assert any("live there" in " ".join(row.get("examples", [])) for row in normalized["pronunciation_rules"])

    preheat_text = render_preheat_text("My uncle's family live there.", normalized)
    assert "[Semantic Pronunciation Priors]" in preheat_text
    assert "SemanticPron: live" in preheat_text
    assert "/l\u026av/" in preheat_text


def test_llm_advisor_prompt_data_includes_semantic_pronunciation_priors() -> None:
    advisor = LLMAdvisor.__new__(LLMAdvisor)
    result = ScoringResult(
        meta=Meta(student_id="S01"),
        script_text="My uncle's family live there.",
    )

    payload = advisor._prepare_prompt_data(result)

    assert "semantic_pronunciation_priors" in payload
    assert payload["semantic_pronunciation_priors"]
    assert payload["semantic_pronunciation_priors"][0]["ipa"] == "l\u026av"

def test_build_local_script_reference_produces_immediate_preheat_baseline() -> None:
    reference = build_local_script_reference("My uncle's family live there.", script_hash="demo")

    assert reference["model"] == "local_fallback"
    assert reference["pause_rules"]
    assert reference["pace_norm"]["target_wpm"]
    assert reference["semantic_pronunciation_priors"]
    assert any(row["word"].lower() == "live" for row in reference["word_pronunciations"])

    preheat_text = render_preheat_text("My uncle's family live there.", reference)
    assert "[Annotated Script]" in preheat_text
    assert "[PAUSE:strong" in preheat_text
    assert "/l\u026av/" in preheat_text
