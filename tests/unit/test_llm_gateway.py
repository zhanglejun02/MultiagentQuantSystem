from __future__ import annotations

import json

import certifi

from quant_system.agents import llm
from quant_system.agents.config import AgentLLMConfig
from quant_system.agents.llm import LLMGateway, LLMMessage


def _config(provider: str, kind: str, key_env: str = "TEST_LLM_KEY") -> AgentLLMConfig:
    return AgentLLMConfig.model_validate({
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
    })


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