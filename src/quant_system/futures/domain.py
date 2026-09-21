"""Futures-specific domain values.

Direction names the position side being changed. Combining it with Offset is
therefore unambiguous: LONG/OPEN adds a long position and LONG/CLOSE reduces it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from quant_system.domain.types import require_aware


class FuturesExchange(StrEnum):
    SHFE = "SHFE"
    DCE = "DCE"
    CZCE = "CZCE"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Offset(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSE_TODAY = "CLOSE_TODAY"
    CLOSE_YESTERDAY = "CLOSE_YESTERDAY"


class FuturesOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class FuturesOrderStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class SimulationStatus(StrEnum):
    RUNNING = "RUNNING"
    MARGIN_CALL = "MARGIN_CALL"
    SAFE_MODE = "SAFE_MODE"
    COMPLETED = "COMPLETED"


class FuturesContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    exchange: FuturesExchange
    product: str
    multiplier: Decimal
    price_tick: Decimal
    long_margin_rate: Decimal
    short_margin_rate: Decimal
    open_fee_per_lot: Decimal = Decimal("0")
    close_fee_per_lot: Decimal = Decimal("0")
    close_today_fee_per_lot: Decimal = Decimal("0")
    last_trading_day: date | None = None

    @field_validator(
        "multiplier",
        "price_tick",
        "long_margin_rate",
        "short_margin_rate",
    )
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("contract multiplier, tick and margin rates must be positive")
        return value

    @field_validator(
        "open_fee_per_lot",
        "close_fee_per_lot",
        "close_today_fee_per_lot",
    )
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("fees must be non-negative")
        return value

    def margin_rate(self, direction: Direction) -> Decimal:
        return self.long_margin_rate if direction == Direction.LONG else self.short_margin_rate

    def fee(self, offset: Offset, quantity: int) -> Decimal:
        if offset == Offset.OPEN:
            rate = self.open_fee_per_lot
        elif offset == Offset.CLOSE_TODAY:
            rate = self.close_today_fee_per_lot
        else:
            rate = self.close_fee_per_lot
        return rate * quantity


class FuturesDailyBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    trading_day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    settlement: Decimal
    pre_settlement: Decimal
    volume: int
    open_interest: int
    available_at: datetime

    @model_validator(mode="after")
    def _valid_bar(self) -> FuturesDailyBar:
        require_aware(self.available_at, "available_at")
        if min(self.open, self.high, self.low, self.close, self.settlement) <= 0:
            raise ValueError("bar prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if self.volume < 0 or self.open_interest < 0:
            raise ValueError("volume and open interest must be non-negative")
        return self


class FuturesOrderIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_order_id: str
    symbol: str
    direction: Direction
    offset: Offset
    quantity: int
    order_type: FuturesOrderType = FuturesOrderType.MARKET
    limit_price: Decimal | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _valid_intent(self) -> FuturesOrderIntent:
        require_aware(self.created_at, "created_at")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type == FuturesOrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        return self


class FuturesOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    client_order_id: str
    symbol: str
    direction: Direction
    offset: Offset
    quantity: int
    order_type: FuturesOrderType
    limit_price: Decimal | None
    status: FuturesOrderStatus
    filled_quantity: int = 0
    average_fill_price: Decimal | None = None
    reject_reason: str = ""
    created_at: datetime
    updated_at: datetime

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            FuturesOrderStatus.FILLED,
            FuturesOrderStatus.CANCELLED,
            FuturesOrderStatus.REJECTED,
        }


class FuturesTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    trade_id: str
    order_id: str
    symbol: str
    direction: Direction
    offset: Offset
    quantity: int
    price: Decimal
    fee: Decimal
    trading_day: date
    traded_at: datetime