"""Append-only, hash-chained audit events (Milestone 1, step S2).

Revision ID: 0002
Revises: 0001

The triggers are the database half of the audit controls in ADR 0004. Any
later migration that rebuilds this table (Alembic batch mode on SQLite)
must re-create them; a test asserts they exist at head.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

TRIGGERS = {
    "audit_events_no_update": """
        CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END
    """,
    "audit_events_no_delete": """
        CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END
    """,
    # A new event must take the next sequence number and link to the head.
    "audit_events_extend_head": """
        CREATE TRIGGER audit_events_extend_head BEFORE INSERT ON audit_events
        WHEN NEW.seq != coalesce((SELECT max(seq) FROM audit_events), 0) + 1
          OR NEW.prev_hash != coalesce(
               (SELECT event_hash FROM audit_events ORDER BY seq DESC LIMIT 1),
               '0000000000000000000000000000000000000000000000000000000000000000')
        BEGIN SELECT RAISE(ABORT, 'audit event does not extend the chain head'); END
    """,
}

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.String(length=27), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("actor_sub", sa.String(length=255), nullable=True),
        sa.Column("requester_sub", sa.String(length=255), nullable=True),
        sa.Column("authorizing_sub", sa.String(length=255), nullable=True),
        sa.Column("reviewer_sub", sa.String(length=255), nullable=True),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("policy_hash", sa.String(length=64), nullable=True),
        sa.Column("customer_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("grant_id", sa.String(length=32), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_audit_events_event_hash_sha256"),
        ),
        sa.CheckConstraint(
            "length(event_type) BETWEEN 3 AND 64 AND event_type GLOB '[a-z]*.[a-z]*' AND event_type NOT GLOB '*[^a-z0-9_.]*'",
            name=op.f("ck_audit_events_event_type_dotted"),
        ),
        sa.CheckConstraint(
            "length(prev_hash) = 64 AND prev_hash NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_audit_events_prev_hash_sha256"),
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'denied', 'failed')",
            name=op.f("ck_audit_events_outcome_known"),
        ),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_audit_events_seq_positive")),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_audit_events")),
        sa.UniqueConstraint("event_hash", name=op.f("uq_audit_events_event_hash")),
        sa.UniqueConstraint("event_id", name=op.f("uq_audit_events_event_id")),
    )
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.create_index("ix_audit_events_correlation_id", ["correlation_id"], unique=False)
        batch_op.create_index("ix_audit_events_task_id", ["task_id"], unique=False)

    for sql in TRIGGERS.values():
        op.execute(sql)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER {name}")
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_events_task_id")
        batch_op.drop_index("ix_audit_events_correlation_id")

    op.drop_table("audit_events")
