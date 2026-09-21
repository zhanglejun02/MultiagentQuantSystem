"""Transactional event and snapshot repository for futures simulations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from quant_system.futures.events import FuturesEvent, stamp_event, verify_event_chain
from quant_system.infra.db import session_scope
from quant_system.infra.orm import FuturesEventRow, FuturesSimulationRow


class FuturesSimulationRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        simulation_id: str,
        name: str,
        initial_balance: str,
        state: dict[str, Any],
        occurred_at: datetime,
    ) -> FuturesEvent:
        event = stamp_event(
            FuturesEvent(
                simulation_id=simulation_id,
                seq=1,
                event_type="SIMULATION_CREATED",
                occurred_at=occurred_at,
                payload={"state": state},
            )
        )
        with session_scope(self._session_factory) as session:
            session.add(
                FuturesSimulationRow(
                    simulation_id=simulation_id,
                    name=name,
                    mode="PAPER",
                    status=str(state["status"]),
                    initial_balance=initial_balance,
                    current_trading_day=state.get("current_trading_day"),
                    last_event_seq=event.seq,
                    last_event_hash=event.event_hash,
                    snapshot=state,
                    created_at=occurred_at,
                    updated_at=occurred_at,
                )
            )
            session.add(self._event_row(event))
        return event

    def append(
        self,
        *,
        simulation_id: str,
        expected_seq: int,
        event_type: str,
        state: dict[str, Any],
        occurred_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> FuturesEvent:
        with session_scope(self._session_factory) as session:
            row = session.get(FuturesSimulationRow, simulation_id)
            if row is None:
                raise KeyError(f"unknown futures simulation {simulation_id}")
            if row.last_event_seq != expected_seq:
                raise RuntimeError("futures simulation changed concurrently")
            event = stamp_event(
                FuturesEvent(
                    simulation_id=simulation_id,
                    seq=expected_seq + 1,
                    event_type=event_type,
                    occurred_at=occurred_at,
                    payload={"details": details or {}, "state": state},
                    previous_event_hash=row.last_event_hash,
                )
            )
            row.status = str(state["status"])
            row.current_trading_day = state.get("current_trading_day")
            row.last_event_seq = event.seq
            row.last_event_hash = event.event_hash
            row.snapshot = state
            row.updated_at = occurred_at
            session.add(self._event_row(event))
        return event

    def get_state(self, simulation_id: str) -> tuple[dict[str, Any], int, str]:
        with session_scope(self._session_factory) as session:
            row = session.get(FuturesSimulationRow, simulation_id)
            if row is None:
                raise KeyError(f"unknown futures simulation {simulation_id}")
            return dict(row.snapshot), row.last_event_seq, row.last_event_hash

    def list_summaries(self) -> list[dict[str, Any]]:
        with session_scope(self._session_factory) as session:
            rows = session.scalars(
                select(FuturesSimulationRow).order_by(FuturesSimulationRow.created_at.desc())
            ).all()
            return [
                {
                    "simulation_id": row.simulation_id,
                    "name": row.name,
                    "mode": row.mode,
                    "status": row.status,
                    "initial_balance": row.initial_balance,
                    "current_trading_day": row.current_trading_day,
                    "last_event_seq": row.last_event_seq,
                    "last_event_hash": row.last_event_hash,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]

    def events(self, simulation_id: str) -> list[FuturesEvent]:
        with session_scope(self._session_factory) as session:
            rows = session.scalars(
                select(FuturesEventRow)
                .where(FuturesEventRow.simulation_id == simulation_id)
                .order_by(FuturesEventRow.seq)
            ).all()
            if not rows:
                if session.get(FuturesSimulationRow, simulation_id) is None:
                    raise KeyError(f"unknown futures simulation {simulation_id}")
                return []
            events = [
                FuturesEvent(
                    schema_version=row.schema_version,
                    simulation_id=row.simulation_id,
                    seq=row.seq,
                    event_type=row.event_type,
                    occurred_at=(
                        row.occurred_at
                        if row.occurred_at.tzinfo is not None
                        else row.occurred_at.replace(tzinfo=UTC)
                    ),
                    payload=row.payload,
                    previous_event_hash=row.previous_event_hash,
                    event_hash=row.event_hash,
                )
                for row in rows
            ]
        verify_event_chain(events)
        return events

    @staticmethod
    def _event_row(event: FuturesEvent) -> FuturesEventRow:
        return FuturesEventRow(
            simulation_id=event.simulation_id,
            seq=event.seq,
            schema_version=event.schema_version,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            payload=event.payload,
            previous_event_hash=event.previous_event_hash,
            event_hash=event.event_hash,
        )