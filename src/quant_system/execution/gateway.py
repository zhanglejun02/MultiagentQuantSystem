"""BrokerGateway port. Only PaperBrokerGateway exists in V1; a real broker
adapter is deliberately NOT implemented."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from quant_system.domain.models import FillRecord
from quant_system.execution.oms import Order


class BrokerGateway(Protocol):
    def submit(self, order: Order) -> None: ...

    def cancel(self, order_id: str) -> None: ...

    def account_cash(self) -> Decimal: ...

    def account_positions(self) -> dict[str, int]: ...

    def known_orders(self) -> list[str]: ...

    def trades(self) -> list[FillRecord]: ...

    def heartbeat_ok(self) -> bool: ...
