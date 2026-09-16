from __future__ import annotations

from contextlib import nullcontext

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
            "researcher": {"provider": provider, "model": "test-model"},
        },
    })


def test_http_transport_uses_certifi_ca(monkeypatch):
    captured = {}
    ssl_context = object()

    class Response:
        @staticmethod
        def read() -> bytes:
            return b'{"ok": true}'

    def create_default_context(*, cafile):
        captured["cafile"] = cafile
        return ssl_context

    def urlopen(request, *, timeout, context):
        captured.update(request=request, timeout=timeout, context=context)
        return nullcontext(Response())

    monkeypatch.setattr(llm.ssl, "create_default_context", create_default_context)
    monkeypatch.setattr(llm, "urlopen", urlopen)

    payload = llm._http_transport("https://llm.example", {}, {"ping": True}, 10)

    assert payload == {"ok": True}
    assert captured["cafile"] == certifi.where()
    assert captured["context"] is ssl_context


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