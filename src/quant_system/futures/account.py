"""Double-sided futures account with daily mark-to-market settlement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from quant_system.domain.types import ZERO
from quant_system.futures.domain import Direction, FuturesContract, Offset


@dataclass
class FuturesPosition:
    symbol: str
    long_today: int = 0
    long_yesterday: int = 0
    short_today: int = 0
    short_yesterday: int = 0
    long_settlement_price: Decimal = ZERO
    short_settlement_price: Decimal = ZERO

    @property
    def long_quantity(self) -> int:
        return self.long_today + self.long_yesterday

    @property
    def short_quantity(self) -> int:
        return self.short_today + self.short_yesterday


@dataclass(frozen=True)
class FuturesAccountSnapshot:
    balance: Decimal
    equity: Decimal
    available: Decimal
    margin: Decimal
    frozen_margin: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    fees: Decimal
    risk_ratio: Decimal


class FuturesAccountLedger:
    def __init__(self, initial_balance: Decimal) -> None:
        if initial_balance <= 0:
            raise ValueError("initial_balance must be positive")
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.realized_pnl = ZERO
        self.fees = ZERO
        self.frozen_margin = ZERO
        self._positions: dict[str, FuturesPosition] = {}
        self._contracts: dict[str, FuturesContract] = {}

    def position(self, symbol: str) -> FuturesPosition:
        return self._positions.setdefault(symbol, FuturesPosition(symbol=symbol))

    def positions(self) -> tuple[FuturesPosition, ...]:
        return tuple(self._positions[symbol] for symbol in sorted(self._positions))

    def export_state(self) -> dict[str, Any]:
        return {
            "initial_balance": str(self.initial_balance),
            "balance": str(self.balance),
            "realized_pnl": str(self.realized_pnl),
            "fees": str(self.fees),
            "frozen_margin": str(self.frozen_margin),
            "positions": [
                {
                    "symbol": position.symbol,
                    "long_today": position.long_today,
                    "long_yesterday": position.long_yesterday,
                    "short_today": position.short_today,
                    "short_yesterday": position.short_yesterday,
                    "long_settlement_price": str(position.long_settlement_price),
                    "short_settlement_price": str(position.short_settlement_price),
                }
                for position in self.positions()
            ],
        }

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any],
        contracts: Mapping[str, FuturesContract],
    ) -> FuturesAccountLedger:
        ledger = cls(Decimal(str(state["initial_balance"])))
        ledger.balance = Decimal(str(state["balance"]))
        ledger.realized_pnl = Decimal(str(state["realized_pnl"]))
        ledger.fees = Decimal(str(state["fees"]))
        ledger.frozen_margin = Decimal(str(state.get("frozen_margin", "0")))
        for raw in state.get("positions", []):
            symbol = str(raw["symbol"])
            ledger._positions[symbol] = FuturesPosition(
                symbol=symbol,
                long_today=int(raw["long_today"]),
                long_yesterday=int(raw["long_yesterday"]),
                short_today=int(raw["short_today"]),
                short_yesterday=int(raw["short_yesterday"]),
                long_settlement_price=Decimal(str(raw["long_settlement_price"])),
                short_settlement_price=Decimal(str(raw["short_settlement_price"])),
            )
            ledger._contracts[symbol] = contracts[symbol]
        ledger._assert_invariants()
        return ledger

    def closable_quantity(self, symbol: str, direction: Direction, offset: Offset) -> int:
        position = self._positions.get(symbol)
        if position is None:
            return 0
        today, yesterday = self._buckets(position, direction)
        if offset == Offset.CLOSE_TODAY:
            return today
        if offset == Offset.CLOSE_YESTERDAY:
            return yesterday
        if offset == Offset.CLOSE:
            return today + yesterday
        return 0

    def total_quantity(self, symbol: str) -> int:
        position = self._positions.get(symbol)
        if position is None:
            return 0
        return position.long_quantity + position.short_quantity

    def apply_trade(
        self,
        *,
        contract: FuturesContract,
        direction: Direction,
        offset: Offset,
        quantity: int,
        price: Decimal,
        fee: Decimal,
        trading_day: date,
    ) -> None:
        del trading_day
        if quantity <= 0 or price <= 0 or fee < 0:
            raise ValueError("trade quantity and price must be positive and fee non-negative")
        self._contracts[contract.symbol] = contract
        position = self.position(contract.symbol)
        if offset == Offset.OPEN:
            self._open(position, direction, quantity, price)
        else:
            self._close(position, contract, direction, offset, quantity, price)
        self.balance -= fee
        self.fees += fee
        self._assert_invariants()

    def settle(
        self,
        *,
        trading_day: date,
        settlements: Mapping[str, Decimal],
        contracts: Mapping[str, FuturesContract],
    ) -> None:
        del trading_day
        variation = ZERO
        for symbol, position in self._positions.items():
            if symbol not in settlements or symbol not in contracts:
                raise ValueError(f"missing settlement or contract for {symbol}")
            settlement = settlements[symbol]
            contract = contracts[symbol]
            variation += self._unrealized(position, contract, settlement)
            if position.long_quantity:
                position.long_settlement_price = settlement
            if position.short_quantity:
                position.short_settlement_price = settlement
            position.long_yesterday += position.long_today
            position.short_yesterday += position.short_today
            position.long_today = 0
            position.short_today = 0
            self._contracts[symbol] = contract
        self.balance += variation
        self.realized_pnl += variation
        self._assert_invariants()

    def snapshot(self, marks: Mapping[str, Decimal]) -> FuturesAccountSnapshot:
        unrealized = ZERO
        margin = ZERO
        for symbol, position in self._positions.items():
            if position.long_quantity == 0 and position.short_quantity == 0:
                continue
            contract = self._contracts[symbol]
            mark = marks.get(symbol)
            if mark is None:
                raise ValueError(f"missing mark for {symbol}")
            unrealized += self._unrealized(position, contract, mark)
            margin += mark * contract.multiplier * (
                position.long_quantity * contract.long_margin_rate
                + position.short_quantity * contract.short_margin_rate
            )
        equity = self.balance + unrealized
        available = equity - margin - self.frozen_margin
        risk_ratio = margin / equity if equity > 0 else Decimal("1")
        return FuturesAccountSnapshot(
            balance=self.balance,
            equity=equity,
            available=available,
            margin=margin,
            frozen_margin=self.frozen_margin,
            unrealized_pnl=unrealized,
            realized_pnl=self.realized_pnl,
            fees=self.fees,
            risk_ratio=risk_ratio,
        )

    @staticmethod
    def _buckets(position: FuturesPosition, direction: Direction) -> tuple[int, int]:
        if direction == Direction.LONG:
            return position.long_today, position.long_yesterday
        return position.short_today, position.short_yesterday

    @staticmethod
    def _open(
        position: FuturesPosition,
        direction: Direction,
        quantity: int,
        price: Decimal,
    ) -> None:
        if direction == Direction.LONG:
            previous = position.long_quantity
            position.long_settlement_price = FuturesAccountLedger._weighted_price(
                position.long_settlement_price, previous, price, quantity
            )
            position.long_today += quantity
        else:
            previous = position.short_quantity
            position.short_settlement_price = FuturesAccountLedger._weighted_price(
                position.short_settlement_price, previous, price, quantity
            )
            position.short_today += quantity

    def _close(
        self,
        position: FuturesPosition,
        contract: FuturesContract,
        direction: Direction,
        offset: Offset,
        quantity: int,
        price: Decimal,
    ) -> None:
        if quantity > self.closable_quantity(position.symbol, direction, offset):
            raise ValueError(f"insufficient {direction.value} position to {offset.value}")
        if direction == Direction.LONG:
            position.long_today, position.long_yesterday = self._consume(
                position.long_today, position.long_yesterday, offset, quantity
            )
            pnl = (price - position.long_settlement_price) * contract.multiplier * quantity
            if position.long_quantity == 0:
                position.long_settlement_price = ZERO
        else:
            position.short_today, position.short_yesterday = self._consume(
                position.short_today, position.short_yesterday, offset, quantity
            )
            pnl = (position.short_settlement_price - price) * contract.multiplier * quantity
            if position.short_quantity == 0:
                position.short_settlement_price = ZERO
        self.balance += pnl
        self.realized_pnl += pnl

    @staticmethod
    def _consume(today: int, yesterday: int, offset: Offset, quantity: int) -> tuple[int, int]:
        if offset == Offset.CLOSE_TODAY:
            return today - quantity, yesterday
        if offset == Offset.CLOSE_YESTERDAY:
            return today, yesterday - quantity
        from_today = min(today, quantity)
        return today - from_today, yesterday - (quantity - from_today)

    @staticmethod
    def _weighted_price(
        previous_price: Decimal,
        previous_quantity: int,
        price: Decimal,
        quantity: int,
    ) -> Decimal:
        total = previous_quantity + quantity
        return (previous_price * previous_quantity + price * quantity) / total

    @staticmethod
    def _unrealized(
        position: FuturesPosition,
        contract: FuturesContract,
        mark: Decimal,
    ) -> Decimal:
        long_pnl = (
            (mark - position.long_settlement_price)
            * contract.multiplier
            * position.long_quantity
        )
        short_pnl = (
            (position.short_settlement_price - mark)
            * contract.multiplier
            * position.short_quantity
        )
        return long_pnl + short_pnl

    def _assert_invariants(self) -> None:
        for position in self._positions.values():
            quantities = (
                position.long_today,
                position.long_yesterday,
                position.short_today,
                position.short_yesterday,
            )
            if any(quantity < 0 for quantity in quantities):
                raise AssertionError("futures position quantity cannot be negative")
        if self.frozen_margin < 0:
            raise AssertionError("frozen margin cannot be negative")