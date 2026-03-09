import sys
import types

import httpx


fake_openai = types.ModuleType("openai")
fake_openai.OpenAI = object
sys.modules.setdefault("openai", fake_openai)

from src.analysis.openai_provider import OpenAIProvider


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _RecordingChatCompletions:
    def __init__(self) -> None:
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse('{"ok": true}')


class _RecordingClient:
    def __init__(self) -> None:
        self.timeouts = []
        self.chat = type("Chat", (), {"completions": _RecordingChatCompletions()})()

    def with_options(self, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return self


class _FakeHttpxResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _RecordingHttpxClient:
    def __init__(self, *args, **kwargs) -> None:
        self.timeout = kwargs.get("timeout")
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, endpoint, params=None, json=None):
        self.calls.append((endpoint, params, json))
        return _FakeHttpxResponse(
            200,
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": '{"ok": true}'},
                            ]
                        }
                    }
                ]
            },
        )


def _build_provider() -> OpenAIProvider:
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.connect_timeout_sec = 5.0
    provider.read_timeout_sec = 12.0
    provider.max_retry_rounds = 1
    provider.max_total_wait_sec = 9.0
    provider.api_keys = ["dummy-key"]
    provider.current_key_index = 0
    provider.base_url = None
    provider.model = "glm-4.5-air"
    provider.preferred_gemini_model = "gemini-3-flash-preview"
    provider.client = None
    provider.genai_model = None
    provider.client_type = "zhipu"
    return provider


def test_openai_compatible_requests_use_provider_budget_timeout():
    provider = _build_provider()
    recording_client = _RecordingClient()
    provider.client = recording_client

    text = provider.generate_response("system", "user")

    assert text == '{"ok": true}'
    assert recording_client.timeouts, "expected with_options(timeout=...) to be used"
    timeout = recording_client.timeouts[0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect <= 5.0
    assert timeout.connect + timeout.read <= provider.max_total_wait_sec


def test_gemini_rest_requests_cap_http_timeout_to_provider_budget(monkeypatch):
    provider = _build_provider()
    provider.client_type = "gemini_rest"
    provider.api_keys = ["AIza-test-key"]
    provider.model = "gemini-3-flash-preview"

    recorded = {}

    def _client_factory(*args, **kwargs):
        client = _RecordingHttpxClient(*args, **kwargs)
        recorded["timeout"] = client.timeout
        return client

    monkeypatch.setattr(httpx, "Client", _client_factory)

    text = provider._generate_via_gemini_google_rest("system", "user", 0.7)

    assert text == '{"ok": true}'
    timeout = recorded["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect <= 5.0
    assert timeout.connect + timeout.read <= provider.max_total_wait_sec
