from datetime import date
from decimal import Decimal

import pytest

from quant_system.domain.models import Bar
from quant_system.domain.types import bar_available_at
from quant_system.factors.fields import bars_to_fields


def test_adjusted_fields_remove_dividend_gap_without_changing_execution_bars():
    days = [date(2025, 1, 2), date(2025, 1, 3)]
    bars = [
        Bar(
            symbol="600036.SSE",
            trade_date=day,
            open=Decimal(price),
            high=Decimal(price),
            low=Decimal(price),
            close=Decimal(price),
            prev_close=Decimal("10"),
            volume=100,
            amount=Decimal("1000"),
            available_at=bar_available_at(day),
        )
        for day, price in zip(days, ("10", "9"), strict=True)
    ]
    adjusted = {(bar.symbol, bar.trade_date): Decimal("10") for bar in bars}
    raw = bars_to_fields(bars)
    fields = bars_to_fields(bars, adjusted)
    assert raw["return_1d"][days[1]]["600036.SSE"] == pytest.approx(-0.1)
    assert fields["return_1d"][days[1]]["600036.SSE"] == 0
    assert fields["open"][days[1]]["600036.SSE"] == 10
    assert fields["volume"] == raw["volume"]
    assert bars[1].close == Decimal("9")
