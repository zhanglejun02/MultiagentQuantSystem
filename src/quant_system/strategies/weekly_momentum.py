"""Fixed weekly momentum baseline over a declared research basket."""

from datetime import timedelta
from decimal import Decimal

from quant_system.backtest.strategy import StrategyContext, StrategyDecision


class WeeklyMomentumStrategy:
    strategy_id = "weekly_momentum"

    def __init__(self, lookback_days: int = 20, top_k: int = 3, equal_weight: bool = False):
        if lookback_days <= 0 or top_k < 3:
            raise ValueError("lookback must be positive and top_k must be at least 3")
        self.lookback_days = lookback_days
        self.top_k = top_k
        self.equal_weight = equal_weight
        self._last_week: tuple[int, int] | None = None

    def on_close(self, ctx: StrategyContext) -> StrategyDecision:
        year, week, _ = ctx.trade_date.isocalendar()
        if self._last_week == (year, week):
            return StrategyDecision(scores={}, weights={}, rebalance=False)
        self._last_week = (year, week)
        symbols = [member.symbol for member in ctx.universe if not member.is_st and not member.is_suspended]
        bars = ctx.feed.strategy_bars(
            symbols,
            ctx.trade_date - timedelta(days=self.lookback_days * 3 + 10),
            ctx.trade_date,
            as_of=ctx.as_of,
        )
        adjusted = ctx.feed.adjusted_closes(bars, as_of=ctx.as_of)
        histories: dict[str, list[Decimal]] = {}
        for bar in bars:
            histories.setdefault(bar.symbol, []).append(adjusted[(bar.symbol, bar.trade_date)])
        scores = {
            symbol: values[-1] / values[-1 - self.lookback_days] - 1
            for symbol, values in histories.items()
            if len(values) > self.lookback_days
        }
        ranked = sorted(scores, key=lambda symbol: (-scores[symbol], symbol))
        selected = sorted(scores) if self.equal_weight else ranked[: self.top_k]
        weights = {symbol: Decimal("0.90") / len(selected) for symbol in selected} if selected else {}
        return StrategyDecision(scores=scores, weights=weights)


def resolve_baseline_strategy(strategy_id: str, config: dict) -> WeeklyMomentumStrategy:
    if strategy_id not in {"weekly_momentum", "weekly_equal_weight"}:
        raise KeyError(strategy_id)
    params = config.get("params", {})
    return WeeklyMomentumStrategy(
        lookback_days=int(params.get("lookback_days", 20)),
        top_k=int(params.get("top_k", 3)),
        equal_weight=strategy_id == "weekly_equal_weight",
    )
