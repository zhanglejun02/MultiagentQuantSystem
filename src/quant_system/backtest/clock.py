"""HistoricalClock: deterministic iteration over trading days."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime

from quant_system.domain.types import cn_dt


class HistoricalClock:
    def __init__(self, trading_days: list[date]) -> None:
        self._days = sorted(trading_days)

    def __iter__(self) -> Iterator[date]:
        return iter(self._days)

    @property
    def days(self) -> list[date]:
        return list(self._days)

    def next_trading_day(self, d: date) -> date | None:
        for day in self._days:
            if day > d:
                return day
        return None

    @staticmethod
    def session_open(d: date) -> datetime:
        return cn_dt(d, 9, 30)

    @staticmethod
    def session_close(d: date) -> datetime:
        return cn_dt(d, 15, 0)

    @staticmethod
    def decision_time(d: date) -> datetime:
        """After post-close data finalization (bars available at 17:00)."""
        return cn_dt(d, 17, 30)
