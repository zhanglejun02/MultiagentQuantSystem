"""Provider-neutral LLM gateway for research agents.

This module belongs to the research plane and must never be imported by the
trading runtime, risk, OMS, ledger, or broker adapters.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import Callable
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi
from pydantic import BaseModel, ConfigDict

from quant_system.agents.config import AgentLLMConfig, AgentRoleName, LLMProviderConfig
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


Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


def _http_transport(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urlopen(  # noqa: S310
            request,
            timeout=timeout_seconds,
            context=ssl_context,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise QuantSystemError(f"LLM provider returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise QuantSystemError(f"LLM provider connection failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise QuantSystemError("LLM provider response must be a JSON object")
    return payload


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


class LLMGateway:
    def __init__(self, config: AgentLLMConfig, transport: Transport | None = None) -> None:
        self._config = config
        self._transport = transport or _http_transport

    def complete(self, role: AgentRoleName, messages: list[LLMMessage]) -> LLMResponse:
        if not messages:
            raise QuantSystemError("LLM request requires at least one message")
        provider, role_config = self._config.route(role)
        api_key = provider.api_key()
        if provider.kind in {"openai", "openai_compatible"}:
            return self._openai_compatible(
                provider_name=role_config.provider,
                provider=provider,
                model=role_config.model,
                temperature=role_config.temperature,
                max_tokens=role_config.max_output_tokens,
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
        api_key: str,
        messages: list[LLMMessage],
    ) -> LLMResponse:
        payload = self._transport(
            _join_url(provider.base_url, "chat/completions"),
            {"Authorization": f"Bearer {api_key}"},
            {
                "model": model,
                "messages": [message.model_dump() for message in messages],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            provider.timeout_seconds,
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise QuantSystemError("invalid OpenAI-compatible response") from exc
        usage = payload.get("usage", {})
        return LLMResponse(
            content=str(content), provider=provider_name, model=model,
            input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
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
        conversation = [
            message.model_dump() for message in messages if message.role != "system"
        ]
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
            content = "".join(
                block["text"] for block in payload["content"] if block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise QuantSystemError("invalid Anthropic response") from exc
        usage = payload.get("usage", {})
        return LLMResponse(
            content=content, provider=provider_name, model=model,
            input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
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
                for message in messages if message.role != "system"
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
                part["text"] for part in payload["candidates"][0]["content"]["parts"]
                if "text" in part
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise QuantSystemError("invalid Gemini response") from exc
        usage = payload.get("usageMetadata", {})
        return LLMResponse(
            content=content, provider=provider_name, model=model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )