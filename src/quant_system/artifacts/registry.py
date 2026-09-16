"""ArtifactRegistry: PostgreSQL-backed metadata registry with lineage."""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from quant_system.artifacts.envelope import ArtifactEnvelope
from quant_system.domain.enums import ArtifactStatus, ArtifactType
from quant_system.domain.exceptions import ArtifactNotFoundError, ImmutabilityError
from quant_system.infra.db import session_scope
from quant_system.infra.orm import ArtifactParentRow, ArtifactRow


def _to_row(env: ArtifactEnvelope) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=env.artifact_id,
        artifact_type=env.artifact_type.value,
        schema_version=env.schema_version,
        created_at=env.created_at,
        producer=env.producer,
        data_snapshot_id=env.data_snapshot_id,
        experiment_id=env.experiment_id,
        code_commit=env.code_commit,
        config_hash=env.config_hash,
        content_hash=env.content_hash,
        uri=env.uri,
        status=env.status.value,
        meta=env.metadata,
    )


def _to_envelope(row: ArtifactRow, parents: tuple[str, ...]) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=row.artifact_id,
        artifact_type=ArtifactType(row.artifact_type),
        schema_version=row.schema_version,
        created_at=row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC),
        producer=row.producer,
        parent_artifact_ids=parents,
        data_snapshot_id=row.data_snapshot_id,
        experiment_id=row.experiment_id,
        code_commit=row.code_commit,
        config_hash=row.config_hash,
        content_hash=row.content_hash,
        uri=row.uri,
        metadata=row.meta or {},
        status=ArtifactStatus(row.status),
    )


class ArtifactRegistry:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def register(self, env: ArtifactEnvelope) -> ArtifactEnvelope:
        with session_scope(self._sf) as s:
            if s.get(ArtifactRow, env.artifact_id) is not None:
                raise ImmutabilityError(f"artifact {env.artifact_id} already registered")
            for pid in env.parent_artifact_ids:
                if s.get(ArtifactRow, pid) is None:
                    raise ArtifactNotFoundError(f"parent artifact {pid} not registered")
            s.add(_to_row(env))
            for pid in env.parent_artifact_ids:
                s.add(ArtifactParentRow(artifact_id=env.artifact_id, parent_artifact_id=pid))
        return env

    def get(self, artifact_id: str) -> ArtifactEnvelope:
        with session_scope(self._sf) as s:
            row = s.get(ArtifactRow, artifact_id)
            if row is None:
                raise ArtifactNotFoundError(artifact_id)
            parents = self._parent_ids(s, artifact_id)
            return _to_envelope(row, parents)

    @staticmethod
    def _parent_ids(s: Session, artifact_id: str) -> tuple[str, ...]:
        rows = s.execute(
            select(ArtifactParentRow.parent_artifact_id).where(
                ArtifactParentRow.artifact_id == artifact_id
            )
        ).scalars().all()
        return tuple(sorted(rows))

    def parents(self, artifact_id: str) -> list[ArtifactEnvelope]:
        return [self.get(pid) for pid in self.get(artifact_id).parent_artifact_ids]

    def children(self, artifact_id: str) -> list[ArtifactEnvelope]:
        with session_scope(self._sf) as s:
            ids = s.execute(
                select(ArtifactParentRow.artifact_id).where(
                    ArtifactParentRow.parent_artifact_id == artifact_id
                )
            ).scalars().all()
        return [self.get(i) for i in sorted(ids)]

    def find_by_type(self, artifact_type: ArtifactType) -> list[ArtifactEnvelope]:
        with session_scope(self._sf) as s:
            ids = s.execute(
                select(ArtifactRow.artifact_id).where(ArtifactRow.artifact_type == artifact_type.value)
            ).scalars().all()
        return [self.get(i) for i in ids]

    def find_by_experiment(self, experiment_id: str) -> list[ArtifactEnvelope]:
        with session_scope(self._sf) as s:
            ids = s.execute(
                select(ArtifactRow.artifact_id).where(ArtifactRow.experiment_id == experiment_id)
            ).scalars().all()
        return [self.get(i) for i in ids]

    def list_all(self) -> list[ArtifactEnvelope]:
        with session_scope(self._sf) as s:
            ids = s.execute(select(ArtifactRow.artifact_id)).scalars().all()
        return [self.get(i) for i in ids]
