"""Persistent registry for immutable factor versions and their promotion lifecycle."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from quant_system.domain.enums import FactorState
from quant_system.domain.exceptions import InvalidTransitionError, QuantSystemError
from quant_system.domain.types import new_id, utc_now
from quant_system.infra.db import session_scope
from quant_system.infra.orm import FactorRow, FactorTransitionRow


class Factor(BaseModel):
    factor_id: str
    factor_key: str
    version: str
    name: str
    category: str
    description: str = ""
    owner: str
    state: FactorState
    code_ref: str
    data_snapshot_id: str | None = None
    validation_artifact_id: str | None = None
    parameters: dict[str, Any] = {}
    metrics: dict[str, Any] = {}
    created_at: str
    updated_at: str


ALLOWED_FACTOR_TRANSITIONS: dict[FactorState, frozenset[FactorState]] = {
    FactorState.PROPOSED: frozenset({FactorState.SCREENED, FactorState.DEPRECATED}),
    FactorState.SCREENED: frozenset({FactorState.CANDIDATE, FactorState.DEPRECATED}),
    FactorState.CANDIDATE: frozenset({FactorState.VALIDATING, FactorState.DEPRECATED}),
    FactorState.VALIDATING: frozenset({FactorState.VALIDATED, FactorState.DEPRECATED}),
    FactorState.VALIDATED: frozenset({FactorState.STRATEGY_ELIGIBLE, FactorState.DEPRECATED}),
    FactorState.STRATEGY_ELIGIBLE: frozenset({FactorState.DEPRECATED}),
    FactorState.DEPRECATED: frozenset(),
}


class FactorRegistry:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    @staticmethod
    def _to_factor(row: FactorRow) -> Factor:
        return Factor(
            factor_id=row.factor_id,
            factor_key=row.factor_key,
            version=row.version,
            name=row.name,
            category=row.category,
            description=row.description,
            owner=row.owner,
            state=FactorState(row.state),
            code_ref=row.code_ref,
            data_snapshot_id=row.data_snapshot_id,
            validation_artifact_id=row.validation_artifact_id,
            parameters=row.parameters or {},
            metrics=row.metrics or {},
            created_at=row.created_at.isoformat(),
            updated_at=row.updated_at.isoformat(),
        )

    def create(
        self,
        *,
        factor_key: str,
        version: str,
        name: str,
        category: str,
        owner: str,
        code_ref: str,
        description: str = "",
        data_snapshot_id: str | None = None,
        validation_artifact_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> Factor:
        now = utc_now()
        with session_scope(self._sf) as session:
            exists = session.execute(
                select(FactorRow).where(
                    FactorRow.factor_key == factor_key,
                    FactorRow.version == version,
                )
            ).scalar_one_or_none()
            if exists is not None:
                raise QuantSystemError(f"factor {factor_key}@{version} already exists")
            row = FactorRow(
                factor_id=new_id("fac_"),
                factor_key=factor_key,
                version=version,
                name=name,
                category=category,
                description=description,
                owner=owner,
                state=FactorState.PROPOSED.value,
                code_ref=code_ref,
                data_snapshot_id=data_snapshot_id,
                validation_artifact_id=validation_artifact_id,
                parameters=parameters or {},
                metrics=metrics or {},
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._to_factor(row)

    def get(self, factor_id: str) -> Factor:
        with session_scope(self._sf) as session:
            row = session.get(FactorRow, factor_id)
            if row is None:
                raise QuantSystemError(f"factor {factor_id} not found")
            return self._to_factor(row)

    def list_all(self) -> list[Factor]:
        with session_scope(self._sf) as session:
            rows = session.execute(
                select(FactorRow).order_by(FactorRow.updated_at.desc())
            ).scalars().all()
            return [self._to_factor(row) for row in rows]

    def transition(
        self,
        factor_id: str,
        target: FactorState,
        *,
        reason: str = "",
        actor: str = "system",
    ) -> Factor:
        if target != FactorState.DEPRECATED:
            raise InvalidTransitionError(
                "positive factor promotion is only allowed through FactorPromotionService"
            )
        return self._transition(factor_id, target, reason=reason, actor=actor)

    def _transition(
        self,
        factor_id: str,
        target: FactorState,
        *,
        reason: str,
        actor: str,
        validation_artifact_id: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> Factor:
        with session_scope(self._sf) as session:
            row = session.get(FactorRow, factor_id)
            if row is None:
                raise QuantSystemError(f"factor {factor_id} not found")
            current = FactorState(row.state)
            if target not in ALLOWED_FACTOR_TRANSITIONS[current]:
                raise InvalidTransitionError(
                    f"factor transition {current} -> {target} not allowed"
                )
            now = utc_now()
            row.state = target.value
            row.updated_at = now
            if validation_artifact_id is not None:
                row.validation_artifact_id = validation_artifact_id
            if metrics is not None:
                row.metrics = metrics
            session.add(FactorTransitionRow(
                factor_id=factor_id,
                from_state=current.value,
                to_state=target.value,
                reason=reason,
                actor=actor,
                at=now,
            ))
        return self.get(factor_id)

    def history(self, factor_id: str) -> list[dict[str, Any]]:
        self.get(factor_id)
        with session_scope(self._sf) as session:
            rows = session.execute(
                select(FactorTransitionRow)
                .where(FactorTransitionRow.factor_id == factor_id)
                .order_by(FactorTransitionRow.id)
            ).scalars().all()
            return [
                {
                    "from": row.from_state,
                    "to": row.to_state,
                    "reason": row.reason,
                    "actor": row.actor,
                    "at": row.at.isoformat(),
                }
                for row in rows
            ]