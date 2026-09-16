"""Typed backtest events. The event log is the authoritative record: a
backtest must be replayable from it."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from quant_system.domain.enums import OrderSide


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "1.0"
    seq: int = 0
    previous_event_hash: str = ""
    event_hash: str = ""
    ts: datetime
    trade_date: date


class MarketEvent(_Event):
    event_type: Literal["MARKET"] = "MARKET"
    kind: str  # SESSION_OPEN | SESSION_CLOSE
    n_bars: int = 0


class SignalEvent(_Event):
    event_type: Literal["SIGNAL"] = "SIGNAL"
    strategy_id: str
    signal_id: str
    scores: dict[str, Decimal]
    decision_time: datetime
    execution_time_assumption: str
    target_spec: str
    prediction_horizon: int


class TargetEvent(_Event):
    event_type: Literal["TARGET"] = "TARGET"
    target_id: str
    strategy_id: str
    weights: dict[str, Decimal]


class OrderIntentEvent(_Event):
    event_type: Literal["ORDER_INTENT"] = "ORDER_INTENT"
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    reference_price: Decimal
    target_id: str


class RiskApprovedEvent(_Event):
    event_type: Literal["RISK_APPROVED"] = "RISK_APPROVED"
    client_order_id: str


class RiskRejectedEvent(_Event):
    event_type: Literal["RISK_REJECTED"] = "RISK_REJECTED"
    client_order_id: str
    violations: list[str]


class OrderSubmittedEvent(_Event):
    event_type: Literal["ORDER_SUBMITTED"] = "ORDER_SUBMITTED"
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: int


class OrderAcceptedEvent(_Event):
    event_type: Literal["ORDER_ACCEPTED"] = "ORDER_ACCEPTED"
    order_id: str
    client_order_id: str


class PartialFillEvent(_Event):
    event_type: Literal["PARTIAL_FILL"] = "PARTIAL_FILL"
    order_id: str
    fill_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    commission: Decimal
    stamp_tax: Decimal
    transfer_fee: Decimal


class FillEvent(_Event):
    event_type: Literal["FILL"] = "FILL"
    order_id: str
    fill_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    commission: Decimal
    stamp_tax: Decimal
    transfer_fee: Decimal


class CancelEvent(_Event):
    event_type: Literal["CANCEL"] = "CANCEL"
    order_id: str
    client_order_id: str
    reason: str


class CorporateActionEvent(_Event):
    event_type: Literal["CORPORATE_ACTION"] = "CORPORATE_ACTION"
    symbol: str
    kind: str
    cash_per_share: Decimal = Decimal("0")
    split_ratio: Decimal = Decimal("1")


class AccountEvent(_Event):
    event_type: Literal["ACCOUNT"] = "ACCOUNT"
    cash: Decimal
    frozen_cash: Decimal
    market_value: Decimal
    nav: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    taxes: Decimal
    positions: dict[str, dict]


AnyEvent = (
    MarketEvent
    | SignalEvent
    | TargetEvent
    | OrderIntentEvent
    | RiskApprovedEvent
    | RiskRejectedEvent
    | OrderSubmittedEvent
    | OrderAcceptedEvent
    | PartialFillEvent
    | FillEvent
    | CancelEvent
    | CorporateActionEvent
    | AccountEvent
)

EVENT_CLASSES: dict[str, type[AnyEvent]] = {
    "MARKET": MarketEvent,
    "SIGNAL": SignalEvent,
    "TARGET": TargetEvent,
    "ORDER_INTENT": OrderIntentEvent,
    "RISK_APPROVED": RiskApprovedEvent,
    "RISK_REJECTED": RiskRejectedEvent,
    "ORDER_SUBMITTED": OrderSubmittedEvent,
    "ORDER_ACCEPTED": OrderAcceptedEvent,
    "PARTIAL_FILL": PartialFillEvent,
    "FILL": FillEvent,
    "CANCEL": CancelEvent,
    "CORPORATE_ACTION": CorporateActionEvent,
    "ACCOUNT": AccountEvent,
}
