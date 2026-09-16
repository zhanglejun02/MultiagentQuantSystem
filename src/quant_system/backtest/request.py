"""BacktestRequest / BacktestResult models and the BacktestEngine port."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator


class PredictionTarget(StrEnum):
    NEXT_CLOSE_TO_CLOSE = "NEXT_CLOSE_TO_CLOSE"
    NEXT_OPEN_TO_CLOSE = "NEXT_OPEN_TO_CLOSE"
    NEXT_OPEN_TO_NEXT_OPEN = "NEXT_OPEN_TO_NEXT_OPEN"
    N_DAY_FORWARD_RETURN = "N_DAY_FORWARD_RETURN"


class PredictionTargetSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    target: PredictionTarget = PredictionTarget.NEXT_OPEN_TO_CLOSE
    prediction_horizon: int = 1
    execution_time_assumption: str = "NEXT_SESSION_OPEN"

    @field_validator("prediction_horizon")
    @classmethod
    def _positive_horizon(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("prediction_horizon must be positive")
        return value


class BacktestRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    dataset_snapshot_id: str
    universe_id: str = "cn_a_all"
    start_date: date
    end_date: date
    initial_cash: Decimal
    benchmark: str | None = None
    decision_frequency: str = "daily"
    execution_model: str = "next_open"
    slippage_model: dict[str, Any] = {"name": "fixed_bps", "bps": "5"}
    fee_model: str = "cn_a"
    rulebook_version: str = "cn_a-2026.2"
    participation_limit: Decimal = Decimal("0.05")
    liquidity_window: int = 20
    cash_reservation_multiplier: Decimal = Decimal("1.002")
    transaction_cost_multiplier: Decimal = Decimal("1")
    cost_stress_multipliers: tuple[Decimal, ...] = (
        Decimal("1"), Decimal("1.5"), Decimal("2"), Decimal("3")
    )
    prediction_target: PredictionTargetSpec = PredictionTargetSpec()
    random_seed: int = 42
    experiment_id: str | None = None
    config: dict[str, Any] = {}


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    request: BacktestRequest
    result_artifact_id: str
    spec_artifact_id: str
    run_dir: str
    performance_metrics: dict[str, Any]
    execution_metrics: dict[str, Any]
    risk_metrics: dict[str, Any]
    violations: list[str]
    final_event_hash: str = ""


class BacktestEngine(Protocol):
    def run(self, request: BacktestRequest) -> BacktestResult: ...
