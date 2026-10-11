"""Task questions and screening findings (Milestone 1, step S9).

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screening_findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=False),
        sa.Column("question_id", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "kind IN ('hidden_cell', 'hidden_row', 'hidden_column', 'hidden_text', 'comment', 'tracked_change', 'outside_template', 'instruction_like')",
            name=op.f("ck_screening_findings_kind_known"),
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.artifact_id"],
            name=op.f("fk_screening_findings_artifact_id_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], name=op.f("fk_screening_findings_task_id_tasks")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screening_findings")),
    )
    with op.batch_alter_table("screening_findings", schema=None) as batch_op:
        batch_op.create_index("ix_screening_findings_task_id", ["task_id"], unique=False)

    op.create_table(
        "task_questions",
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=32), nullable=False),
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("section", sa.String(length=255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("location_json", sa.Text(), nullable=False),
        sa.CheckConstraint("ordinal >= 1", name=op.f("ck_task_questions_ordinal_positive")),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifacts.artifact_id"],
            name=op.f("fk_task_questions_artifact_id_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], name=op.f("fk_task_questions_task_id_tasks")
        ),
        sa.PrimaryKeyConstraint("task_id", "question_id", name=op.f("pk_task_questions")),
        sa.UniqueConstraint("task_id", "ordinal", name=op.f("uq_task_questions_task_id_ordinal")),
    )


def downgrade() -> None:
    op.drop_table("task_questions")
    with op.batch_alter_table("screening_findings", schema=None) as batch_op:
        batch_op.drop_index("ix_screening_findings_task_id")

    op.drop_table("screening_findings")
