"""Registry and lifecycle: authorization, gates, pause, and audited refusals."""

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine, insert, select, update

from stewardry.platform import registry
from stewardry.platform.audit import AuditLog, verify
from stewardry.platform.db.schema import (
    ENGAGEMENT_STATUSES,
    agent_policy_versions,
    agents,
    audit_events,
    role_assignments,
    users,
)
from stewardry.platform.policy import AgentPolicyFile, load_agent_policy, load_baseline
from stewardry.platform.redaction import Redactor
from stewardry.platform.registry import AgentNotRunnable, NotAuthorized, RegistryError

ROOT = Path(__file__).resolve().parents[2]
AGENT_FILE = ROOT / "policies" / "agents" / "questionnaire-agent.yaml"
BASELINE = load_baseline(ROOT / "policies" / "platform-baseline.yaml")
AGENT = "questionnaire-agent"
NOW = dt.datetime(2026, 10, 10, tzinfo=dt.UTC)
PLATFORM, OWNER, GRC = "sub-platform", "sub-owner", "sub-bob"


@pytest.fixture
def db(engine: Engine) -> Iterator[Engine]:
    with engine.begin() as conn:
        for sub, name, role in [
            (PLATFORM, "platform", "platform_engineer"),
            (OWNER, "owner", "engagement_owner"),
            (GRC, "bob", "grc"),
        ]:
            conn.execute(
                insert(users).values(sub=sub, username=name, display_name=name, created_at=NOW)
            )
            conn.execute(
                insert(role_assignments).values(
                    sub=sub, role=role, assigned_by_sub=sub, assigned_at=NOW
                )
            )
    yield engine


@pytest.fixture
def log() -> AuditLog:
    return AuditLog(Redactor())


def agent_file(**policy_changes: Any) -> AgentPolicyFile:
    data = yaml.safe_load(AGENT_FILE.read_text())
    for path, value in policy_changes.items():
        target = data
        *parents, leaf = path.split("__")
        for key in parents:
            target = target[key]
        target[leaf] = value
    return AgentPolicyFile.model_validate(data)


def apply(db: Engine, log: AuditLog, file: AgentPolicyFile | None = None) -> registry.ApplyResult:
    return registry.apply_policy(db, log, PLATFORM, file or load_agent_policy(AGENT_FILE), BASELINE)


def evaluated(db: Engine, log: AuditLog) -> None:
    with db.connect() as conn:
        version = registry.current_policy_version(conn, AGENT)
    assert version is not None
    registry.accept_evaluation(db, log, OWNER, AGENT, version.policy_hash, "evals/report-1.md")


def status(db: Engine) -> tuple[str, str]:
    with db.connect() as conn:
        agent = registry.get_agent(conn, AGENT)
    assert agent is not None
    return agent.engagement_status, agent.operational_state


def events(db: Engine, outcome: str | None = None) -> list[tuple[str, str]]:
    query = select(audit_events.c.event_type, audit_events.c.outcome).order_by(audit_events.c.seq)
    with db.connect() as conn:
        rows = [tuple(r) for r in conn.execute(query)]
    return [r for r in rows if outcome is None or r[1] == outcome]


# ------------------------------------------------------------------- apply


def test_first_apply_registers_in_discovery_and_needs_evaluation(db: Engine, log: AuditLog) -> None:
    result = apply(db, log)
    assert result.created and result.reevaluation_required
    assert status(db) == ("discovery", "enabled")
    assert events(db) == [
        ("registry.agent_registered", "succeeded"),
        ("registry.policy_applied", "succeeded"),
    ]


def test_reapplying_the_same_file_creates_no_version(db: Engine, log: AuditLog) -> None:
    first = apply(db, log)
    again = apply(db, log)
    assert not again.created and again.policy_version_id == first.policy_version_id


def test_registration_changes_update_the_agent_without_a_new_version(
    db: Engine, log: AuditLog
) -> None:
    first = apply(db, log)
    again = apply(db, log, agent_file(registration__name="Renamed agent"))
    assert again.policy_version_id == first.policy_version_id
    with db.connect() as conn:
        assert registry.get_agent(conn, AGENT).name == "Renamed agent"  # type: ignore[union-attr]
    assert ("registry.agent_updated", "succeeded") in events(db)


def test_budget_change_keeps_evaluation_but_model_change_needs_a_new_one(
    db: Engine, log: AuditLog
) -> None:
    apply(db, log)
    evaluated(db, log)
    budget = apply(
        db,
        log,
        agent_file(
            policy__budgets={"per_run_usd": 2, "per_run_model_calls": 9, "per_run_tool_calls": 9}
        ),
    )
    assert budget.created and not budget.reevaluation_required and budget.gated_changes == ()
    model = apply(
        db,
        log,
        agent_file(
            policy__model={
                "id": "claude-opus-5-5",
                "effort": "high",
                "prompt_version": "questionnaire-draft@1",
            }
        ),
    )
    assert model.created and model.reevaluation_required and model.gated_changes == ("model",)


def test_an_older_version_cannot_be_reactivated(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    apply(
        db,
        log,
        agent_file(policy__customer_scope="per_task", policy__model_tools=["submit_answer"]),
    )
    with pytest.raises(RegistryError, match="older version"):
        apply(db, log)
    assert events(db, "failed") == [("registry.policy_applied", "failed")]


def test_only_a_platform_engineer_can_apply(db: Engine, log: AuditLog) -> None:
    with pytest.raises(NotAuthorized, match="platform_engineer"):
        registry.apply_policy(db, log, GRC, load_agent_policy(AGENT_FILE), BASELINE)
    with db.connect() as conn:
        assert registry.get_agent(conn, AGENT) is None
    assert events(db) == [("registry.policy_applied", "denied")]


def test_baseline_violations_are_refused_and_audited(db: Engine, log: AuditLog) -> None:
    file = agent_file(
        policy__model={
            "id": "other-model",
            "effort": "low",
            "prompt_version": "questionnaire-draft@1",
        }
    )
    with pytest.raises(RegistryError, match="not allowed by the platform baseline"):
        apply(db, log, file)
    assert events(db) == [("registry.policy_applied", "denied")]


def test_unknown_owners_are_refused(db: Engine, log: AuditLog) -> None:
    with pytest.raises(RegistryError, match="unknown owner"):
        apply(db, log, agent_file(registration__business_owner="nobody"))
    assert events(db) == [("registry.policy_applied", "failed")]


def test_retired_agents_cannot_get_new_policies(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    registry.retire(db, log, PLATFORM, AGENT, "replaced")
    with pytest.raises(RegistryError, match="retired"):
        apply(db, log, agent_file(registration__name="Back again"))


# ------------------------------------------------------------- state machine


@pytest.mark.parametrize("from_status", [s for s in ENGAGEMENT_STATUSES if s != "retired"])
@pytest.mark.parametrize("to_status", ENGAGEMENT_STATUSES)
def test_lifecycle_state_machine(
    db: Engine, log: AuditLog, from_status: str, to_status: str
) -> None:
    apply(db, log)
    evaluated(db, log)
    with db.begin() as conn:
        conn.execute(update(agents).values(engagement_status=from_status))
    allowed = to_status in registry.TRANSITIONS[from_status]
    if allowed:
        registry.transition_status(db, log, OWNER, AGENT, to_status, "docs/gates/g1.md")
        assert status(db)[0] == to_status
    else:
        with pytest.raises(RegistryError, match="cannot move"):
            registry.transition_status(db, log, OWNER, AGENT, to_status, "docs/gates/g1.md")
        assert status(db)[0] == from_status


def test_gates_need_the_owner_role_and_evidence(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    with pytest.raises(NotAuthorized):
        registry.transition_status(db, log, PLATFORM, AGENT, "build", "brief.md")
    with pytest.raises(RegistryError, match="evidence"):
        registry.transition_status(db, log, OWNER, AGENT, "build", "  ")
    registry.transition_status(db, log, OWNER, AGENT, "build", "docs/brief.md")
    assert status(db)[0] == "build"


def test_status_cannot_advance_while_reevaluation_is_required(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    registry.transition_status(db, log, OWNER, AGENT, "build", "docs/brief.md")
    with pytest.raises(RegistryError, match="accepted evaluation"):
        registry.transition_status(db, log, OWNER, AGENT, "shadow", "gate.md")
    evaluated(db, log)
    registry.transition_status(db, log, OWNER, AGENT, "shadow", "gate.md")
    assert status(db)[0] == "shadow"


def test_evaluation_must_cover_the_current_version(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    with pytest.raises(RegistryError, match="current policy version"):
        registry.accept_evaluation(db, log, OWNER, AGENT, "0" * 64, "report.md")
    with pytest.raises(RegistryError, match="report reference"):
        registry.accept_evaluation(db, log, OWNER, AGENT, "0" * 64, "")
    with pytest.raises(NotAuthorized):
        registry.accept_evaluation(db, log, GRC, AGENT, "0" * 64, "report.md")


def test_unregistered_agents_cannot_transition(db: Engine, log: AuditLog) -> None:
    with pytest.raises(RegistryError, match="not registered"):
        registry.transition_status(db, log, OWNER, "ghost-agent", "build", "x")


# -------------------------------------------------- pause, resume, retire


def test_pause_and_resume_need_a_platform_engineer_and_a_reason(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    with pytest.raises(NotAuthorized):
        registry.pause(db, log, OWNER, AGENT, "incident")
    with pytest.raises(RegistryError, match="reason"):
        registry.pause(db, log, PLATFORM, AGENT, "")
    registry.pause(db, log, PLATFORM, AGENT, "incident 1")
    assert status(db)[1] == "paused"
    with pytest.raises(RegistryError, match="not enabled"):
        registry.pause(db, log, PLATFORM, AGENT, "again")
    registry.resume(db, log, PLATFORM, AGENT, "incident 1 resolved")
    assert status(db)[1] == "enabled"
    with pytest.raises(RegistryError, match="not paused"):
        registry.resume(db, log, PLATFORM, AGENT, "again")


def test_retired_agents_stay_retired(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    registry.retire(db, log, PLATFORM, AGENT, "replaced")
    assert status(db) == ("retired", "paused")
    for operation in (registry.resume, registry.pause, registry.retire):
        with pytest.raises(RegistryError, match="retired"):
            operation(db, log, PLATFORM, AGENT, "try")
    with pytest.raises(RegistryError, match="not registered"):
        registry.pause(db, log, PLATFORM, "ghost-agent", "x")


# ------------------------------------------------------- runnable boundary


def test_only_enabled_agents_past_discovery_can_run(db: Engine, log: AuditLog) -> None:
    with db.connect() as conn, pytest.raises(AgentNotRunnable, match="not registered"):
        registry.check_runnable(conn, AGENT)
    apply(db, log)
    with db.connect() as conn, pytest.raises(AgentNotRunnable, match="discovery"):
        registry.check_runnable(conn, AGENT)
    registry.transition_status(db, log, OWNER, AGENT, "build", "docs/brief.md")
    with db.connect() as conn:
        assert registry.check_runnable(conn, AGENT).agent_id == AGENT
    registry.retire(db, log, PLATFORM, AGENT, "done")
    with db.connect() as conn, pytest.raises(AgentNotRunnable, match="retired"):
        registry.check_runnable(conn, AGENT)


def test_pause_stops_the_next_boundary_of_a_running_loop(db: Engine, log: AuditLog) -> None:
    """The executor (S5) calls check_runnable before every model or tool call."""
    version = apply(db, log).policy_version_id
    registry.transition_status(db, log, OWNER, AGENT, "build", "docs/brief.md")
    completed = []
    with pytest.raises(AgentNotRunnable, match="paused"):
        for step in range(5):
            with db.connect() as conn:
                registry.check_runnable(conn, AGENT, version)
            completed.append(step)
            if step == 1:
                registry.pause(db, log, PLATFORM, AGENT, "operator stop")
    assert completed == [0, 1]


def test_a_run_cannot_use_another_agents_policy_version(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    registry.transition_status(db, log, OWNER, AGENT, "build", "docs/brief.md")
    with db.connect() as conn, pytest.raises(AgentNotRunnable, match="not questionnaire-agent's"):
        registry.check_runnable(conn, AGENT, 999)


# ----------------------------------------------------------------- audit


def test_refusals_are_recorded_and_the_chain_stays_valid(db: Engine, log: AuditLog) -> None:
    apply(db, log)
    for attempt in (
        lambda: registry.pause(db, log, GRC, AGENT, "not mine"),
        lambda: registry.transition_status(db, log, OWNER, AGENT, "live", "x"),
    ):
        with pytest.raises(RegistryError):
            attempt()
    assert events(db, "denied") == [
        ("registry.paused", "denied"),
        ("registry.status_changed", "denied"),
    ]
    with db.connect() as conn:
        assert verify(conn).ok
        versions = conn.execute(select(agent_policy_versions.c.id)).scalars().all()
    assert versions == [1]
