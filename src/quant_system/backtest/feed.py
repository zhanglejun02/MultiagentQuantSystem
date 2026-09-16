"""MarketDataFeed: separates the strategy's PIT-limited view from the
exchange simulator's ground-truth bars."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from quant_system.data.providers.base import MarketDataProvider
from quant_system.domain.models import Bar
from quant_system.domain.types import bar_available_at


class MarketDataFeed:
    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider

    def market_bars(self, trade_date: date) -> dict[str, Bar]:
        """Ground truth for the exchange simulator on trade_date."""
        bars = self._provider.get_bars(
            None, trade_date, trade_date, as_of=bar_available_at(trade_date)
        )
        return {b.symbol: b for b in bars}

    def strategy_bars(
        self, symbols: list[str] | None, start: date, end: date, *, as_of: datetime
    ) -> list[Bar]:
        """PIT-limited view for strategies; never returns bars not yet available."""
        return self._provider.get_bars(symbols, start, end, as_of=as_of)

    def historical_average_volume(
        self, symbol: str, *, end: date, as_of: datetime, window: int
    ) -> int:
        bars = self._provider.get_bars([symbol], date.min, end, as_of=as_of)
        volumes = [bar.volume for bar in bars[-window:]]
        if not volumes:
            return 0
        return int(sum(Decimal(volume) for volume in volumes) / len(volumes))
