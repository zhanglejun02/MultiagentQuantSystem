from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from quant_system.infra.db import Base


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_type: Mapped[str] = mapped_column(String(40), index=True)
    schema_version: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    producer: Mapped[str] = mapped_column(String(200))
    data_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    experiment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    code_commit: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    uri: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class ArtifactParentRow(Base):
    __tablename__ = "artifact_parents"

    artifact_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("artifacts.artifact_id"), primary_key=True
    )
    parent_artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    __table_args__ = (Index("ix_artifact_parents_parent", "parent_artifact_id"),)


class ExperimentRow(Base):
    __tablename__ = "experiments"

    experiment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String(30), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class ExperimentTransitionRow(Base):
    __tablename__ = "experiment_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("experiments.experiment_id"), index=True
    )
    from_state: Mapped[str] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(100), default="system")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExperimentRunRow(Base):
    __tablename__ = "experiment_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("experiments.experiment_id"), index=True
    )
    code_commit: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64))
    data_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    output_artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    random_seed: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    logs_uri: Mapped[str] = mapped_column(Text, default="")


class FactorRow(Base):
    __tablename__ = "factors"

    factor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    factor_key: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(30), index=True)
    code_ref: Mapped[str] = mapped_column(String(300))
    data_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    validation_artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("uq_factors_key_version", "factor_key", "version", unique=True),)


class FactorTransitionRow(Base):
    __tablename__ = "factor_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    factor_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("factors.factor_id"), index=True
    )
    from_state: Mapped[str] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(100), default="system")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FactorRunRow(Base):
    __tablename__ = "factor_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    expression: Mapped[str] = mapped_column(Text)
    universe_id: Mapped[str] = mapped_column(String(100), index=True)
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    target: Mapped[str] = mapped_column(String(50))
    horizon: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), index=True)
    inputs: Mapped[list] = mapped_column(JSON, default=list)
    complexity: Mapped[int] = mapped_column(Integer)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    ic_series: Mapped[list] = mapped_column(JSON, default=list)
    quantile_returns: Mapped[dict] = mapped_column(JSON, default=dict)
    decay: Mapped[list] = mapped_column(JSON, default=list)
    yearly_ic: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StrategyRow(Base):
    __tablename__ = "strategies"

    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_key: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), index=True)
    package_artifact_id: Mapped[str] = mapped_column(String(64))
    factor_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_strategies_key_version", "strategy_key", "version", unique=True),
    )


class StrategyTransitionRow(Base):
    __tablename__ = "strategy_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("strategies.strategy_id"), index=True
    )
    from_state: Mapped[str] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(100), default="system")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FuturesSimulationRow(Base):
    __tablename__ = "futures_simulations"

    simulation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    mode: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    initial_balance: Mapped[str] = mapped_column(String(50))
    current_trading_day: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_event_seq: Mapped[int] = mapped_column(Integer)
    last_event_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FuturesEventRow(Base):
    __tablename__ = "futures_events"

    simulation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("futures_simulations.simulation_id"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_version: Mapped[str] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)
    previous_event_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)

    __table_args__ = (Index("ix_futures_events_simulation_seq", "simulation_id", "seq"),)
