"""Request body schemas for the dashboard API."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from quant_system.domain.enums import ExperimentState, FactorState, StrategyState
from quant_system.futures.domain import Direction, FuturesOrderType, Offset


class ExperimentCreate(BaseModel):
    name: str
    description: str = ""


class TransitionRequest(BaseModel):
    target: ExperimentState
    reason: str = ""
    actor: str = "dashboard"


class FactorCreate(BaseModel):
    factor_key: str
    version: str
    name: str
    category: str
    description: str = ""
    owner: str
    code_ref: str
    data_snapshot_id: str | None = None
    validation_artifact_id: str | None = None
    parameters: dict[str, Any] = {}
    metrics: dict[str, Any] = {}


class FactorTransitionRequest(BaseModel):
    target: FactorState
    reason: str = ""
    actor: str = "dashboard"


class FactorPromotionRequest(BaseModel):
    target: FactorState
    validation_artifact_id: str


class StrategyCreate(BaseModel):
    strategy_key: str
    version: str
    package_artifact_id: str
    factor_ids: list[str]


class StrategyPromotionRequest(BaseModel):
    target: StrategyState
    backtest_result_artifact_id: str = ""
    validation_report_artifact_id: str = ""
    approved_by: str = ""


class FactorEvaluateRequest(BaseModel):
    expression: str
    universe_id: str = "cn_a_all"
    start_date: date
    end_date: date
    target: str = "NEXT_OPEN_TO_CLOSE"
    horizon: int = 1


class FuturesSimulationCreate(BaseModel):
    name: str = "期货仿真"
    initial_balance: Decimal = Decimal("100000")
    mode: str = "PAPER"


class FuturesOrderCreate(BaseModel):
    client_order_id: str | None = None
    symbol: str
    direction: Direction
    offset: Offset
    quantity: int
    order_type: FuturesOrderType = FuturesOrderType.MARKET
    limit_price: Decimal | None = None
