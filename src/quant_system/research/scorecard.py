"""Scorecard extraction from backtest metrics documents."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class Scorecard(BaseModel):
    gross_return: float | None = None
    net_return: float | None = None
    excess_return: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    calmar: float | None = None
    annualized_turnover: float | None = None
    transaction_cost: float | None = None
    fill_ratio: float | None = None
    capacity_proxy: float | None = None
    ic_mean: float | None = None
    rank_ic_mean: float | None = None
    icir: float | None = None
    prediction_target: str | None = None
    prediction_horizon: int | None = None
    yearly_returns: dict[str, float] = {}
    monthly_returns: dict[str, float] = {}


def build_scorecard(performance: dict[str, Any], execution: dict[str, Any]) -> Scorecard:
    ic_mean_key = next(
        (key for key in performance if key.startswith("rank_ic_") and key.endswith("_mean")),
        None,
    )
    icir_key = next(
        (key for key in performance if key.startswith("rank_ic_") and key.endswith("_icir")),
        None,
    )
    return Scorecard(
        gross_return=performance.get("total_return"),
        net_return=performance.get("total_return"),  # metrics are already net of costs
        excess_return=performance.get("excess_return"),
        sharpe=performance.get("sharpe"),
        sortino=performance.get("sortino"),
        max_drawdown=performance.get("max_drawdown"),
        calmar=performance.get("calmar"),
        annualized_turnover=execution.get("annualized_turnover"),
        transaction_cost=execution.get("transaction_cost"),
        fill_ratio=execution.get("fill_ratio"),
        capacity_proxy=execution.get("total_traded_value"),
        rank_ic_mean=performance.get(ic_mean_key) if ic_mean_key else None,
        icir=performance.get(icir_key) if icir_key else None,
        prediction_target=performance.get("prediction_target"),
        prediction_horizon=performance.get("prediction_horizon"),
        yearly_returns=performance.get("yearly_returns", {}),
        monthly_returns=performance.get("monthly_returns", {}),
    )
