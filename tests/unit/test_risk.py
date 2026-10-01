from __future__ import annotations

from datetime import date
from decimal import Decimal

from quant_system.backtest.account.ledger import AccountLedger
from quant_system.domain.enums import OrderSide, OrderType
from quant_system.domain.models import OrderIntent
from quant_system.risk.pretrade import (
    AvailableCashRule,
    AvailablePositionRule,
    DuplicateOrderRule,
    MaxActiveOrdersRule,
    MaxDailyTurnoverRule,
    MaxOrderValueRule,
    MaxPositionWeightRule,
    PreTradeRiskEngine,
    PriceDeviationRule,
    RiskContext,
    StaleMarketDataRule,
)
from quant_system.risk.runtime import CircuitBreakerConfig, KillSwitch, TradingRuntime

D = date(2024, 3, 4)


def make_intent(**kw) -> OrderIntent:
    defaults = dict(
        client_order_id="c1", strategy_id="s", target_id="t", account_id="a",
        symbol="600000.SSE", side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=1000, reference_price=Decimal("10.00"),
    )
    defaults.update(kw)
    return OrderIntent(**defaults)


def make_ctx(cash: str = "100000", **kw) -> RiskContext:
    ledger = AccountLedger("t", Decimal(cash))
    ledger.start_of_day(D)
    defaults = dict(
        ledger=ledger, trade_date=D, last_close={"600000.SSE": Decimal("10.00")},
        market_data_date=D,
    )
    defaults.update(kw)
    return RiskContext(**defaults)


def test_max_order_value():
    rule = MaxOrderValueRule(Decimal("5000"))
    assert rule.check(make_intent(), make_ctx()) is not None
    assert rule.check(make_intent(quantity=100), make_ctx()) is None


def test_max_position_weight():
    rule = MaxPositionWeightRule(Decimal("0.05"))
    assert rule.check(make_intent(), make_ctx()) is not None  # 10% projected


def test_max_daily_turnover():
    rule = MaxDailyTurnoverRule(Decimal("0.05"))
    ctx = make_ctx(today_submitted_value=Decimal("0"))
    assert rule.check(make_intent(), ctx) is not None


def test_max_active_orders():
    rule = MaxActiveOrdersRule(0)
    assert rule.check(make_intent(), make_ctx()) is not None


def test_duplicate_order():
    rule = DuplicateOrderRule()
    ctx = make_ctx(seen_client_order_ids={"c1"})
    assert rule.check(make_intent(), ctx) is not None
    assert rule.check(make_intent(client_order_id="c2"), ctx) is None


def test_price_deviation():
    rule = PriceDeviationRule(Decimal("0.02"))
    bad = make_intent(order_type=OrderType.LIMIT, limit_price=Decimal("11.00"))
    ok = make_intent(order_type=OrderType.LIMIT, limit_price=Decimal("10.10"))
    assert rule.check(bad, make_ctx()) is not None
    assert rule.check(ok, make_ctx()) is None


def test_stale_market_data():
    rule = StaleMarketDataRule(3)
    ctx = make_ctx(market_data_date=date(2024, 2, 20))
    assert rule.check(make_intent(), ctx) is not None


def test_available_cash():
    rule = AvailableCashRule()
    assert rule.check(make_intent(quantity=100000), make_ctx("1000")) is not None
    assert rule.check(make_intent(quantity=100), make_ctx()) is None


def test_available_position():
    rule = AvailablePositionRule()
    sell = make_intent(side=OrderSide.SELL, quantity=100)
    assert rule.check(sell, make_ctx()) is not None  # nothing held


def test_kill_switch_rejects_everything():
    ks = KillSwitch()
    ks.engage("test")
    engine = PreTradeRiskEngine([], TradingRuntime(ks))
    violations = engine.evaluate(make_intent(), make_ctx())
    assert violations and violations[0].rule == "KillSwitch"


def test_rule_exception_fails_closed():
    class BrokenRule:
        name = "BrokenRule"

        def check(self, intent, ctx):
            raise RuntimeError("boom")

    engine = PreTradeRiskEngine([BrokenRule()], TradingRuntime(KillSwitch()))
    violations = engine.evaluate(make_intent(), make_ctx())
    assert violations and "rejecting" in violations[0].message


def test_circuit_breakers_engage_kill_switch():
    ks = KillSwitch()
    rt = TradingRuntime(ks, CircuitBreakerConfig(max_daily_loss_pct=Decimal("0.05")))
    rt.check_circuit_breakers(daily_pnl_pct=Decimal("-0.06"), drawdown_pct=Decimal("0"))
    assert ks.engaged
    assert not rt.can_trade()


def test_safe_mode_on_disconnect():
    rt = TradingRuntime(KillSwitch())
    rt.check_circuit_breakers(daily_pnl_pct=Decimal("0"), drawdown_pct=Decimal("0"),
                              broker_connected=False)
    assert rt.state.value == "SAFE_MODE"
    rt.exit_safe_mode("operator-1")
    assert rt.can_trade()
