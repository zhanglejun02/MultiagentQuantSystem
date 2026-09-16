"""ExchangeSimulator: applies market rules conservatively to daily bars.

Conservative daily-bar semantics:
- suspended securities never fill;
- a buy cannot fill when the market opens at/above limit-up;
- a sell cannot fill when the market opens at/below limit-down;
- fills never exceed the participation-capped volume;
- limit orders only fill if the execution price satisfies the limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_system.backtest.fill import FillModel
from quant_system.domain.enums import OrderSide, OrderType
from quant_system.domain.models import Bar
from quant_system.execution.oms import Order
from quant_system.rules.rulebook import TradingRules


@dataclass(frozen=True)
class ExecutionOutcome:
    filled_quantity: int
    price: Decimal | None
    reason: str  # empty when filled; else SUSPENDED / LIMIT_UP_NO_FILL / ...


class ExchangeSimulator:
    def __init__(self, fill_model: FillModel) -> None:
        self._fill_model = fill_model

    def execute(
        self,
        order: Order,
        bar: Bar | None,
        rules: TradingRules,
        *,
        liquidity_volume: int,
        reference_price: Decimal,
    ) -> ExecutionOutcome:
        if rules.is_suspended or bar is None:
            return ExecutionOutcome(0, None, "SUSPENDED")

        if order.order_type not in rules.allowed_order_types:
            return ExecutionOutcome(0, None, "ORDER_TYPE_NOT_ALLOWED")

        if order.quantity % rules.lot_size != 0 and order.side == OrderSide.BUY:
            return ExecutionOutcome(0, None, "LOT_SIZE_VIOLATION")

        up = rules.limit_up(reference_price)
        down = rules.limit_down(reference_price)
        if order.side == OrderSide.BUY and up is not None and bar.open >= up:
            return ExecutionOutcome(0, None, "LIMIT_UP_NO_FILL")
        if order.side == OrderSide.SELL and down is not None and bar.open <= down:
            return ExecutionOutcome(0, None, "LIMIT_DOWN_NO_FILL")

        if order.limit_price is not None and not rules.is_valid_price(
            order.limit_price, reference_price
        ):
            return ExecutionOutcome(0, None, "INVALID_LIMIT_PRICE")

        proposal = self._fill_model.propose(
            order, bar, rules, liquidity_volume=liquidity_volume
        )
        if proposal is None or proposal.quantity <= 0:
            return ExecutionOutcome(0, None, "PARTICIPATION_LIMIT")

        if order.order_type == OrderType.LIMIT and order.limit_price is not None:
            if order.side == OrderSide.BUY and proposal.price > order.limit_price:
                return ExecutionOutcome(0, None, "PRICE_NOT_REACHED")
            if order.side == OrderSide.SELL and proposal.price < order.limit_price:
                return ExecutionOutcome(0, None, "PRICE_NOT_REACHED")

        return ExecutionOutcome(proposal.quantity, proposal.price, "")
