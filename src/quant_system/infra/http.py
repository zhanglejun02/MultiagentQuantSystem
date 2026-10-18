"""Shared read-only outbound HTTPS transport.

Single home for certifi trust, timeouts, response-size caps, redirect policy
and structured error conversion. Ingestion modules keep only parsing logic and
inject fakes in tests by patching their imported ``request_json``/``request_bytes``.
Never used for order routing.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

import certifi

from quant_system.domain.exceptions import QuantSystemError

_ERROR_BODY_LIMIT = 500


class HttpStatusError(QuantSystemError):
    """Server answered with a non-2xx status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class HttpConnectionError(QuantSystemError):
    """Connection, TLS or DNS failure before a response arrived."""

    def __init__(self, reason: object) -> None:
        super().__init__(f"connection failed: {reason}")
        self.reason = reason


class HttpResponseTooLargeError(QuantSystemError):
    """Response body exceeded the configured size cap."""


_ssl_context: ssl.SSLContext | None = None


def default_ssl_context() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    return _ssl_context


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def request_bytes(
    url: str,
    *,
    method: str | None = None,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int | None = None,
    allow_redirects: bool = True,
) -> bytes:
    if not url.startswith("https://"):
        raise QuantSystemError("outbound transport requires HTTPS")
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPSHandler(context=default_ssl_context())]
    if not allow_redirects:
        handlers.insert(0, _NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            body: bytes = response.read(max_bytes + 1) if max_bytes is not None else response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:_ERROR_BODY_LIMIT]
        except Exception:
            detail = ""
        raise HttpStatusError(int(exc.code), detail) from exc
    except urllib.error.URLError as exc:
        raise HttpConnectionError(exc.reason) from exc
    if max_bytes is not None and len(body) > max_bytes:
        raise HttpResponseTooLargeError(f"response exceeded {max_bytes} bytes")
    return body


def request_json(
    url: str,
    *,
    method: str | None = None,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int | None = None,
    allow_redirects: bool = True,
) -> Any:
    body = request_bytes(
        url,
        method=method,
        data=data,
        headers=headers,
        timeout=timeout,
        max_bytes=max_bytes,
        allow_redirects=allow_redirects,
    )
    return json.loads(body.decode("utf-8"))
