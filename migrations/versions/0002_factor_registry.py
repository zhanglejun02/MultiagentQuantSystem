"""Factor registry and promotion history.

Revision ID: 0002_factor_registry
Revises: 0001_initial
Create Date: 2026-09-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_factor_registry"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "factors",
        sa.Column("factor_id", sa.String(64), primary_key=True),
        sa.Column("factor_key", sa.String(100), nullable=False, index=True),
        sa.Column("version", sa.String(30), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(50), nullable=False, index=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("state", sa.String(30), nullable=False, index=True),
        sa.Column("code_ref", sa.String(300), nullable=False),
        sa.Column("data_snapshot_id", sa.String(64), nullable=True),
        sa.Column("validation_artifact_id", sa.String(64), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_factors_key_version", "factors", ["factor_key", "version"], unique=True
    )
    op.create_table(
        "factor_transitions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "factor_id",
            sa.String(64),
            sa.ForeignKey("factors.factor_id"),
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
    op.drop_table("factor_transitions")
    op.drop_index("uq_factors_key_version", table_name="factors")
    op.drop_table("factors")