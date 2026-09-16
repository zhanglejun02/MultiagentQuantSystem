"""Shared test helpers: deterministic scripted strategy + engine factory."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from quant_system.backtest.engine import EventDrivenBacktestEngine
from quant_system.backtest.strategy import StrategyContext, StrategyDecision


class ScheduledWeightsStrategy:
    """Returns pre-scripted target weights per decision date; holds the last
    schedule entry on later dates. Used to force specific engine scenarios."""

    strategy_id = "scheduled"

    def __init__(self, schedule: dict[date, dict[str, Decimal]]) -> None:
        self._schedule = dict(sorted(schedule.items()))

    def on_close(self, ctx: StrategyContext) -> StrategyDecision:
        weights: dict[str, Decimal] = {}
        for d, w in self._schedule.items():
            if d <= ctx.trade_date:
                weights = w
        return StrategyDecision(scores={}, weights=weights)


def make_engine_with(provider, rulebook, store, registry, strategy) -> EventDrivenBacktestEngine:
    return EventDrivenBacktestEngine(
        provider=provider,
        rulebook=rulebook,
        store=store,
        registry=registry,
        strategy_resolver=lambda _sid, _cfg: strategy,
    )
