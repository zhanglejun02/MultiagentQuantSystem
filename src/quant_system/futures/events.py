"""Tamper-evident event envelope for futures simulation."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_system.domain.types import require_aware


class FuturesEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "1.0"
    simulation_id: str
    seq: int
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    previous_event_hash: str = ""
    event_hash: str = ""


def event_hash(event: FuturesEvent) -> str:
    payload = event.model_dump(mode="json", exclude={"event_hash"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode()).hexdigest()


def stamp_event(event: FuturesEvent) -> FuturesEvent:
    require_aware(event.occurred_at, "occurred_at")
    if event.schema_version != "1.0":
        raise ValueError(f"unsupported futures event schema {event.schema_version}")
    return event.model_copy(update={"event_hash": event_hash(event)})


def verify_event_chain(events: list[FuturesEvent]) -> str:
    previous = ""
    for expected_seq, event in enumerate(events, start=1):
        if event.seq != expected_seq:
            raise ValueError(f"event sequence gap: expected {expected_seq}, got {event.seq}")
        if event.previous_event_hash != previous:
            raise ValueError(f"event hash link mismatch at seq {event.seq}")
        if event.event_hash != event_hash(event):
            raise ValueError(f"event hash mismatch at seq {event.seq}")
        previous = event.event_hash
    return previous