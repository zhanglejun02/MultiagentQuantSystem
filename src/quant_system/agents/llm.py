"""Provider-neutral LLM gateway for research agents.

This module belongs to the research plane and must never be imported by the
trading runtime, risk, OMS, ledger, or broker adapters.
"""

from __future__ import annotations

import io
import json
import math
from collections.abc import Callable
from typing import Any, Literal, Protocol

import certifi
import pycurl
from pydantic import BaseModel, ConfigDict

from quant_system.agents.config import AgentLLMConfig, AgentRoleConfig, LLMProviderConfig
from quant_system.domain.exceptions import QuantSystemError


class LLMMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw_request_id: str | None = None


class CompletionGateway(Protocol):
    def complete(self, role: str, messages: list[LLMMessage]) -> LLMResponse: ...


Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


def _send_http_request(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise QuantSystemError("LLM request timeout must be finite and positive")
    output = io.BytesIO()
    request = pycurl.Curl()
    try:
        request.setopt(pycurl.URL, url.encode("utf-8"))
        request.setopt(pycurl.POST, 1)
        request.setopt(pycurl.POSTFIELDS, json.dumps(body).encode("utf-8"))
        request.setopt(
            pycurl.HTTPHEADER,
            [
                f"{name}: {value}".encode()
                for name, value in {
                    "Content-Type": "application/json",
                    **headers,
                }.items()
            ],
        )
        request.setopt(pycurl.CAINFO, certifi.where().encode("utf-8"))
        request.setopt(pycurl.TIMEOUT_MS, max(1, int(timeout_seconds * 1000)))
        request.setopt(pycurl.WRITEDATA, output)
        request.perform()
        status_code = int(request.getinfo(pycurl.RESPONSE_CODE))
        if status_code >= 400:
            raise QuantSystemError(f"LLM provider returned HTTP {status_code}")
        try:
            payload = json.loads(output.getvalue().decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise QuantSystemError("LLM provider response is not valid JSON") from exc
    finally:
        request.close()
    if not isinstance(payload, dict):
        raise QuantSystemError("LLM provider response must be a JSON object")
    return payload


def _http_transport_once(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        return _send_http_request(url, headers, body, timeout_seconds)
    except pycurl.error as exc:
        raise QuantSystemError("LLM provider connection failed (single attempt)") from exc


def _http_transport(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Legacy retry helper; gateway completions use the single-attempt transport."""
    for _ in range(3):
        try:
            return _send_http_request(url, headers, body, timeout_seconds)
        except pycurl.error:
            continue
    raise QuantSystemError("LLM provider connection failed after 3 attempts")


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


class LLMGateway:
    def __init__(self, config: AgentLLMConfig, transport: Transport | None = None) -> None:
        self._config = config
        self._transport = (
            _http_transport_once if transport is None or transport is _http_transport else transport
        )

    def complete(self, role: str, messages: list[LLMMessage]) -> LLMResponse:
        """Complete a configured role without implicit provider retries."""
        _, binding = self._config.route(role)
        return self.complete_bound(binding, messages)

    def complete_bound(
        self,
        binding: AgentRoleConfig,
        messages: list[LLMMessage],
        *,
        timeout_seconds: float | None = None,
    ) -> LLMResponse:
        """Use an explicit immutable binding and at most one transport invocation."""
        if not messages:
            raise QuantSystemError("LLM request requires at least one message")
        try:
            provider = self._config.providers[binding.provider]
        except KeyError as exc:
            raise QuantSystemError(f"LLM provider is not configured: {binding.provider}") from exc
        if timeout_seconds is not None:
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise QuantSystemError("LLM request timeout must be finite and positive")
            provider = provider.model_copy(
                update={"timeout_seconds": min(provider.timeout_seconds, timeout_seconds)}
            )
        role_config = binding
        api_key = provider.api_key()
        if provider.kind in {"openai", "openai_compatible"}:
            return self._openai_compatible(
                provider_name=role_config.provider,
                provider=provider,
                model=role_config.model,
                temperature=role_config.temperature,
                max_tokens=role_config.max_output_tokens,
                thinking=role_config.thinking,
                api_key=api_key,
                messages=messages,
            )
        if provider.kind == "anthropic":
            return self._anthropic(
                provider_name=role_config.provider,
                provider=provider,
                model=role_config.model,
                temperature=role_config.temperature,
                max_tokens=role_config.max_output_tokens,
                api_key=api_key,
                messages=messages,
            )
        return self._gemini(
            provider_name=role_config.provider,
            provider=provider,
            model=role_config.model,
            temperature=role_config.temperature,
            max_tokens=role_config.max_output_tokens,
            api_key=api_key,
            messages=messages,
        )

    def _openai_compatible(
        self,
        *,
        provider_name: str,
        provider: LLMProviderConfig,
        model: str,
        temperature: float,
        max_tokens: int,
        thinking: Literal["enabled", "disabled"] | None,
        api_key: str,
        messages: list[LLMMessage],
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": [message.model_dump() for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if thinking is not None:
            body["thinking"] = {"type": thinking}
        payload = self._transport(
            _join_url(provider.base_url, "chat/completions"),
            {"Authorization": f"Bearer {api_key}"},
            body,
            provider.timeout_seconds,
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise QuantSystemError("invalid OpenAI-compatible response") from exc
        usage = payload.get("usage", {})
        return LLMResponse(
            content=str(content),
            provider=provider_name,
            model=model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            raw_request_id=payload.get("id"),
        )

    def _anthropic(
        self,
        *,
        provider_name: str,
        provider: LLMProviderConfig,
        model: str,
        temperature: float,
        max_tokens: int,
        api_key: str,
        messages: list[LLMMessage],
    ) -> LLMResponse:
        system = "\n\n".join(message.content for message in messages if message.role == "system")
        conversation = [message.model_dump() for message in messages if message.role != "system"]
        body: dict[str, Any] = {
            "model": model,
            "messages": conversation,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if system:
            body["system"] = system
        payload = self._transport(
            _join_url(provider.base_url, "v1/messages"),
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            body,
            provider.timeout_seconds,
        )
        try:
            content = "".join(block["text"] for block in payload["content"] if block.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise QuantSystemError("invalid Anthropic response") from exc
        usage = payload.get("usage", {})
        return LLMResponse(
            content=content,
            provider=provider_name,
            model=model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            raw_request_id=payload.get("id"),
        )

    def _gemini(
        self,
        *,
        provider_name: str,
        provider: LLMProviderConfig,
        model: str,
        temperature: float,
        max_tokens: int,
        api_key: str,
        messages: list[LLMMessage],
    ) -> LLMResponse:
        system = "\n\n".join(message.content for message in messages if message.role == "system")
        body: dict[str, Any] = {
            "contents": [
                {
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": [{"text": message.content}],
                }
                for message in messages
                if message.role != "system"
            ],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        payload = self._transport(
            _join_url(
                provider.base_url,
                f"v1beta/models/{model}:generateContent",
            ),
            {"x-goog-api-key": api_key},
            body,
            provider.timeout_seconds,
        )
        try:
            content = "".join(
                part["text"] for part in payload["candidates"][0]["content"]["parts"] if "text" in part
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise QuantSystemError("invalid Gemini response") from exc
        usage = payload.get("usageMetadata", {})
        return LLMResponse(
            content=content,
            provider=provider_name,
            model=model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )
