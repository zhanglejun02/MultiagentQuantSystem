"""Independent PIT-safe factor evaluation, separate from strategy backtests."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from quant_system.backtest.metrics import ic_series_stats, max_drawdown, spearman_ic
from quant_system.backtest.request import PredictionTarget
from quant_system.data.providers.local_parquet import LocalParquetProvider
from quant_system.domain.models import Bar
from quant_system.domain.types import bar_available_at, new_id, utc_now
from quant_system.factors.dsl import FactorExpression
from quant_system.factors.fields import bars_to_fields
from quant_system.infra.db import session_scope
from quant_system.infra.orm import FactorRunRow


class FactorRun(BaseModel):
    run_id: str
    expression: str
    universe_id: str
    start_date: str
    end_date: str
    target: str
    horizon: int
    status: str
    inputs: list[str]
    complexity: int
    metrics: dict[str, Any]
    ic_series: list[dict[str, Any]]
    quantile_returns: dict[str, float | None]
    decay: list[dict[str, Any]]
    yearly_ic: dict[str, float]
    created_at: str


def _forward_returns(
    *,
    index: int,
    horizon: int,
    target: PredictionTarget,
    days: list[date],
    scores: dict[str, float],
    opens: dict[date, dict[str, float]],
    closes: dict[date, dict[str, float]],
) -> dict[str, float]:
    if target == PredictionTarget.NEXT_CLOSE_TO_CLOSE:
        start_index, end_index = index, index + horizon
        start_prices, end_prices = closes, closes
    elif target == PredictionTarget.NEXT_OPEN_TO_NEXT_OPEN:
        start_index, end_index = index + 1, index + horizon + 1
        start_prices, end_prices = opens, opens
    else:
        start_index, end_index = index + 1, index + horizon
        start_prices, end_prices = opens, closes
    if start_index >= len(days) or end_index >= len(days):
        return {}
    start_day, end_day = days[start_index], days[end_index]
    return {
        symbol: end_prices[end_day][symbol] / start_prices[start_day][symbol] - 1.0
        for symbol in scores
        if symbol in start_prices.get(start_day, {})
        and symbol in end_prices.get(end_day, {})
        and start_prices[start_day][symbol] > 0
    }


def _quantile_means(scores: dict[str, float], returns: dict[str, float]) -> list[float | None]:
    ranked = sorted(
        ((score, returns[symbol]) for symbol, score in scores.items() if symbol in returns),
        key=lambda item: item[0],
    )
    buckets: list[list[float]] = [[] for _ in range(5)]
    for position, (_, value) in enumerate(ranked):
        bucket = min(position * 5 // max(len(ranked), 1), 4)
        buckets[bucket].append(value)
    return [statistics.fmean(bucket) if bucket else None for bucket in buckets]


def _mean_present(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.fmean(present) if present else None


class FactorLab:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        provider: LocalParquetProvider,
        *,
        adjusted_features: bool = False,
    ) -> None:
        self._sf = session_factory
        self._provider = provider
        self._adjusted_features = adjusted_features

    def _fields(self, bars: list[Bar], as_of: date):
        adjusted = (
            self._provider.adjusted_closes(bars, as_of=bar_available_at(as_of))
            if self._adjusted_features
            else None
        )
        return bars_to_fields(bars, adjusted)

    @staticmethod
    def _to_run(row: FactorRunRow) -> FactorRun:
        return FactorRun(
            run_id=row.run_id,
            expression=row.expression,
            universe_id=row.universe_id,
            start_date=row.start_date,
            end_date=row.end_date,
            target=row.target,
            horizon=row.horizon,
            status=row.status,
            inputs=row.inputs or [],
            complexity=row.complexity,
            metrics=row.metrics or {},
            ic_series=row.ic_series or [],
            quantile_returns=row.quantile_returns or {},
            decay=row.decay or [],
            yearly_ic=row.yearly_ic or {},
            created_at=row.created_at.isoformat(),
        )

    def list_runs(self) -> list[FactorRun]:
        with session_scope(self._sf) as session:
            rows = (
                session.execute(select(FactorRunRow).order_by(FactorRunRow.created_at.desc())).scalars().all()
            )
            return [self._to_run(row) for row in rows]

    def evaluate(
        self,
        *,
        expression: str,
        universe_id: str,
        start_date: date,
        end_date: date,
        target: str,
        horizon: int,
    ) -> FactorRun:
        if universe_id != "cn_a_all":
            raise ValueError("V0 currently supports only cn_a_all")
        if start_date >= end_date:
            raise ValueError("start_date must be before end_date")
        if not 1 <= horizon <= 20:
            raise ValueError("horizon must be between 1 and 20")
        target_value = PredictionTarget(target)
        formula = FactorExpression(expression)
        warmup_start = start_date - timedelta(days=400)
        all_days = self._provider.trading_calendar(warmup_start, end_date)
        outcome_bars = self._provider.get_bars(None, warmup_start, end_date, as_of=bar_available_at(end_date))
        outcome_fields = self._fields(outcome_bars, end_date)
        opens = outcome_fields["open"]
        closes = outcome_fields["close"]
        scores_by_day: dict[date, dict[str, float]] = {}
        eligible_counts: dict[date, int] = {}

        for day in (value for value in all_days if start_date <= value <= end_date):
            visible_bars = self._provider.get_bars(None, warmup_start, day, as_of=bar_available_at(day))
            visible_days = sorted({bar.trade_date for bar in visible_bars})
            if not visible_days:
                continue
            current_symbols = sorted({bar.symbol for bar in visible_bars if bar.trade_date == day})
            eligible_counts[day] = len(current_symbols)
            scores = formula.scores(
                symbols=current_symbols,
                index=len(visible_days) - 1,
                days=visible_days,
                fields=self._fields(visible_bars, day),
            )
            if scores:
                scores_by_day[day] = scores

        ic_points: list[dict[str, Any]] = []
        quantile_daily: list[list[float | None]] = []
        long_short_returns: list[float] = []
        top_sets: list[set[str]] = []
        observation_days = sorted(closes)
        day_index = {day: index for index, day in enumerate(observation_days)}
        scored_values = 0
        eligible_values = 0
        for day, scores in sorted(scores_by_day.items()):
            if day not in day_index:
                continue
            forward = _forward_returns(
                index=day_index[day],
                horizon=horizon,
                target=target_value,
                days=observation_days,
                scores=scores,
                opens=opens,
                closes=closes,
            )
            ic = spearman_ic(scores, forward)
            if ic is not None:
                ic_points.append({"date": day.isoformat(), "ic": ic})
            quantiles = _quantile_means(scores, forward)
            if quantiles[0] is not None and quantiles[-1] is not None:
                quantile_daily.append(quantiles)
                long_short_returns.append(quantiles[-1] - quantiles[0])
            ranked_symbols = sorted(scores, key=lambda symbol: scores[symbol])
            top_count = max(1, len(ranked_symbols) // 5)
            top_sets.append(set(ranked_symbols[-top_count:]))
            scored_values += len(scores)
            eligible_values += eligible_counts.get(day, 0)

        ic_values = [point["ic"] for point in ic_points]
        ic_stats = ic_series_stats(ic_values)
        turnover_values = [
            1.0 - len(previous & current) / max(len(previous), 1)
            for previous, current in zip(top_sets, top_sets[1:], strict=False)
        ]
        quantile_returns = {
            f"Q{index + 1}": _mean_present([row[index] for row in quantile_daily]) for index in range(5)
        }
        wealth = [1.0]
        for value in long_short_returns:
            wealth.append(wealth[-1] * (1.0 + value))
        ls_std = statistics.stdev(long_short_returns) if len(long_short_returns) > 1 else 0.0
        yearly_values: dict[str, list[float]] = defaultdict(list)
        for point in ic_points:
            yearly_values[point["date"][:4]].append(point["ic"])
        yearly_ic = {year: statistics.fmean(values) for year, values in yearly_values.items()}
        decay = []
        for decay_horizon in range(1, 6):
            values = []
            for day, scores in scores_by_day.items():
                if day not in day_index:
                    continue
                forward = _forward_returns(
                    index=day_index[day],
                    horizon=decay_horizon,
                    target=target_value,
                    days=observation_days,
                    scores=scores,
                    opens=opens,
                    closes=closes,
                )
                ic_value = spearman_ic(scores, forward)
                if ic_value is not None:
                    values.append(ic_value)
            decay.append({"horizon": decay_horizon, "rank_ic": ic_series_stats(values)["ic_mean"]})

        metrics = {
            "rank_ic": ic_stats["ic_mean"],
            "icir": ic_stats["icir"],
            "ic_win_rate": (sum(value > 0 for value in ic_values) / len(ic_values) if ic_values else None),
            "n_ic_days": ic_stats["n_ic_days"],
            "coverage": scored_values / eligible_values if eligible_values else None,
            "turnover": statistics.fmean(turnover_values) if turnover_values else None,
            "long_short_sharpe": (
                statistics.fmean(long_short_returns) / ls_std * math.sqrt(252) if ls_std > 0 else None
            ),
            "long_short_annual_return": (
                statistics.fmean(long_short_returns) * 252 if long_short_returns else None
            ),
            "long_short_max_drawdown": max_drawdown(wealth),
        }
        now = utc_now()
        row = FactorRunRow(
            run_id=new_id("frun_"),
            expression=formula.expression,
            universe_id=universe_id,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            target=target_value.value,
            horizon=horizon,
            status="COMPLETED",
            inputs=formula.inputs,
            complexity=formula.complexity,
            metrics=metrics,
            ic_series=ic_points,
            quantile_returns=quantile_returns,
            decay=decay,
            yearly_ic=yearly_ic,
            created_at=now,
        )
        with session_scope(self._sf) as session:
            session.add(row)
            session.flush()
            return self._to_run(row)
