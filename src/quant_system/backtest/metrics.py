"""Backtest performance / execution / risk metrics.

Metrics use float math (reporting only); accounting stays Decimal upstream.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

from quant_system.backtest.request import PredictionTarget, PredictionTargetSpec

TRADING_DAYS_PER_YEAR = 252


def _returns(navs: list[float]) -> list[float]:
    return [navs[i] / navs[i - 1] - 1.0 for i in range(1, len(navs)) if navs[i - 1] > 0]


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    return math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))


def max_drawdown(navs: list[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def compute_performance_metrics(
    nav_series: list[tuple[date, Decimal]],
    initial_cash: Decimal,
    benchmark_series: list[tuple[date, Decimal]] | None = None,
) -> dict:
    navs = [float(v) for _, v in nav_series]
    if not navs:
        return {"error": "empty nav series"}
    rets = _returns(navs)
    n_days = len(navs)
    years = max(n_days / TRADING_DAYS_PER_YEAR, 1e-9)
    total_return = navs[-1] / float(initial_cash) - 1.0
    ann_return = (1.0 + total_return) ** (1.0 / years) - 1.0 if total_return > -1 else -1.0
    vol = _std(rets) * math.sqrt(TRADING_DAYS_PER_YEAR)
    mean_daily = sum(rets) / len(rets) if rets else 0.0
    sharpe = (mean_daily / _std(rets) * math.sqrt(TRADING_DAYS_PER_YEAR)) if _std(rets) > 0 else 0.0
    downside = [r for r in rets if r < 0]
    dstd = _std(downside) if len(downside) >= 2 else 0.0
    sortino = (mean_daily / dstd * math.sqrt(TRADING_DAYS_PER_YEAR)) if dstd > 0 else 0.0
    mdd = max_drawdown(navs)
    calmar = ann_return / abs(mdd) if mdd < 0 else 0.0

    monthly: dict[str, float] = {}
    yearly: dict[str, float] = {}
    prev_nav = float(initial_cash)
    prev_month = None
    month_start_nav = prev_nav
    year_start_nav: dict[str, float] = {}
    for d, v in nav_series:
        fv = float(v)
        ym = f"{d.year}-{d.month:02d}"
        y = str(d.year)
        if prev_month is not None and ym != prev_month:
            monthly[prev_month] = prev_nav / month_start_nav - 1.0
            month_start_nav = prev_nav
        year_start_nav.setdefault(y, prev_nav)
        prev_month = ym
        prev_nav = fv
    if prev_month is not None:
        monthly[prev_month] = prev_nav / month_start_nav - 1.0
    for y, start_nav in year_start_nav.items():
        last_nav = next(
            float(v) for d, v in reversed(nav_series) if str(d.year) == y
        )
        yearly[y] = last_nav / start_nav - 1.0

    out = {
        "total_return": total_return,
        "annualized_return": ann_return,
        "annualized_volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": mdd,
        "calmar": calmar,
        "n_trading_days": n_days,
        "final_nav": navs[-1],
        "monthly_returns": monthly,
        "yearly_returns": yearly,
    }
    if benchmark_series:
        bench = [float(v) for _, v in benchmark_series]
        if bench and bench[0] > 0:
            bench_total = bench[-1] / bench[0] - 1.0
            out["benchmark_return"] = bench_total
            out["excess_return"] = total_return - bench_total
    return out


def compute_execution_metrics(
    total_ordered_qty: int,
    total_filled_qty: int,
    total_traded_value: Decimal,
    total_fees: Decimal,
    total_taxes: Decimal,
    nav_series: list[tuple[date, Decimal]],
    n_rejects: int,
    n_partials: int,
) -> dict:
    avg_nav = (
        sum(float(v) for _, v in nav_series) / len(nav_series) if nav_series else 0.0
    )
    years = max(len(nav_series) / TRADING_DAYS_PER_YEAR, 1e-9)
    turnover = float(total_traded_value) / avg_nav / years if avg_nav > 0 else 0.0
    return {
        "fill_ratio": (total_filled_qty / total_ordered_qty) if total_ordered_qty else 1.0,
        "total_traded_value": float(total_traded_value),
        "annualized_turnover": turnover,
        "transaction_cost": float(total_fees + total_taxes),
        "fees": float(total_fees),
        "taxes": float(total_taxes),
        "rejected_orders": n_rejects,
        "partial_fills": n_partials,
    }


def spearman_ic(scores: dict[str, float], forward_returns: dict[str, float]) -> float | None:
    common = sorted(set(scores) & set(forward_returns))
    if len(common) < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        r = [0.0] * len(values)
        for rank, idx in enumerate(order):
            r[idx] = float(rank)
        return r

    xs = ranks([scores[s] for s in common])
    ys = ranks([forward_returns[s] for s in common])
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys, strict=True))
    vx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    vy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def ic_series_stats(ics: list[float]) -> dict:
    if not ics:
        return {"ic_mean": None, "icir": None, "n_ic_days": 0}
    mean = sum(ics) / len(ics)
    std = _std(ics)
    return {
        "ic_mean": mean,
        "icir": (mean / std) if std > 0 else None,
        "n_ic_days": len(ics),
    }


def compute_prediction_ic(
    scores_by_day: dict[date, dict[str, float]],
    opens_by_day: dict[date, dict[str, float]],
    closes_by_day: dict[date, dict[str, float]],
    spec: PredictionTargetSpec,
) -> dict:
    days = sorted(closes_by_day)
    ics: list[float] = []
    horizon = spec.prediction_horizon
    for index, day in enumerate(days):
        scores = scores_by_day.get(day)
        if not scores:
            continue
        if spec.target == PredictionTarget.NEXT_CLOSE_TO_CLOSE:
            start_index, end_index = index, index + horizon
            start_prices, end_prices = closes_by_day, closes_by_day
        elif spec.target == PredictionTarget.NEXT_OPEN_TO_CLOSE:
            start_index = index + 1
            end_index = index + horizon
            start_prices, end_prices = opens_by_day, closes_by_day
        elif spec.target == PredictionTarget.NEXT_OPEN_TO_NEXT_OPEN:
            start_index = index + 1
            end_index = index + horizon + 1
            start_prices, end_prices = opens_by_day, opens_by_day
        else:
            start_index, end_index = index, index + horizon
            start_prices, end_prices = closes_by_day, closes_by_day
        if end_index >= len(days) or start_index >= len(days):
            continue
        start_day, end_day = days[start_index], days[end_index]
        forward = {
            symbol: end_prices[end_day][symbol] / start_prices[start_day][symbol] - 1.0
            for symbol in scores
            if symbol in start_prices[start_day]
            and symbol in end_prices[end_day]
            and start_prices[start_day][symbol] > 0
        }
        ic = spearman_ic(scores, forward)
        if ic is not None:
            ics.append(ic)
    stats = ic_series_stats(ics)
    prefix = f"rank_ic_{spec.target.value.lower()}_h{horizon}"
    return {
        f"{prefix}_mean": stats["ic_mean"],
        f"{prefix}_icir": stats["icir"],
        f"{prefix}_observations": stats["n_ic_days"],
        "prediction_target": spec.target.value,
        "prediction_horizon": horizon,
        "execution_time_assumption": spec.execution_time_assumption,
    }
