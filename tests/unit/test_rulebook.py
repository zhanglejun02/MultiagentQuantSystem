from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from tests.helpers import ScheduledWeightsStrategy, make_engine_with

from quant_system.backtest.request import BacktestRequest
from quant_system.domain.enums import Board, Exchange, SecurityType
from quant_system.domain.exceptions import MarketRuleViolationError
from quant_system.domain.types import cn_dt

TS = cn_dt(date(2024, 3, 4), 9, 30)
ARGS = (Exchange.SSE, Board.MAIN, SecurityType.STOCK, "600000.SSE", TS)


def test_main_board_10pct_limit(rulebook):
    rules = rulebook.get_rules(*ARGS)
    assert rules.price_limit_ratio == Decimal("0.10")
    assert rules.limit_up(Decimal("10.00")) == Decimal("11.00")
    assert rules.limit_down(Decimal("10.00")) == Decimal("9.00")
    assert rules.lot_size == 100
    assert rules.tick_size == Decimal("0.01")


def test_st_5pct_limit_on_main_board(rulebook):
    rules = rulebook.get_rules(*ARGS, is_st=True)
    assert rules.price_limit_ratio == Decimal("0.05")
    assert rules.limit_up(Decimal("10.00")) == Decimal("10.50")


def test_star_and_chinext_20pct(rulebook):
    star = rulebook.get_rules(Exchange.SSE, Board.STAR, SecurityType.STOCK, "688001.SSE", TS)
    chinext = rulebook.get_rules(Exchange.SZSE, Board.CHINEXT, SecurityType.STOCK, "300001.SZSE", TS)
    assert star.price_limit_ratio == Decimal("0.20")
    assert chinext.price_limit_ratio == Decimal("0.20")


def test_ipo_no_limit_window(rulebook):
    # STAR: 5 no-limit days
    rules = rulebook.get_rules(
        Exchange.SSE, Board.STAR, SecurityType.STOCK, "688002.SSE",
        cn_dt(date(2024, 4, 3), 9, 30), list_date=date(2024, 4, 1),
        listed_trading_days=3,
    )
    assert rules.price_limit_ratio is None
    assert rules.limit_up(Decimal("50")) is None
    later = rulebook.get_rules(
        Exchange.SSE, Board.STAR, SecurityType.STOCK, "688002.SSE",
        cn_dt(date(2024, 4, 10), 9, 30), list_date=date(2024, 4, 1),
        listed_trading_days=6,
    )
    assert later.price_limit_ratio == Decimal("0.20")


def test_main_board_ipo_first_five_trading_days_then_limit_resumes(rulebook):
    fifth = rulebook.get_rules(
        Exchange.SSE, Board.MAIN, SecurityType.STOCK, "603999.SSE",
        cn_dt(date(2024, 4, 8), 9, 30), list_date=date(2024, 4, 1),
        listed_trading_days=5,
    )
    sixth = rulebook.get_rules(
        Exchange.SSE, Board.MAIN, SecurityType.STOCK, "603999.SSE",
        cn_dt(date(2024, 4, 9), 9, 30), list_date=date(2024, 4, 1),
        listed_trading_days=6,
    )
    assert fifth.price_limit_ratio is None
    assert sixth.price_limit_ratio == Decimal("0.10")


def test_main_board_st_rule_version_boundary_2026_07_05_06(rulebook):
    before = rulebook.get_rules(
        Exchange.SSE, Board.MAIN, SecurityType.STOCK, "600000.SSE",
        cn_dt(date(2026, 7, 5), 9, 30), is_st=True,
    )
    after = rulebook.get_rules(
        Exchange.SSE, Board.MAIN, SecurityType.STOCK, "600000.SSE",
        cn_dt(date(2026, 7, 6), 9, 30), is_st=True,
    )
    assert (before.rule_version, before.price_limit_ratio) == (
        "main-st-2023.1", Decimal("0.05")
    )
    assert (after.rule_version, after.price_limit_ratio) == (
        "main-st-2026.2", Decimal("0.10")
    )
    assert "fixture policy" in after.source_metadata["rule_source"]


def test_unmodeled_security_type_fails_closed(rulebook):
    with pytest.raises(MarketRuleViolationError, match="UNSUPPORTED"):
        rulebook.get_rules(
            Exchange.SSE, Board.MAIN, SecurityType.ETF, "510300.SSE", TS,
        )


def test_backtest_rejects_mismatched_rulebook_version(
    provider, rulebook, store, registry
):
    engine = make_engine_with(
        provider, rulebook, store, registry, ScheduledWeightsStrategy({})
    )
    with pytest.raises(MarketRuleViolationError):
        engine.run(BacktestRequest(
            strategy_id="scheduled", dataset_snapshot_id="",
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 3),
            initial_cash=Decimal("100000"), rulebook_version="wrong-version",
        ))


def test_limit_price_rounding_half_up(rulebook):
    rules = rulebook.get_rules(*ARGS)
    # 9.87 * 1.1 = 10.857 -> 10.86
    assert rules.limit_up(Decimal("9.87")) == Decimal("10.86")


def test_fee_schedule_effective_dating(rulebook):
    rules = rulebook.get_rules(*ARGS)
    assert rules.fee_schedule.stamp_tax_sell_rate == Decimal("0.0005")
    with pytest.raises(MarketRuleViolationError):
        rulebook.get_rules(
            Exchange.SSE, Board.MAIN, SecurityType.STOCK, "600000.SSE",
            cn_dt(date(2020, 1, 6), 9, 30),
        )


def test_suspension_flag_carried(rulebook):
    rules = rulebook.get_rules(*ARGS, is_suspended=True)
    assert rules.is_suspended


def test_unknown_board_rejected(rulebook):
    with pytest.raises(MarketRuleViolationError):
        rulebook.get_rules(Exchange.SSE, Board.BSE, SecurityType.STOCK, "830001.BSE", TS)
