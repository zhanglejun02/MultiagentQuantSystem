from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import HTTPError

import pytest
from tests.unit.test_macro_parsers import SAFE_TABLE, SAFE_URL

from quant_system.macro import ingestion
from quant_system.macro.ingestion import (
    FRED_OBSERVATIONS_URL,
    MacroSourceConfig,
    SourceUnavailable,
    UrllibMacroTransport,
    collect_sources,
    load_macro_config,
)
from quant_system.macro.models import MacroCollectRequest

FETCHED_AT = datetime(2026, 10, 17, 8, tzinfo=UTC)


class OfflineTransport:
    def __init__(self, pages: dict[str, bytes | Exception]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def get(self, url: str, *, params: Mapping[str, str], timeout: float) -> bytes:
        self.calls.append((url, dict(params), timeout))
        key = f"{url}:{params.get('offset', '')}" if params else url
        body = self.pages.get(key, OSError("source not available"))
        if isinstance(body, Exception):
            raise body
        return body


def fred_page(offset: int, count: int, rows: list[tuple[str, str]]) -> bytes:
    return json.dumps(
        {
            "offset": offset,
            "count": count,
            "observations": [{"date": observed, "value": value} for observed, value in rows],
        }
    ).encode()


def test_macro_fred_bounded_pagination_and_no_secret_in_raw_manifest() -> None:
    transport = OfflineTransport(
        {
            f"{FRED_OBSERVATIONS_URL}:0": fred_page(0, 3, [("2026-01-01", "0"), ("2026-01-02", ".")]),
            f"{FRED_OBSERVATIONS_URL}:2": fred_page(2, 3, [("2026-01-03", "-0.5")]),
        }
    )
    batch = collect_sources(
        MacroCollectRequest(series_ids=["DFII10"], start_date=date(2026, 1, 1)),
        end_date=date(2026, 10, 17),
        transport=transport,
        clock=lambda: FETCHED_AT,
        fred_api_key="test-secret-never-persist",
        config=MacroSourceConfig(fred_page_size=2),
    )
    assert [row.value for row in batch.observations] == [0, -0.5]
    assert len(transport.calls) == 2
    assert batch.missing == {}
    manifest = json.dumps(batch.responses)
    assert "test-secret" not in manifest and "api_key" not in manifest
    assert all(row.raw_content_hash for row in batch.observations)


def test_macro_missing_fred_key_still_collects_safe(monkeypatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    transport = OfflineTransport(
        {
            "https://www.safe.gov.cn/safe/whcb/index.html": f'<a href="{SAFE_URL}">Official reserve assets (2026)</a>'.encode(),
            SAFE_URL: SAFE_TABLE.encode(),
        }
    )
    batch = collect_sources(
        MacroCollectRequest(start_date=date(2026, 1, 1)),
        end_date=date(2026, 10, 17),
        transport=transport,
        clock=lambda: FETCHED_AT,
    )
    assert len(batch.observations) == 4
    assert batch.missing["DGS10"] == "FRED_API_KEY_NOT_CONFIGURED"
    assert {row.series_id for row in batch.observations} == {"CN_GOLD_RESERVES", "CN_GOLD_RESERVES_USD"}
    assert all(not params for _, params, _ in transport.calls)


def test_macro_source_error_is_sanitized_and_no_fallback() -> None:
    secret = "test-secret-never-persist"
    transport = OfflineTransport({f"{FRED_OBSERVATIONS_URL}:0": OSError(f"https://bad/?api_key={secret}")})
    batch = collect_sources(
        MacroCollectRequest(series_ids=["DGS10"]),
        end_date=date(2026, 10, 17),
        transport=transport,
        clock=lambda: FETCHED_AT,
        fred_api_key=secret,
    )
    assert batch.observations == []
    assert batch.missing == {"DGS10": "TRANSPORT_ERROR"}
    assert len(transport.calls) == 1
    assert secret not in json.dumps(batch.responses)
    assert "bad" not in json.dumps(batch.responses)


def test_macro_fred_pagination_limit_does_not_publish_truncated_series() -> None:
    transport = OfflineTransport({f"{FRED_OBSERVATIONS_URL}:0": fred_page(0, 1000, [("2026-01-01", "1")])})
    batch = collect_sources(
        MacroCollectRequest(series_ids=["DGS10"]),
        end_date=date(2026, 10, 17),
        transport=transport,
        clock=lambda: FETCHED_AT,
        fred_api_key="secret",
        config=MacroSourceConfig(fred_page_size=1, fred_max_pages=2),
    )
    assert batch.observations == []
    assert batch.missing == {"DGS10": "FRED_PAGINATION_LIMIT"}
    assert len(transport.calls) == 1


def test_macro_safe_missing_year_is_explicit_and_config_rejects_arbitrary_hosts() -> None:
    transport = OfflineTransport(
        {
            "https://www.safe.gov.cn/safe/whcb/index.html": f'<a href="{SAFE_URL}">Official reserve assets (2026)</a>'.encode(),
            SAFE_URL: SAFE_TABLE.encode(),
        }
    )
    batch = collect_sources(
        MacroCollectRequest(series_ids=["CN_GOLD_RESERVES"], start_date=date(2025, 1, 1)),
        end_date=date(2026, 10, 17),
        transport=transport,
        clock=lambda: FETCHED_AT,
    )
    assert batch.missing["SAFE:2025"] == "SAFE_YEAR_PAGE_NOT_DISCOVERED"
    assert len(transport.calls) == 2
    with pytest.raises(ValueError):
        MacroSourceConfig(safe_published_pages={2026: "https://evil.example/safe/table.html"})


@pytest.mark.parametrize("error_code", [401, 429, 500])
def test_macro_urllib_http_errors_never_expose_url_or_key(monkeypatch, error_code: int) -> None:
    secret = "never-log-this-key"

    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url.startswith(FRED_OBSERVATIONS_URL + "?")
            assert timeout == 3
            raise HTTPError(request.full_url, error_code, "source echoed " + secret, {}, None)

    monkeypatch.setattr(ingestion.urllib.request, "build_opener", lambda *handlers: Opener())
    with pytest.raises(SourceUnavailable) as raised:
        UrllibMacroTransport().get(FRED_OBSERVATIONS_URL, params={"api_key": secret}, timeout=3)
    assert str(raised.value) == f"HTTP_{error_code}"
    assert secret not in str(raised.value)
    assert raised.value.__suppress_context__


def test_macro_urllib_tls_response_size_and_host_restrictions(monkeypatch) -> None:
    handlers_seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 5
            return b"12345"

    class Opener:
        def open(self, request, *, timeout):
            return Response()

    def build(*handlers):
        handlers_seen.extend(handlers)
        return Opener()

    monkeypatch.setattr(ingestion.urllib.request, "build_opener", build)
    with pytest.raises(SourceUnavailable, match="RESPONSE_TOO_LARGE"):
        UrllibMacroTransport(max_response_bytes=4).get(FRED_OBSERVATIONS_URL, params={}, timeout=1)
    assert any(isinstance(handler, ingestion.urllib.request.HTTPSHandler) for handler in handlers_seen)
    assert any(isinstance(handler, ingestion._NoRedirect) for handler in handlers_seen)
    with pytest.raises(ValueError, match="allowlisted"):
        UrllibMacroTransport().get("https://evil.example/safe/table.html", params={}, timeout=1)


def test_macro_echoed_credentials_and_untrusted_error_are_not_frozen() -> None:
    secret = "never-save-this-key"
    for response in [
        json.dumps({"error": f"https://fred/?api_key={secret}"}).encode(),
        json.dumps({"api_key": "redacted"}).encode(),
        SourceUnavailable(secret),
    ]:
        transport = OfflineTransport({f"{FRED_OBSERVATIONS_URL}:0": response})
        batch = collect_sources(
            MacroCollectRequest(series_ids=["DGS10"]),
            end_date=date(2026, 10, 17),
            transport=transport,
            clock=lambda: FETCHED_AT,
            fred_api_key=secret,
        )
        assert not batch.observations
        assert len(batch.responses) == 1
        assert "body_base64" not in batch.responses[0]
        assert secret not in json.dumps(batch.responses)
        assert "api_key" not in json.dumps(batch.responses)
        assert secret not in json.dumps(batch.missing)


def test_macro_internal_page_survives_discovery_failure_without_provider_fallback() -> None:
    transport = OfflineTransport({SAFE_URL: SAFE_TABLE.encode()})
    batch = collect_sources(
        MacroCollectRequest(series_ids=["CN_GOLD_RESERVES"], start_date=date(2026, 1, 1)),
        end_date=date(2026, 10, 17),
        transport=transport,
        clock=lambda: FETCHED_AT,
        config=MacroSourceConfig(safe_published_pages={2026: SAFE_URL}),
    )
    assert len(batch.observations) == 4
    assert batch.missing == {"SAFE_DISCOVERY": "SAFE_INDEX_UNAVAILABLE_OR_CHANGED"}
    assert len(transport.calls) == 2


def test_macro_safe_year_bounds_and_request_budget_are_visible() -> None:
    transport = OfflineTransport(
        {
            "https://www.safe.gov.cn/safe/whcb/index.html": f'<a href="{SAFE_URL}">Official reserve assets (2026)</a>'.encode(),
        }
    )
    batch = collect_sources(
        MacroCollectRequest(series_ids=["CN_GOLD_RESERVES"], start_date=date(2023, 1, 1)),
        end_date=date(2026, 10, 17),
        transport=transport,
        clock=lambda: FETCHED_AT,
        config=MacroSourceConfig(safe_max_years=1, max_requests=1),
    )
    assert not batch.observations
    assert batch.missing["SAFE:2023"] == "SAFE_YEAR_LIMIT"
    assert batch.missing["SAFE:2026"] == "REQUEST_BUDGET_EXHAUSTED"
    assert len(transport.calls) == 1


def test_macro_repository_config_is_typed_and_has_no_credentials() -> None:
    path = Path(__file__).resolve().parents[2] / "configs" / "data" / "macro.yaml"
    config = load_macro_config(path)
    assert config.timeout_seconds == 15
    assert config.safe_published_pages == {}
    assert "api_key" not in config.model_dump_json()
    with pytest.raises(ValueError):
        MacroSourceConfig(timeout_seconds=0)
