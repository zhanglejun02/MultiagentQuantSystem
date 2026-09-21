"""Deterministic daily futures fixture used by the local paper gateway."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from quant_system.domain.types import cn_dt
from quant_system.futures.domain import FuturesDailyBar

_SERIES: dict[str, tuple[Decimal, ...]] = {
    "rb2701.SHFE": tuple(
        Decimal(value) for value in (3480, 3492, 3475, 3510, 3532, 3520, 3555, 3570)
    ),
    "m2701.DCE": tuple(
        Decimal(value) for value in (2860, 2876, 2890, 2882, 2910, 2935, 2922, 2950)
    ),
    "MA701.CZCE": tuple(
        Decimal(value) for value in (2510, 2524, 2508, 2540, 2562, 2550, 2580, 2596)
    ),
}


class FixtureFuturesData:
    def __init__(self, start: date = date(2026, 9, 7)) -> None:
        self._bars: dict[str, list[FuturesDailyBar]] = {}
        for symbol, closes in _SERIES.items():
            bars: list[FuturesDailyBar] = []
            trading_day = start
            previous = closes[0] - Decimal("8")
            for close in closes:
                while trading_day.weekday() >= 5:
                    trading_day += timedelta(days=1)
                open_price = previous + Decimal("2")
                bars.append(
                    FuturesDailyBar(
                        symbol=symbol,
                        trading_day=trading_day,
                        open=open_price,
                        high=max(open_price, close) + Decimal("12"),
                        low=min(open_price, close) - Decimal("12"),
                        close=close,
                        settlement=close + Decimal("1"),
                        pre_settlement=previous,
                        volume=100_000,
                        open_interest=250_000,
                        available_at=cn_dt(trading_day, 17),
                    )
                )
                previous = close + Decimal("1")
                trading_day += timedelta(days=1)
            self._bars[symbol] = bars

    @property
    def trading_days(self) -> tuple[date, ...]:
        first = next(iter(self._bars.values()))
        return tuple(bar.trading_day for bar in first)

    def bars_for_day(self, trading_day: date) -> dict[str, FuturesDailyBar]:
        return {
            symbol: bar
            for symbol, bars in self._bars.items()
            for bar in bars
            if bar.trading_day == trading_day
        }

    def initial_marks(self) -> dict[str, Decimal]:
        return {symbol: bars[0].pre_settlement for symbol, bars in self._bars.items()}