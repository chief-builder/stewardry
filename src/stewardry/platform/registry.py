"""Agent registry and lifecycle (spec section 4.2, ADR 0009).

Each function runs its own transaction. A successful change commits with its
audit event. A denied or failed request commits only its audit event, then
raises, so blocked attempts are always recorded (spec section 4.5).

Who may do what (spec section 3):
- platform engineer: register agents and apply policies, pause, resume, retire;
- engagement owner: move an agent through the rollout gates, accept evals.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, NoReturn

from sqlalchemy import Connection, Engine, Row, insert, select, update

from stewardry.platform.audit import AuditEvent, AuditLog
from stewardry.platform.db.schema import agent_policy_versions, agents, users, utcnow
from stewardry.platform.policy import AgentPolicyFile, PlatformBaseline, PolicyError, gated_changes
from stewardry.platform.roles import active_roles

PLATFORM_ENGINEER = "platform_engineer"
ENGAGEMENT_OWNER = "engagement_owner"

# Allowed engagement-status moves (spec section 9). Retiring is separate.
TRANSITIONS: dict[str, frozenset[str]] = {
    "discovery": frozenset({"build"}),
    "build": frozenset({"shadow"}),
    "shadow": frozenset({"assisted", "build"}),
    "assisted": frozenset({"live", "shadow"}),
    "live": frozenset({"handed_off", "assisted"}),
    "handed_off": frozenset(),
    "retired": frozenset(),
}
# Statuses that use the agent with real sessions: an un-evaluated policy
# version cannot move into them.
EVALUATED_STATUSES = frozenset({"shadow", "assisted", "live", "handed_off"})
# Statuses in which runs may start (build validates against fixtures).
RUNNABLE_STATUSES = frozenset({"build", "shadow", "assisted", "live", "handed_off"})


class RegistryError(Exception):
    """A registry request was refused. The refusal has been audited."""


class NotAuthorized(RegistryError):
    pass


class AgentNotRunnable(RegistryError):
    pass


@dataclass(frozen=True)
class ApplyResult:
    agent_id: str
    policy_version_id: int
    policy_hash: str
    created: bool
    reevaluation_required: bool
    gated_changes: tuple[str, ...]


def _baseline_hash(baseline: PlatformBaseline) -> str:
    text = json.dumps(baseline.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _refuse(
    engine: Engine,
    log: AuditLog,
    event_type: str,
    actor_sub: str,
    agent_id: str | None,
    reason: str,
    error: type[RegistryError] = RegistryError,
    outcome: str = "denied",
) -> NoReturn:
    with engine.begin() as conn:
        log.append(
            conn,
            AuditEvent(
                event_type,
                outcome,
                actor_sub=actor_sub,
                agent_id=agent_id,
                details={"reason": reason},
            ),
        )
    raise error(reason)


def _require_role(
    engine: Engine, log: AuditLog, event_type: str, actor_sub: str, agent_id: str | None, role: str
) -> None:
    with engine.connect() as conn:
        roles = active_roles(conn, actor_sub)
    if role not in roles:
        _refuse(
            engine, log, event_type, actor_sub, agent_id, f"requires the {role} role", NotAuthorized
        )


def get_agent(conn: Connection, agent_id: str) -> Row[Any] | None:
    return conn.execute(select(agents).where(agents.c.agent_id == agent_id)).first()


def current_policy_version(conn: Connection, agent_id: str) -> Row[Any] | None:
    return conn.execute(
        select(agent_policy_versions)
        .where(agent_policy_versions.c.agent_id == agent_id)
        .order_by(agent_policy_versions.c.id.desc())
        .limit(1)
    ).first()


def _sub_for(conn: Connection, username: str) -> str | None:
    return conn.execute(select(users.c.sub).where(users.c.username == username)).scalar()


# ------------------------------------------------------------------ policy


def apply_policy(
    engine: Engine,
    log: AuditLog,
    actor_sub: str,
    agent_file: AgentPolicyFile,
    baseline: PlatformBaseline,
) -> ApplyResult:
    """Register the agent if new, update its registration, and store the policy
    as a new immutable version unless it matches the current one."""
    event = "registry.policy_applied"
    agent_id = agent_file.agent_id
    _require_role(engine, log, event, actor_sub, agent_id, PLATFORM_ENGINEER)
    try:
        baseline.check(agent_file)
    except PolicyError as exc:
        _refuse(engine, log, event, actor_sub, agent_id, str(exc))

    reg = agent_file.registration
    with engine.connect() as conn:
        owners = {
            "business_owner_sub": _sub_for(conn, reg.business_owner),
            "operational_owner_sub": _sub_for(conn, reg.operational_owner),
        }
        existing = get_agent(conn, agent_id)
        current = current_policy_version(conn, agent_id)
        older = conn.execute(
            select(agent_policy_versions.c.id).where(
                agent_policy_versions.c.agent_id == agent_id,
                agent_policy_versions.c.policy_hash == agent_file.policy_hash(),
            )
        ).scalar()
    missing = [name for name, sub in owners.items() if sub is None]
    if missing:
        _refuse(
            engine,
            log,
            event,
            actor_sub,
            agent_id,
            f"unknown owner user(s): {missing}",
            outcome="failed",
        )
    if existing is not None and existing.engagement_status == "retired":
        _refuse(engine, log, event, actor_sub, agent_id, "agent is retired")
    if older is not None and (current is None or older != current.id):
        _refuse(
            engine,
            log,
            event,
            actor_sub,
            agent_id,
            f"policy matches older version {older}; re-activating a version is not supported",
            outcome="failed",
        )

    now = utcnow()
    record = {
        "name": reg.name,
        "purpose": reg.purpose,
        "owning_team": reg.owning_team,
        "risk_tier": reg.risk_tier,
        "runbook_url": reg.runbook_url,
        **owners,
    }
    with engine.begin() as conn:
        if existing is None:
            conn.execute(
                insert(agents).values(
                    agent_id=agent_id,
                    engagement_status="discovery",
                    operational_state="enabled",
                    created_at=now,
                    updated_at=now,
                    **record,
                )
            )
            log.append(
                conn,
                AuditEvent(
                    "registry.agent_registered",
                    "succeeded",
                    actor_sub=actor_sub,
                    agent_id=agent_id,
                    details={"engagement_status": "discovery"},
                ),
            )
        else:
            changed = sorted(k for k, v in record.items() if existing._mapping[k] != v)
            if changed:
                conn.execute(
                    update(agents)
                    .where(agents.c.agent_id == agent_id)
                    .values(updated_at=now, **record)
                )
                log.append(
                    conn,
                    AuditEvent(
                        "registry.agent_updated",
                        "succeeded",
                        actor_sub=actor_sub,
                        agent_id=agent_id,
                        details={"changed": changed},
                    ),
                )

        if current is not None and current.policy_hash == agent_file.policy_hash():
            log.append(
                conn,
                AuditEvent(
                    event,
                    "succeeded",
                    actor_sub=actor_sub,
                    agent_id=agent_id,
                    policy_hash=current.policy_hash,
                    details={"new_version": False},
                ),
            )
            return ApplyResult(
                agent_id, current.id, current.policy_hash, False, current.reevaluation_required, ()
            )

        changes = tuple(gated_changes(current.policy_json, agent_file)) if current else ()
        needs_eval = current is None or bool(changes) or current.reevaluation_required
        inserted = conn.execute(
            insert(agent_policy_versions).values(
                agent_id=agent_id,
                policy_hash=agent_file.policy_hash(),
                schema_version=agent_file.schema_version,
                policy_json=agent_file.policy_json(),
                applied_by_sub=actor_sub,
                applied_at=now,
                reevaluation_required=needs_eval,
            )
        )
        version_id = int(inserted.lastrowid)
        log.append(
            conn,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=actor_sub,
                agent_id=agent_id,
                policy_hash=agent_file.policy_hash(),
                details={
                    "new_version": True,
                    "policy_version_id": version_id,
                    "gated_changes": list(changes),
                    "reevaluation_required": needs_eval,
                    "baseline_hash": _baseline_hash(baseline),
                },
            ),
        )
    return ApplyResult(agent_id, version_id, agent_file.policy_hash(), True, needs_eval, changes)


def accept_evaluation(
    engine: Engine, log: AuditLog, actor_sub: str, agent_id: str, policy_hash: str, report_ref: str
) -> None:
    """Record that an accepted eval report covers the current policy version."""
    event = "registry.evaluation_accepted"
    _require_role(engine, log, event, actor_sub, agent_id, ENGAGEMENT_OWNER)
    if not report_ref.strip():
        _refuse(engine, log, event, actor_sub, agent_id, "an eval report reference is required")
    with engine.connect() as conn:
        current = current_policy_version(conn, agent_id)
    if current is None or current.policy_hash != policy_hash:
        _refuse(
            engine,
            log,
            event,
            actor_sub,
            agent_id,
            "the report must cover the agent's current policy version",
        )
    with engine.begin() as conn:
        conn.execute(
            update(agent_policy_versions)
            .where(agent_policy_versions.c.id == current.id)
            .values(reevaluation_required=False)
        )
        log.append(
            conn,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=actor_sub,
                agent_id=agent_id,
                policy_hash=policy_hash,
                details={"report_ref": report_ref, "policy_version_id": current.id},
            ),
        )


# --------------------------------------------------------------- lifecycle


def transition_status(
    engine: Engine, log: AuditLog, actor_sub: str, agent_id: str, to_status: str, evidence_ref: str
) -> None:
    """Move an agent through a rollout gate, with the gate evidence attached."""
    event = "registry.status_changed"
    _require_role(engine, log, event, actor_sub, agent_id, ENGAGEMENT_OWNER)
    with engine.connect() as conn:
        agent = get_agent(conn, agent_id)
        version = current_policy_version(conn, agent_id)
    if agent is None:
        _refuse(engine, log, event, actor_sub, agent_id, "agent is not registered")
    from_status = agent.engagement_status
    if to_status not in TRANSITIONS.get(from_status, frozenset()):
        _refuse(
            engine,
            log,
            event,
            actor_sub,
            agent_id,
            f"cannot move from {from_status} to {to_status}",
        )
    if not evidence_ref.strip():
        _refuse(engine, log, event, actor_sub, agent_id, "gate evidence is required")
    if to_status in EVALUATED_STATUSES and (version is None or version.reevaluation_required):
        _refuse(
            engine,
            log,
            event,
            actor_sub,
            agent_id,
            "the current policy version needs an accepted evaluation first",
        )
    with engine.begin() as conn:
        conn.execute(
            update(agents)
            .where(agents.c.agent_id == agent_id)
            .values(engagement_status=to_status, updated_at=utcnow())
        )
        log.append(
            conn,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=actor_sub,
                agent_id=agent_id,
                policy_hash=version.policy_hash if version else None,
                details={"from": from_status, "to": to_status, "evidence_ref": evidence_ref},
            ),
        )


def _operate(
    engine: Engine,
    log: AuditLog,
    actor_sub: str,
    agent_id: str,
    reason: str,
    event: str,
    allowed_from: str,
    values: dict[str, str],
    what: str,
) -> None:
    _require_role(engine, log, event, actor_sub, agent_id, PLATFORM_ENGINEER)
    if not reason.strip():
        _refuse(engine, log, event, actor_sub, agent_id, "a reason is required")
    with engine.connect() as conn:
        agent = get_agent(conn, agent_id)
    if agent is None:
        _refuse(engine, log, event, actor_sub, agent_id, "agent is not registered")
    if agent.engagement_status == "retired":
        _refuse(engine, log, event, actor_sub, agent_id, "agent is retired")
    if allowed_from and agent.operational_state != allowed_from:
        _refuse(engine, log, event, actor_sub, agent_id, f"agent is not {allowed_from}")
    with engine.begin() as conn:
        conn.execute(
            update(agents)
            .where(agents.c.agent_id == agent_id)
            .values(updated_at=utcnow(), **values)
        )
        log.append(
            conn,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=actor_sub,
                agent_id=agent_id,
                details={"reason": reason, "change": what},
            ),
        )


def pause(engine: Engine, log: AuditLog, actor_sub: str, agent_id: str, reason: str) -> None:
    _operate(
        engine,
        log,
        actor_sub,
        agent_id,
        reason,
        "registry.paused",
        "enabled",
        {"operational_state": "paused"},
        "enabled -> paused",
    )


def resume(engine: Engine, log: AuditLog, actor_sub: str, agent_id: str, reason: str) -> None:
    _operate(
        engine,
        log,
        actor_sub,
        agent_id,
        reason,
        "registry.resumed",
        "paused",
        {"operational_state": "enabled"},
        "paused -> enabled",
    )


def retire(engine: Engine, log: AuditLog, actor_sub: str, agent_id: str, reason: str) -> None:
    _operate(
        engine,
        log,
        actor_sub,
        agent_id,
        reason,
        "registry.retired",
        "",
        {"engagement_status": "retired", "operational_state": "paused"},
        "-> retired",
    )


# ------------------------------------------------------------ boundary check


def check_runnable(
    conn: Connection, agent_id: str, bound_policy_version_id: int | None = None
) -> Row[Any]:
    """Called at run admission and at every controlled boundary (each model or
    tool call, from S5 on). Raises AgentNotRunnable; the caller audits it."""
    agent = get_agent(conn, agent_id)
    if agent is None:
        raise AgentNotRunnable(f"agent {agent_id} is not registered")
    if agent.engagement_status not in RUNNABLE_STATUSES:
        raise AgentNotRunnable(f"agent {agent_id} is {agent.engagement_status}")
    if agent.operational_state != "enabled":
        raise AgentNotRunnable(f"agent {agent_id} is {agent.operational_state}")
    if bound_policy_version_id is not None:
        bound = conn.execute(
            select(agent_policy_versions.c.agent_id).where(
                agent_policy_versions.c.id == bound_policy_version_id
            )
        ).scalar()
        if bound != agent_id:
            raise AgentNotRunnable(f"policy version {bound_policy_version_id} is not {agent_id}'s")
    return agent
