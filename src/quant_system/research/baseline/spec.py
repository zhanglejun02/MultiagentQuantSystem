from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResearchSegment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    start: date
    end: date


class BaselineSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    data_start: date
    data_end: date
    symbols: tuple[str, ...]
    benchmark: str
    benchmark_source: Literal["tushare", "tencent"] = "tushare"
    reference_source: Literal["tushare", "eastmoney"] = "tushare"
    adjustment_source: Literal["tushare", "cash_actions"] = "tushare"
    dividend_source: Literal["tushare", "eastmoney"] = "tushare"
    lookback_days: int = Field(default=20, ge=1)
    top_k: int = Field(default=3, ge=3)
    initial_cash: Decimal = Field(default=Decimal("1000000"), gt=0)
    segments: tuple[ResearchSegment, ...]
    limitations: tuple[str, ...]

    @model_validator(mode="after")
    def check_protocol(self) -> "BaselineSpec":
        if tuple(segment.name for segment in self.segments) != ("development", "validation", "holdout"):
            raise ValueError("segments must be development, validation, holdout in order")
        if len(set(self.symbols)) != len(self.symbols) or len(self.symbols) < self.top_k:
            raise ValueError("basket must contain unique symbols and at least top_k members")
        if any(
            not symbol.startswith(("600", "601", "603", "605")) or not symbol.endswith(".SH")
            for symbol in self.symbols
        ):
            raise ValueError("V1 baseline supports only SSE main-board stocks")
        previous_end = self.data_start
        for segment in self.segments:
            if not previous_end < segment.start <= segment.end <= self.data_end:
                raise ValueError("segments must be ordered, disjoint and inside the data range")
            previous_end = segment.end
        if self.data_start + timedelta(days=self.lookback_days * 3) > self.segments[0].start:
            raise ValueError("insufficient pre-development warmup range")
        if not self.limitations:
            raise ValueError("research limitations must be declared")
        return self

    @classmethod
    def load(cls, path: Path) -> "BaselineSpec":
        return cls.model_validate(yaml.safe_load(path.read_text()))
