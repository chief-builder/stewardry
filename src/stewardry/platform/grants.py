"""Tasks, task grants, and whose authority a run uses (spec sections 4.3, 4.3.2).

- Sales submits a task for an assigned customer. Submission authorizes intake
  reads under the requester's own identity only.
- A user with a drafting role (GRC) explicitly authorizes drafting under their
  own identity. That creates a time-limited task grant bound to their hub
  subject, the task, and the agent's current policy version.
- `acquire_execution` is the only way to get the acting identity for a run. It
  reads it from the active grant (or the requester, for stages that need no
  grant), never from task fields or caller arguments. If the grant has expired
  or been revoked, or that user's credential is not available, the task pauses.
  There is no fallback to another identity.

Each function runs its own transactions; refusals are audited before raising.
"""

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from sqlalchemy import Connection, Engine, Row, insert, select, update

from stewardry.platform import registry
from stewardry.platform.access import assigned_customers
from stewardry.platform.audit import AuditEvent, AuditLog, record_now
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db.schema import task_grants, tasks, utcnow
from stewardry.platform.roles import active_roles

DEFAULT_GRANT_TTL = dt.timedelta(hours=8)
MAX_GRANT_TTL = dt.timedelta(hours=24)
AUTHORIZABLE_STATUSES = frozenset({"awaiting_authorization", "paused"})


class TaskError(Exception):
    """A task request was refused. The refusal has been audited."""


class AwaitingAuthorization(TaskError):
    """The run cannot proceed under valid authority; the task is paused."""


@dataclass(frozen=True)
class Execution:
    """Everything a run needs to act, resolved from protected records."""

    task_id: str
    agent_id: str
    customer_id: str
    stage: str
    acting_sub: str
    grant_id: str | None
    policy_version_id: int
    credential: GatewayCredential


def _refuse(
    engine: Engine,
    log: AuditLog,
    event_type: str,
    actor_sub: str | None,
    reason: str,
    ids: Mapping[str, Any] | None = None,
) -> NoReturn:
    record_now(
        engine,
        log,
        AuditEvent(
            event_type, "denied", actor_sub=actor_sub, details={"reason": reason}, **(ids or {})
        ),
    )
    raise TaskError(reason)


def get_task(conn: Connection, task_id: str) -> Row[Any] | None:
    return conn.execute(select(tasks).where(tasks.c.task_id == task_id)).first()


def active_grant(conn: Connection, task_id: str, stage: str, now: dt.datetime) -> Row[Any] | None:
    return conn.execute(
        select(task_grants)
        .where(
            task_grants.c.task_id == task_id,
            task_grants.c.stage == stage,
            task_grants.c.revoked_at.is_(None),
            task_grants.c.expires_at > now,
        )
        .order_by(task_grants.c.granted_at.desc())
        .limit(1)
    ).first()


# ------------------------------------------------------------------ submit


def submit_task(
    engine: Engine,
    log: AuditLog,
    requester_sub: str,
    agent_id: str,
    customer_id: str,
    submission_key: str,
    jira_issue_key: str | None = None,
) -> str:
    """Create a task, or return the existing one for a repeated submission."""
    event = "task.submitted"
    ids = {"agent_id": agent_id, "customer_id": customer_id}
    if not submission_key.strip():
        _refuse(engine, log, event, requester_sub, "a submission key is required", ids)
    with engine.connect() as conn:
        try:
            registry.check_runnable(conn, agent_id)
        except registry.AgentNotRunnable as exc:
            _refuse(engine, log, event, requester_sub, str(exc), ids)
        current = registry.current_policy(conn, agent_id)
        roles = active_roles(conn, requester_sub)
        customers = assigned_customers(conn, requester_sub)
        duplicate = conn.execute(
            select(tasks.c.task_id).where(
                tasks.c.requester_sub == requester_sub, tasks.c.submission_key == submission_key
            )
        ).scalar()
    if current is None or "intake" not in current[1].stages:
        _refuse(engine, log, event, requester_sub, "agent has no intake stage", ids)
    version, policy = current
    if not roles & set(policy.stages["intake"].acting_roles):
        _refuse(engine, log, event, requester_sub, "requester has no intake role", ids)
    if customer_id not in customers:
        _refuse(
            engine, log, event, requester_sub, "requester is not assigned to this customer", ids
        )
    if duplicate is not None:
        record_now(
            engine,
            log,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=requester_sub,
                requester_sub=requester_sub,
                task_id=duplicate,
                details={"duplicate": True},
                **ids,
            ),
        )
        return str(duplicate)

    task_id = uuid.uuid4().hex
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            insert(tasks).values(
                task_id=task_id,
                agent_id=agent_id,
                customer_id=customer_id,
                requester_sub=requester_sub,
                submission_key=submission_key,
                jira_issue_key=jira_issue_key,
                status="awaiting_authorization",
                created_at=now,
                updated_at=now,
            )
        )
        log.append(
            conn,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=requester_sub,
                requester_sub=requester_sub,
                task_id=task_id,
                policy_hash=version.policy_hash,
                details={"jira_issue_key": jira_issue_key},
                **ids,
            ),
        )
    return task_id


# --------------------------------------------------------------- authorize


def authorize_stage(
    engine: Engine,
    log: AuditLog,
    authorizer_sub: str,
    task_id: str,
    stage: str = "drafting",
    ttl: dt.timedelta = DEFAULT_GRANT_TTL,
) -> str:
    """The authorizing user grants work on this task under their own identity."""
    event = "task.authorized"
    now = utcnow()
    with engine.connect() as conn:
        task = get_task(conn, task_id)
    if task is None:
        _refuse(engine, log, event, authorizer_sub, "task does not exist", {"task_id": task_id})
    ids = {
        "task_id": task_id,
        "agent_id": task.agent_id,
        "customer_id": task.customer_id,
        "requester_sub": task.requester_sub,
    }
    with engine.connect() as conn:
        try:
            registry.check_runnable(conn, task.agent_id)
        except registry.AgentNotRunnable as exc:
            _refuse(engine, log, event, authorizer_sub, str(exc), ids)
        current = registry.current_policy(conn, task.agent_id)
        roles = active_roles(conn, authorizer_sub)
        customers = assigned_customers(conn, authorizer_sub)
        existing = active_grant(conn, task_id, stage, now)
    if current is None or stage not in current[1].stages:
        _refuse(engine, log, event, authorizer_sub, f"agent has no {stage} stage", ids)
    version, policy = current
    if not policy.stages[stage].requires_task_grant:
        _refuse(engine, log, event, authorizer_sub, f"stage {stage} does not take a grant", ids)
    if not roles & set(policy.stages[stage].acting_roles):
        _refuse(engine, log, event, authorizer_sub, f"authorizer has no {stage} role", ids)
    if authorizer_sub == task.requester_sub:
        _refuse(
            engine,
            log,
            event,
            authorizer_sub,
            "the requester cannot authorize their own task",
            ids,
        )
    if task.customer_id not in customers:
        _refuse(
            engine, log, event, authorizer_sub, "authorizer is not assigned to this customer", ids
        )
    if not dt.timedelta(0) < ttl <= MAX_GRANT_TTL:
        _refuse(
            engine,
            log,
            event,
            authorizer_sub,
            f"grant lifetime must be within {MAX_GRANT_TTL}",
            ids,
        )
    if existing is not None:
        if existing.authorizing_sub == authorizer_sub:
            return str(existing.grant_id)  # repeated click: same grant, no new authority
        _refuse(
            engine,
            log,
            event,
            authorizer_sub,
            "another user has an active grant on this task; it must be revoked first",
            ids,
        )
    if task.status not in AUTHORIZABLE_STATUSES:
        _refuse(engine, log, event, authorizer_sub, f"task is {task.status}", ids)

    grant_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(
            insert(task_grants).values(
                grant_id=grant_id,
                task_id=task_id,
                authorizing_sub=authorizer_sub,
                policy_version_id=version.id,
                stage=stage,
                granted_at=now,
                expires_at=now + ttl,
            )
        )
        conn.execute(
            update(tasks)
            .where(tasks.c.task_id == task_id)
            .values(status="drafting", updated_at=now)
        )
        log.append(
            conn,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=authorizer_sub,
                authorizing_sub=authorizer_sub,
                grant_id=grant_id,
                policy_hash=version.policy_hash,
                details={"stage": stage, "expires_at": (now + ttl).isoformat()},
                **ids,
            ),
        )
    return grant_id


def revoke_grant(engine: Engine, log: AuditLog, actor_sub: str, grant_id: str, reason: str) -> None:
    """The authorizing user or a platform engineer cancels a grant. The task pauses."""
    event = "task.grant_revoked"
    with engine.connect() as conn:
        grant = conn.execute(select(task_grants).where(task_grants.c.grant_id == grant_id)).first()
        roles = active_roles(conn, actor_sub)
    if grant is None:
        _refuse(engine, log, event, actor_sub, "grant does not exist", {"grant_id": grant_id})
    ids = {"grant_id": grant_id, "task_id": grant.task_id}
    if actor_sub != grant.authorizing_sub and "platform_engineer" not in roles:
        _refuse(
            engine,
            log,
            event,
            actor_sub,
            "only the authorizing user or a platform engineer can revoke",
            ids,
        )
    if not reason.strip():
        _refuse(engine, log, event, actor_sub, "a reason is required", ids)
    if grant.revoked_at is not None:
        _refuse(engine, log, event, actor_sub, "grant is already revoked", ids)
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(task_grants)
            .where(task_grants.c.grant_id == grant_id)
            .values(revoked_at=now, revoke_reason=reason)
        )
        conn.execute(
            update(tasks)
            .where(tasks.c.task_id == grant.task_id)
            .values(status="paused", updated_at=now)
        )
        log.append(
            conn,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=actor_sub,
                authorizing_sub=grant.authorizing_sub,
                details={"reason": reason},
                **ids,
            ),
        )


# ----------------------------------------------------------------- execute


def _pause(
    engine: Engine,
    log: AuditLog,
    task: Row[Any],
    stage: str,
    reason: str,
    error: type[TaskError],
    **ids: Any,
) -> NoReturn:
    with engine.begin() as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.task_id == task.task_id)
            .values(status="paused", updated_at=utcnow())
        )
        log.append(
            conn,
            AuditEvent(
                "task.execution",
                "denied",
                task_id=task.task_id,
                agent_id=task.agent_id,
                customer_id=task.customer_id,
                requester_sub=task.requester_sub,
                details={"stage": stage, "reason": reason, "task_paused": True},
                **ids,
            ),
        )
    raise error(reason)


def acquire_execution(
    engine: Engine, log: AuditLog, credentials: CredentialStore, task_id: str, stage: str
) -> Execution:
    """Resolve whose authority a run uses, from protected records only."""
    now = utcnow()
    with engine.connect() as conn:
        task = get_task(conn, task_id)
        if task is None:
            raise TaskError("task does not exist")
        current = registry.current_policy(conn, task.agent_id)
        grant = active_grant(conn, task_id, stage, now)
        latest = conn.execute(
            select(task_grants)
            .where(task_grants.c.task_id == task_id, task_grants.c.stage == stage)
            .order_by(task_grants.c.granted_at.desc())
            .limit(1)
        ).first()
    if current is None or stage not in current[1].stages:
        _pause(engine, log, task, stage, f"agent has no {stage} stage", TaskError)
    version, policy = current

    if policy.stages[stage].requires_task_grant:
        if grant is None:
            if latest is None:
                reason = "drafting has not been authorized"
            elif latest.revoked_at is not None:
                reason = "the grant was revoked"
            else:
                reason = "the grant has expired"
            _pause(engine, log, task, stage, reason, AwaitingAuthorization)
        acting_sub, grant_id, policy_version_id = (
            grant.authorizing_sub,
            grant.grant_id,
            grant.policy_version_id,
        )
    else:
        acting_sub, grant_id, policy_version_id = task.requester_sub, None, version.id

    with engine.connect() as conn:
        try:
            registry.check_runnable(conn, task.agent_id, policy_version_id)
        except registry.AgentNotRunnable as exc:
            _pause(
                engine,
                log,
                task,
                stage,
                str(exc),
                AwaitingAuthorization,
                actor_sub=acting_sub,
                grant_id=grant_id,
            )
        still_assigned = task.customer_id in assigned_customers(conn, acting_sub)
    if not still_assigned:
        _pause(
            engine,
            log,
            task,
            stage,
            "acting user is no longer assigned to this customer",
            AwaitingAuthorization,
            actor_sub=acting_sub,
            grant_id=grant_id,
        )

    credential = credentials.get(acting_sub, now)
    if credential is None:
        _pause(
            engine,
            log,
            task,
            stage,
            "the acting user's credential is not available; they must sign in again",
            AwaitingAuthorization,
            actor_sub=acting_sub,
            grant_id=grant_id,
        )
    return Execution(
        task.task_id,
        task.agent_id,
        task.customer_id,
        stage,
        acting_sub,
        grant_id,
        policy_version_id,
        credential,
    )
