"""Consistent DSL fields for factor research and strategy decisions."""

from datetime import date
from decimal import Decimal

from quant_system.domain.models import Bar
from quant_system.factors.dsl import FieldData


def bars_to_fields(
    bars: list[Bar],
    adjusted_closes: dict[tuple[str, date], Decimal] | None = None,
) -> FieldData:
    fields: FieldData = {
        name: {}
        for name in (
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "return_1d",
        )
    }
    previous: dict[str, Decimal] = {}
    for bar in sorted(bars, key=lambda item: (item.trade_date, item.symbol)):
        close = adjusted_closes[(bar.symbol, bar.trade_date)] if adjusted_closes is not None else bar.close
        factor = close / bar.close
        reference = (
            previous.get(bar.symbol, bar.prev_close * factor)
            if adjusted_closes is not None
            else bar.prev_close
        )
        values = {
            "open": float(bar.open * factor),
            "high": float(bar.high * factor),
            "low": float(bar.low * factor),
            "close": float(close),
            "volume": float(bar.volume),
            "amount": float(bar.amount),
            "return_1d": float(close / reference - 1) if reference else 0.0,
        }
        for name, value in values.items():
            fields[name].setdefault(bar.trade_date, {})[bar.symbol] = value
        previous[bar.symbol] = close
    return fields
