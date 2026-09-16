"""Fill model ports + conservative daily-bar implementations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from quant_system.backtest.slippage import SlippageModel
from quant_system.domain.models import Bar
from quant_system.domain.types import quantize_price
from quant_system.execution.oms import Order
from quant_system.rules.rulebook import TradingRules


@dataclass(frozen=True)
class FillProposal:
    quantity: int
    price: Decimal


class FillModel(Protocol):
    def propose(
        self, order: Order, bar: Bar, rules: TradingRules, *, liquidity_volume: int
    ) -> FillProposal | None: ...


class NextOpenFillModel:
    """Fill at next session's open, adjusted by slippage, clamped to price limits."""

    def __init__(self, slippage: SlippageModel) -> None:
        self._slippage = slippage

    def propose(
        self, order: Order, bar: Bar, rules: TradingRules, *, liquidity_volume: int
    ) -> FillProposal | None:
        del liquidity_volume
        price = quantize_price(self._slippage.adjust(bar.open, order.side), rules.tick_size)
        up = rules.limit_up(bar.prev_close)
        down = rules.limit_down(bar.prev_close)
        if up is not None and price > up:
            price = up
        if down is not None and price < down:
            price = down
        if order.remaining_quantity <= 0:
            return None
        return FillProposal(quantity=order.remaining_quantity, price=price)


class VolumeParticipationFillModel:
    """Caps fill size at a fraction of the bar's volume (lot-aligned)."""

    def __init__(self, base: FillModel, participation_limit: Decimal) -> None:
        self._base = base
        self._participation = participation_limit

    def propose(
        self, order: Order, bar: Bar, rules: TradingRules, *, liquidity_volume: int
    ) -> FillProposal | None:
        proposal = self._base.propose(
            order, bar, rules, liquidity_volume=liquidity_volume
        )
        if proposal is None:
            return None
        max_qty = int(Decimal(liquidity_volume) * self._participation)
        max_qty = (max_qty // rules.lot_size) * rules.lot_size
        qty = min(proposal.quantity, max_qty)
        if qty <= 0:
            return None
        return FillProposal(quantity=qty, price=proposal.price)
