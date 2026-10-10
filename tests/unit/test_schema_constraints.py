"""The database itself rejects invalid rows, independent of application code."""

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, Table, insert, select, update
from sqlalchemy.exc import IntegrityError, StatementError

from stewardry.platform.db.schema import (
    agent_policy_versions,
    agents,
    artifacts,
    customers,
    jobs,
    role_assignments,
    runs,
    task_grants,
    tasks,
    usage_records,
    users,
    utcnow,
)

NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.UTC)
HASH = "a" * 64


def seed(conn: Connection) -> None:
    for sub, name in [("sub-alice", "alice"), ("sub-bob", "bob"), ("sub-plat", "platform")]:
        conn.execute(
            insert(users).values(sub=sub, username=name, display_name=name, created_at=NOW)
        )
    conn.execute(insert(customers).values(customer_id="acme-synthetic", display_name="Acme"))
    conn.execute(
        insert(agents).values(
            agent_id="questionnaire-agent",
            name="Questionnaire agent",
            purpose="Draft cited answers",
            owning_team="GRC",
            business_owner_sub="sub-bob",
            operational_owner_sub="sub-plat",
            engagement_status="build",
            operational_state="enabled",
            risk_tier=2,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    conn.execute(
        insert(agent_policy_versions).values(
            id=1,
            agent_id="questionnaire-agent",
            policy_hash=HASH,
            schema_version=1,
            policy_json="{}",
            applied_by_sub="sub-plat",
            applied_at=NOW,
            reevaluation_required=True,
        )
    )
    conn.execute(insert(tasks).values(**task_row()))


def task_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "task_id": "t1",
        "agent_id": "questionnaire-agent",
        "customer_id": "acme-synthetic",
        "requester_sub": "sub-alice",
        "submission_key": "k1",
        "status": "submitted",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return row | overrides


def grant_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "grant_id": "g1",
        "task_id": "t1",
        "authorizing_sub": "sub-bob",
        "policy_version_id": 1,
        "stage": "drafting",
        "granted_at": NOW,
        "expires_at": NOW + dt.timedelta(hours=1),
    }
    return row | overrides


def run_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "run_id": "r1",
        "task_id": "t1",
        "agent_id": "questionnaire-agent",
        "policy_version_id": 1,
        "acting_sub": "sub-bob",
        "provenance": "offline-replay",
        "status": "queued",
        "started_at": NOW,
    }
    return row | overrides


def job_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "job_id": "j1",
        "kind": "draft_question",
        "idempotency_key": "t1:q1:draft",
        "payload_json": "{}",
        "status": "queued",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return row | overrides


@pytest.fixture
def conn(engine: Engine) -> Any:
    with engine.begin() as connection:
        seed(connection)
        yield connection


def rejects(conn: Connection, table: Table, row: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(insert(table).values(**row))


def test_foreign_keys_are_enforced(conn: Connection) -> None:
    rejects(conn, tasks, task_row(task_id="t2", submission_key="k2", requester_sub="nobody"))
    rejects(conn, tasks, task_row(task_id="t2", submission_key="k2", customer_id="unknown"))


def test_unknown_enum_values_are_rejected(conn: Connection) -> None:
    rejects(conn, tasks, task_row(task_id="t2", submission_key="k2", status="approved-by-model"))
    rejects(conn, runs, run_row(provenance="probably-connected"))
    rejects(
        conn,
        role_assignments,
        {
            "sub": "sub-alice",
            "role": "superuser",
            "assigned_by_sub": "sub-plat",
            "assigned_at": NOW,
        },
    )
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(agents).values(engagement_status="production"))
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(agents).values(risk_tier=4))


def test_agent_ids_must_be_slugs(conn: Connection) -> None:
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(update(agents).values(agent_id="Questionnaire Agent"))


def test_duplicate_submission_is_rejected_per_requester(conn: Connection) -> None:
    rejects(conn, tasks, task_row(task_id="t2"))  # same requester, same submission key
    conn.execute(insert(tasks).values(**task_row(task_id="t3", requester_sub="sub-bob")))


def test_job_idempotency_key_is_unique(conn: Connection) -> None:
    conn.execute(insert(jobs).values(**job_row()))
    rejects(conn, jobs, job_row(job_id="j2"))


def test_job_lease_fields_match_status(conn: Connection) -> None:
    rejects(conn, jobs, job_row(status="leased"))
    rejects(conn, jobs, job_row(lease_owner="worker-1", lease_expires_at=NOW))
    conn.execute(
        insert(jobs).values(**job_row(status="leased", lease_owner="w1", lease_expires_at=NOW))
    )


def test_only_one_active_assignment_of_a_role(conn: Connection) -> None:
    row = {"sub": "sub-alice", "role": "sales", "assigned_by_sub": "sub-plat", "assigned_at": NOW}
    conn.execute(insert(role_assignments).values(**row))
    rejects(conn, role_assignments, row)
    conn.execute(update(role_assignments).values(revoked_at=NOW + dt.timedelta(minutes=1)))
    conn.execute(insert(role_assignments).values(**row))  # re-assign after revocation


def test_grant_must_expire_after_it_is_granted(conn: Connection) -> None:
    rejects(conn, task_grants, grant_row(expires_at=NOW))


def test_grant_revocation_needs_a_reason(conn: Connection) -> None:
    rejects(conn, task_grants, grant_row(revoked_at=NOW))
    rejects(conn, task_grants, grant_row(revoked_at=NOW, revoke_reason=""))
    conn.execute(insert(task_grants).values(**grant_row(revoked_at=NOW, revoke_reason="cancelled")))


def test_unknown_usage_is_null_not_zero(conn: Connection) -> None:
    conn.execute(insert(runs).values(**run_row()))
    usage = {
        "run_id": "r1",
        "model_id": "claude-opus-5-5",
        "prompt_version": "draft@1",
        "recorded_at": NOW,
    }
    conn.execute(insert(usage_records).values(**usage))
    stored = conn.execute(select(usage_records.c.input_tokens)).scalar_one()
    assert stored is None
    rejects(conn, usage_records, usage | {"input_tokens": -1})
    rejects(conn, usage_records, usage | {"estimated_cost_micro_usd": 10})  # no pricing_ref


def test_artifact_hash_and_kind_are_checked(conn: Connection) -> None:
    row = {
        "artifact_id": "a1",
        "sha256": HASH,
        "size_bytes": 3,
        "media_type": "text/plain",
        "kind": "upload",
        "customer_id": "acme-synthetic",
        "created_by_sub": "sub-alice",
        "created_at": NOW,
    }
    conn.execute(insert(artifacts).values(**row))
    rejects(conn, artifacts, row | {"artifact_id": "a2", "sha256": "../../etc/passwd"})
    rejects(conn, artifacts, row | {"artifact_id": "a3", "kind": "released_to_customer"})


def test_timestamps_round_trip_as_utc(conn: Connection) -> None:
    stored = conn.execute(select(tasks.c.created_at)).scalar_one()
    assert stored == NOW
    assert stored.tzinfo == dt.UTC


def test_naive_timestamps_are_rejected(conn: Connection) -> None:
    with pytest.raises(StatementError, match="timezone-aware"), conn.begin_nested():
        conn.execute(update(tasks).values(updated_at=dt.datetime(2026, 10, 10)))


def test_utcnow_is_timezone_aware() -> None:
    assert utcnow().tzinfo == dt.UTC
