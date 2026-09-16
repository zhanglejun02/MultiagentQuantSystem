"""Initial schema: artifacts, lineage, experiments, runs, transitions.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column("artifact_type", sa.String(40), nullable=False, index=True),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producer", sa.String(200), nullable=False),
        sa.Column("data_snapshot_id", sa.String(64), nullable=True, index=True),
        sa.Column("experiment_id", sa.String(64), nullable=True, index=True),
        sa.Column("code_commit", sa.String(64), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
    )
    op.create_table(
        "artifact_parents",
        sa.Column("artifact_id", sa.String(64),
                  sa.ForeignKey("artifacts.artifact_id"), primary_key=True),
        sa.Column("parent_artifact_id", sa.String(64), primary_key=True),
    )
    op.create_index("ix_artifact_parents_parent", "artifact_parents", ["parent_artifact_id"])
    op.create_table(
        "experiments",
        sa.Column("experiment_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
    )
    op.create_table(
        "experiment_transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("experiment_id", sa.String(64),
                  sa.ForeignKey("experiments.experiment_id"), nullable=False, index=True),
        sa.Column("from_state", sa.String(30), nullable=False),
        sa.Column("to_state", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "experiment_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("experiment_id", sa.String(64),
                  sa.ForeignKey("experiments.experiment_id"), nullable=False, index=True),
        sa.Column("code_commit", sa.String(64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("data_snapshot_id", sa.String(64), nullable=True),
        sa.Column("input_artifact_ids", sa.JSON(), nullable=False),
        sa.Column("output_artifact_ids", sa.JSON(), nullable=False),
        sa.Column("random_seed", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("logs_uri", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("experiment_runs")
    op.drop_table("experiment_transitions")
    op.drop_table("experiments")
    op.drop_index("ix_artifact_parents_parent", table_name="artifact_parents")
    op.drop_table("artifact_parents")
    op.drop_table("artifacts")
