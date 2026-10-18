"""Translate a pinned research DSL into weekly long-only portfolio targets."""

from datetime import date
from decimal import Decimal

from quant_system.backtest.strategy import StrategyContext, StrategyDecision
from quant_system.factors.dsl import FactorExpression
from quant_system.factors.fields import bars_to_fields
from quant_system.strategies.weekly_momentum import resolve_baseline_strategy


class WeeklyFactorStrategy:
    strategy_id = "weekly_factor"

    def __init__(self, expression: str, top_k: int = 3):
        if top_k < 3:
            raise ValueError("top_k must be at least 3")
        self.formula = FactorExpression(expression)
        self.top_k = top_k
        self._last_week: tuple[int, int] | None = None

    def on_close(self, ctx: StrategyContext) -> StrategyDecision:
        year, week, _ = ctx.trade_date.isocalendar()
        if self._last_week == (year, week):
            return StrategyDecision(scores={}, weights={}, rebalance=False)
        self._last_week = (year, week)
        symbols = [member.symbol for member in ctx.universe if not member.is_st and not member.is_suspended]
        bars = ctx.feed.strategy_bars(symbols, date.min, ctx.trade_date, as_of=ctx.as_of)
        days = sorted({bar.trade_date for bar in bars})
        if not days:
            return StrategyDecision(scores={}, weights={})
        fields = bars_to_fields(bars, ctx.feed.adjusted_closes(bars, as_of=ctx.as_of))
        raw_scores = self.formula.scores(symbols=symbols, index=len(days) - 1, days=days, fields=fields)
        scores = {symbol: Decimal(str(value)) for symbol, value in raw_scores.items()}
        selected = sorted(scores, key=lambda symbol: (-scores[symbol], symbol))[: self.top_k]
        weights = {symbol: Decimal("0.90") / len(selected) for symbol in selected} if selected else {}
        return StrategyDecision(scores=scores, weights=weights)


def resolve_research_strategy(strategy_id: str, config: dict):
    if strategy_id == "weekly_factor":
        params = config["params"]
        return WeeklyFactorStrategy(params["expression"], int(params.get("top_k", 3)))
    return resolve_baseline_strategy(strategy_id, config)
