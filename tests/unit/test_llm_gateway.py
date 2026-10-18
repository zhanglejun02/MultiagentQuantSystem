from __future__ import annotations

import json

import certifi
import pytest

from quant_system.agents import llm
from quant_system.agents.config import AgentLLMConfig, AgentRoleConfig
from quant_system.agents.llm import LLMGateway, LLMMessage
from quant_system.domain.exceptions import QuantSystemError


def _config(provider: str, kind: str, key_env: str = "TEST_LLM_KEY") -> AgentLLMConfig:
    return AgentLLMConfig.model_validate(
        {
            "providers": {
                provider: {
                    "kind": kind,
                    "api_key_env": key_env,
                    "base_url": "https://llm.example",
                }
            },
            "roles": {
                "researcher": {
                    "provider": provider,
                    "model": "test-model",
                    "thinking": "disabled",
                },
            },
        }
    )


def test_http_transport_uses_certifi_ca(monkeypatch):
    captured = {"options": {}}

    class Curl:
        def setopt(self, option, value):
            captured["options"][option] = value

        def perform(self):
            captured["options"][llm.pycurl.WRITEDATA].write(b'{"ok": true}')

        @staticmethod
        def getinfo(option):
            return 200

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(llm.pycurl, "Curl", Curl)

    payload = llm._http_transport("https://llm.example", {}, {"ping": True}, 10)

    assert payload == {"ok": True}
    assert captured["options"][llm.pycurl.CAINFO] == certifi.where().encode()
    assert json.loads(captured["options"][llm.pycurl.POSTFIELDS]) == {"ping": True}


def test_http_transport_retries_transient_connection_failure(monkeypatch):
    attempts = 0

    class Curl:
        def __init__(self):
            self.options = {}

        def setopt(self, option, value):
            self.options[option] = value

        def perform(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise llm.pycurl.error(35, "connection reset")
            self.options[llm.pycurl.WRITEDATA].write(b'{"ok": true}')

        @staticmethod
        def getinfo(option):
            return 200

        @staticmethod
        def close():
            pass

    monkeypatch.setattr(llm.pycurl, "Curl", Curl)

    assert llm._http_transport("https://llm.example", {}, {"ping": True}, 10) == {"ok": True}
    assert attempts == 2


def test_openai_compatible_gateway_builds_request(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    captured = {}

    def transport(url, headers, body, timeout):
        captured.update(url=url, headers=headers, body=body, timeout=timeout)
        return {
            "id": "request-1",
            "choices": [{"message": {"content": "candidate"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        }

    response = LLMGateway(_config("deepseek", "openai_compatible"), transport).complete(
        "researcher", [LLMMessage(role="user", content="propose a factor")]
    )
    assert captured["url"] == "https://llm.example/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert response.content == "candidate"
    assert response.input_tokens == 12


def test_anthropic_gateway_separates_system_prompt(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    captured = {}

    def transport(url, headers, body, timeout):
        captured.update(url=url, headers=headers, body=body, timeout=timeout)
        return {
            "id": "request-2",
            "content": [{"type": "text", "text": "critique"}],
            "usage": {"input_tokens": 9, "output_tokens": 3},
        }

    response = LLMGateway(_config("anthropic", "anthropic"), transport).complete(
        "researcher",
        [LLMMessage(role="system", content="be strict"), LLMMessage(role="user", content="review")],
    )
    assert captured["url"] == "https://llm.example/v1/messages"
    assert captured["body"]["system"] == "be strict"
    assert response.content == "critique"


def test_gemini_gateway_uses_model_endpoint(monkeypatch):
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    captured = {}

    def transport(url, headers, body, timeout):
        captured.update(url=url, headers=headers, body=body, timeout=timeout)
        return {
            "candidates": [{"content": {"parts": [{"text": "decision"}]}}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2},
        }

    response = LLMGateway(_config("gemini", "gemini"), transport).complete(
        "researcher", [LLMMessage(role="user", content="coordinate")]
    )
    assert captured["url"].endswith("/v1beta/models/test-model:generateContent")
    assert captured["headers"]["x-goog-api-key"] == "secret"
    assert response.content == "decision"


@pytest.mark.parametrize("kind", ["openai_compatible", "anthropic", "gemini"])
def test_complete_bound_uses_explicit_model_and_remaining_timeout_without_mutation(monkeypatch, kind):
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    config = _config("one", kind)
    original = config.model_dump()
    captured = []

    def transport(url, headers, body, timeout):
        captured.append((url, body, timeout))
        return {
            "choices": [{"message": {"content": "bound"}}],
            "content": [{"type": "text", "text": "bound"}],
            "candidates": [{"content": {"parts": [{"text": "bound"}]}}],
        }

    binding = AgentRoleConfig(provider="one", model="bound-model", max_output_tokens=17)
    gateway = LLMGateway(config, transport)
    response = gateway.complete_bound(
        binding, [LLMMessage(role="user", content="review")], timeout_seconds=0.25
    )
    assert len(captured) == 1
    url, body, timeout = captured[0]
    assert timeout == 0.25
    assert response.model == "bound-model"
    if kind == "gemini":
        assert "/bound-model:generateContent" in url
        assert body["generationConfig"]["maxOutputTokens"] == 17
    else:
        assert body["model"] == "bound-model"
        assert body["max_tokens"] == 17
    gateway.complete_bound(binding, [LLMMessage(role="user", content="review")], timeout_seconds=900)
    assert captured[-1][2] == config.providers["one"].timeout_seconds
    assert config.model_dump() == original


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("legacy_transport", [False, True])
def test_gateway_default_and_bound_completions_make_one_http_attempt(monkeypatch, bound, legacy_transport):
    monkeypatch.setenv("TEST_LLM_KEY", "secret")
    attempts = 0

    class Curl:
        def setopt(self, option, value):
            pass

        def perform(self):
            nonlocal attempts
            attempts += 1
            raise llm.pycurl.error(35, "connection reset")

        def close(self):
            pass

    monkeypatch.setattr(llm.pycurl, "Curl", Curl)
    config = _config("one", "openai")
    gateway = LLMGateway(config, llm._http_transport if legacy_transport else None)
    messages = [LLMMessage(role="user", content="review")]
    with pytest.raises(QuantSystemError, match="single attempt"):
        if bound:
            gateway.complete_bound(config.roles["researcher"], messages)
        else:
            gateway.complete("researcher", messages)
    assert attempts == 1


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_complete_bound_rejects_invalid_timeout_without_transport(monkeypatch, timeout):
    config = _config("one", "openai")

    def transport(*args):
        pytest.fail("invalid timeout must not send")

    with pytest.raises(QuantSystemError, match="timeout"):
        LLMGateway(config, transport).complete_bound(
            config.roles["researcher"],
            [LLMMessage(role="user", content="review")],
            timeout_seconds=timeout,
        )


def test_http_error_does_not_expose_response_body_or_credentials(monkeypatch):
    class Curl:
        def __init__(self):
            self.options = {}

        def setopt(self, option, value):
            self.options[option] = value

        def perform(self):
            self.options[llm.pycurl.WRITEDATA].write(b'{"error":"secret-key-sentinel"}')

        def getinfo(self, option):
            return 401

        def close(self):
            pass

    monkeypatch.setattr(llm.pycurl, "Curl", Curl)
    with pytest.raises(QuantSystemError, match="HTTP 401") as caught:
        llm._http_transport_once("https://llm.example", {}, {}, 1)
    assert "secret-key-sentinel" not in str(caught.value)


def test_complete_bound_routes_to_bound_provider_without_global_role_change(monkeypatch):
    monkeypatch.setenv("OTHER_LLM_KEY", "other-test-key")
    config = _config("one", "openai")
    payload = config.model_dump()
    payload["providers"]["two"] = {
        "kind": "openai_compatible",
        "api_key_env": "OTHER_LLM_KEY",
        "base_url": "https://second.example",
        "timeout_seconds": 11,
    }
    config = AgentLLMConfig.model_validate(payload)
    original = config.model_dump()
    captured = []

    def transport(url, headers, body, timeout):
        captured.append((url, headers, body, timeout))
        return {"choices": [{"message": {"content": "bound"}}]}

    binding = AgentRoleConfig(provider="two", model="second-model")
    response = LLMGateway(config, transport).complete_bound(
        binding, [LLMMessage(role="user", content="review")]
    )
    assert response.provider == "two"
    assert response.model == "second-model"
    assert captured[0][0] == "https://second.example/chat/completions"
    assert captured[0][1]["Authorization"] == "Bearer other-test-key"
    assert captured[0][3] == 11
    assert config.model_dump() == original
    assert config.roles["researcher"].provider == "one"
