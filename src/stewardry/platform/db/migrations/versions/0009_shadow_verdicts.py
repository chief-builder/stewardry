"""Shadow review verdicts (Milestone 1, step S12).

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadow_verdicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=32), nullable=False),
        sa.Column("reviewer_sub", sa.String(length=255), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("edited_answer", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "verdict != 'edit' OR coalesce(length(edited_answer), 0) > 0",
            name=op.f("ck_shadow_verdicts_edit_has_answer"),
        ),
        sa.CheckConstraint(
            "verdict != 'reject' OR coalesce(length(reason), 0) > 0",
            name=op.f("ck_shadow_verdicts_reject_has_reason"),
        ),
        sa.CheckConstraint(
            "verdict IN ('agree', 'edit', 'reject')", name=op.f("ck_shadow_verdicts_verdict_known")
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_sub"], ["users.sub"], name=op.f("fk_shadow_verdicts_reviewer_sub_users")
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "question_id"],
            ["question_drafts.task_id", "question_drafts.question_id"],
            name=op.f("fk_shadow_verdicts_task_id_question_drafts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_verdicts")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_shadow_verdicts_idempotency_key")),
    )
    with op.batch_alter_table("shadow_verdicts", schema=None) as batch_op:
        batch_op.create_index("ix_shadow_verdicts_task_id", ["task_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("shadow_verdicts", schema=None) as batch_op:
        batch_op.drop_index("ix_shadow_verdicts_task_id")

    op.drop_table("shadow_verdicts")
