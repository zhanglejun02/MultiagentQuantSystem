"""Add tamper-evident futures paper simulations.

Revision ID: 0004_futures_simulation
Revises: 0003_factor_runs
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_futures_simulation"
down_revision = "0003_factor_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "futures_simulations",
        sa.Column("simulation_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False, index=True),
        sa.Column("status", sa.String(30), nullable=False, index=True),
        sa.Column("initial_balance", sa.String(50), nullable=False),
        sa.Column("current_trading_day", sa.String(10), nullable=True),
        sa.Column("last_event_seq", sa.Integer(), nullable=False),
        sa.Column("last_event_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "futures_events",
        sa.Column(
            "simulation_id",
            sa.String(64),
            sa.ForeignKey("futures_simulations.simulation_id"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False, index=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("previous_event_hash", sa.String(64), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_index(
        "ix_futures_events_simulation_seq",
        "futures_events",
        ["simulation_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_futures_events_simulation_seq", table_name="futures_events")
    op.drop_table("futures_events")
    op.drop_table("futures_simulations")