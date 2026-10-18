"""Typed requests, source semantics and point-in-time observation facts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_system.domain.types import require_aware


class SeriesDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str
    unit: str
    observation_frequency: str
    release_frequency: str
    source_url: str
    stale_after_days: int
    limitation: str


SERIES: dict[str, SeriesDefinition] = {
    "DGS10": SeriesDefinition(
        title="US Treasury 10-year nominal constant maturity yield",
        unit="percent",
        observation_frequency="daily",
        release_frequency="business_daily",
        source_url="https://fred.stlouisfed.org/series/DGS10",
        stale_after_days=7,
        limitation="A nominal yield, not a realized return or an inflation forecast.",
    ),
    "DFII10": SeriesDefinition(
        title="US Treasury 10-year inflation-indexed real constant maturity yield",
        unit="percent",
        observation_frequency="daily",
        release_frequency="business_daily",
        source_url="https://fred.stlouisfed.org/series/DFII10",
        stale_after_days=7,
        limitation="Real yields may be negative; this is not nominal yield minus current CPI.",
    ),
    "DTWEXBGS": SeriesDefinition(
        title="Nominal broad trade-weighted US dollar index",
        unit="index_jan_2006_100",
        observation_frequency="daily",
        release_frequency="weekly",
        source_url="https://fred.stlouisfed.org/series/DTWEXBGS",
        stale_after_days=14,
        limitation="Broad trade-weighted dollar index, not ICE DXY.",
    ),
    "DEXCHUS": SeriesDefinition(
        title="Chinese yuan per one US dollar, New York noon buying rate",
        unit="CNY_per_USD",
        observation_frequency="daily",
        release_frequency="weekly",
        source_url="https://fred.stlouisfed.org/series/DEXCHUS",
        stale_after_days=14,
        limitation="New York noon rate, not PBOC central parity; daily observations are released weekly.",
    ),
    "CN_GOLD_RESERVES": SeriesDefinition(
        title="China official gold reserves, quantity",
        unit="ten_thousand_troy_ounces",
        observation_frequency="monthly",
        release_frequency="monthly",
        source_url="https://www.safe.gov.cn/safe/whcb/index.html",
        stale_after_days=75,
        limitation="China's reserve stock, not global central bank demand or gross purchases.",
    ),
    "CN_GOLD_RESERVES_USD": SeriesDefinition(
        title="China official gold reserves, US dollar valuation",
        unit="hundred_million_USD",
        observation_frequency="monthly",
        release_frequency="monthly",
        source_url="https://www.safe.gov.cn/safe/whcb/index.html",
        stale_after_days=75,
        limitation="Valuation changes include gold price and exchange-rate effects, not just quantity changes.",
    ),
}

DEFAULT_SERIES = ["DGS10", "DFII10", "DTWEXBGS", "DEXCHUS", "CN_GOLD_RESERVES"]


class MacroCollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_ids: list[str] = Field(default_factory=lambda: list(DEFAULT_SERIES), min_length=1, max_length=6)
    start_date: date = date(2023, 1, 1)
    end_date: date | None = None

    @field_validator("series_ids")
    @classmethod
    def known_series(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(value not in SERIES for value in values):
            raise ValueError("series_ids must be unique registered macro series IDs")
        return values

    @model_validator(mode="after")
    def ordered_dates(self) -> MacroCollectRequest:
        if self.end_date is not None and self.start_date > self.end_date:
            raise ValueError("start_date must not exceed end_date")
        return self


class MacroAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)


class MacroObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    series_id: str
    observation_date: date
    period: str
    value: float
    unit: str
    source: Literal["FRED", "SAFE"]
    source_url: str
    published_at: datetime | None = None
    ingested_at: datetime
    available_at: datetime
    vintage: dict[str, str] | None = None
    raw_content_hash: str = ""

    @field_validator("published_at", "ingested_at", "available_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None

    @model_validator(mode="after")
    def observation_semantics(self) -> MacroObservation:
        definition = SERIES.get(self.series_id)
        if definition is None or self.unit != definition.unit:
            raise ValueError("unknown series or incompatible unit")
        if self.series_id in {"DTWEXBGS", "DEXCHUS"} and self.value <= 0:
            raise ValueError("FX and index observations must be positive")
        if self.series_id.startswith("CN_GOLD_RESERVES") and self.value < 0:
            raise ValueError("reserve quantities and valuations must be nonnegative")
        if self.available_at < self.ingested_at:
            raise ValueError("current downloads cannot be available before ingestion")
        return self


class SeriesCoverage(BaseModel):
    count: int
    first_observation: date | None = None
    last_observation: date | None = None
    observation_frequency: str
    release_frequency: str
    stale: bool
    status: Literal["AVAILABLE", "MISSING", "STALE"]


class MacroCollection(BaseModel):
    snapshot_id: str
    raw_snapshot_id: str
    as_of: datetime
    available_at: datetime
    coverage: dict[str, SeriesCoverage]
    missing: dict[str, str]
    summary: dict[str, Any]
    quality_errors: list[str] = Field(default_factory=list)

    @field_validator("as_of", "available_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        return require_aware(value)


class MacroSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["macro-snapshot-v1"] = "macro-snapshot-v1"
    raw_snapshot_id: str
    as_of: datetime
    available_at: datetime
    request: MacroCollectRequest
    resolved_end_date: date
    series_ids: list[str]
    observations: list[MacroObservation]
    missing: dict[str, str]
    quality_errors: list[str] = Field(default_factory=list)

    @field_validator("as_of", "available_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        return require_aware(value)


class MacroQualityError(ValueError):
    def __init__(self, errors: list[str], *, raw_snapshot_id: str | None = None) -> None:
        self.quality_errors = list(errors)
        self.raw_snapshot_id = raw_snapshot_id
        super().__init__("macro collection has zero usable observations: " + "; ".join(errors))
