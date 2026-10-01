"""BrokerGateway port. Only PaperBrokerGateway exists in V1; a real broker
adapter is deliberately NOT implemented."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from quant_system.domain.enums import OrderSide
from quant_system.domain.models import FillRecord
from quant_system.execution.oms import Order
from quant_system.risk.runtime import TradingRuntime


class BrokerGateway(Protocol):
    def submit(self, order: Order) -> None: ...

    def cancel(self, order_id: str) -> None: ...

    def account_cash(self) -> Decimal: ...

    def account_positions(self) -> dict[str, int]: ...

    def known_orders(self) -> list[str]: ...

    def trades(self) -> list[FillRecord]: ...

    def heartbeat_ok(self) -> bool: ...


class SafetyGatedBrokerGateway:
    """Enforces runtime state immediately before broker side effects."""

    def __init__(self, inner: BrokerGateway, runtime: TradingRuntime) -> None:
        self._inner = inner
        self._runtime = runtime

    def submit(self, order: Order) -> None:
        self._runtime.require_order_submission_allowed(
            increases_risk=order.side == OrderSide.BUY
        )
        self._inner.submit(order)

    def cancel(self, order_id: str) -> None:
        self._runtime.require_cancel_allowed()
        self._inner.cancel(order_id)

    def account_cash(self) -> Decimal:
        return self._inner.account_cash()

    def account_positions(self) -> dict[str, int]:
        return self._inner.account_positions()

    def known_orders(self) -> list[str]:
        return self._inner.known_orders()

    def trades(self) -> list[FillRecord]:
        return self._inner.trades()

    def heartbeat_ok(self) -> bool:
        return self._inner.heartbeat_ok()

    def apply_fill(self, fill: FillRecord) -> None:
        apply_fill = getattr(self._inner, "apply_fill", None)
        if apply_fill is None:
            raise TypeError("wrapped broker does not support simulated fills")
        apply_fill(fill)
