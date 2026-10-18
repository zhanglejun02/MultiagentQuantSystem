from datetime import date
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.models import Bar
from quant_system.domain.types import bar_available_at, cn_dt


def test_adjustments_are_filtered_by_availability(tmp_path):
    day = date(2025, 1, 2)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "symbol": "600000.SSE",
                    "trade_date": day,
                    "factor": 2.0,
                    "available_at": bar_available_at(day),
                },
            ]
        ),
        tmp_path / "adjustments.parquet",
    )
    provider = LocalParquetProvider(str(tmp_path))
    bar = Bar(
        symbol="600000.SSE",
        trade_date=day,
        open=10,
        high=10,
        low=10,
        close=10,
        prev_close=10,
        volume=1000,
        amount=10000,
        available_at=bar_available_at(day),
    )
    with pytest.raises(QuantSystemError, match="missing PIT"):
        provider.adjusted_closes([bar], as_of=cn_dt(day, 16, 0))
    assert provider.adjusted_closes([bar], as_of=bar_available_at(day))[(bar.symbol, day)] == Decimal("20")


def test_benchmark_requires_complete_calendar_and_previous_close_anchor(tmp_path):
    day = date(2025, 1, 2)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "symbol": "000300.SH",
                    "trade_date": day,
                    "close": 110.0,
                    "prev_close": 100.0,
                    "available_at": bar_available_at(day),
                },
            ]
        ),
        tmp_path / "benchmarks.parquet",
    )
    provider = LocalParquetProvider(str(tmp_path))
    assert [value for _, value in provider.benchmark_series("000300.SH", [day])] == [100, 110]
    with pytest.raises(QuantSystemError, match="missing or duplicated"):
        provider.benchmark_series("UNKNOWN", [day])
    with pytest.raises(QuantSystemError, match="missing or duplicated"):
        provider.benchmark_series("000300.SH", [day, date(2025, 1, 3)])
