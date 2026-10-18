from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from quant_system.macro.fred import parse_fred_observations
from quant_system.macro.models import DEFAULT_SERIES, MacroCollectRequest
from quant_system.macro.safe import (
    discover_safe_html_page,
    discover_safe_year_pages,
    parse_safe_observations,
    validate_safe_url,
)

FETCHED_AT = datetime(2026, 10, 17, 8, tzinfo=UTC)


def test_macro_request_defaults_and_no_arbitrary_url() -> None:
    request = MacroCollectRequest()
    assert request.series_ids == DEFAULT_SERIES
    assert request.start_date == date(2023, 1, 1)
    assert request.end_date is None
    request.series_ids.clear()
    assert MacroCollectRequest().series_ids == DEFAULT_SERIES
    with pytest.raises(ValidationError):
        MacroCollectRequest.model_validate({"url": "https://example.com"})
    with pytest.raises(ValidationError):
        MacroCollectRequest(series_ids=["arbitrary"])
    with pytest.raises(ValidationError):
        MacroCollectRequest(start_date=date(2026, 2, 1), end_date=date(2026, 1, 1))


def test_macro_fred_zero_negative_and_missing_are_distinct() -> None:
    observations = parse_fred_observations(
        {
            "observations": [
                {"date": "2023-01-01", "value": "0"},
                {"date": "2023-01-02", "value": "-0.45", "realtime_start": "2026-10-17"},
                {"date": "2023-01-03", "value": "."},
            ]
        },
        series_id="DFII10",
        ingested_at=FETCHED_AT,
    )
    assert [row.value for row in observations] == [0.0, -0.45]
    assert all(row.available_at == FETCHED_AT for row in observations)
    assert all(row.published_at is None for row in observations)
    assert observations[1].vintage == {
        "kind": "current_download_not_historical_pit",
        "realtime_start": "2026-10-17",
        "realtime_end": "",
    }


@pytest.mark.parametrize("series_id,value", [("DEXCHUS", "0"), ("DTWEXBGS", "-1"), ("DGS10", "nan")])
def test_macro_fred_rejects_invalid_values(series_id: str, value: str) -> None:
    with pytest.raises(ValueError):
        parse_fred_observations(
            {"observations": [{"date": "2026-10-16", "value": value}]},
            series_id=series_id,
            ingested_at=FETCHED_AT,
        )


SAFE_URL = "https://www.safe.gov.cn/safe/2026/0206/27116.html"
SAFE_TABLE = """<table>
<tr><th rowspan="2">Item</th><th colspan="2">2026.01</th>
<th colspan="2">2026.02</th><th colspan="2">2026.12</th></tr>
<tr><th>100 million USD</th><th>100 million SDR</th>
<th>100 million USD</th><th>100 million SDR</th>
<th>100 million USD</th><th>100 million SDR</th></tr>
<tr><td rowspan="2">Gold</td><td>3695.82</td><td>2674.51</td>
<td>3875.88</td><td>2819.30</td><td></td><td></td></tr>
<tr><td colspan="2">7419\u4e07\u76ce\u53f8</td>
<td>7422\u4e07\u76ce\u53f8</td><td>7422\u4e07\u76ce\u53f8</td><td></td><td></td></tr>
</table>"""


def test_macro_safe_rowspan_colspan_currency_units_and_empty_future_month() -> None:
    observations = parse_safe_observations(
        SAFE_TABLE,
        source_url=SAFE_URL,
        ingested_at=FETCHED_AT,
        year=2026,
    )
    quantity = [row for row in observations if row.series_id == "CN_GOLD_RESERVES"]
    valuation = [row for row in observations if row.series_id == "CN_GOLD_RESERVES_USD"]
    assert [row.value for row in quantity] == [7419, 7422]
    assert [row.value for row in valuation] == [3695.82, 3875.88]
    assert quantity[0].unit == "ten_thousand_troy_ounces"
    assert valuation[0].unit == "hundred_million_USD"
    assert quantity[0].observation_date == date(2026, 1, 31)
    assert all(row.observation_date.month != 12 for row in observations)
    assert all(row.available_at == FETCHED_AT and row.published_at is None for row in observations)


def test_macro_safe_discovery_uses_title_year_not_url_year() -> None:
    pages = discover_safe_year_pages(
        '<a href="/safe/2020/0207/23935.html">Official reserve assets (2023)</a>'
        '<a href="https://evil.example/table.html">Official reserve assets (2024)</a>'
    )
    assert pages == {2023: "https://www.safe.gov.cn/safe/2020/0207/23935.html"}
    assert discover_safe_html_page('<a href="../0206/27116.html">html</a>', source_url=SAFE_URL) == SAFE_URL
    with pytest.raises(ValueError):
        validate_safe_url("https://www.safe.gov.cn.evil.example/safe/table.html")


def test_macro_safe_rejects_conflicting_quantity_or_wrong_year() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        parse_safe_observations(
            SAFE_TABLE.replace("7422\u4e07\u76ce\u53f8</td><td>7422", "7422\u4e07\u76ce\u53f8</td><td>7423"),
            source_url=SAFE_URL,
            ingested_at=FETCHED_AT,
        )
    with pytest.raises(ValueError, match="year"):
        parse_safe_observations(SAFE_TABLE, source_url=SAFE_URL, ingested_at=FETCHED_AT, year=2025)
