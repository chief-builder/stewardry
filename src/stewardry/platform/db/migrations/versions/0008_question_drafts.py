"""Question drafts (Milestone 1, step S11).

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "question_drafts",
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("question_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("claimed_strength", sa.String(length=16), nullable=True),
        sa.Column("strength", sa.String(length=32), nullable=True),
        sa.Column("expert_reason", sa.String(length=16), nullable=True),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.Column("citations_json", sa.Text(), nullable=False),
        sa.Column("conflict_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "expert_reason IS NULL OR expert_reason IN ('missing', 'stale', 'conflict', 'ambiguous', 'unsupported', 'no_answer')",
            name=op.f("ck_question_drafts_expert_reason_known"),
        ),
        sa.CheckConstraint(
            "status != 'needs_expert' OR expert_reason IS NOT NULL",
            name=op.f("ck_question_drafts_expert_has_reason"),
        ),
        sa.CheckConstraint(
            "status != 'ready_for_review' OR (coalesce(length(answer), 0) > 0 AND strength = 'direct_current' AND citations_json != '[]' AND expert_reason IS NULL)",
            name=op.f("ck_question_drafts_review_needs_cited_direct_answer"),
        ),
        sa.CheckConstraint(
            "status IN ('ready_for_review', 'needs_expert')",
            name=op.f("ck_question_drafts_status_known"),
        ),
        sa.CheckConstraint(
            "strength IS NULL OR strength IN ('direct_current', 'partial_ambiguous', 'insufficient_conflicting_stale')",
            name=op.f("ck_question_drafts_strength_known"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_question_drafts_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "question_id"],
            ["task_questions.task_id", "task_questions.question_id"],
            name=op.f("fk_question_drafts_task_id_task_questions"),
        ),
        sa.PrimaryKeyConstraint("task_id", "question_id", name=op.f("pk_question_drafts")),
    )


def downgrade() -> None:
    op.drop_table("question_drafts")
