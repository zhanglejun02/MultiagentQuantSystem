from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from quant_system.backtest.account.ledger import AccountLedger, CorporateAction
from quant_system.domain.enums import OrderSide
from quant_system.domain.exceptions import (
    InsufficientCashError,
    InsufficientPositionError,
    LedgerInvariantError,
)
from quant_system.domain.models import FillRecord
from quant_system.domain.types import cn_dt

D1, D2, D3 = date(2024, 3, 4), date(2024, 3, 5), date(2024, 3, 6)


def fill(symbol: str, side: OrderSide, qty: int, price: str, d: date,
         commission: str = "5", stamp: str = "0", transfer: str = "0.10") -> FillRecord:
    return FillRecord(
        fill_id=f"f-{symbol}-{d}-{side}", order_id="o1", symbol=symbol, side=side,
        quantity=qty, price=Decimal(price), commission=Decimal(commission),
        stamp_tax=Decimal(stamp), transfer_fee=Decimal(transfer),
        trade_date=d, filled_at=cn_dt(d, 9, 30),
    )


def make_ledger(cash: str = "100000") -> AccountLedger:
    ledger = AccountLedger("test", Decimal(cash))
    ledger.start_of_day(D1)
    return ledger


def test_buy_updates_cash_position_and_fees():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    assert ledger.cash == Decimal("100000") - Decimal("10000") - Decimal("5.10")
    pos = ledger.positions["600000.SSE"]
    assert pos.total == 1000
    assert pos.available == 0  # T+1: not sellable same day
    assert ledger.fees_paid == Decimal("5.10")


def test_t_plus_1_sell_same_day_rejected_next_day_ok():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    with pytest.raises(InsufficientPositionError):
        ledger.apply_fill(fill("600000.SSE", OrderSide.SELL, 1000, "10.50", D1))
    ledger.start_of_day(D2)
    assert ledger.positions["600000.SSE"].available == 1000
    ledger.apply_fill(fill("600000.SSE", OrderSide.SELL, 1000, "10.50", D2, stamp="5.25"))
    assert ledger.positions["600000.SSE"].total == 0


def test_realized_pnl_includes_fees():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    ledger.start_of_day(D2)
    avg_cost = ledger.positions["600000.SSE"].avg_cost  # includes buy fees
    ledger.apply_fill(fill("600000.SSE", OrderSide.SELL, 1000, "11.00", D2, stamp="5.50"))
    expected = (Decimal("11.00") - avg_cost) * 1000 - Decimal("5") - Decimal("5.50") - Decimal("0.10")
    assert ledger.realized_pnl == expected


def test_insufficient_cash_rejected():
    ledger = make_ledger("1000")
    with pytest.raises(InsufficientCashError):
        ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))


def test_sell_more_than_held_rejected():
    ledger = make_ledger()
    with pytest.raises(InsufficientPositionError):
        ledger.apply_fill(fill("600000.SSE", OrderSide.SELL, 100, "10.00", D1))


def test_freeze_unfreeze_cash_net_zero():
    ledger = make_ledger()
    ledger.freeze_cash_for_order("o1", Decimal("50000"))
    assert ledger.cash == Decimal("50000")
    assert ledger.frozen_cash == Decimal("50000")
    ledger.check_invariants()
    ledger.unfreeze_cash_for_order("o1")
    assert ledger.cash == Decimal("100000")
    assert ledger.frozen_cash == Decimal("0")
    ledger.check_invariants()


def test_buy_consumes_frozen_cash():
    ledger = make_ledger()
    ledger.freeze_cash_for_order("o1", Decimal("20000"))
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1), order_id="o1")
    ledger.unfreeze_cash_for_order("o1")
    assert ledger.frozen_cash == Decimal("0")
    assert ledger.cash == Decimal("100000") - Decimal("10005.10")
    ledger.check_invariants()


def test_nav_and_mark_to_market():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    ledger.mark_to_market({"600000.SSE": Decimal("12.00")})
    assert ledger.market_value == Decimal("12000")
    assert ledger.nav == ledger.cash + Decimal("12000")
    assert ledger.unrealized_pnl == (Decimal("12.00") - ledger.positions["600000.SSE"].avg_cost) * 1000


def test_cash_dividend_corporate_action():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    before = ledger.cash
    before_cost = ledger.positions["600000.SSE"].avg_cost
    ledger.apply_corporate_action(
        CorporateAction(symbol="600000.SSE", kind="CASH_DIVIDEND",
                        ex_date=D2, cash_per_share=Decimal("0.5"))
    )
    assert ledger.cash == before + Decimal("500")
    assert ledger.positions["600000.SSE"].avg_cost == before_cost - Decimal("0.5")
    ledger.check_invariants()


def test_ex_date_price_drop_is_offset_by_cash_dividend():
    ledger = make_ledger()
    ledger.apply_fill(
        fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1, commission="0", transfer="0")
    )
    ledger.mark_to_market({"600000.SSE": Decimal("10.00")}, price_date=D1)
    before_nav = ledger.nav
    ledger.apply_corporate_action(
        CorporateAction(
            symbol="600000.SSE", kind="CASH_DIVIDEND", ex_date=D2,
            cash_per_share=Decimal("0.50"),
        )
    )
    ledger.mark_to_market({"600000.SSE": Decimal("9.50")}, price_date=D2)
    assert ledger.nav == before_nav
    assert ledger.positions["600000.SSE"].unrealized_pnl == Decimal("0")


def test_suspended_position_keeps_last_price_with_staleness_metadata():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10", D1))
    ledger.mark_to_market({"600000.SSE": Decimal("10")}, price_date=D1)
    ledger.start_of_day(D3)
    ledger.mark_to_market({}, price_date=D3, suspended_symbols={"600000.SSE"})
    snapshot = ledger.snapshot()["positions"]["600000.SSE"]
    assert snapshot["last_price"] == "10"
    assert snapshot["price_as_of"] == D1.isoformat()
    assert snapshot["stale_price_age"] == 2
    assert snapshot["is_suspended"] is True


def test_split_adjusts_pending_settlement_before_t_plus_one_release():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    ledger.apply_corporate_action(
        CorporateAction(
            symbol="600000.SSE", kind="STOCK_SPLIT", ex_date=D2,
            split_ratio=Decimal("1.5"),
        )
    )
    pos = ledger.positions["600000.SSE"]
    assert (pos.total, pos.available, pos.frozen_for_sell) == (1500, 0, 0)
    ledger.start_of_day(D2)
    assert pos.available == 1500
    assert pos.avg_cost == Decimal("10005.10") / Decimal(1500)
    ledger.check_invariants()


def test_split_adjusts_available_and_frozen_sell_quantities():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    ledger.start_of_day(D2)
    ledger.freeze_shares_for_order("sell-1", "600000.SSE", 400)
    ledger.apply_corporate_action(
        CorporateAction(
            symbol="600000.SSE", kind="BONUS_SHARE", ex_date=D2,
            split_ratio=Decimal("1.5"),
        )
    )
    pos = ledger.positions["600000.SSE"]
    assert (pos.total, pos.available, pos.frozen_for_sell) == (1500, 900, 600)
    ledger.unfreeze_shares_for_order("sell-1")
    assert (pos.available, pos.frozen_for_sell) == (1500, 0)
    ledger.check_invariants()


def test_invariant_catches_tampering():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    ledger.cash += Decimal("1")  # simulate corruption
    with pytest.raises(LedgerInvariantError):
        ledger.check_invariants()


def test_every_balance_change_explainable_from_entries():
    ledger = make_ledger()
    ledger.apply_fill(fill("600000.SSE", OrderSide.BUY, 1000, "10.00", D1))
    ledger.start_of_day(D2)
    ledger.apply_fill(fill("600000.SSE", OrderSide.SELL, 500, "10.20", D2, stamp="2.55"))
    flow = sum((e.cash_delta for e in ledger.entries), Decimal("0"))
    assert ledger.initial_cash + flow == ledger.cash + ledger.frozen_cash
