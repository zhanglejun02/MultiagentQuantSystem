from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PricePoint(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    date: date
    close: Decimal = Field(gt=0)
    open: Decimal | None = Field(default=None, gt=0)
    high: Decimal | None = Field(default=None, gt=0)
    low: Decimal | None = Field(default=None, gt=0)
    volume: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_ohlc(self) -> PricePoint:
        if self.high is not None and self.low is not None:
            if self.high < max(self.close, self.open or self.close, self.low):
                raise ValueError("invalid high price")
            if self.low > min(self.close, self.open or self.close, self.high):
                raise ValueError("invalid low price")
        return self


class Quote(BaseModel):
    symbol: str
    name: str
    kind: Literal["spot", "etf", "futures"]
    unit: Literal["CNY/g", "CNY/share"]
    source: str
    source_url: str
    fetched_at: datetime
    points: list[PricePoint] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates(self) -> Quote:
        dates = [point.date for point in self.points]
        if dates != sorted(set(dates)):
            raise ValueError("quote dates must be unique and increasing")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must include timezone")
        return self

    def is_stale(self, today: date) -> bool:
        return (today - self.points[-1].date).days > 4


ETF_NAMES = {
    "518880": "华安黄金ETF",
    "518800": "国泰黄金ETF",
    "159934": "易方达黄金ETF",
    "159937": "博时黄金ETF",
}


class Holding(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    symbol: Literal["518880", "518800", "159934", "159937"]
    quantity: Decimal = Field(ge=0, max_digits=16, decimal_places=4)
    average_cost: Decimal = Field(ge=0, max_digits=16, decimal_places=6)


class Allocation(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    other_assets: Decimal = Field(default=Decimal(0), ge=0, max_digits=20, decimal_places=2)
    target_weight: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    holdings: list[Holding] = Field(default_factory=list, max_length=4)
    watchlist: list[Literal["518880", "518800", "159934", "159937"]] = Field(
        default=["518880", "518800", "159934", "159937"]
    )
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_symbols(self) -> Allocation:
        if len({holding.symbol for holding in self.holdings}) != len(self.holdings):
            raise ValueError("duplicate holdings")
        if len(set(self.watchlist)) != len(self.watchlist):
            raise ValueError("duplicate watchlist symbols")
        return self


def allocation_summary(allocation: Allocation, prices: dict[str, Decimal]) -> dict[str, Any]:
    active = [holding for holding in allocation.holdings if holding.quantity > 0]
    missing = [holding.symbol for holding in active if holding.symbol not in prices]
    value = sum(
        (holding.quantity * prices[holding.symbol] for holding in active if holding.symbol in prices),
        Decimal(0),
    )
    cost = sum((holding.quantity * holding.average_cost for holding in active), Decimal(0))
    total = value + allocation.other_assets
    weight = value / total if total > 0 and active and not missing else None
    return {
        "gold_value": None if missing else value,
        "total_assets": None if missing else total,
        "weight": weight,
        "target_weight": allocation.target_weight,
        "deviation": weight - allocation.target_weight if weight is not None else None,
        "unrealized_pnl": value - cost if active and not missing else None,
        "unpriced_symbols": missing,
        "has_holdings": bool(active),
    }
