"""Tasks, grants, and whose authority a run uses. No fallback identity, ever."""

import datetime as dt

import pytest
from sqlalchemy import Engine, select, update

from stewardry.platform import registry
from stewardry.platform.access import AccessMapping, apply_access_mapping
from stewardry.platform.audit import AuditLog, verify
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db.schema import audit_events, task_grants, tasks
from stewardry.platform.grants import (
    AwaitingAuthorization,
    TaskError,
    acquire_execution,
    authorize_stage,
    get_task,
    revoke_grant,
    submit_task,
)
from tests.unit.conftest import (
    AGENT,
    ALICE,
    BOB,
    CAROL,
    LARKSPUR,
    OWNER,
    PLATFORM,
    QUILLON,
    mapping_data,
)

LATER = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)


def credential(token: str) -> GatewayCredential:
    return GatewayCredential(access_token=token, expires_at=LATER)


def signed_in(*subs: str) -> CredentialStore:
    store = CredentialStore()
    for sub in subs:
        store.put(f"session-{sub}", sub, credential(f"token-{sub}"))
    return store


def submit(world: Engine, log: AuditLog, key: str = "req-1") -> str:
    return submit_task(world, log, ALICE, AGENT, LARKSPUR, key, "SQR-1")


def status(world: Engine, task_id: str) -> str:
    with world.connect() as conn:
        task = get_task(conn, task_id)
    assert task is not None
    return str(task.status)


def denials(world: Engine, event_type: str) -> list[str]:
    with world.connect() as conn:
        rows = conn.execute(
            select(audit_events.c.details_json).where(
                audit_events.c.event_type == event_type, audit_events.c.outcome == "denied"
            )
        ).scalars()
        return list(rows)


# ------------------------------------------------------------------ submit


def test_sales_submits_for_an_assigned_customer(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    assert status(world, task_id) == "awaiting_authorization"


def test_repeated_submission_returns_the_same_task(world: Engine, log: AuditLog) -> None:
    assert submit(world, log) == submit(world, log)
    with world.connect() as conn:
        assert len(conn.execute(select(tasks.c.task_id)).all()) == 1


@pytest.mark.parametrize(
    ("sub", "customer", "key", "message"),
    [
        (BOB, LARKSPUR, "k", "no intake role"),
        (ALICE, QUILLON, "k", "not assigned to this customer"),
        (ALICE, LARKSPUR, " ", "submission key"),
    ],
)
def test_submission_refusals(
    world: Engine, log: AuditLog, sub: str, customer: str, key: str, message: str
) -> None:
    with pytest.raises(TaskError, match=message):
        submit_task(world, log, sub, AGENT, customer, key)
    assert len(denials(world, "task.submitted")) == 1


def test_paused_agents_take_no_submissions(world: Engine, log: AuditLog) -> None:
    registry.pause(world, log, PLATFORM, AGENT, "incident")
    with pytest.raises(TaskError, match="paused"):
        submit(world, log)


# --------------------------------------------------------------- authorize


def test_grc_authorizes_drafting_under_their_own_identity(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    grant_id = authorize_stage(world, log, BOB, task_id)
    assert status(world, task_id) == "drafting"
    with world.connect() as conn:
        grant = conn.execute(select(task_grants)).one()
        version = registry.current_policy_version(conn, AGENT)
    assert grant.grant_id == grant_id and grant.authorizing_sub == BOB
    assert version is not None and grant.policy_version_id == version.id
    assert grant.expires_at - grant.granted_at == dt.timedelta(hours=8)


@pytest.mark.parametrize(
    ("sub", "message"),
    [
        (ALICE, "no drafting role"),  # Sales cannot authorize drafting
        (CAROL, "not assigned to this customer"),  # GRC without this customer
        (OWNER, "no drafting role"),
        (PLATFORM, "no drafting role"),  # platform status grants no source access
    ],
)
def test_authorization_refusals(world: Engine, log: AuditLog, sub: str, message: str) -> None:
    task_id = submit(world, log)
    with pytest.raises(TaskError, match=message):
        authorize_stage(world, log, sub, task_id)
    assert status(world, task_id) == "awaiting_authorization"


def test_requester_cannot_authorize_their_own_task(world: Engine, log: AuditLog) -> None:
    data = mapping_data()
    next(u for u in data["users"] if u["username"] == "alice")["roles"] = ["sales", "grc"]
    apply_access_mapping(world, log, PLATFORM, AccessMapping.model_validate(data))
    task_id = submit(world, log)
    with pytest.raises(TaskError, match="cannot authorize their own task"):
        authorize_stage(world, log, ALICE, task_id)


def test_grant_lifetime_is_bounded(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    for ttl in (dt.timedelta(0), dt.timedelta(hours=25)):
        with pytest.raises(TaskError, match="lifetime"):
            authorize_stage(world, log, BOB, task_id, ttl=ttl)


def test_repeated_authorization_reuses_the_grant(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    assert authorize_stage(world, log, BOB, task_id) == authorize_stage(world, log, BOB, task_id)


def test_a_second_authorizer_must_wait_for_revocation(world: Engine, log: AuditLog) -> None:
    data = mapping_data()
    next(u for u in data["users"] if u["username"] == "carol")["customers"] = [LARKSPUR]
    apply_access_mapping(world, log, PLATFORM, AccessMapping.model_validate(data))
    task_id = submit(world, log)
    grant_id = authorize_stage(world, log, BOB, task_id)
    with pytest.raises(TaskError, match="must be revoked first"):
        authorize_stage(world, log, CAROL, task_id)
    revoke_grant(world, log, BOB, grant_id, "handing over")
    assert authorize_stage(world, log, CAROL, task_id) != grant_id


def test_unknown_or_closed_tasks_cannot_be_authorized(world: Engine, log: AuditLog) -> None:
    with pytest.raises(TaskError, match="does not exist"):
        authorize_stage(world, log, BOB, "no-such-task")
    task_id = submit(world, log)
    with world.begin() as conn:
        conn.execute(update(tasks).values(status="completed"))
    with pytest.raises(TaskError, match="task is completed"):
        authorize_stage(world, log, BOB, task_id)


# ------------------------------------------------------------------ revoke


def test_revocation_rules(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    grant_id = authorize_stage(world, log, BOB, task_id)
    with pytest.raises(TaskError, match="authorizing user or a platform engineer"):
        revoke_grant(world, log, ALICE, grant_id, "not mine")
    with pytest.raises(TaskError, match="reason"):
        revoke_grant(world, log, BOB, grant_id, "")
    revoke_grant(world, log, PLATFORM, grant_id, "incident")
    assert status(world, task_id) == "paused"
    with pytest.raises(TaskError, match="already revoked"):
        revoke_grant(world, log, BOB, grant_id, "again")
    with pytest.raises(TaskError, match="does not exist"):
        revoke_grant(world, log, BOB, "ghost", "x")


# ----------------------------------------------------------------- execute


def test_drafting_runs_as_the_authorizing_user(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    grant_id = authorize_stage(world, log, BOB, task_id)
    execution = acquire_execution(world, log, signed_in(ALICE, BOB), task_id, "drafting")
    assert (execution.acting_sub, execution.grant_id) == (BOB, grant_id)
    assert execution.credential.access_token == f"token-{BOB}"


def test_intake_runs_as_the_requester_without_a_grant(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    execution = acquire_execution(world, log, signed_in(ALICE, BOB), task_id, "intake")
    assert (execution.acting_sub, execution.grant_id) == (ALICE, None)
    assert execution.credential.access_token == f"token-{ALICE}"


def test_missing_credential_pauses_and_never_falls_back(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    authorize_stage(world, log, BOB, task_id)
    # Alice (the requester) is signed in; Bob (the authorizer) is not.
    with pytest.raises(AwaitingAuthorization, match="must sign in again"):
        acquire_execution(world, log, signed_in(ALICE), task_id, "drafting")
    assert status(world, task_id) == "paused"
    assert len(denials(world, "task.execution")) == 1


def test_restart_drops_credentials_and_the_task_pauses(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    authorize_stage(world, log, BOB, task_id)
    assert acquire_execution(world, log, signed_in(BOB), task_id, "drafting").acting_sub == BOB
    restarted = CredentialStore()
    with pytest.raises(AwaitingAuthorization):
        acquire_execution(world, log, restarted, task_id, "drafting")
    assert status(world, task_id) == "paused"


def test_task_fields_cannot_change_the_acting_identity(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    authorize_stage(world, log, BOB, task_id)
    with world.begin() as conn:  # someone rewrites the task to name another user
        conn.execute(update(tasks).values(requester_sub=CAROL))
    execution = acquire_execution(world, log, signed_in(ALICE, BOB, CAROL), task_id, "drafting")
    assert execution.acting_sub == BOB


@pytest.mark.parametrize(
    ("break_grant", "message"),
    [
        ("expire", "expired"),
        ("revoke", "revoked"),
        ("none", "not been authorized"),
    ],
)
def test_unusable_grants_pause_the_task(
    world: Engine, log: AuditLog, break_grant: str, message: str
) -> None:
    task_id = submit(world, log)
    if break_grant != "none":
        grant_id = authorize_stage(world, log, BOB, task_id)
        if break_grant == "expire":
            past = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
            with world.begin() as conn:
                conn.execute(
                    update(task_grants).values(
                        granted_at=past - dt.timedelta(hours=1), expires_at=past
                    )
                )
        else:
            revoke_grant(world, log, BOB, grant_id, "stop")
    with pytest.raises(AwaitingAuthorization, match=message):
        acquire_execution(world, log, signed_in(ALICE, BOB), task_id, "drafting")
    assert status(world, task_id) == "paused"


def test_paused_agent_pauses_the_task(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    authorize_stage(world, log, BOB, task_id)
    registry.pause(world, log, PLATFORM, AGENT, "incident")
    with pytest.raises(AwaitingAuthorization, match="paused"):
        acquire_execution(world, log, signed_in(BOB), task_id, "drafting")


def test_losing_the_customer_pauses_the_task(world: Engine, log: AuditLog) -> None:
    task_id = submit(world, log)
    authorize_stage(world, log, BOB, task_id)
    data = mapping_data()
    next(u for u in data["users"] if u["username"] == "bob")["customers"] = [QUILLON]
    apply_access_mapping(world, log, PLATFORM, AccessMapping.model_validate(data))
    with pytest.raises(AwaitingAuthorization, match="no longer assigned"):
        acquire_execution(world, log, signed_in(BOB), task_id, "drafting")


def test_unknown_task_or_stage(world: Engine, log: AuditLog) -> None:
    with pytest.raises(TaskError, match="does not exist"):
        acquire_execution(world, log, signed_in(BOB), "ghost", "drafting")
    task_id = submit(world, log)
    with pytest.raises(TaskError, match="no review stage"):
        acquire_execution(world, log, signed_in(BOB), task_id, "review")
    with world.connect() as conn:
        assert verify(conn).ok
