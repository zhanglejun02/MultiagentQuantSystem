"""Bounded official-source collection with an injectable network boundary."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import certifi
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_system.artifacts.envelope import hash_bytes
from quant_system.domain.types import require_aware, utc_now
from quant_system.macro.fred import FRED_SERIES, parse_fred_observations
from quant_system.macro.models import MacroCollectRequest, MacroObservation
from quant_system.macro.safe import (
    SAFE_INDEX_URL,
    discover_safe_html_page,
    discover_safe_year_pages,
    parse_safe_observations,
    validate_safe_url,
)

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


class MacroTransport(Protocol):
    def get(self, url: str, *, params: Mapping[str, str], timeout: float) -> bytes: ...


class SourceUnavailable(ValueError):
    pass


def _transport_error_code(error: Exception) -> str:
    if not isinstance(error, SourceUnavailable):
        return "TRANSPORT_ERROR"
    code = str(error)
    known = {
        "TRANSPORT_ERROR",
        "RESPONSE_TOO_LARGE",
        "SAFE_QUERY_NOT_ALLOWED",
        "RESPONSE_TOO_LARGE_OR_INVALID",
        "RESPONSE_CONTAINED_CREDENTIAL_MATERIAL",
    }
    return code if code in known or re.fullmatch(r"HTTP_[1-5]\d\d", code) else "TRANSPORT_ERROR"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibMacroTransport:
    def __init__(self, *, max_response_bytes: int = 2_000_000) -> None:
        self._max_response_bytes = max_response_bytes

    def get(self, url: str, *, params: Mapping[str, str], timeout: float) -> bytes:
        if url != FRED_OBSERVATIONS_URL:
            validate_safe_url(url)
            if params:
                raise SourceUnavailable("SAFE_QUERY_NOT_ALLOWED")
        query = urlencode(params)
        request = urllib.request.Request(
            url + ("?" + query if query else ""),
            headers={"User-Agent": "QuantMacroResearch/1.0", "Accept-Encoding": "identity"},
        )
        opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(self._max_response_bytes + 1)
        except urllib.error.HTTPError as error:
            raise SourceUnavailable(f"HTTP_{int(error.code)}") from None
        except Exception:
            raise SourceUnavailable("TRANSPORT_ERROR") from None
        if len(body) > self._max_response_bytes:
            raise SourceUnavailable("RESPONSE_TOO_LARGE")
        return body


class MacroSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(default=15, gt=0, le=60)
    fred_page_size: int = Field(default=1000, ge=1, le=10000)
    fred_max_pages: int = Field(default=12, ge=1, le=50)
    safe_max_years: int = Field(default=8, ge=1, le=20)
    max_requests: int = Field(default=64, ge=1, le=256)
    max_response_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    safe_published_pages: dict[int, str] = Field(default_factory=dict)

    @field_validator("safe_published_pages")
    @classmethod
    def official_pages(cls, pages: dict[int, str]) -> dict[int, str]:
        if len(pages) > 20 or any(not 1900 <= year <= 2200 for year in pages):
            raise ValueError("SAFE configured publication years exceed bounds")
        for url in pages.values():
            validate_safe_url(url)
        return pages


def load_macro_config(path: str | Path) -> MacroSourceConfig:
    with Path(path).open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    return MacroSourceConfig.model_validate(payload)


@dataclass
class SourceBatch:
    observations: list[MacroObservation] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)
    missing: dict[str, str] = field(default_factory=dict)


Fetch = Callable[[str, Mapping[str, str]], tuple[bytes, datetime, str]]


def expanded_series(series_ids: list[str]) -> list[str]:
    result = list(series_ids)
    if "CN_GOLD_RESERVES" in result and "CN_GOLD_RESERVES_USD" not in result:
        result.append("CN_GOLD_RESERVES_USD")
    return result


def _fred_series(
    series_id: str,
    *,
    start_date: date,
    end_date: date,
    api_key: str,
    config: MacroSourceConfig,
    fetch: Fetch,
) -> list[MacroObservation]:
    observations: dict[date, MacroObservation] = {}
    offset = 0
    expected_count: int | None = None
    for _ in range(config.fred_max_pages):
        body, fetched_at, digest = fetch(
            FRED_OBSERVATIONS_URL,
            {
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "observation_start": start_date.isoformat(),
                "observation_end": end_date.isoformat(),
                "sort_order": "asc",
                "limit": str(config.fred_page_size),
                "offset": str(offset),
            },
        )
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise SourceUnavailable("FRED_INVALID_RESPONSE")
        count = payload.get("count")
        rows = payload.get("observations")
        if (
            type(count) is not int
            or count < 0
            or not isinstance(rows, list)
            or len(rows) > config.fred_page_size
            or payload.get("offset", offset) != offset
        ):
            raise SourceUnavailable("FRED_INVALID_PAGINATION")
        if expected_count is not None and count != expected_count:
            raise SourceUnavailable("FRED_COUNT_CHANGED_DURING_PAGINATION")
        expected_count = count
        if count > config.fred_page_size * config.fred_max_pages:
            raise SourceUnavailable("FRED_PAGINATION_LIMIT")
        for observation in parse_fred_observations(
            payload,
            series_id=series_id,
            ingested_at=fetched_at,
            raw_content_hash=digest,
        ):
            if not start_date <= observation.observation_date <= end_date:
                raise SourceUnavailable("FRED_OUTSIDE_REQUESTED_WINDOW")
            if observation.observation_date in observations:
                raise SourceUnavailable("FRED_DUPLICATE_DATE_ACROSS_PAGES")
            observations[observation.observation_date] = observation
        offset += len(rows)
        if offset == count:
            return [observations[observed] for observed in sorted(observations)]
        if not rows or offset > count:
            raise SourceUnavailable("FRED_INVALID_PAGINATION")
    raise SourceUnavailable("FRED_PAGINATION_LIMIT")


def _html(body: bytes) -> str:
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return body.decode("gb18030")


def _safe_series(
    *,
    start_date: date,
    end_date: date,
    config: MacroSourceConfig,
    fetch: Fetch,
    batch: SourceBatch,
) -> None:
    pages = dict(config.safe_published_pages)
    try:
        body, _, _ = fetch(SAFE_INDEX_URL, {})
        discovered = discover_safe_year_pages(_html(body))
        pages = {**discovered, **pages}
    except Exception:
        batch.missing["SAFE_DISCOVERY"] = "SAFE_INDEX_UNAVAILABLE_OR_CHANGED"
    years = list(range(start_date.year, end_date.year + 1))
    for year in years[: -config.safe_max_years]:
        batch.missing[f"SAFE:{year}"] = "SAFE_YEAR_LIMIT"
    for year in years[-config.safe_max_years :]:
        source_url = pages.get(year)
        if source_url is None:
            batch.missing[f"SAFE:{year}"] = "SAFE_YEAR_PAGE_NOT_DISCOVERED"
            continue
        try:
            body, fetched_at, digest = fetch(source_url, {})
            html = _html(body)
            linked = discover_safe_html_page(html, source_url=source_url)
            if linked is not None and linked != source_url:
                source_url = linked
                body, fetched_at, digest = fetch(source_url, {})
                html = _html(body)
            observations = parse_safe_observations(
                html,
                source_url=source_url,
                ingested_at=fetched_at,
                year=year,
                raw_content_hash=digest,
            )
            in_window = [row for row in observations if start_date <= row.observation_date <= end_date]
            if not in_window:
                batch.missing[f"SAFE:{year}"] = "SAFE_NO_OBSERVATIONS_IN_WINDOW"
            batch.observations.extend(in_window)
        except SourceUnavailable as error:
            batch.missing[f"SAFE:{year}"] = str(error)
        except Exception:
            batch.missing[f"SAFE:{year}"] = "SAFE_INVALID_TABLE"


def collect_sources(
    request: MacroCollectRequest,
    *,
    end_date: date,
    transport: MacroTransport,
    clock: Callable[[], datetime] = utc_now,
    config: MacroSourceConfig | None = None,
    fred_api_key: str | None = None,
) -> SourceBatch:
    settings = config or MacroSourceConfig()
    secret = fred_api_key if fred_api_key is not None else os.environ.get("FRED_API_KEY", "")
    batch = SourceBatch()
    request_count = 0

    def fetch(url: str, params: Mapping[str, str]) -> tuple[bytes, datetime, str]:
        nonlocal request_count
        if request_count >= settings.max_requests:
            raise SourceUnavailable("REQUEST_BUDGET_EXHAUSTED")
        request_count += 1
        safe_parameters = {name: value for name, value in params.items() if name != "api_key"}
        try:
            body = transport.get(url, params=params, timeout=settings.timeout_seconds)
            if not isinstance(body, bytes) or len(body) > settings.max_response_bytes:
                raise SourceUnavailable("RESPONSE_TOO_LARGE_OR_INVALID")
            if (secret and secret.encode() in body) or b"api_key" in body.lower():
                raise SourceUnavailable("RESPONSE_CONTAINED_CREDENTIAL_MATERIAL")
        except Exception as error:
            code = _transport_error_code(error)
            failed_at = require_aware(clock()).astimezone(UTC)
            batch.responses.append(
                {
                    "source_url": url,
                    "request_parameters": safe_parameters,
                    "ingested_at": failed_at.isoformat(),
                    "available_at": failed_at.isoformat(),
                    "error": code,
                }
            )
            raise SourceUnavailable(code) from None
        fetched_at = require_aware(clock()).astimezone(UTC)
        digest = hash_bytes(body)
        batch.responses.append(
            {
                "source_url": url,
                "request_parameters": safe_parameters,
                "ingested_at": fetched_at.isoformat(),
                "available_at": fetched_at.isoformat(),
                "content_hash": digest,
                "body_base64": base64.b64encode(body).decode("ascii"),
            }
        )
        return body, fetched_at, digest

    for series_id in request.series_ids:
        if series_id not in FRED_SERIES:
            continue
        if not secret:
            batch.missing[series_id] = "FRED_API_KEY_NOT_CONFIGURED"
            continue
        try:
            rows = _fred_series(
                series_id,
                start_date=request.start_date,
                end_date=end_date,
                api_key=secret,
                config=settings,
                fetch=fetch,
            )
            batch.observations.extend(rows)
            if not rows:
                batch.missing[series_id] = "FRED_NO_USABLE_OBSERVATIONS"
        except SourceUnavailable as error:
            batch.missing[series_id] = str(error)
        except Exception:
            batch.missing[series_id] = "FRED_INVALID_RESPONSE"
    requested = expanded_series(request.series_ids)
    if any(series_id.startswith("CN_GOLD_RESERVES") for series_id in requested):
        _safe_series(
            start_date=request.start_date, end_date=end_date, config=settings, fetch=fetch, batch=batch
        )
    batch.observations = [row for row in batch.observations if row.series_id in requested]
    present = {row.series_id for row in batch.observations}
    for series_id in requested:
        if series_id not in present:
            batch.missing.setdefault(series_id, "SOURCE_HAS_NO_USABLE_OBSERVATIONS")
    batch.observations.sort(key=lambda row: (row.series_id, row.observation_date))
    return batch
