import sys
import types


class _FakeOpenAIClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


fake_openai = types.ModuleType("openai")
fake_openai.OpenAI = _FakeOpenAIClient
sys.modules["openai"] = fake_openai

from src.analysis import openai_provider as provider_module


def test_zhipu_defaults_to_glm_4_7_when_model_not_configured(monkeypatch):
    monkeypatch.setattr(provider_module.openai, "OpenAI", _FakeOpenAIClient)
    monkeypatch.setattr(
        provider_module,
        "load_config",
        lambda: {
            "llm.connect_timeout_sec": 5.0,
            "llm.read_timeout_sec": 12.0,
            "llm.max_retries": 2,
            "llm.max_total_wait_sec": 20.0,
            "llm.base_url": "",
            "llm.model": "",
            "llm.zhipu_model": "",
            "engines.gemini.api_key": "",
            "engines.gemini.model": "gemini-3-flash-preview",
        },
    )

    provider = provider_module.OpenAIProvider(
        api_key="testid.testsecret1234567890",
        model=None,
    )

    assert provider.client_type == "zhipu"
    assert provider.model == "glm-4.7"
    assert provider.client.kwargs["base_url"] == "https://api.z.ai/api/paas/v4"
