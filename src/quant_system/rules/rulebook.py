"""MarketRuleBook port and TradingRules value object.

A-share rules are exchange/board/security/status/effective-date dependent and
live in versioned config — never scattered through engine code. This module
does not claim regulatory completeness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from quant_system.domain.enums import Board, Exchange, OrderType, SecurityType
from quant_system.domain.types import quantize_price


@dataclass(frozen=True)
class FeeSchedule:
    effective_from: date
    commission_rate: Decimal
    min_commission: Decimal
    stamp_tax_sell_rate: Decimal
    transfer_fee_rate: Decimal


@dataclass(frozen=True)
class TradingRules:
    rulebook_version: str
    rule_version: str
    source_metadata: dict[str, str]
    exchange: Exchange
    board: Board
    symbol: str
    tick_size: Decimal
    lot_size: int
    price_limit_ratio: Decimal | None  # None = no price limit today (e.g. IPO window)
    t_plus_sell: int
    is_suspended: bool
    is_st: bool
    sessions: tuple[tuple[str, str], ...]
    allowed_order_types: tuple[OrderType, ...]
    fee_schedule: FeeSchedule
    extras: dict = field(default_factory=dict)

    def limit_up(self, prev_close: Decimal) -> Decimal | None:
        if self.price_limit_ratio is None:
            return None
        return quantize_price(prev_close * (Decimal(1) + self.price_limit_ratio), self.tick_size)

    def limit_down(self, prev_close: Decimal) -> Decimal | None:
        if self.price_limit_ratio is None:
            return None
        return quantize_price(prev_close * (Decimal(1) - self.price_limit_ratio), self.tick_size)

    def round_lots(self, quantity: int) -> int:
        return (quantity // self.lot_size) * self.lot_size

    def is_valid_price(self, price: Decimal, prev_close: Decimal) -> bool:
        if (price % self.tick_size) != 0:
            return False
        up, down = self.limit_up(prev_close), self.limit_down(prev_close)
        if up is not None and price > up:
            return False
        if down is not None and price < down:
            return False
        return True


class MarketRuleBook(Protocol):
    rulebook_version: str

    def get_rules(
        self,
        exchange: Exchange,
        board: Board,
        security_type: SecurityType,
        symbol: str,
        timestamp: datetime,
        *,
        is_st: bool = False,
        is_suspended: bool = False,
        list_date: date | None = None,
        listed_trading_days: int | None = None,
    ) -> TradingRules: ...
