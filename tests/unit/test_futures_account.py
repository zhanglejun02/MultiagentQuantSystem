from datetime import date
from decimal import Decimal

from quant_system.domain.types import cn_dt
from quant_system.futures.account import FuturesAccountLedger
from quant_system.futures.domain import (
    Direction,
    FuturesContract,
    FuturesOrderIntent,
    FuturesOrderType,
    Offset,
)
from quant_system.futures.oms import FuturesOMS
from quant_system.futures.risk import FuturesPreTradeRisk
from quant_system.risk.runtime import KillSwitch, TradingRuntime


def test_open_long_and_daily_settlement_preserve_account_equity() -> None:
    contract = FuturesContract(
        symbol="rb2701.SHFE",
        name="螺纹钢 2505",
        exchange="SHFE",
        product="rb",
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        long_margin_rate=Decimal("0.12"),
        short_margin_rate=Decimal("0.12"),
    )
    ledger = FuturesAccountLedger(initial_balance=Decimal("100000"))

    ledger.apply_trade(
        contract=contract,
        direction=Direction.LONG,
        offset=Offset.OPEN,
        quantity=2,
        price=Decimal("3500"),
        fee=Decimal("4"),
        trading_day=date(2026, 9, 16),
    )

    opened = ledger.snapshot({contract.symbol: Decimal("3500")})
    assert opened.balance == Decimal("99996")
    assert opened.margin == Decimal("8400")
    assert opened.available == Decimal("91596")
    assert opened.equity == Decimal("99996")

    ledger.settle(
        trading_day=date(2026, 9, 16),
        settlements={contract.symbol: Decimal("3520")},
        contracts={contract.symbol: contract},
    )

    settled = ledger.snapshot({contract.symbol: Decimal("3520")})
    position = ledger.position(contract.symbol)
    assert settled.balance == Decimal("100396")
    assert settled.margin == Decimal("8448")
    assert settled.available == Decimal("91948")
    assert settled.equity == Decimal("100396")
    assert position.long_today == 0
    assert position.long_yesterday == 2
    assert position.long_settlement_price == Decimal("3520")


def test_close_today_cannot_consume_yesterday_position() -> None:
    contract = FuturesContract(
        symbol="m2701.DCE",
        name="豆粕 2505",
        exchange="DCE",
        product="m",
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        long_margin_rate=Decimal("0.10"),
        short_margin_rate=Decimal("0.10"),
    )
    ledger = FuturesAccountLedger(Decimal("100000"))
    ledger.apply_trade(
        contract=contract,
        direction=Direction.SHORT,
        offset=Offset.OPEN,
        quantity=3,
        price=Decimal("2900"),
        fee=Decimal("0"),
        trading_day=date(2026, 9, 16),
    )
    ledger.settle(
        trading_day=date(2026, 9, 16),
        settlements={contract.symbol: Decimal("2890")},
        contracts={contract.symbol: contract},
    )

    assert ledger.closable_quantity(contract.symbol, Direction.SHORT, Offset.CLOSE_TODAY) == 0
    assert ledger.closable_quantity(contract.symbol, Direction.SHORT, Offset.CLOSE_YESTERDAY) == 3


def test_oms_is_idempotent_and_risk_rejects_unfunded_open() -> None:
    contract = FuturesContract(
        symbol="rb2701.SHFE",
        name="螺纹钢 2505",
        exchange="SHFE",
        product="rb",
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        long_margin_rate=Decimal("0.12"),
        short_margin_rate=Decimal("0.12"),
    )
    intent = FuturesOrderIntent(
        client_order_id="same-command",
        symbol=contract.symbol,
        direction=Direction.LONG,
        offset=Offset.OPEN,
        quantity=20,
        order_type=FuturesOrderType.MARKET,
        created_at=cn_dt(date(2026, 9, 16), 16),
    )
    oms = FuturesOMS()
    assert oms.create(intent).order_id == oms.create(intent).order_id
    assert len(oms.all()) == 1

    decision = FuturesPreTradeRisk(TradingRuntime(KillSwitch())).evaluate(
        intent=intent,
        contract=contract,
        ledger=FuturesAccountLedger(Decimal("10000")),
        mark=Decimal("3500"),
        marks={contract.symbol: Decimal("3500")},
        active_orders=0,
        margin_call=False,
    )
    assert decision.approved is False
    assert decision.reason == "insufficient available funds"