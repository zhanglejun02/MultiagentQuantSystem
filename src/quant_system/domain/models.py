from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from quant_system.domain.enums import Board, Exchange, OrderSide, OrderType, SecurityType
from quant_system.domain.types import require_aware


class Bar(BaseModel):
    """Daily OHLCV bar with explicit PIT availability. Prices are unadjusted."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    prev_close: Decimal
    volume: int
    amount: Decimal
    available_at: datetime

    @field_validator("available_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return require_aware(v, "available_at")


class SecurityMaster(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    exchange: Exchange
    board: Board
    security_type: SecurityType = SecurityType.STOCK
    list_date: date
    delist_date: date | None = None
    available_at: datetime

    @field_validator("available_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return require_aware(v, "available_at")


class StatusInterval(BaseModel):
    """Effective-dated security status (ST / suspension), point-in-time correct."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    flag: str  # SecurityFlag value
    effective_from: date
    effective_to: date | None = None  # inclusive; None = open-ended
    available_at: datetime

    def covers(self, d: date) -> bool:
        return self.effective_from <= d and (self.effective_to is None or d <= self.effective_to)


class CorporateActionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    kind: str
    ex_date: date
    cash_per_share: Decimal = Decimal("0")
    split_ratio: Decimal = Decimal("1")
    available_at: datetime

    @field_validator("available_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return require_aware(v, "available_at")


class PortfolioTarget(BaseModel):
    """Target weights decided at decision_time; weights sum to <= 1 (rest cash)."""

    model_config = ConfigDict(frozen=True)

    target_id: str
    strategy_id: str
    decision_time: datetime
    trade_date: date  # the decision day (T); execution happens at T+1
    weights: dict[str, Decimal]
    scores: dict[str, Decimal] = {}

    @field_validator("decision_time")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return require_aware(v, "decision_time")


class OrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_order_id: str
    strategy_id: str
    target_id: str
    account_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: Decimal | None = None
    reference_price: Decimal  # last known close at decision time
    signal_id: str | None = None


class FillRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    commission: Decimal
    stamp_tax: Decimal
    transfer_fee: Decimal
    trade_date: date
    filled_at: datetime

    @property
    def total_fees(self) -> Decimal:
        return self.commission + self.stamp_tax + self.transfer_fee
