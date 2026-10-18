from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quant_system.gold.ingestion import parse_etf, parse_spot
from quant_system.gold.models import Allocation, Holding, PricePoint, Quote, allocation_summary


def test_gold_quote_freshness_and_invalid_prices() -> None:
    quote = Quote(
        symbol="Au99.99",
        name="Gold",
        kind="spot",
        unit="CNY/g",
        source="SGE",
        source_url="https://www.sge.com.cn/",
        points=[PricePoint(date=date(2026, 10, 9), close=Decimal("900"))],
        fetched_at=datetime(2026, 10, 15, tzinfo=UTC),
    )
    assert quote.is_stale(date(2026, 10, 15)) is True
    assert quote.is_stale(date(2026, 10, 9)) is False
    with pytest.raises(ValidationError):
        PricePoint(date=date(2026, 10, 9), close=Decimal("NaN"))


def test_gold_allocation_does_not_double_count_assets() -> None:
    allocation = Allocation(
        other_assets=Decimal("90000"),
        target_weight=Decimal("0.10"),
        holdings=[Holding(symbol="518880", quantity=Decimal("1000"), average_cost=Decimal("9"))],
    )
    summary = allocation_summary(allocation, {"518880": Decimal("10")})
    assert summary["gold_value"] == Decimal("10000")
    assert summary["total_assets"] == Decimal("100000")
    assert summary["weight"] == Decimal("0.10")
    assert summary["unrealized_pnl"] == Decimal("1000")


def test_missing_gold_quote_keeps_valuation_unknown() -> None:
    allocation = Allocation(holdings=[Holding(symbol="518880", quantity=1, average_cost=9)])
    summary = allocation_summary(allocation, {})
    assert summary["gold_value"] is None
    assert summary["weight"] is None
    assert summary["unpriced_symbols"] == ["518880"]


def test_allocation_rejects_duplicate_holdings() -> None:
    with pytest.raises(ValidationError):
        Allocation(holdings=[Holding(symbol="518880", quantity=1, average_cost=9)] * 2)


def test_default_allocation_watchlists_are_independent() -> None:
    first = Allocation()
    first.watchlist.clear()
    assert len(Allocation().watchlist) == 4


def test_gold_parsers_use_correct_ohlc_and_exclude_intraday() -> None:
    now = datetime(2026, 10, 15, tzinfo=UTC)
    spot = parse_spot(
        {"time": [["2026-10-14", 900, 905, 895, 910], ["2026-10-15", 910, 915, 905, 920]]},
        date(2026, 10, 14),
        now,
    )
    assert len(spot.points) == 1
    assert spot.points[0].low == Decimal(895)
    assert spot.points[0].high == Decimal(910)
    etf = parse_etf(
        {"code": 0, "data": {"sh518880": {"day": [["2026-10-14", "9.0", "9.1", "9.2", "8.9", "1234"]]}}},
        "518880",
        date(2026, 10, 14),
        now,
    )
    assert etf.points[0].high == Decimal("9.2")
    assert etf.points[0].low == Decimal("8.9")
    assert etf.points[0].volume is None


def test_gold_parser_rejects_empty_invalid_and_duplicate_series() -> None:
    now = datetime(2026, 10, 15, tzinfo=UTC)
    with pytest.raises(ValidationError):
        parse_spot({"time": []}, date(2026, 10, 14), now)
    with pytest.raises(ValidationError):
        parse_spot({"time": [["2026-10-14", 900, 905, 910, 895]]}, date(2026, 10, 14), now)
    with pytest.raises(ValidationError):
        parse_spot({"time": [["2026-10-14", 900, 905, 895, 910]] * 2}, date(2026, 10, 14), now)
