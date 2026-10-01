"""Deterministic end-of-day market snapshot calculations."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any


def build_market_snapshot(
    trade_date: date,
    datasets: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    equity_rows = datasets.get("equity_daily", ())
    futures_rows = datasets.get("futures_daily", ())
    equity_returns = [_return(row, "close", "prev_close") for row in equity_rows]

    dominant: list[Mapping[str, Any]] = []
    by_product: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in futures_rows:
        by_product[str(row.get("product") or _product(str(row["symbol"])))] .append(row)
    for product in sorted(by_product):
        dominant.append(
            max(
                by_product[product],
                key=lambda row: (
                    int(row.get("open_interest") or 0),
                    int(row.get("volume") or 0),
                    str(row.get("symbol") or ""),
                ),
            )
        )

    dominant_contracts = [
        {
            "product": str(row.get("product") or _product(str(row["symbol"]))),
            "symbol": str(row["symbol"]),
            "close": float(row["close"]),
            "settlement": float(row["settlement"]),
            "return": _return(row, "settlement", "pre_settlement"),
            "volume": int(row["volume"]),
            "open_interest": int(row["open_interest"]),
        }
        for row in dominant
    ]

    return {
        "schema_version": "1.0",
        "trade_date": trade_date.isoformat(),
        "equity": {
            "symbols": len(equity_rows),
            "advancers": sum(value > 0 for value in equity_returns),
            "decliners": sum(value < 0 for value in equity_returns),
            "unchanged": sum(value == 0 for value in equity_returns),
            "median_return": _median(equity_returns),
            "total_amount": sum(float(row["amount"]) for row in equity_rows),
        },
        "futures": {
            "contracts": len(futures_rows),
            "dominant_contracts": dominant_contracts,
        },
    }


def _return(row: Mapping[str, Any], current: str, previous: str) -> float:
    previous_value = float(row[previous])
    return float(row[current]) / previous_value - 1.0 if previous_value else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _product(symbol: str) -> str:
    match = re.match(r"[A-Za-z]+", symbol)
    return match.group(0) if match else symbol.split(".", 1)[0]