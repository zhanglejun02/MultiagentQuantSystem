"""Slippage model port + fixed-bps implementation."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from quant_system.domain.enums import OrderSide

BPS = Decimal("10000")


class SlippageModel(Protocol):
    def adjust(self, price: Decimal, side: OrderSide) -> Decimal: ...


class FixedBpsSlippageModel:
    def __init__(self, bps: Decimal) -> None:
        self.bps = bps

    def adjust(self, price: Decimal, side: OrderSide) -> Decimal:
        delta = price * self.bps / BPS
        return price + delta if side == OrderSide.BUY else price - delta


class ZeroSlippageModel:
    def adjust(self, price: Decimal, side: OrderSide) -> Decimal:
        return price
