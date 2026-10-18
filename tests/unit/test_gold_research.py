from datetime import UTC, date, datetime, timedelta

from quant_system.gold.analysis import StrategyParameters, evaluate_gold_strategy
from quant_system.gold.models import PricePoint, Quote


def _quote(symbol: str = "518880", *, rising: bool = True) -> Quote:
    return Quote(
        symbol=symbol, name="Gold ETF", kind="etf", unit="CNY/share", source="test",
        source_url="https://example.com/", fetched_at=datetime(2026, 10, 16, tzinfo=UTC),
        points=[PricePoint(date=date(2026, 1, 1) + timedelta(days=index),
                           open=10 + index / 100 if rising else 20 - index / 100,
                           close=10 + index / 100 if rising else 20 - index / 100)
                for index in range(180)],
    )


def test_gold_strategy_has_mechanical_rules_and_no_live_eligibility() -> None:
    result = evaluate_gold_strategy(_quote(), StrategyParameters(), max_weight=0.15)
    assert result["signal"] == "ENTER"
    assert result["target_weight"] <= 0.15
    assert result["deployment_eligible"] is False
    assert result["diagnostic"]["execution"] == "NEXT_OPEN"
    assert result["diagnostic"]["trades"][0]["execution_date"] > result["diagnostic"]["trades"][0]["signal_date"]


def test_gold_strategy_waits_on_declining_or_insufficient_history() -> None:
    result = evaluate_gold_strategy(_quote(rising=False), StrategyParameters(), max_weight=0.1)
    assert result["signal"] == "EXIT_OR_WAIT"
    assert result["target_weight"] == 0
    quote = _quote().model_copy(update={"points": _quote().points[:5]})
    insufficient = evaluate_gold_strategy(quote, StrategyParameters(), max_weight=0.1)
    assert insufficient["signal"] == "INSUFFICIENT_DATA"
    assert insufficient["diagnostic"]["status"] == "UNAVAILABLE"
    assert insufficient["double_cost"]["status"] == "UNAVAILABLE"
    assert insufficient["limitations"]