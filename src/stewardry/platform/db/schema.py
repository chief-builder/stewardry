"""Database schema (SQLAlchemy Core).

The allowed values below are the single source for both the Python code and
the database CHECK constraints. Changing one needs a migration.
The audit log is not here; it lands with step S2.
"""

import datetime as dt
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Dialect,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)

ROLES = ("sales", "grc", "legal", "platform_engineer", "auditor", "engagement_owner", "sme")
ENGAGEMENT_STATUSES = (
    "discovery",
    "build",
    "shadow",
    "assisted",
    "live",
    "handed_off",
    "retired",
)
OPERATIONAL_STATES = ("enabled", "paused")
TASK_STATUSES = (
    "submitted",
    "awaiting_authorization",
    "drafting",
    "paused",
    "in_review",
    "completed",
    "rejected",
    "failed",
    "cancelled",
)
RUN_STATUSES = ("queued", "running", "paused", "succeeded", "failed", "cancelled")
JOB_STATUSES = ("queued", "leased", "succeeded", "failed")
PROVENANCE = ("connected", "live-model-eval", "offline-replay", "scripted-example")
ARTIFACT_KINDS = ("upload", "working_draft", "approved_file")

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class UTCTimestamp(TypeDecorator[dt.datetime]):
    """UTC timestamps stored as fixed-width ISO-8601 text.

    The fixed format keeps text comparison equal to time comparison, which
    the CHECK constraints rely on. Naive datetimes are rejected.
    """

    impl = String(27)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(dt.UTC).strftime(TIMESTAMP_FORMAT)

    def process_result_value(self, value: Any | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        return dt.datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=dt.UTC)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _one_of(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _slug(column: str) -> str:
    return f"length({column}) BETWEEN 1 AND 64 AND {column} NOT GLOB '*[^a-z0-9-]*'"


def _sha256(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


naming = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=naming)

users = Table(
    "users",
    metadata,
    Column("sub", String(255), primary_key=True),
    Column("username", String(255), nullable=False, unique=True),
    Column("display_name", String(255), nullable=False),
    Column("created_at", UTCTimestamp, nullable=False),
    CheckConstraint("length(sub) > 0", name="sub_not_empty"),
)

role_assignments = Table(
    "role_assignments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sub", ForeignKey("users.sub"), nullable=False),
    Column("role", String(32), nullable=False),
    Column("assigned_by_sub", ForeignKey("users.sub"), nullable=False),
    Column("assigned_at", UTCTimestamp, nullable=False),
    Column("revoked_at", UTCTimestamp),
    CheckConstraint(_one_of("role", ROLES), name="role_known"),
    CheckConstraint("revoked_at IS NULL OR revoked_at >= assigned_at", name="revoked_after"),
)
# At most one active assignment of a role per user.
Index(
    "uq_role_assignments_active",
    role_assignments.c.sub,
    role_assignments.c.role,
    unique=True,
    sqlite_where=text("revoked_at IS NULL"),
)

customers = Table(
    "customers",
    metadata,
    Column("customer_id", String(64), primary_key=True),
    Column("display_name", String(255), nullable=False),
    CheckConstraint(_slug("customer_id"), name="customer_id_slug"),
)

agents = Table(
    "agents",
    metadata,
    Column("agent_id", String(64), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("purpose", Text, nullable=False),
    Column("owning_team", String(255), nullable=False),
    Column("business_owner_sub", ForeignKey("users.sub"), nullable=False),
    Column("operational_owner_sub", ForeignKey("users.sub"), nullable=False),
    Column("engagement_status", String(32), nullable=False),
    Column("operational_state", String(16), nullable=False),
    Column("risk_tier", Integer, nullable=False),
    Column("runbook_url", String(1024)),
    Column("created_at", UTCTimestamp, nullable=False),
    Column("updated_at", UTCTimestamp, nullable=False),
    CheckConstraint(_slug("agent_id"), name="agent_id_slug"),
    CheckConstraint(_one_of("engagement_status", ENGAGEMENT_STATUSES), name="status_known"),
    CheckConstraint(_one_of("operational_state", OPERATIONAL_STATES), name="state_known"),
    CheckConstraint("risk_tier IN (1, 2, 3)", name="risk_tier_known"),
)

agent_policy_versions = Table(
    "agent_policy_versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("agent_id", ForeignKey("agents.agent_id"), nullable=False),
    Column("policy_hash", String(64), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("policy_json", Text, nullable=False),
    Column("applied_by_sub", ForeignKey("users.sub"), nullable=False),
    Column("applied_at", UTCTimestamp, nullable=False),
    Column("reevaluation_required", Boolean, nullable=False),
    UniqueConstraint("agent_id", "policy_hash"),
    CheckConstraint(_sha256("policy_hash"), name="policy_hash_sha256"),
)

tasks = Table(
    "tasks",
    metadata,
    Column("task_id", String(32), primary_key=True),
    Column("agent_id", ForeignKey("agents.agent_id"), nullable=False),
    Column("customer_id", ForeignKey("customers.customer_id"), nullable=False),
    Column("requester_sub", ForeignKey("users.sub"), nullable=False),
    # Lets a repeated submission resolve to the same task (idempotency).
    Column("submission_key", String(128), nullable=False),
    Column("jira_issue_key", String(64)),
    Column("status", String(32), nullable=False),
    Column("created_at", UTCTimestamp, nullable=False),
    Column("updated_at", UTCTimestamp, nullable=False),
    UniqueConstraint("requester_sub", "submission_key"),
    CheckConstraint(_one_of("status", TASK_STATUSES), name="status_known"),
)

task_grants = Table(
    "task_grants",
    metadata,
    Column("grant_id", String(32), primary_key=True),
    Column("task_id", ForeignKey("tasks.task_id"), nullable=False),
    Column("authorizing_sub", ForeignKey("users.sub"), nullable=False),
    Column("policy_version_id", ForeignKey("agent_policy_versions.id"), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("granted_at", UTCTimestamp, nullable=False),
    Column("expires_at", UTCTimestamp, nullable=False),
    Column("revoked_at", UTCTimestamp),
    Column("revoke_reason", Text),
    CheckConstraint("length(stage) > 0", name="stage_not_empty"),
    CheckConstraint("expires_at > granted_at", name="expires_after_granted"),
    CheckConstraint(
        "(revoked_at IS NULL AND revoke_reason IS NULL) OR "
        "(revoked_at IS NOT NULL AND coalesce(length(revoke_reason), 0) > 0)",
        name="revocation_has_reason",
    ),
)

runs = Table(
    "runs",
    metadata,
    Column("run_id", String(32), primary_key=True),
    Column("task_id", ForeignKey("tasks.task_id"), nullable=False),
    Column("agent_id", ForeignKey("agents.agent_id"), nullable=False),
    Column("policy_version_id", ForeignKey("agent_policy_versions.id"), nullable=False),
    Column("acting_sub", ForeignKey("users.sub"), nullable=False),
    Column("grant_id", ForeignKey("task_grants.grant_id")),
    Column("provenance", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_at", UTCTimestamp, nullable=False),
    Column("ended_at", UTCTimestamp),
    CheckConstraint(_one_of("provenance", PROVENANCE), name="provenance_known"),
    CheckConstraint(_one_of("status", RUN_STATUSES), name="status_known"),
    CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ended_after_started"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("job_id", String(32), primary_key=True),
    Column("run_id", ForeignKey("runs.run_id")),
    Column("kind", String(64), nullable=False),
    Column("idempotency_key", String(128), nullable=False, unique=True),
    Column("payload_json", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("lease_owner", String(64)),
    Column("lease_expires_at", UTCTimestamp),
    Column("created_at", UTCTimestamp, nullable=False),
    Column("updated_at", UTCTimestamp, nullable=False),
    CheckConstraint(_one_of("status", JOB_STATUSES), name="status_known"),
    CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    CheckConstraint(
        "(status = 'leased') = (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
        name="lease_matches_status",
    ),
)

# Missing usage is NULL ("unknown"), never zero (spec section 4.7).
usage_records = Table(
    "usage_records",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", ForeignKey("runs.run_id"), nullable=False),
    Column("model_id", String(64), nullable=False),
    Column("prompt_version", String(64), nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cache_read_tokens", Integer),
    Column("estimated_cost_micro_usd", BigInteger),
    Column("pricing_ref", String(128)),
    Column("latency_ms", Integer),
    Column("recorded_at", UTCTimestamp, nullable=False),
    CheckConstraint(
        "coalesce(input_tokens, 0) >= 0 AND coalesce(output_tokens, 0) >= 0"
        " AND coalesce(cache_read_tokens, 0) >= 0"
        " AND coalesce(estimated_cost_micro_usd, 0) >= 0 AND coalesce(latency_ms, 0) >= 0",
        name="non_negative",
    ),
    CheckConstraint(
        "estimated_cost_micro_usd IS NULL OR pricing_ref IS NOT NULL",
        name="cost_names_pricing_ref",
    ),
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("artifact_id", String(32), primary_key=True),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("media_type", String(255), nullable=False),
    Column("original_name", String(255)),
    Column("kind", String(32), nullable=False),
    Column("task_id", ForeignKey("tasks.task_id")),
    Column("customer_id", ForeignKey("customers.customer_id"), nullable=False),
    Column("created_by_sub", ForeignKey("users.sub"), nullable=False),
    Column("created_at", UTCTimestamp, nullable=False),
    CheckConstraint(_sha256("sha256"), name="sha256_hex"),
    CheckConstraint("size_bytes >= 0", name="size_non_negative"),
    CheckConstraint(_one_of("kind", ARTIFACT_KINDS), name="kind_known"),
)
Index("ix_artifacts_sha256", artifacts.c.sha256)

AUDIT_OUTCOMES = ("succeeded", "denied", "failed")
GENESIS_HASH = "0" * 64

# Append-only (enforced by triggers in migration 0002) and hash-chained.
# No foreign keys: a denied attempt may name an agent, user, or task that
# does not exist, and audit rows must outlive the rows they describe.
audit_events = Table(
    "audit_events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=False),
    Column("event_id", String(32), nullable=False, unique=True),
    Column("occurred_at", UTCTimestamp, nullable=False),
    Column("event_type", String(64), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("actor_sub", String(255)),
    Column("requester_sub", String(255)),
    Column("authorizing_sub", String(255)),
    Column("reviewer_sub", String(255)),
    Column("agent_id", String(64)),
    Column("policy_hash", String(64)),
    Column("customer_id", String(64)),
    Column("task_id", String(32)),
    Column("run_id", String(32)),
    Column("grant_id", String(32)),
    Column("correlation_id", String(64)),
    Column("details_json", Text, nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("event_hash", String(64), nullable=False, unique=True),
    CheckConstraint("seq >= 1", name="seq_positive"),
    CheckConstraint(_one_of("outcome", AUDIT_OUTCOMES), name="outcome_known"),
    CheckConstraint(
        "length(event_type) BETWEEN 3 AND 64 AND event_type GLOB '[a-z]*.[a-z]*'"
        " AND event_type NOT GLOB '*[^a-z0-9_.]*'",
        name="event_type_dotted",
    ),
    CheckConstraint(_sha256("prev_hash"), name="prev_hash_sha256"),
    CheckConstraint(_sha256("event_hash"), name="event_hash_sha256"),
)
Index("ix_audit_events_task_id", audit_events.c.task_id)
Index("ix_audit_events_correlation_id", audit_events.c.correlation_id)
