"""Initial schema (Milestone 1, step S1).

Revision ID: 0001
Revises: none

Timestamps are stored as fixed-width UTC text; see schema.UTCTimestamp.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.CheckConstraint(
            "length(customer_id) BETWEEN 1 AND 64 AND customer_id NOT GLOB '*[^a-z0-9-]*'",
            name=op.f("ck_customers_customer_id_slug"),
        ),
        sa.PrimaryKeyConstraint("customer_id", name=op.f("pk_customers")),
    )
    op.create_table(
        "users",
        sa.Column("sub", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint("length(sub) > 0", name=op.f("ck_users_sub_not_empty")),
        sa.PrimaryKeyConstraint("sub", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
    )
    op.create_table(
        "agents",
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("owning_team", sa.String(length=255), nullable=False),
        sa.Column("business_owner_sub", sa.String(length=255), nullable=False),
        sa.Column("operational_owner_sub", sa.String(length=255), nullable=False),
        sa.Column("engagement_status", sa.String(length=32), nullable=False),
        sa.Column("operational_state", sa.String(length=16), nullable=False),
        sa.Column("risk_tier", sa.Integer(), nullable=False),
        sa.Column("runbook_url", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.Column("updated_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "engagement_status IN ('discovery', 'build', 'shadow', 'assisted', 'live', 'handed_off', 'retired')",
            name=op.f("ck_agents_status_known"),
        ),
        sa.CheckConstraint(
            "length(agent_id) BETWEEN 1 AND 64 AND agent_id NOT GLOB '*[^a-z0-9-]*'",
            name=op.f("ck_agents_agent_id_slug"),
        ),
        sa.CheckConstraint(
            "operational_state IN ('enabled', 'paused')", name=op.f("ck_agents_state_known")
        ),
        sa.CheckConstraint("risk_tier IN (1, 2, 3)", name=op.f("ck_agents_risk_tier_known")),
        sa.ForeignKeyConstraint(
            ["business_owner_sub"], ["users.sub"], name=op.f("fk_agents_business_owner_sub_users")
        ),
        sa.ForeignKeyConstraint(
            ["operational_owner_sub"],
            ["users.sub"],
            name=op.f("fk_agents_operational_owner_sub_users"),
        ),
        sa.PrimaryKeyConstraint("agent_id", name=op.f("pk_agents")),
    )
    op.create_table(
        "role_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sub", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("assigned_by_sub", sa.String(length=255), nullable=False),
        sa.Column("assigned_at", sa.String(length=27), nullable=False),
        sa.Column("revoked_at", sa.String(length=27), nullable=True),
        sa.CheckConstraint(
            "role IN ('sales', 'grc', 'legal', 'platform_engineer', 'auditor', 'engagement_owner', 'sme')",
            name=op.f("ck_role_assignments_role_known"),
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= assigned_at",
            name=op.f("ck_role_assignments_revoked_after"),
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_sub"],
            ["users.sub"],
            name=op.f("fk_role_assignments_assigned_by_sub_users"),
        ),
        sa.ForeignKeyConstraint(["sub"], ["users.sub"], name=op.f("fk_role_assignments_sub_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_role_assignments")),
    )
    with op.batch_alter_table("role_assignments", schema=None) as batch_op:
        batch_op.create_index(
            "uq_role_assignments_active",
            ["sub", "role"],
            unique=True,
            sqlite_where=sa.text("revoked_at IS NULL"),
        )

    op.create_table(
        "agent_policy_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("policy_json", sa.Text(), nullable=False),
        sa.Column("applied_by_sub", sa.String(length=255), nullable=False),
        sa.Column("applied_at", sa.String(length=27), nullable=False),
        sa.Column("reevaluation_required", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "length(policy_hash) = 64 AND policy_hash NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_agent_policy_versions_policy_hash_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.agent_id"], name=op.f("fk_agent_policy_versions_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["applied_by_sub"],
            ["users.sub"],
            name=op.f("fk_agent_policy_versions_applied_by_sub_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_policy_versions")),
        sa.UniqueConstraint(
            "agent_id", "policy_hash", name=op.f("uq_agent_policy_versions_agent_id_policy_hash")
        ),
    )
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("requester_sub", sa.String(length=255), nullable=False),
        sa.Column("submission_key", sa.String(length=128), nullable=False),
        sa.Column("jira_issue_key", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.Column("updated_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "status IN ('submitted', 'awaiting_authorization', 'drafting', 'paused', 'in_review', 'completed', 'rejected', 'failed', 'cancelled')",
            name=op.f("ck_tasks_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.agent_id"], name=op.f("fk_tasks_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.customer_id"], name=op.f("fk_tasks_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["requester_sub"], ["users.sub"], name=op.f("fk_tasks_requester_sub_users")
        ),
        sa.PrimaryKeyConstraint("task_id", name=op.f("pk_tasks")),
        sa.UniqueConstraint(
            "requester_sub", "submission_key", name=op.f("uq_tasks_requester_sub_submission_key")
        ),
    )
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=True),
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("created_by_sub", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "kind IN ('upload', 'working_draft', 'approved_file')",
            name=op.f("ck_artifacts_kind_known"),
        ),
        sa.CheckConstraint(
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_artifacts_sha256_hex"),
        ),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_artifacts_size_non_negative")),
        sa.ForeignKeyConstraint(
            ["created_by_sub"], ["users.sub"], name=op.f("fk_artifacts_created_by_sub_users")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_artifacts_customer_id_customers"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], name=op.f("fk_artifacts_task_id_tasks")
        ),
        sa.PrimaryKeyConstraint("artifact_id", name=op.f("pk_artifacts")),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index("ix_artifacts_sha256", ["sha256"], unique=False)

    op.create_table(
        "task_grants",
        sa.Column("grant_id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("authorizing_sub", sa.String(length=255), nullable=False),
        sa.Column("policy_version_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("granted_at", sa.String(length=27), nullable=False),
        sa.Column("expires_at", sa.String(length=27), nullable=False),
        sa.Column("revoked_at", sa.String(length=27), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoke_reason IS NULL) OR (revoked_at IS NOT NULL AND coalesce(length(revoke_reason), 0) > 0)",
            name=op.f("ck_task_grants_revocation_has_reason"),
        ),
        sa.CheckConstraint(
            "expires_at > granted_at", name=op.f("ck_task_grants_expires_after_granted")
        ),
        sa.CheckConstraint("length(stage) > 0", name=op.f("ck_task_grants_stage_not_empty")),
        sa.ForeignKeyConstraint(
            ["authorizing_sub"], ["users.sub"], name=op.f("fk_task_grants_authorizing_sub_users")
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["agent_policy_versions.id"],
            name=op.f("fk_task_grants_policy_version_id_agent_policy_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.task_id"], name=op.f("fk_task_grants_task_id_tasks")
        ),
        sa.PrimaryKeyConstraint("grant_id", name=op.f("pk_task_grants")),
    )
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version_id", sa.Integer(), nullable=False),
        sa.Column("acting_sub", sa.String(length=255), nullable=False),
        sa.Column("grant_id", sa.String(length=32), nullable=True),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.String(length=27), nullable=False),
        sa.Column("ended_at", sa.String(length=27), nullable=True),
        sa.CheckConstraint(
            "provenance IN ('connected', 'live-model-eval', 'offline-replay', 'scripted-example')",
            name=op.f("ck_runs_provenance_known"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_runs_status_known"),
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name=op.f("ck_runs_ended_after_started")
        ),
        sa.ForeignKeyConstraint(
            ["acting_sub"], ["users.sub"], name=op.f("fk_runs_acting_sub_users")
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.agent_id"], name=op.f("fk_runs_agent_id_agents")
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"], ["task_grants.grant_id"], name=op.f("fk_runs_grant_id_task_grants")
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            ["agent_policy_versions.id"],
            name=op.f("fk_runs_policy_version_id_agent_policy_versions"),
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], name=op.f("fk_runs_task_id_tasks")),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_runs")),
    )
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.String(length=27), nullable=True),
        sa.Column("created_at", sa.String(length=27), nullable=False),
        sa.Column("updated_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "(status = 'leased') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_jobs_lease_matches_status"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'leased', 'succeeded', 'failed')",
            name=op.f("ck_jobs_status_known"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_jobs_attempts_non_negative")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], name=op.f("fk_jobs_run_id_runs")),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_jobs_idempotency_key")),
    )
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_micro_usd", sa.BigInteger(), nullable=True),
        sa.Column("pricing_ref", sa.String(length=128), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.String(length=27), nullable=False),
        sa.CheckConstraint(
            "coalesce(input_tokens, 0) >= 0 AND coalesce(output_tokens, 0) >= 0 AND coalesce(cache_read_tokens, 0) >= 0 AND coalesce(estimated_cost_micro_usd, 0) >= 0 AND coalesce(latency_ms, 0) >= 0",
            name=op.f("ck_usage_records_non_negative"),
        ),
        sa.CheckConstraint(
            "estimated_cost_micro_usd IS NULL OR pricing_ref IS NOT NULL",
            name=op.f("ck_usage_records_cost_names_pricing_ref"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.run_id"], name=op.f("fk_usage_records_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_records")),
    )


def downgrade() -> None:
    op.drop_table("usage_records")
    op.drop_table("jobs")
    op.drop_table("runs")
    op.drop_table("task_grants")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index("ix_artifacts_sha256")

    op.drop_table("artifacts")
    op.drop_table("tasks")
    op.drop_table("agent_policy_versions")
    with op.batch_alter_table("role_assignments", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_role_assignments_active", sqlite_where=sa.text("revoked_at IS NULL")
        )

    op.drop_table("role_assignments")
    op.drop_table("agents")
    op.drop_table("users")
    op.drop_table("customers")
