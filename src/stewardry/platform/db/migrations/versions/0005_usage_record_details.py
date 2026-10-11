"""Usage record details for model calls (Milestone 1, step S6).

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("stop_reason", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("attempts", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_records", schema=None) as batch_op:
        batch_op.drop_column("attempts")
        batch_op.drop_column("stop_reason")
        batch_op.drop_column("request_hash")
        batch_op.drop_column("cache_write_tokens")
