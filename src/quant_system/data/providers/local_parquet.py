"""Local Parquet market/reference data provider for testing and fixtures.

Layout of a data directory (a candidate for a DataSnapshot):
    bars.parquet          - daily bars with available_at column
    securities.json       - security master records
    status_intervals.json - ST / suspension intervals with available_at
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

from quant_system.domain.exceptions import PITViolationError, QuantSystemError
from quant_system.domain.models import Bar, CorporateActionRecord, SecurityMaster, StatusInterval
from quant_system.domain.types import bar_available_at, require_aware, to_decimal


class LocalParquetProvider:
    """Implements MarketDataProvider + ReferenceDataProvider over local files."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = Path(data_dir)
        self._bars: list[Bar] | None = None
        self._securities: list[SecurityMaster] | None = None
        self._statuses: list[StatusInterval] | None = None
        self._corporate_actions: list[CorporateActionRecord] | None = None
        self._adjustments: list[dict] | None = None

    # -- loading -----------------------------------------------------------
    def _load_bars(self) -> list[Bar]:
        if self._bars is None:
            table = pq.read_table(self.data_dir / "bars.parquet")
            rows = table.to_pylist()
            self._bars = [
                Bar(
                    symbol=r["symbol"],
                    trade_date=r["trade_date"],
                    open=to_decimal(r["open"]),
                    high=to_decimal(r["high"]),
                    low=to_decimal(r["low"]),
                    close=to_decimal(r["close"]),
                    prev_close=to_decimal(r["prev_close"]),
                    volume=int(r["volume"]),
                    amount=to_decimal(r["amount"]),
                    available_at=r["available_at"],
                )
                for r in rows
            ]
        return self._bars

    def _load_securities(self) -> list[SecurityMaster]:
        if self._securities is None:
            raw = json.loads((self.data_dir / "securities.json").read_text())
            self._securities = [SecurityMaster(**r) for r in raw]
        return self._securities

    def _load_statuses(self) -> list[StatusInterval]:
        if self._statuses is None:
            raw = json.loads((self.data_dir / "status_intervals.json").read_text())
            self._statuses = [StatusInterval(**r) for r in raw]
        return self._statuses

    def _load_corporate_actions(self) -> list[CorporateActionRecord]:
        if self._corporate_actions is None:
            path = self.data_dir / "corporate_actions.json"
            raw = json.loads(path.read_text()) if path.exists() else []
            self._corporate_actions = [CorporateActionRecord(**r) for r in raw]
        return self._corporate_actions

    # -- MarketDataProvider --------------------------------------------------
    def get_bars(
        self, symbols: list[str] | None, start: date, end: date, *, as_of: datetime
    ) -> list[Bar]:
        require_aware(as_of, "as_of")
        wanted = set(symbols) if symbols is not None else None
        out = [
            b
            for b in self._load_bars()
            if start <= b.trade_date <= end
            and (wanted is None or b.symbol in wanted)
            and b.available_at <= as_of
        ]
        return sorted(out, key=lambda b: (b.trade_date, b.symbol))

    def trading_calendar(self, start: date, end: date) -> list[date]:
        days = {b.trade_date for b in self._load_bars() if start <= b.trade_date <= end}
        return sorted(days)

    # -- ReferenceDataProvider ------------------------------------------------
    def get_securities(self, *, as_of: datetime) -> list[SecurityMaster]:
        require_aware(as_of, "as_of")
        versions: dict[str, SecurityMaster] = {}
        for security in self._load_securities():
            if security.available_at > as_of:
                continue
            current = versions.get(security.symbol)
            if current is None or security.available_at > current.available_at:
                versions[security.symbol] = security
        return sorted(versions.values(), key=lambda security: security.symbol)

    def get_status_intervals(self, *, as_of: datetime) -> list[StatusInterval]:
        require_aware(as_of, "as_of")
        return [s for s in self._load_statuses() if s.available_at <= as_of]

    def get_corporate_actions(
        self, start: date, end: date, *, as_of: datetime
    ) -> list[CorporateActionRecord]:
        require_aware(as_of, "as_of")
        return sorted(
            (
                action for action in self._load_corporate_actions()
                if start <= action.ex_date <= end and action.available_at <= as_of
            ),
            key=lambda action: (action.ex_date, action.symbol, action.kind),
        )

    # -- helpers -----------------------------------------------------------
    def adjusted_closes(self, bars: list[Bar], *, as_of: datetime) -> dict[tuple[str, date], Decimal]:
        require_aware(as_of, "as_of")
        if self._adjustments is None:
            path = self.data_dir / "adjustments.parquet"
            if not path.exists():
                raise QuantSystemError("adjusted features require adjustments.parquet")
            self._adjustments = pq.read_table(path).to_pylist()
        factors = {
            (row["symbol"], row["trade_date"]): to_decimal(row["factor"])
            for row in self._adjustments if row["available_at"] <= as_of
        }
        result = {}
        for bar in bars:
            key = (bar.symbol, bar.trade_date)
            if key not in factors or factors[key] <= 0:
                raise QuantSystemError(f"missing PIT adjustment factor: {key}")
            result[key] = bar.close * factors[key]
        return result

    def benchmark_series(self, symbol: str, days: list[date]) -> list[tuple[date, Decimal]]:
        path = self.data_dir / "benchmarks.parquet"
        if not path.exists():
            raise QuantSystemError("benchmark requested but benchmarks.parquet is missing")
        rows = [row for row in pq.read_table(path).to_pylist() if row["symbol"] == symbol]
        by_day = {row["trade_date"]: row for row in rows}
        if len(by_day) != len(rows) or any(day not in by_day for day in days):
            raise QuantSystemError(f"benchmark dates missing or duplicated: {symbol}")
        if not days:
            return []
        first = by_day[days[0]]
        values = [(days[0], to_decimal(first["prev_close"]))]
        for day in days:
            row = by_day[day]
            if row["available_at"] > bar_available_at(day):
                raise QuantSystemError(f"benchmark observation not available at close: {day}")
            value = to_decimal(row["close"])
            if value <= 0:
                raise QuantSystemError("benchmark prices must be positive")
            values.append((day, value))
        if values[0][1] <= 0:
            raise QuantSystemError("benchmark anchor must be positive")
        return values

    def assert_pit(self, bars: list[Bar], as_of: datetime) -> None:
        for b in bars:
            if b.available_at > as_of:
                raise PITViolationError(
                    f"bar {b.symbol}@{b.trade_date} available_at={b.available_at} > as_of={as_of}"
                )
