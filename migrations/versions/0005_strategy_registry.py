"""Add strategy registry and deployment lifecycle.

Revision ID: 0005_strategy_registry
Revises: 0004_futures_simulation
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_strategy_registry"
down_revision = "0004_futures_simulation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE factors SET state = 'PROPOSED' WHERE state = 'DRAFT'")
    op.execute("UPDATE factors SET state = 'VALIDATED' WHERE state = 'APPROVED'")
    op.execute("UPDATE factors SET state = 'STRATEGY_ELIGIBLE' WHERE state = 'ACTIVE'")
    op.create_table(
        "strategies",
        sa.Column("strategy_id", sa.String(64), primary_key=True),
        sa.Column("strategy_key", sa.String(100), nullable=False, index=True),
        sa.Column("version", sa.String(30), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, index=True),
        sa.Column("package_artifact_id", sa.String(64), nullable=False),
        sa.Column("factor_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_strategies_key_version",
        "strategies",
        ["strategy_key", "version"],
        unique=True,
    )
    op.create_table(
        "strategy_transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "strategy_id",
            sa.String(64),
            sa.ForeignKey("strategies.strategy_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("from_state", sa.String(30), nullable=False),
        sa.Column("to_state", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("strategy_transitions")
    op.drop_index("uq_strategies_key_version", table_name="strategies")
    op.drop_table("strategies")