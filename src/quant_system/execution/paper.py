"""PaperBrokerGateway: simulated broker keeping its own independent account
view (used to exercise reconciliation before any real broker exists)."""

from __future__ import annotations

from decimal import Decimal

from quant_system.domain.models import FillRecord
from quant_system.domain.types import ZERO
from quant_system.execution.oms import Order


class PaperBrokerGateway:
    def __init__(self, initial_cash: Decimal) -> None:
        self._cash: Decimal = initial_cash
        self._positions: dict[str, int] = {}
        self._orders: dict[str, Order] = {}
        self._trades: list[FillRecord] = []
        self._alive = True

    # -- BrokerGateway -------------------------------------------------------
    def submit(self, order: Order) -> None:
        self._orders[order.order_id] = order

    def cancel(self, order_id: str) -> None:
        self._orders.pop(order_id, None)

    def account_cash(self) -> Decimal:
        return self._cash

    def account_positions(self) -> dict[str, int]:
        return {s: q for s, q in self._positions.items() if q != 0}

    def known_orders(self) -> list[str]:
        return list(self._orders.keys())

    def trades(self) -> list[FillRecord]:
        return list(self._trades)

    def heartbeat_ok(self) -> bool:
        return self._alive

    # -- simulation hooks ------------------------------------------------------
    def apply_fill(self, fill: FillRecord) -> None:
        """Broker-side bookkeeping, independent from the internal ledger."""
        notional = fill.price * fill.quantity
        if fill.side.value == "BUY":
            self._cash -= notional + fill.total_fees
            self._positions[fill.symbol] = self._positions.get(fill.symbol, 0) + fill.quantity
        else:
            self._cash += notional - fill.total_fees
            self._positions[fill.symbol] = self._positions.get(fill.symbol, 0) - fill.quantity
        self._trades.append(fill)

    def set_alive(self, alive: bool) -> None:
        self._alive = alive

    def force_cash(self, cash: Decimal) -> None:
        """Test hook to inject a mismatch."""
        self._cash = cash

    def force_position(self, symbol: str, qty: int) -> None:
        """Test hook to inject a mismatch."""
        if qty == 0:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = qty

    @property
    def zero(self) -> Decimal:
        return ZERO
