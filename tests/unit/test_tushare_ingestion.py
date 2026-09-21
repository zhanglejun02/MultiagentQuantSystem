from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from quant_system.data.ingestion.tushare import TushareClient, TushareDailySync
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import cn_dt


class StubTushareClient:
    def query(
        self, api_name: str, *, params: dict[str, Any], fields: str
    ) -> list[dict[str, Any]]:
        del fields
        if api_name == "stock_basic":
            if params["list_status"] == "L":
                return [
                    {
                        "ts_code": "600000.SH",
                        "name": "浦发银行",
                        "market": "主板",
                        "exchange": "SSE",
                        "list_status": "L",
                        "list_date": "19991110",
                        "delist_date": None,
                    },
                    {
                        "ts_code": "300001.SZ",
                        "name": "特锐德",
                        "market": "创业板",
                        "exchange": "SZSE",
                        "list_status": "L",
                        "list_date": "20091030",
                        "delist_date": None,
                    },
                    {
                        "ts_code": "430001.BJ",
                        "name": "北交所样本",
                        "market": "北交所",
                        "exchange": "BSE",
                        "list_status": "L",
                        "list_date": "20211115",
                        "delist_date": None,
                    },
                ]
            if params["list_status"] == "D":
                return [
                    {
                        "ts_code": "600001.SH",
                        "name": "退市样本",
                        "market": "主板",
                        "exchange": "SSE",
                        "list_status": "D",
                        "list_date": "20000101",
                        "delist_date": "20240103",
                    }
                ]
            return []
        if api_name == "trade_cal":
            return [{"cal_date": "20240102", "is_open": 1}]
        if api_name == "daily":
            return [
                {
                    "ts_code": "600000.SH",
                    "trade_date": "20240102",
                    "open": 10.1,
                    "high": 10.5,
                    "low": 10.0,
                    "close": 10.4,
                    "pre_close": 10.0,
                    "vol": 123.45,
                    "amount": 128.5,
                },
                {
                    "ts_code": "430001.BJ",
                    "trade_date": "20240102",
                    "open": 5,
                    "high": 5,
                    "low": 5,
                    "close": 5,
                    "pre_close": 5,
                    "vol": 10,
                    "amount": 5,
                },
            ]
        raise AssertionError(f"unexpected API: {api_name}")


def test_tushare_client_rejects_missing_token(monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(QuantSystemError, match="TUSHARE_TOKEN"):
        TushareClient()


def test_tushare_client_surfaces_api_error():
    def transport(url, body, timeout):
        del url, body, timeout
        return {"code": 2002, "msg": "权限不足", "data": None}

    client = TushareClient("token", transport=transport)
    with pytest.raises(QuantSystemError, match=r"code=2002.*权限不足"):
        client.query("trade_cal", params={}, fields="cal_date")


def test_sync_writes_normalized_pit_dataset(tmp_path: Path):
    output = tmp_path / "tushare"
    result = TushareDailySync(StubTushareClient(), request_interval=0).sync(
        output,
        start=date(2024, 1, 2),
        end=date(2024, 1, 2),
    )

    rows = pq.read_table(output / "bars.parquet").to_pylist()
    assert result.fetched_bars == 1
    assert rows[0]["symbol"] == "600000.SSE"
    assert rows[0]["volume"] == 12_345
    assert rows[0]["amount"] == 128_500
    assert rows[0]["available_at"] == cn_dt(date(2024, 1, 2), 17, 0)
    assert (output / "status_intervals.json").read_text().strip() == "[]"
    assert (output / "corporate_actions.json").read_text().strip() == "[]"
    assert (output / "dataset_metadata.json").exists()

    provider = LocalParquetProvider(str(output))
    before_delist = provider.get_securities(as_of=cn_dt(date(2024, 1, 2), 17, 0))
    after_delist = provider.get_securities(as_of=cn_dt(date(2024, 1, 3), 17, 0))
    before = next(security for security in before_delist if security.symbol == "600001.SSE")
    after = next(security for security in after_delist if security.symbol == "600001.SSE")
    assert before.delist_date is None
    assert after.delist_date == date(2024, 1, 3)
    assert all(security.exchange.value != "BSE" for security in after_delist)


def test_sync_merges_existing_bars_by_symbol_and_date(tmp_path: Path):
    output = tmp_path / "tushare"
    sync = TushareDailySync(StubTushareClient(), request_interval=0)
    sync.sync(output, start=date(2024, 1, 2), end=date(2024, 1, 2))
    result = sync.sync(output, start=date(2024, 1, 2), end=date(2024, 1, 2))

    assert result.total_bars == 1