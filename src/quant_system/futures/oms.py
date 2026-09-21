"""Idempotent futures order management."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from quant_system.domain.types import new_id, utc_now
from quant_system.futures.domain import (
    FuturesOrder,
    FuturesOrderIntent,
    FuturesOrderStatus,
)


class FuturesOMS:
    def __init__(
        self,
        *,
        id_factory: Callable[[str], str] = new_id,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self._orders: dict[str, FuturesOrder] = {}
        self._by_client_id: dict[str, str] = {}
        self._id_factory = id_factory
        self._now = now_provider

    def restore(self, orders: list[FuturesOrder]) -> None:
        self._orders = {order.order_id: order for order in orders}
        self._by_client_id = {order.client_order_id: order.order_id for order in orders}

    def create(self, intent: FuturesOrderIntent) -> FuturesOrder:
        existing = self.get_by_client_id(intent.client_order_id)
        if existing is not None:
            return existing
        now = self._now()
        order = FuturesOrder(
            order_id=self._id_factory("ford_"),
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            direction=intent.direction,
            offset=intent.offset,
            quantity=intent.quantity,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            status=FuturesOrderStatus.SUBMITTED,
            created_at=now,
            updated_at=now,
        )
        self._store(order)
        return order

    def reject(self, order_id: str, reason: str) -> FuturesOrder:
        order = self.get(order_id)
        if order.is_terminal:
            return order
        return self._replace(
            order.model_copy(
                update={
                    "status": FuturesOrderStatus.REJECTED,
                    "reject_reason": reason,
                    "updated_at": self._now(),
                }
            )
        )

    def accept(self, order_id: str) -> FuturesOrder:
        order = self.get(order_id)
        if order.status != FuturesOrderStatus.SUBMITTED:
            raise ValueError(f"cannot accept order in {order.status.value}")
        return self._replace(
            order.model_copy(
                update={"status": FuturesOrderStatus.ACCEPTED, "updated_at": self._now()}
            )
        )

    def cancel(self, order_id: str) -> FuturesOrder:
        order = self.get(order_id)
        if order.is_terminal:
            raise ValueError(f"cannot cancel terminal order {order_id}")
        return self._replace(
            order.model_copy(
                update={"status": FuturesOrderStatus.CANCELLED, "updated_at": self._now()}
            )
        )

    def record_fill(self, order_id: str, quantity: int, price: Decimal) -> FuturesOrder:
        order = self.get(order_id)
        if order.status not in {
            FuturesOrderStatus.ACCEPTED,
            FuturesOrderStatus.PARTIALLY_FILLED,
        }:
            raise ValueError(f"cannot fill order in {order.status.value}")
        if quantity <= 0 or quantity > order.remaining_quantity:
            raise ValueError(f"invalid fill quantity {quantity}")
        previous_value = (order.average_fill_price or Decimal("0")) * order.filled_quantity
        filled = order.filled_quantity + quantity
        average = (previous_value + price * quantity) / filled
        status = (
            FuturesOrderStatus.FILLED
            if filled == order.quantity
            else FuturesOrderStatus.PARTIALLY_FILLED
        )
        return self._replace(
            order.model_copy(
                update={
                    "filled_quantity": filled,
                    "average_fill_price": average,
                    "status": status,
                    "updated_at": self._now(),
                }
            )
        )

    def get(self, order_id: str) -> FuturesOrder:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise KeyError(f"unknown futures order {order_id}") from exc

    def get_by_client_id(self, client_order_id: str) -> FuturesOrder | None:
        order_id = self._by_client_id.get(client_order_id)
        return self._orders.get(order_id) if order_id else None

    def all(self) -> list[FuturesOrder]:
        return sorted(self._orders.values(), key=lambda order: order.created_at)

    def active(self) -> list[FuturesOrder]:
        return [order for order in self.all() if not order.is_terminal]

    def _store(self, order: FuturesOrder) -> None:
        self._orders[order.order_id] = order
        self._by_client_id[order.client_order_id] = order.order_id

    def _replace(self, order: FuturesOrder) -> FuturesOrder:
        self._orders[order.order_id] = order
        return order