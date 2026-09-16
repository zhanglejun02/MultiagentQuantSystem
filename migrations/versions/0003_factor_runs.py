"""Independent factor evaluation runs.

Revision ID: 0003_factor_runs
Revises: 0002_factor_registry
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_factor_runs"
down_revision = "0002_factor_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factor_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.Column("universe_id", sa.String(100), nullable=False, index=True),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("target", sa.String(50), nullable=False),
        sa.Column("horizon", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("complexity", sa.Integer(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("ic_series", sa.JSON(), nullable=False),
        sa.Column("quantile_returns", sa.JSON(), nullable=False),
        sa.Column("decay", sa.JSON(), nullable=False),
        sa.Column("yearly_ic", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("factor_runs")