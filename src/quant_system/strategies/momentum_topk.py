"""Example strategy: cross-sectional momentum top-K.

NOT presented as a profitable strategy. It exists solely to exercise the
framework (data -> signal -> target -> orders -> fills -> ledger -> metrics).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from quant_system.backtest.strategy import StrategyContext, StrategyDecision
from quant_system.domain.types import to_decimal


class MomentumTopKStrategy:
    strategy_id = "momentum_topk"

    def __init__(self, lookback_days: int = 20, top_k: int = 3, exclude_st: bool = True) -> None:
        self.lookback_days = lookback_days
        self.top_k = top_k
        self.exclude_st = exclude_st

    def on_close(self, ctx: StrategyContext) -> StrategyDecision:
        eligible = [
            m for m in ctx.universe
            if not m.is_suspended and not (self.exclude_st and m.is_st)
        ]
        start = ctx.trade_date - timedelta(days=self.lookback_days * 3 + 10)
        bars = ctx.feed.strategy_bars(
            [m.symbol for m in eligible], start, ctx.trade_date, as_of=ctx.as_of
        )
        closes: dict[str, list[Decimal]] = {}
        for bar in bars:  # bars are sorted by (date, symbol)
            closes.setdefault(bar.symbol, []).append(bar.close)

        scores: dict[str, Decimal] = {}
        for symbol, series in closes.items():
            if len(series) <= self.lookback_days:
                continue
            past, now = series[-1 - self.lookback_days], series[-1]
            if past > 0:
                scores[symbol] = now / past - Decimal(1)

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[: self.top_k]
        if not ranked:
            return StrategyDecision(scores=scores, weights={})
        weight = to_decimal("0.95") / len(ranked)  # keep a small cash buffer
        weights = {symbol: weight for symbol, _ in ranked}
        return StrategyDecision(scores=scores, weights=weights)


def resolve_strategy(strategy_id: str, config: dict) -> MomentumTopKStrategy:
    params = config.get("params", {})
    if strategy_id == "momentum_topk":
        return MomentumTopKStrategy(
            lookback_days=int(params.get("lookback_days", 20)),
            top_k=int(params.get("top_k", 3)),
            exclude_st=bool(params.get("exclude_st", True)),
        )
    raise KeyError(f"unknown strategy_id: {strategy_id}")
