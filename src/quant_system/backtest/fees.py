"""Fee model port + configurable implementation driven by the versioned
FeeSchedule from the market rulebook. No fee/tax numbers live in code."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from quant_system.domain.enums import OrderSide
from quant_system.domain.types import ZERO
from quant_system.rules.rulebook import FeeSchedule

CENT = Decimal("0.01")


@dataclass(frozen=True)
class FeeBreakdown:
    commission: Decimal
    stamp_tax: Decimal
    transfer_fee: Decimal

    @property
    def total(self) -> Decimal:
        return self.commission + self.stamp_tax + self.transfer_fee


class FeeModel(Protocol):
    def compute(
        self, schedule: FeeSchedule, side: OrderSide, quantity: int, price: Decimal
    ) -> FeeBreakdown: ...


class ConfigurableFeeModel:
    def __init__(self, multiplier: Decimal = Decimal("1")) -> None:
        if multiplier <= 0:
            raise ValueError("fee multiplier must be positive")
        self._multiplier = multiplier

    def compute(
        self, schedule: FeeSchedule, side: OrderSide, quantity: int, price: Decimal
    ) -> FeeBreakdown:
        notional = price * quantity
        commission = max(notional * schedule.commission_rate, schedule.min_commission)
        stamp_tax = notional * schedule.stamp_tax_sell_rate if side == OrderSide.SELL else ZERO
        transfer_fee = notional * schedule.transfer_fee_rate
        return FeeBreakdown(
            commission=(commission * self._multiplier).quantize(CENT, rounding=ROUND_HALF_UP),
            stamp_tax=(stamp_tax * self._multiplier).quantize(CENT, rounding=ROUND_HALF_UP),
            transfer_fee=(transfer_fee * self._multiplier).quantize(CENT, rounding=ROUND_HALF_UP),
        )


@dataclass(frozen=True)
class CashReservationPolicy:
    multiplier: Decimal

    def reserve(
        self,
        fee_model: FeeModel,
        schedule: FeeSchedule,
        side: OrderSide,
        quantity: int,
        reference_price: Decimal,
    ) -> Decimal:
        fees = fee_model.compute(schedule, side, quantity, reference_price)
        return (reference_price * quantity + fees.total) * self.multiplier
