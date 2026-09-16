from __future__ import annotations

from datetime import date
from decimal import Decimal

from quant_system.backtest.exchange import ExchangeSimulator
from quant_system.backtest.fees import ConfigurableFeeModel
from quant_system.backtest.fill import NextOpenFillModel, VolumeParticipationFillModel
from quant_system.backtest.slippage import FixedBpsSlippageModel, ZeroSlippageModel
from quant_system.domain.enums import Board, Exchange, OrderSide, OrderType, SecurityType
from quant_system.domain.models import Bar, CorporateActionRecord, OrderIntent
from quant_system.domain.types import bar_available_at, cn_dt
from quant_system.execution.oms import OrderManagementSystem
from quant_system.rules.reference_price import CorporateActionReferencePriceService

D = date(2024, 3, 4)


def make_bar(open_: str = "10.00", volume: int = 1_000_000) -> Bar:
    return Bar(
        symbol="600000.SSE", trade_date=D, open=Decimal(open_), high=Decimal("10.50"),
        low=Decimal("9.80"), close=Decimal("10.20"), prev_close=Decimal("10.00"),
        volume=volume, amount=Decimal("10200000"), available_at=bar_available_at(D),
    )


def make_order(qty: int = 10000, side: OrderSide = OrderSide.BUY):
    oms = OrderManagementSystem()
    return oms.create_order(OrderIntent(
        client_order_id="c1", strategy_id="s", target_id="t", account_id="a",
        symbol="600000.SSE", side=side, order_type=OrderType.MARKET,
        quantity=qty, reference_price=Decimal("10.00"),
    ))


def rules_for(rulebook):
    return rulebook.get_rules(
        Exchange.SSE, Board.MAIN, SecurityType.STOCK, "600000.SSE", cn_dt(D, 9, 30)
    )


def test_fee_min_commission_and_sell_only_stamp_tax(rulebook):
    model = ConfigurableFeeModel()
    schedule = rules_for(rulebook).fee_schedule
    buy = model.compute(schedule, OrderSide.BUY, 100, Decimal("10.00"))  # notional 1000
    assert buy.commission == Decimal("5")  # min commission binds
    assert buy.stamp_tax == Decimal("0")
    sell = model.compute(schedule, OrderSide.SELL, 100000, Decimal("10.00"))  # notional 1e6
    assert sell.commission == Decimal("250.00")
    assert sell.stamp_tax == Decimal("500.00")
    assert sell.transfer_fee == Decimal("10.00")


def test_fixed_bps_slippage_direction():
    slip = FixedBpsSlippageModel(Decimal("10"))  # 10 bps
    assert slip.adjust(Decimal("10.00"), OrderSide.BUY) == Decimal("10.01")
    assert slip.adjust(Decimal("10.00"), OrderSide.SELL) == Decimal("9.99")


def test_next_open_fill_clamps_to_limit(rulebook):
    rules = rules_for(rulebook)
    model = NextOpenFillModel(FixedBpsSlippageModel(Decimal("200")))  # absurd 2%
    bar = make_bar(open_="10.95")  # 10.95 * 1.02 > 11.00 limit
    proposal = model.propose(make_order(), bar, rules, liquidity_volume=1_000_000)
    assert proposal is not None
    assert proposal.price == Decimal("11.00")  # clamped to limit-up


def test_volume_participation_caps_and_lot_aligns(rulebook):
    rules = rules_for(rulebook)
    base = NextOpenFillModel(ZeroSlippageModel())
    model = VolumeParticipationFillModel(base, Decimal("0.05"))
    bar = make_bar(volume=9_999_999)
    proposal = model.propose(make_order(qty=10000), bar, rules, liquidity_volume=5000)
    assert proposal is not None
    assert proposal.quantity == 200


def test_zero_participation_volume_no_fill(rulebook):
    rules = rules_for(rulebook)
    model = VolumeParticipationFillModel(NextOpenFillModel(ZeroSlippageModel()), Decimal("0.05"))
    bar = make_bar(volume=9_999_999)
    assert model.propose(make_order(), bar, rules, liquidity_volume=1000) is None


def test_ex_dividend_reference_price_drives_price_band(rulebook):
    rules = rules_for(rulebook)
    action = CorporateActionRecord(
        symbol="600000.SSE", kind="CASH_DIVIDEND", ex_date=D,
        cash_per_share=Decimal("1"), available_at=cn_dt(D, 9, 0),
    )
    reference = CorporateActionReferencePriceService().calculate(
        Decimal("10"), [action], rule_version=rules.rule_version,
        security_status="NORMAL",
    )
    bar = make_bar(open_="9.90")
    outcome = ExchangeSimulator(NextOpenFillModel(ZeroSlippageModel())).execute(
        make_order(), bar, rules, liquidity_volume=1_000_000,
        reference_price=reference,
    )
    assert reference == Decimal("9")
    assert outcome.reason == "LIMIT_UP_NO_FILL"


def test_official_reference_price_takes_priority_over_derived_value(rulebook):
    rules = rules_for(rulebook)
    reference = CorporateActionReferencePriceService().calculate(
        Decimal("10"), [], rule_version=rules.rule_version,
        security_status="ST", available_reference_price=Decimal("9.73"),
    )
    assert reference == Decimal("9.73")
