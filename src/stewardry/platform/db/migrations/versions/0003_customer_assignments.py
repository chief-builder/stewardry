"""Customer assignments (Milestone 1, step S4a).

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customer_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sub", sa.String(length=255), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("assigned_by_sub", sa.String(length=255), nullable=False),
        sa.Column("assigned_at", sa.String(length=27), nullable=False),
        sa.Column("revoked_at", sa.String(length=27), nullable=True),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= assigned_at",
            name=op.f("ck_customer_assignments_revoked_after"),
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_sub"],
            ["users.sub"],
            name=op.f("fk_customer_assignments_assigned_by_sub_users"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_customer_assignments_customer_id_customers"),
        ),
        sa.ForeignKeyConstraint(
            ["sub"], ["users.sub"], name=op.f("fk_customer_assignments_sub_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customer_assignments")),
    )
    with op.batch_alter_table("customer_assignments", schema=None) as batch_op:
        batch_op.create_index(
            "uq_customer_assignments_active",
            ["sub", "customer_id"],
            unique=True,
            sqlite_where=sa.text("revoked_at IS NULL"),
        )


def downgrade() -> None:
    with op.batch_alter_table("customer_assignments", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_customer_assignments_active", sqlite_where=sa.text("revoked_at IS NULL")
        )

    op.drop_table("customer_assignments")
