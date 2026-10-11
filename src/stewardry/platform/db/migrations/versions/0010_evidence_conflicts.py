"""Evidence conflicts shared across tasks (Milestone 1, step S13 fix).

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_conflicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("passages_json", sa.Text(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_evidence_conflicts_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], name=op.f("fk_evidence_conflicts_task_id_tasks")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_conflicts")),
        sa.UniqueConstraint("passages_json", name=op.f("uq_evidence_conflicts_passages_json")),
    )


def downgrade() -> None:
    op.drop_table("evidence_conflicts")
