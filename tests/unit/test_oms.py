from __future__ import annotations

from decimal import Decimal

import pytest

from quant_system.domain.enums import OrderSide, OrderStatus, OrderType, TradingRuntimeState
from quant_system.domain.exceptions import InvalidTransitionError, KillSwitchEngagedError
from quant_system.domain.models import OrderIntent
from quant_system.execution.gateway import SafetyGatedBrokerGateway
from quant_system.execution.oms import OrderManagementSystem
from quant_system.execution.paper import PaperBrokerGateway
from quant_system.risk.runtime import KillSwitch, TradingRuntime


def intent(client_order_id: str = "c1", qty: int = 1000) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id, strategy_id="s", target_id="t", account_id="a",
        symbol="600000.SSE", side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=qty, reference_price=Decimal("10.00"),
    )


def make_oms() -> OrderManagementSystem:
    return OrderManagementSystem(TradingRuntime(KillSwitch()))


def test_client_order_id_idempotency():
    oms = make_oms()
    o1 = oms.create_order(intent("dup"))
    o2 = oms.create_order(intent("dup"))
    assert o1.order_id == o2.order_id
    assert len(oms.all_orders()) == 1


@pytest.mark.parametrize("halt", ["cancel_only", "kill_switch"])
def test_runtime_safety_state_blocks_new_orders(halt: str):
    kill_switch = KillSwitch()
    runtime = TradingRuntime(kill_switch)
    oms = OrderManagementSystem(runtime)

    if halt == "cancel_only":
        runtime.set_state(
            TradingRuntimeState.CANCEL_ONLY,
            reason="broker unstable",
            operator="risk-daemon",
        )
    else:
        kill_switch.engage("operator halt")

    with pytest.raises(KillSwitchEngagedError):
        oms.create_order(intent())
    assert oms.all_orders() == []


def test_safe_mode_allows_only_risk_reducing_order():
    runtime = TradingRuntime(KillSwitch())
    runtime.enter_safe_mode("reconciliation mismatch")
    oms = OrderManagementSystem(runtime)

    sell = oms.create_order(intent("sell", qty=100).model_copy(update={"side": OrderSide.SELL}))
    assert sell.side == OrderSide.SELL
    with pytest.raises(KillSwitchEngagedError):
        oms.create_order(intent("buy"))


def test_gateway_rechecks_runtime_immediately_before_submit():
    runtime = TradingRuntime(KillSwitch())
    order = OrderManagementSystem(runtime).create_order(intent())
    broker = PaperBrokerGateway(Decimal("100000"))
    gateway = SafetyGatedBrokerGateway(broker, runtime)
    runtime.enter_safe_mode("late reconciliation mismatch")

    with pytest.raises(KillSwitchEngagedError):
        gateway.submit(order)
    assert broker.known_orders() == []


def test_valid_lifecycle_to_filled():
    oms = make_oms()
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
    oms = make_oms()
    order = oms.create_order(intent())
    with pytest.raises(InvalidTransitionError):
        order.transition(OrderStatus.FILLED)  # CREATED -> FILLED illegal


def test_terminal_state_locked():
    oms = make_oms()
    order = oms.create_order(intent())
    order.transition(OrderStatus.REJECTED, reason="risk")
    with pytest.raises(InvalidTransitionError):
        order.transition(OrderStatus.RISK_APPROVED)


def test_overfill_rejected():
    oms = make_oms()
    order = oms.create_order(intent(qty=100))
    for status in (OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTING,
                   OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED):
        order.transition(status)
    with pytest.raises(InvalidTransitionError):
        order.record_fill(200, Decimal("10"))
