import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from quant_system.data.ingestion.baseline import BaselineDatasetBuilder
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import bar_available_at, cn_dt
from quant_system.research.baseline.spec import BaselineSpec
from quant_system.rules.reference_price import CorporateActionReferencePriceService


class ResearchVendor:
    def __init__(self, missing_factor=False):
        self.calls = []
        self.missing_factor = missing_factor

    def query(self, api_name, *, params, fields):
        self.calls.append(api_name)
        if api_name == "index_daily":
            return [
                {"trade_date": "20250102", "close": 100, "pre_close": 100},
                {"trade_date": "20250103", "close": 101, "pre_close": 100},
            ]
        if api_name == "stock_basic":
            return [
                {
                    "ts_code": "600036.SH",
                    "name": "Bank",
                    "market": "主板",
                    "list_date": "20000101",
                    "delist_date": None,
                }
            ]
        if api_name == "daily":
            return [
                {
                    "ts_code": "600036.SH",
                    "trade_date": day,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "pre_close": close,
                    "vol": 1000,
                    "amount": 10000,
                }
                for day, close in (("20250102", 10), ("20250103", 9))
            ]
        if api_name == "adj_factor":
            values = [("20250102", 1)] if self.missing_factor else [("20250102", 1), ("20250103", 10 / 9)]
            return [{"trade_date": day, "adj_factor": factor} for day, factor in values]
        if api_name == "dividend":
            return [
                {
                    "ex_date": "20250103",
                    "ann_date": "20241201",
                    "div_proc": "实施",
                    "stk_div": 0,
                    "cash_div_tax": 1,
                }
            ]
        if api_name == "namechange":
            return [{"name": "Bank", "start_date": "20000101", "end_date": None}]
        raise AssertionError(api_name)


def prepare(builder, directory):
    return builder.prepare(
        directory,
        symbols=("600036.SH",),
        benchmark="000300.SH",
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        protocol_hash="protocol",
    )


def test_preparation_caches_vendor_rows_and_restores_unadjusted_reference(tmp_path):
    vendor = ResearchVendor()
    builder = BaselineDatasetBuilder(vendor, tmp_path / "raw", request_interval=0)
    metadata = prepare(builder, tmp_path / "prepared")
    first_call_count = len(vendor.calls)
    prepare(builder, tmp_path / "other")
    assert len(vendor.calls) == first_call_count
    assert metadata["n_cash_dividends"] == 1
    provider = LocalParquetProvider(str(tmp_path / "prepared"))
    day = date(2025, 1, 3)
    bar = provider.get_bars(None, day, day, as_of=bar_available_at(day))[0]
    actions = provider.get_corporate_actions(day, day, as_of=cn_dt(day, 9, 15))
    reference = CorporateActionReferencePriceService().calculate(
        bar.prev_close,
        actions,
        rule_version="test",
        security_status="NORMAL",
    )
    assert bar.prev_close == Decimal("10")
    assert reference == Decimal("9")
    assert provider.get_corporate_actions(day, day, as_of=cn_dt(day, 8, 59)) == []
    assert len(pq.read_table(tmp_path / "prepared" / "adjustments.parquet")) == 2


def test_missing_adjustment_blocks_prepared_publication(tmp_path):
    builder = BaselineDatasetBuilder(
        ResearchVendor(missing_factor=True), tmp_path / "raw", request_interval=0
    )
    with pytest.raises(QuantSystemError, match="missing or duplicated"):
        prepare(builder, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()


def test_cash_actions_generate_forward_only_adjustment_factors(tmp_path):
    builder = BaselineDatasetBuilder(ResearchVendor(), tmp_path / "raw", request_interval=0)
    builder.prepare(
        tmp_path / "prepared",
        symbols=("600036.SH",),
        benchmark="000300.SH",
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        protocol_hash="protocol",
        adjustment_source="cash_actions",
    )
    values = pq.read_table(tmp_path / "prepared" / "adjustments.parquet").to_pylist()
    assert values[0]["factor"] == 1
    assert values[1]["factor"] == pytest.approx(10 / 9)
    assert list((tmp_path / "prepared" / "raw" / "daily").glob("*.json"))


def test_prepared_write_failure_leaves_no_partial_dataset(tmp_path, monkeypatch):
    def fail_write(*args, **kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr("quant_system.data.ingestion.baseline.pq.write_table", fail_write)
    builder = BaselineDatasetBuilder(ResearchVendor(), tmp_path / "raw", request_interval=0)
    with pytest.raises(OSError, match="disk failure"):
        prepare(builder, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()
    assert not list(tmp_path.glob(".prepare-*"))


def test_large_dividend_reference_disagreement_is_rejected(tmp_path):
    class ConflictingVendor(ResearchVendor):
        def query(self, api_name, *, params, fields):
            rows = super().query(api_name, params=params, fields=fields)
            if api_name == "daily":
                rows[-1]["pre_close"] = 8
            return rows

    builder = BaselineDatasetBuilder(ConflictingVendor(), tmp_path / "raw", request_interval=0)
    with pytest.raises(QuantSystemError, match="disagreement"):
        prepare(builder, tmp_path / "prepared")
    assert not (tmp_path / "prepared").exists()


def test_tampered_raw_cache_is_rejected(tmp_path):
    builder = BaselineDatasetBuilder(ResearchVendor(), tmp_path / "raw", request_interval=0)
    prepare(builder, tmp_path / "prepared")
    cache = next((tmp_path / "raw" / "daily").glob("*.json"))
    payload = json.loads(cache.read_text())
    payload["rows"][0]["close"] = 999
    cache.write_text(json.dumps(payload))
    with pytest.raises(QuantSystemError, match="integrity"):
        prepare(builder, tmp_path / "other")


def test_protocol_rejects_overlapping_holdout():
    spec = BaselineSpec.load(Path("configs/research/a_share_baseline.yaml"))
    payload = spec.model_dump(mode="json")
    payload["segments"][2]["start"] = "2025-06-01"
    with pytest.raises(ValueError, match="ordered, disjoint"):
        BaselineSpec.model_validate(payload)
