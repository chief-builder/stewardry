"""Evidence snapshots (Milestone 1, step S10).

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_snapshots",
        sa.Column("snapshot_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("acting_sub", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("page_id", sa.String(length=64), nullable=False),
        sa.Column("page_version", sa.Integer(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("passages_json", sa.Text(), nullable=False),
        sa.Column("problems_json", sa.Text(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.Column("invalidated_at", sa.String(length=27), nullable=True),
        sa.Column("invalidated_reason", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_evidence_snapshots_sha256_hex"),
        ),
        sa.CheckConstraint(
            "(invalidated_at IS NULL AND invalidated_reason IS NULL) OR (invalidated_at IS NOT NULL AND coalesce(length(invalidated_reason), 0) > 0)",
            name=op.f("ck_evidence_snapshots_invalidation_has_reason"),
        ),
        sa.CheckConstraint(
            "NOT eligible OR metadata_json IS NOT NULL",
            name=op.f("ck_evidence_snapshots_eligible_has_metadata"),
        ),
        sa.CheckConstraint(
            "length(page_id) > 0", name=op.f("ck_evidence_snapshots_page_id_not_empty")
        ),
        sa.ForeignKeyConstraint(
            ["acting_sub"], ["users.sub"], name=op.f("fk_evidence_snapshots_acting_sub_users")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_evidence_snapshots_customer_id_customers"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_evidence_snapshots_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], name=op.f("fk_evidence_snapshots_task_id_tasks")
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_evidence_snapshots")),
    )
    with op.batch_alter_table("evidence_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            "ix_evidence_snapshots_scope",
            ["run_id", "acting_sub", "task_id", "page_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("evidence_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_evidence_snapshots_scope")

    op.drop_table("evidence_snapshots")
