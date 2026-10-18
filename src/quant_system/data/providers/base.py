"""Provider ports. The system never binds directly to AKShare/Tushare/Qlib;
those become adapters implementing these protocols."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from quant_system.domain.models import Bar, CorporateActionRecord, SecurityMaster, StatusInterval


class MarketDataProvider(Protocol):
    def get_bars(
        self, symbols: list[str] | None, start: date, end: date, *, as_of: datetime
    ) -> list[Bar]:
        """Daily bars with available_at <= as_of. NEVER returns future data."""
        ...

    def trading_calendar(self, start: date, end: date) -> list[date]: ...

    def adjusted_closes(self, bars: list[Bar], *, as_of: datetime) -> dict[tuple[str, date], Decimal]: ...


class ReferenceDataProvider(Protocol):
    def get_securities(self, *, as_of: datetime) -> list[SecurityMaster]: ...

    def get_status_intervals(self, *, as_of: datetime) -> list[StatusInterval]: ...

    def get_corporate_actions(
        self, start: date, end: date, *, as_of: datetime
    ) -> list[CorporateActionRecord]: ...


class FundamentalDataProvider(Protocol):
    def get_fundamentals(
        self, symbols: list[str], fields: list[str], *, as_of: datetime
    ) -> list[dict]:
        """Records must carry available_at (announcement time), filtered by as_of."""
        ...
