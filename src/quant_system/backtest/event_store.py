"""Append-only EventStore with JSONL persistence for replay."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from quant_system.backtest.events import EVENT_CLASSES, AnyEvent


class EventStore:
    def __init__(self) -> None:
        self._events: list[AnyEvent] = []
        self._seq = 0

    def append(self, event: AnyEvent) -> AnyEvent:
        self._seq += 1
        previous = self._events[-1].event_hash if self._events else ""
        stamped = event.model_copy(
            update={"seq": self._seq, "previous_event_hash": previous, "event_hash": ""}
        )
        stamped = stamped.model_copy(update={"event_hash": self._hash(stamped)})
        self._events.append(stamped)
        return stamped

    @staticmethod
    def _hash(event: AnyEvent) -> str:
        payload = event.model_dump(mode="json", exclude={"event_hash"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sha256(canonical.encode()).hexdigest()

    @staticmethod
    def load_event(raw: dict) -> AnyEvent:
        event_type = raw.get("event_type")
        if not isinstance(event_type, str):
            raise ValueError("event_type must be a string")
        cls = EVENT_CLASSES.get(event_type)
        if cls is None:
            raise ValueError(f"unsupported event type {event_type}")
        return cls(**raw)

    @property
    def final_event_hash(self) -> str:
        return self._events[-1].event_hash if self._events else ""

    @property
    def events(self) -> list[AnyEvent]:
        return list(self._events)

    def save_jsonl(self, path: str) -> None:
        with Path(path).open("w") as f:
            for e in self._events:
                f.write(json.dumps(e.model_dump(mode="json"), sort_keys=True, ensure_ascii=False))
                f.write("\n")

    @staticmethod
    def load_jsonl(path: str, *, expected_final_hash: str | None = None) -> list[AnyEvent]:
        events: list[AnyEvent] = []
        previous = ""
        with Path(path).open() as f:
            for line in f:
                if not line.strip():
                    continue
                raw = json.loads(line)
                if raw.get("schema_version") != "1.0":
                    raise ValueError(f"unsupported event schema {raw.get('schema_version')}")
                event = EventStore.load_event(raw)
                expected_seq = len(events) + 1
                if event.seq != expected_seq:
                    raise ValueError(f"event sequence gap: expected {expected_seq}, got {event.seq}")
                if event.previous_event_hash != previous:
                    raise ValueError(f"event hash link mismatch at seq {event.seq}")
                if event.event_hash != EventStore._hash(event):
                    raise ValueError(f"event hash mismatch at seq {event.seq}")
                events.append(event)
                previous = event.event_hash
        if expected_final_hash is not None and previous != expected_final_hash:
            raise ValueError("event log final hash does not match BacktestResultArtifact")
        return events
