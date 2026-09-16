"""Experiment / ExperimentRun services persisted in PostgreSQL."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from quant_system.artifacts.envelope import hash_json
from quant_system.domain.enums import ExperimentState
from quant_system.domain.exceptions import QuantSystemError
from quant_system.domain.types import new_id, utc_now
from quant_system.infra.db import session_scope
from quant_system.infra.orm import ExperimentRow, ExperimentRunRow, ExperimentTransitionRow
from quant_system.research.lifecycle import validate_transition


class Experiment(BaseModel):
    experiment_id: str
    name: str
    description: str = ""
    state: ExperimentState
    metadata: dict[str, Any] = {}


class ExperimentRun(BaseModel):
    run_id: str
    experiment_id: str
    code_commit: str
    config: dict[str, Any]
    config_hash: str
    data_snapshot_id: str | None
    input_artifact_ids: list[str]
    output_artifact_ids: list[str]
    random_seed: int
    status: str
    metrics: dict[str, Any] = {}
    logs_uri: str = ""


class ExperimentService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def create(self, name: str, description: str = "", metadata: dict | None = None) -> Experiment:
        exp = Experiment(
            experiment_id=new_id("exp_"), name=name, description=description,
            state=ExperimentState.IDEA, metadata=metadata or {},
        )
        now = utc_now()
        with session_scope(self._sf) as s:
            s.add(ExperimentRow(
                experiment_id=exp.experiment_id, name=name, description=description,
                state=exp.state.value, created_at=now, updated_at=now, meta=exp.metadata,
            ))
        return exp

    def get(self, experiment_id: str) -> Experiment:
        with session_scope(self._sf) as s:
            row = s.get(ExperimentRow, experiment_id)
            if row is None:
                raise QuantSystemError(f"experiment {experiment_id} not found")
            return Experiment(
                experiment_id=row.experiment_id, name=row.name, description=row.description,
                state=ExperimentState(row.state), metadata=row.meta or {},
            )

    def transition(
        self, experiment_id: str, target: ExperimentState, *, reason: str = "", actor: str = "system"
    ) -> Experiment:
        with session_scope(self._sf) as s:
            row = s.get(ExperimentRow, experiment_id)
            if row is None:
                raise QuantSystemError(f"experiment {experiment_id} not found")
            current = ExperimentState(row.state)
            validate_transition(current, target)
            row.state = target.value
            row.updated_at = utc_now()
            s.add(ExperimentTransitionRow(
                experiment_id=experiment_id, from_state=current.value, to_state=target.value,
                reason=reason, actor=actor, at=utc_now(),
            ))
        return self.get(experiment_id)

    def history(self, experiment_id: str) -> list[dict]:
        with session_scope(self._sf) as s:
            rows = s.execute(
                select(ExperimentTransitionRow)
                .where(ExperimentTransitionRow.experiment_id == experiment_id)
                .order_by(ExperimentTransitionRow.id)
            ).scalars().all()
            return [
                {"from": r.from_state, "to": r.to_state, "reason": r.reason,
                 "actor": r.actor, "at": r.at.isoformat()}
                for r in rows
            ]

    def list_all(self) -> list[Experiment]:
        with session_scope(self._sf) as s:
            rows = s.execute(select(ExperimentRow)).scalars().all()
            return [
                Experiment(
                    experiment_id=r.experiment_id, name=r.name, description=r.description,
                    state=ExperimentState(r.state), metadata=r.meta or {},
                )
                for r in rows
            ]

    # -- runs ----------------------------------------------------------------
    def record_run(
        self,
        experiment_id: str,
        *,
        code_commit: str,
        config: dict,
        data_snapshot_id: str | None,
        input_artifact_ids: list[str],
        output_artifact_ids: list[str],
        random_seed: int,
        status: str,
        metrics: dict | None = None,
        logs_uri: str = "",
    ) -> ExperimentRun:
        run = ExperimentRun(
            run_id=new_id("run_"), experiment_id=experiment_id, code_commit=code_commit,
            config=config, config_hash=hash_json(config), data_snapshot_id=data_snapshot_id,
            input_artifact_ids=input_artifact_ids, output_artifact_ids=output_artifact_ids,
            random_seed=random_seed, status=status, metrics=metrics or {}, logs_uri=logs_uri,
        )
        with session_scope(self._sf) as s:
            s.add(ExperimentRunRow(
                run_id=run.run_id, experiment_id=experiment_id, code_commit=code_commit,
                config=config, config_hash=run.config_hash, data_snapshot_id=data_snapshot_id,
                input_artifact_ids=input_artifact_ids, output_artifact_ids=output_artifact_ids,
                random_seed=random_seed, started_at=utc_now(), ended_at=utc_now(),
                status=status, metrics=metrics or {}, logs_uri=logs_uri,
            ))
        return run

    def runs(self, experiment_id: str) -> list[ExperimentRun]:
        with session_scope(self._sf) as s:
            rows = s.execute(
                select(ExperimentRunRow).where(ExperimentRunRow.experiment_id == experiment_id)
            ).scalars().all()
            return [
                ExperimentRun(
                    run_id=r.run_id, experiment_id=r.experiment_id, code_commit=r.code_commit,
                    config=r.config or {}, config_hash=r.config_hash,
                    data_snapshot_id=r.data_snapshot_id,
                    input_artifact_ids=r.input_artifact_ids or [],
                    output_artifact_ids=r.output_artifact_ids or [],
                    random_seed=r.random_seed, status=r.status, metrics=r.metrics or {},
                    logs_uri=r.logs_uri,
                )
                for r in rows
            ]
