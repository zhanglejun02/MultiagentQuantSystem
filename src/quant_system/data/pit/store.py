"""PITDataStore port + generic in-memory implementation.

Every record carries the four PIT timestamps. A query may NEVER return a
record whose available_at > as_of.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from quant_system.domain.types import require_aware


class PITRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str                      # e.g. symbol or entity id
    field: str
    value: Any
    event_time: datetime          # when the fact happened in the world
    effective_time: datetime      # when it takes effect economically
    available_at: datetime        # when a consumer could first know it
    ingested_at: datetime         # when our system stored it

    @field_validator("event_time", "effective_time", "available_at", "ingested_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return require_aware(v)


class PITDataStore(Protocol):
    def append(self, record: PITRecord) -> None: ...

    def query(self, key: str, field: str, *, as_of: datetime) -> list[PITRecord]: ...

    def latest(self, key: str, field: str, *, as_of: datetime) -> PITRecord | None: ...


class InMemoryPITStore:
    def __init__(self) -> None:
        self._records: list[PITRecord] = []

    def append(self, record: PITRecord) -> None:
        self._records.append(record)

    def query(self, key: str, field: str, *, as_of: datetime) -> list[PITRecord]:
        require_aware(as_of, "as_of")
        return sorted(
            (
                r
                for r in self._records
                if r.key == key and r.field == field and r.available_at <= as_of
            ),
            key=lambda r: r.available_at,
        )

    def latest(self, key: str, field: str, *, as_of: datetime) -> PITRecord | None:
        rows = self.query(key, field, as_of=as_of)
        return rows[-1] if rows else None
