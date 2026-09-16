"""AccountLedger: double-entry-style cash/position accounting with T+1
available-position semantics. All money is Decimal; fail closed on invariant
violations. Every balance change is explainable from ledger entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from quant_system.domain.enums import OrderSide
from quant_system.domain.exceptions import (
    InsufficientCashError,
    InsufficientPositionError,
    LedgerInvariantError,
)
from quant_system.domain.models import FillRecord
from quant_system.domain.types import ZERO


@dataclass
class Position:
    symbol: str
    total: int = 0
    available: int = 0          # sellable now (T+1 applied)
    frozen_for_sell: int = 0    # reserved by open sell orders
    avg_cost: Decimal = ZERO
    last_price: Decimal = ZERO
    price_as_of: date | None = None
    is_suspended: bool = False

    @property
    def market_value(self) -> Decimal:
        return self.last_price * self.total

    @property
    def unrealized_pnl(self) -> Decimal:
        return (self.last_price - self.avg_cost) * self.total


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    trade_date: date
    kind: str  # BUY_FILL | SELL_FILL | FREEZE | UNFREEZE | DEPOSIT
    symbol: str
    quantity: int
    cash_delta: Decimal          # change to (cash + frozen_cash)
    fee: Decimal = ZERO
    tax: Decimal = ZERO
    note: str = ""


@dataclass
class CorporateAction:
    """Explicit corporate-action interface (dividend/split), applied via ledger."""

    symbol: str
    kind: str            # CASH_DIVIDEND | SPLIT
    ex_date: date
    cash_per_share: Decimal = ZERO
    split_ratio: Decimal = Decimal("1")


class AccountLedger:
    def __init__(self, account_id: str, initial_cash: Decimal) -> None:
        if initial_cash < 0:
            raise LedgerInvariantError("initial cash must be non-negative")
        self.account_id = account_id
        self.initial_cash = initial_cash
        self.cash: Decimal = initial_cash
        self.frozen_cash: Decimal = ZERO
        self.positions: dict[str, Position] = {}
        self.realized_pnl: Decimal = ZERO
        self.fees_paid: Decimal = ZERO
        self.taxes_paid: Decimal = ZERO
        self.entries: list[LedgerEntry] = []
        self.fills: list[FillRecord] = []
        self._frozen_by_order: dict[str, Decimal] = {}
        self._shares_frozen_by_order: dict[str, tuple[str, int]] = {}
        self._pending_settle: dict[str, int] = {}  # bought today, sellable T+1
        self._seq = 0
        self.current_date: date | None = None

    # -- day lifecycle -------------------------------------------------------
    def start_of_day(self, d: date) -> None:
        """Release T+1 settlement: shares bought before today become available."""
        self.current_date = d
        for symbol, qty in self._pending_settle.items():
            pos = self.positions.get(symbol)
            if pos is not None:
                pos.available += qty
        self._pending_settle.clear()

    # -- freezing ------------------------------------------------------------
    def freeze_cash_for_order(self, order_id: str, amount: Decimal) -> None:
        if amount > self.cash:
            raise InsufficientCashError(
                f"cannot freeze {amount}; cash available {self.cash}"
            )
        self.cash -= amount
        self.frozen_cash += amount
        self._frozen_by_order[order_id] = self._frozen_by_order.get(order_id, ZERO) + amount
        self._record("FREEZE", "", 0, ZERO, note=f"order={order_id} amount={amount}")

    def frozen_for_order(self, order_id: str) -> Decimal:
        return self._frozen_by_order.get(order_id, ZERO)

    def unfreeze_cash_for_order(self, order_id: str) -> None:
        amount = self._frozen_by_order.pop(order_id, ZERO)
        if amount > 0:
            self.frozen_cash -= amount
            self.cash += amount
            self._record("UNFREEZE", "", 0, ZERO, note=f"order={order_id} amount={amount}")

    def freeze_shares_for_order(self, order_id: str, symbol: str, quantity: int) -> None:
        pos = self.positions.get(symbol)
        if pos is None or pos.available < quantity:
            available = pos.available if pos else 0
            raise InsufficientPositionError(
                f"cannot freeze {quantity} {symbol}; available {available}"
            )
        pos.available -= quantity
        pos.frozen_for_sell += quantity
        self._shares_frozen_by_order[order_id] = (symbol, quantity)

    def unfreeze_shares_for_order(self, order_id: str) -> None:
        entry = self._shares_frozen_by_order.pop(order_id, None)
        if entry:
            symbol, qty = entry
            pos = self.positions[symbol]
            pos.frozen_for_sell -= qty
            pos.available += qty

    # -- fills ----------------------------------------------------------------
    def apply_fill(self, fill: FillRecord, *, order_id: str | None = None) -> None:
        if fill.side == OrderSide.BUY:
            self._apply_buy(fill, order_id)
        else:
            self._apply_sell(fill, order_id)
        self.fills.append(fill)
        self.check_invariants()

    def _apply_buy(self, fill: FillRecord, order_id: str | None) -> None:
        cost = fill.price * fill.quantity + fill.total_fees
        if order_id is not None and order_id in self._frozen_by_order:
            frozen = self._frozen_by_order[order_id]
            take = min(frozen, cost)
            self.frozen_cash -= take
            self._frozen_by_order[order_id] = frozen - take
            shortfall = cost - take
            if shortfall > 0:
                if shortfall > self.cash:
                    raise InsufficientCashError(f"buy cost {cost} exceeds frozen+cash")
                self.cash -= shortfall
        else:
            if cost > self.cash:
                raise InsufficientCashError(f"buy cost {cost} exceeds cash {self.cash}")
            self.cash -= cost

        pos = self.positions.setdefault(fill.symbol, Position(symbol=fill.symbol))
        new_total = pos.total + fill.quantity
        pos.avg_cost = (pos.avg_cost * pos.total + cost) / new_total
        pos.total = new_total
        pos.last_price = fill.price
        self._pending_settle[fill.symbol] = (
            self._pending_settle.get(fill.symbol, 0) + fill.quantity
        )
        self.fees_paid += fill.commission + fill.transfer_fee
        self.taxes_paid += fill.stamp_tax
        self._record(
            "BUY_FILL", fill.symbol, fill.quantity, -cost,
            fee=fill.commission + fill.transfer_fee, tax=fill.stamp_tax,
            note=f"px={fill.price}",
        )

    def _apply_sell(self, fill: FillRecord, order_id: str | None) -> None:
        pos = self.positions.get(fill.symbol)
        if pos is None or pos.total < fill.quantity:
            raise InsufficientPositionError(
                f"sell {fill.quantity} {fill.symbol} exceeds holdings"
            )
        if order_id is not None and order_id in self._shares_frozen_by_order:
            symbol, frozen_qty = self._shares_frozen_by_order[order_id]
            take = min(frozen_qty, fill.quantity)
            pos.frozen_for_sell -= take
            remaining = frozen_qty - take
            if remaining > 0:
                self._shares_frozen_by_order[order_id] = (symbol, remaining)
            else:
                self._shares_frozen_by_order.pop(order_id)
            extra = fill.quantity - take
            if extra > 0:
                if pos.available < extra:
                    raise InsufficientPositionError("sell exceeds frozen+available shares")
                pos.available -= extra
        else:
            if pos.available < fill.quantity:
                raise InsufficientPositionError(
                    f"sell {fill.quantity} {fill.symbol} exceeds available {pos.available} (T+1)"
                )
            pos.available -= fill.quantity

        proceeds = fill.price * fill.quantity - fill.total_fees
        self.cash += proceeds
        self.realized_pnl += (fill.price - pos.avg_cost) * fill.quantity - fill.total_fees
        pos.total -= fill.quantity
        pos.last_price = fill.price
        if pos.total == 0:
            pos.avg_cost = ZERO
        self.fees_paid += fill.commission + fill.transfer_fee
        self.taxes_paid += fill.stamp_tax
        self._record(
            "SELL_FILL", fill.symbol, -fill.quantity, proceeds,
            fee=fill.commission + fill.transfer_fee, tax=fill.stamp_tax,
            note=f"px={fill.price}",
        )

    # -- corporate actions ---------------------------------------------------
    def apply_corporate_action(self, action: CorporateAction) -> None:
        self.current_date = action.ex_date
        pos = self.positions.get(action.symbol)
        if pos is None or pos.total == 0:
            return
        if action.kind == "CASH_DIVIDEND":
            amount = action.cash_per_share * pos.total
            self.cash += amount
            pos.avg_cost = max(ZERO, pos.avg_cost - action.cash_per_share)
            self._record("DEPOSIT", action.symbol, 0, amount, note="cash dividend")
        elif action.kind in {"STOCK_SPLIT", "BONUS_SHARE", "SPLIT"}:
            ratio = action.split_ratio
            if ratio <= 0:
                raise LedgerInvariantError("split ratio must be positive")

            def adjusted(quantity: int) -> int:
                value = Decimal(quantity) * ratio
                if value != value.to_integral_value():
                    raise LedgerInvariantError(
                        f"fractional shares unsupported: {quantity} * {ratio}"
                    )
                return int(value)

            pos.total = adjusted(pos.total)
            pos.available = adjusted(pos.available)
            pos.frozen_for_sell = adjusted(pos.frozen_for_sell)
            if action.symbol in self._pending_settle:
                self._pending_settle[action.symbol] = adjusted(
                    self._pending_settle[action.symbol]
                )
            for order_id, (symbol, quantity) in tuple(self._shares_frozen_by_order.items()):
                if symbol == action.symbol:
                    self._shares_frozen_by_order[order_id] = (symbol, adjusted(quantity))
            pos.avg_cost = pos.avg_cost / ratio
            pos.last_price = pos.last_price / ratio
        else:
            raise LedgerInvariantError(f"unsupported corporate action {action.kind}")
        self.check_invariants()

    # -- valuation ------------------------------------------------------------
    def mark_to_market(
        self,
        prices: dict[str, Decimal],
        *,
        price_date: date | None = None,
        suspended_symbols: set[str] | None = None,
    ) -> None:
        for symbol, pos in self.positions.items():
            if symbol in prices:
                pos.last_price = prices[symbol]
                pos.price_as_of = price_date
            if suspended_symbols is not None:
                pos.is_suspended = symbol in suspended_symbols

    @property
    def market_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions.values()), ZERO)

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum((p.unrealized_pnl for p in self.positions.values()), ZERO)

    @property
    def nav(self) -> Decimal:
        return self.cash + self.frozen_cash + self.market_value

    # -- invariants -----------------------------------------------------------
    def check_invariants(self) -> None:
        if self.cash < 0:
            raise LedgerInvariantError(f"negative cash {self.cash}")
        if self.frozen_cash < 0:
            raise LedgerInvariantError(f"negative frozen cash {self.frozen_cash}")
        for pos in self.positions.values():
            if pos.total < 0 or pos.available < 0 or pos.frozen_for_sell < 0:
                raise LedgerInvariantError(f"negative position state for {pos.symbol}: {pos}")
            pending = self._pending_settle.get(pos.symbol, 0)
            if pos.available + pos.frozen_for_sell + pending != pos.total:
                raise LedgerInvariantError(
                    f"{pos.symbol}: available({pos.available}) + frozen({pos.frozen_for_sell})"
                    f" + pending_settle({pending}) != total({pos.total})"
                )
        flow = sum((e.cash_delta for e in self.entries), ZERO)
        if self.initial_cash + flow != self.cash + self.frozen_cash:
            raise LedgerInvariantError(
                f"cash flow mismatch: initial {self.initial_cash} + flow {flow}"
                f" != cash {self.cash} + frozen {self.frozen_cash}"
            )

    def _record(
        self,
        kind: str,
        symbol: str,
        quantity: int,
        cash_delta: Decimal,
        *,
        fee: Decimal = ZERO,
        tax: Decimal = ZERO,
        note: str = "",
    ) -> None:
        self._seq += 1
        self.entries.append(
            LedgerEntry(
                seq=self._seq,
                trade_date=self.current_date or date.min,
                kind=kind,
                symbol=symbol,
                quantity=quantity,
                cash_delta=cash_delta,
                fee=fee,
                tax=tax,
                note=note,
            )
        )

    def snapshot(self) -> dict:
        return {
            "cash": str(self.cash),
            "frozen_cash": str(self.frozen_cash),
            "market_value": str(self.market_value),
            "nav": str(self.nav),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "fees": str(self.fees_paid),
            "taxes": str(self.taxes_paid),
            "positions": {
                s: {
                    "total": p.total,
                    "available": p.available,
                    "frozen_for_sell": p.frozen_for_sell,
                    "pending_settle": self._pending_settle.get(s, 0),
                    "avg_cost": str(p.avg_cost),
                    "last_price": str(p.last_price),
                    "price_as_of": p.price_as_of.isoformat() if p.price_as_of else None,
                    "stale_price_age": (
                        (self.current_date - p.price_as_of).days
                        if self.current_date is not None and p.price_as_of is not None else None
                    ),
                    "is_suspended": p.is_suspended,
                }
                for s, p in self.positions.items()
                if p.total != 0
            },
        }
