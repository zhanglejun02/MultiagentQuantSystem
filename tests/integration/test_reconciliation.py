from __future__ import annotations

from datetime import date
from decimal import Decimal

from quant_system.backtest.account.ledger import AccountLedger
from quant_system.domain.enums import MismatchKind, OrderSide, TradingRuntimeState
from quant_system.domain.models import FillRecord
from quant_system.domain.types import cn_dt
from quant_system.execution.oms import OrderManagementSystem
from quant_system.execution.paper import PaperBrokerGateway
from quant_system.execution.reconciliation import ReconciliationService
from quant_system.risk.runtime import KillSwitch, TradingRuntime

D = date(2024, 3, 4)


def setup_world():
    ledger = AccountLedger("t", Decimal("100000"))
    ledger.start_of_day(D)
    broker = PaperBrokerGateway(Decimal("100000"))
    runtime = TradingRuntime(KillSwitch())
    oms = OrderManagementSystem(runtime)
    svc = ReconciliationService(broker, ledger, oms, runtime)
    return ledger, broker, oms, runtime, svc


def apply_both(ledger, broker):
    fill = FillRecord(
        fill_id="f1", order_id="o1", symbol="600000.SSE", side=OrderSide.BUY,
        quantity=1000, price=Decimal("10.00"), commission=Decimal("5"),
        stamp_tax=Decimal("0"), transfer_fee=Decimal("0.10"),
        trade_date=D, filled_at=cn_dt(D, 9, 30),
    )
    ledger.apply_fill(fill)
    broker.apply_fill(fill)


def test_reconciliation_passes_when_consistent():
    ledger, broker, oms, runtime, svc = setup_world()
    apply_both(ledger, broker)
    assert svc.reconcile() == []
    assert runtime.state == TradingRuntimeState.RUNNING


def test_cash_mismatch_triggers_safe_mode():
    ledger, broker, oms, runtime, svc = setup_world()
    apply_both(ledger, broker)
    broker.force_cash(Decimal("50000"))
    mismatches = svc.reconcile()
    assert any(m.kind == MismatchKind.CASH_MISMATCH for m in mismatches)
    assert runtime.state == TradingRuntimeState.SAFE_MODE


def test_unknown_position_detected():
    ledger, broker, oms, runtime, svc = setup_world()
    broker.force_position("999999.SSE", 500)
    mismatches = svc.reconcile()
    kinds = {m.kind for m in mismatches}
    assert MismatchKind.UNKNOWN_POSITION in kinds
    assert runtime.state == TradingRuntimeState.SAFE_MODE


def test_position_quantity_mismatch_detected():
    ledger, broker, oms, runtime, svc = setup_world()
    apply_both(ledger, broker)
    broker.force_position("600000.SSE", 900)
    mismatches = svc.reconcile()
    assert any(m.kind == MismatchKind.POSITION_MISMATCH for m in mismatches)


def test_trade_count_mismatch_detected():
    ledger, broker, oms, runtime, svc = setup_world()
    apply_both(ledger, broker)
    broker._trades.append(broker.trades()[0])  # duplicate at broker side
    mismatches = svc.reconcile()
    assert any(m.kind == MismatchKind.TRADE_MISMATCH for m in mismatches)
