"""Strategy runtime port. Strategies see ONLY a PIT-limited context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from quant_system.backtest.feed import MarketDataFeed
from quant_system.data.universe.service import UniverseMember


@dataclass(frozen=True)
class StrategyContext:
    trade_date: date
    as_of: datetime  # decision timestamp; all data access is limited to <= as_of
    universe: list[UniverseMember]
    feed: MarketDataFeed
    nav: Decimal
    positions: dict[str, int]
    params: dict[str, Any]


@dataclass(frozen=True)
class StrategyDecision:
    scores: dict[str, Decimal]
    weights: dict[str, Decimal]  # target weights; sum <= 1, remainder is cash


class Strategy(Protocol):
    strategy_id: str

    def on_close(self, ctx: StrategyContext) -> StrategyDecision: ...
