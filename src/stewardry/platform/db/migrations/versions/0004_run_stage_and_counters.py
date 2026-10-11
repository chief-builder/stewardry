"""Run stage and per-run counters (Milestone 1, step S5).

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_counters",
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=96), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.CheckConstraint("value >= 0", name=op.f("ck_run_counters_value_non_negative")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_run_counters_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("run_id", "name", name=op.f("pk_run_counters")),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stage", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("stage")

    op.drop_table("run_counters")
