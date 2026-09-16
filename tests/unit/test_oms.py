from __future__ import annotations

from decimal import Decimal

import pytest

from quant_system.domain.enums import OrderSide, OrderStatus, OrderType
from quant_system.domain.exceptions import InvalidTransitionError
from quant_system.domain.models import OrderIntent
from quant_system.execution.oms import OrderManagementSystem


def intent(client_order_id: str = "c1", qty: int = 1000) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id, strategy_id="s", target_id="t", account_id="a",
        symbol="600000.SSE", side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=qty, reference_price=Decimal("10.00"),
    )


def test_client_order_id_idempotency():
    oms = OrderManagementSystem()
    o1 = oms.create_order(intent("dup"))
    o2 = oms.create_order(intent("dup"))
    assert o1.order_id == o2.order_id
    assert len(oms.all_orders()) == 1


def test_valid_lifecycle_to_filled():
    oms = OrderManagementSystem()
    order = oms.create_order(intent())
    for status in (OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING,
                   OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED):
        order.transition(status)
    order.record_fill(400, Decimal("10.00"))
    assert order.status == OrderStatus.PARTIALLY_FILLED
    order.record_fill(600, Decimal("10.10"))
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == 1000
    assert order.avg_fill_price == Decimal("10.06")


def test_invalid_transition_rejected():
    oms = OrderManagementSystem()
    order = oms.create_order(intent())
    with pytest.raises(InvalidTransitionError):
        order.transition(OrderStatus.FILLED)  # CREATED -> FILLED illegal


def test_terminal_state_locked():
    oms = OrderManagementSystem()
    order = oms.create_order(intent())
    order.transition(OrderStatus.REJECTED, reason="risk")
    with pytest.raises(InvalidTransitionError):
        order.transition(OrderStatus.RISK_APPROVED)


def test_overfill_rejected():
    oms = OrderManagementSystem()
    order = oms.create_order(intent(qty=100))
    for status in (OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING,
                   OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED):
        order.transition(status)
    with pytest.raises(InvalidTransitionError):
        order.record_fill(200, Decimal("10"))
