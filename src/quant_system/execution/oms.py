"""Order domain object with a validated state machine, plus an idempotent OMS."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from quant_system.domain.enums import OrderSide, OrderStatus, OrderType
from quant_system.domain.exceptions import InvalidTransitionError
from quant_system.domain.models import OrderIntent
from quant_system.domain.types import ZERO, new_id, utc_now

VALID_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.RISK_APPROVED, OrderStatus.REJECTED}),
    OrderStatus.RISK_APPROVED: frozenset({OrderStatus.SUBMITTING, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTING: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED}),
    OrderStatus.SUBMITTED: frozenset({OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED}),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}

TERMINAL_STATUSES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
)


class Order(BaseModel):
    order_id: str = Field(default_factory=lambda: new_id("ord_"))
    client_order_id: str
    strategy_id: str
    signal_id: str | None = None
    target_id: str
    account_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price: Decimal | None = None
    reference_price: Decimal = ZERO
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: int = 0
    avg_fill_price: Decimal = ZERO
    reject_reason: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def transition(self, new_status: OrderStatus, *, reason: str = "") -> None:
        allowed = VALID_TRANSITIONS[self.status]
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"order {self.order_id}: {self.status} -> {new_status} not allowed"
            )
        self.status = new_status
        if reason:
            self.reject_reason = reason
        self.updated_at = utc_now()

    def record_fill(self, quantity: int, price: Decimal) -> None:
        if quantity <= 0 or quantity > self.remaining_quantity:
            raise InvalidTransitionError(
                f"order {self.order_id}: invalid fill quantity {quantity} (remaining {self.remaining_quantity})"
            )
        total_value = self.avg_fill_price * self.filled_quantity + price * quantity
        self.filled_quantity += quantity
        self.avg_fill_price = total_value / self.filled_quantity
        self.transition(
            OrderStatus.FILLED if self.remaining_quantity == 0 else OrderStatus.PARTIALLY_FILLED
        )


class OrderManagementSystem:
    """In-memory OMS. client_order_id is the idempotency key."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._by_client_id: dict[str, str] = {}

    def create_order(self, intent: OrderIntent) -> Order:
        existing_id = self._by_client_id.get(intent.client_order_id)
        if existing_id is not None:
            return self._orders[existing_id]  # idempotent: no duplicate order
        order = Order(
            client_order_id=intent.client_order_id,
            strategy_id=intent.strategy_id,
            signal_id=intent.signal_id,
            target_id=intent.target_id,
            account_id=intent.account_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=intent.quantity,
            limit_price=intent.limit_price,
            reference_price=intent.reference_price,
        )
        self._orders[order.order_id] = order
        self._by_client_id[order.client_order_id] = order.order_id
        return order

    def get(self, order_id: str) -> Order:
        return self._orders[order_id]

    def get_by_client_id(self, client_order_id: str) -> Order | None:
        oid = self._by_client_id.get(client_order_id)
        return self._orders[oid] if oid else None

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    def active_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if not o.is_terminal]
