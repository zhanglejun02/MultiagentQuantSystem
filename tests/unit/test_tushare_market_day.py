from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from quant_system.data.ingestion.tushare import TushareMarketDaySync
from quant_system.domain.exceptions import QuantSystemError


class StubMarketClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def query(
        self, api_name: str, *, params: dict[str, Any], fields: str
    ) -> list[dict[str, Any]]:
        self.calls.append(api_name)
        del fields
        if api_name == "trade_cal":
            return [{"cal_date": "20260921", "is_open": 1}]
        if api_name == "daily":
            return [{
                "ts_code": "600000.SH", "trade_date": "20260921", "open": 10,
                "high": 10.5, "low": 9.9, "close": 10.2, "pre_close": 10,
                "vol": 123.45, "amount": 128.5,
            }]
        if api_name == "fut_daily":
            if params["exchange"] != "SHFE":
                return []
            return [{
                "ts_code": "RB2701.SHF", "trade_date": "20260921", "open": 3500,
                "high": 3550, "low": 3480, "close": 3520, "settle": 3518,
                "pre_settle": 3490, "pre_close": 3492, "vol": 100_000,
                "amount": 200_000, "oi": 250_000,
            }]
        if api_name == "suspend_d":
            raise QuantSystemError("permission denied")
        if api_name in {"adj_factor", "namechange"}:
            return []
        if api_name == "index_daily":
            return []
        raise AssertionError(api_name)


class NoFuturesPermissionClient(StubMarketClient):
    def query(
        self, api_name: str, *, params: dict[str, Any], fields: str
    ) -> list[dict[str, Any]]:
        if api_name == "fut_daily":
            raise QuantSystemError("code=40203: permission denied")
        return super().query(api_name, params=params, fields=fields)


class RateLimitedCalendarClient(StubMarketClient):
    def query(
        self, api_name: str, *, params: dict[str, Any], fields: str
    ) -> list[dict[str, Any]]:
        if api_name == "trade_cal":
            raise QuantSystemError("code=40203: rate limit")
        return super().query(api_name, params=params, fields=fields)


def test_market_day_sync_normalizes_core_datasets_and_optional_failures(tmp_path: Path) -> None:
    result = TushareMarketDaySync(StubMarketClient(), request_interval=0).sync(
        tmp_path, trade_date=date(2026, 9, 21)
    )

    assert result.unavailable_optional_datasets == ("suspensions",)
    assert result.datasets["equity_daily"][0]["volume"] == 12_345
    assert result.datasets["futures_daily"][0]["symbol"] == "RB2701.SHFE"
    assert result.datasets["futures_daily"][0]["product"] == "rb"
    output = Path(result.output_dir)
    assert pq.read_table(output / "equity_daily.parquet").num_rows == 1
    assert pq.read_table(output / "futures_daily.parquet").num_rows == 1
    assert (output / "dataset_metadata.json").exists()


def test_latest_open_date_uses_most_recent_calendar_row(tmp_path: Path) -> None:
    client = StubMarketClient()
    sync = TushareMarketDaySync(client, request_interval=0)
    latest = sync.latest_open_date(date(2026, 9, 21))
    assert latest == date(2026, 9, 21)

    sync.sync(tmp_path, trade_date=latest)
    assert client.calls.count("trade_cal") == 1


def test_market_day_sync_records_required_acquisition_error(tmp_path: Path) -> None:
    result = TushareMarketDaySync(
        NoFuturesPermissionClient(), request_interval=0
    ).sync(tmp_path, trade_date=date(2026, 9, 21))

    assert result.datasets["futures_daily"] == []
    futures_errors = [
        error
        for error in result.acquisition_errors
        if error.startswith("futures_daily[")
    ]
    assert len(futures_errors) == 1
    assert "40203" in futures_errors[0]


def test_market_day_sync_records_calendar_rate_limit(tmp_path: Path) -> None:
    result = TushareMarketDaySync(
        RateLimitedCalendarClient(), request_interval=0
    ).sync(tmp_path, trade_date=date(2026, 9, 21))

    assert result.datasets["trade_calendar"] == []
    assert any(error.startswith("trade_calendar:") for error in result.acquisition_errors)


def test_market_day_sync_can_skip_unavailable_futures(tmp_path: Path) -> None:
    client = NoFuturesPermissionClient()
    result = TushareMarketDaySync(client, request_interval=0).sync(
        tmp_path,
        trade_date=date(2026, 9, 21),
        include_futures=False,
        verify_trade_date=False,
    )

    assert result.datasets["equity_daily"]
    assert result.datasets["futures_daily"] == []
    assert not any(call == "fut_daily" for call in client.calls)
    assert not any(call == "trade_cal" for call in client.calls)
    assert not any(error.startswith("futures_daily") for error in result.acquisition_errors)


def test_market_day_sync_removes_stale_files_when_dataset_becomes_empty(
    tmp_path: Path,
) -> None:
    trading_day = date(2026, 9, 21)
    sync = TushareMarketDaySync(StubMarketClient(), request_interval=0)
    first = sync.sync(tmp_path, trade_date=trading_day)
    futures_path = Path(first.output_dir) / "futures_daily.parquet"
    assert futures_path.exists()

    second = sync.sync(
        tmp_path,
        trade_date=trading_day,
        include_futures=False,
        verify_trade_date=False,
    )

    assert not futures_path.exists()
    assert second.datasets["futures_daily"] == []