"""Conservative deterministic matching for daily paper simulation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from quant_system.futures.domain import (
    Direction,
    FuturesContract,
    FuturesDailyBar,
    FuturesOrder,
    FuturesOrderType,
    Offset,
)


@dataclass(frozen=True)
class MatchResult:
    quantity: int
    price: Decimal


class DailyPaperMatcher:
    def __init__(
        self,
        *,
        slippage_ticks: Decimal = Decimal("1"),
        participation_limit: Decimal = Decimal("0.05"),
    ) -> None:
        if slippage_ticks < 0:
            raise ValueError("slippage_ticks must be non-negative")
        if not Decimal("0") < participation_limit <= Decimal("1"):
            raise ValueError("participation_limit must be in (0, 1]")
        self.slippage_ticks = slippage_ticks
        self.participation_limit = participation_limit

    def match(
        self,
        order: FuturesOrder,
        bar: FuturesDailyBar,
        contract: FuturesContract,
    ) -> MatchResult | None:
        if order.remaining_quantity <= 0:
            return None
        buy = self._is_buy(order.direction, order.offset)
        if order.order_type == FuturesOrderType.MARKET:
            adjustment = contract.price_tick * self.slippage_ticks
            price = bar.open + adjustment if buy else bar.open - adjustment
        else:
            assert order.limit_price is not None
            if buy and bar.low > order.limit_price:
                return None
            if not buy and bar.high < order.limit_price:
                return None
            price = min(bar.open, order.limit_price) if buy else max(bar.open, order.limit_price)
        capacity = int(
            (Decimal(bar.volume) * self.participation_limit).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        quantity = min(order.remaining_quantity, capacity)
        return MatchResult(quantity=quantity, price=price) if quantity > 0 else None

    @staticmethod
    def _is_buy(direction: Direction, offset: Offset) -> bool:
        return (direction == Direction.LONG and offset == Offset.OPEN) or (
            direction == Direction.SHORT and offset != Offset.OPEN
        )