"""The executor against a real database: admission, calls, and mid-run changes."""

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, select, update

from stewardry.platform import registry
from stewardry.platform.access import AccessMapping, apply_access_mapping
from stewardry.platform.audit import AuditLog, verify
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db.schema import audit_events, run_counters, runs, task_grants, tasks
from stewardry.platform.execution import (
    MAX_DENIED_CALLS,
    ConfluenceListPagesArgs,
    ConfluenceReadPageArgs,
    Executor,
    RunContext,
    RunStopped,
    ToolCall,
    ToolSpec,
)
from stewardry.platform.grants import authorize_stage, revoke_grant, submit_task
from stewardry.platform.policy import AgentPolicyFile, load_baseline
from tests.unit.conftest import AGENT, ALICE, BOB, LARKSPUR, PLATFORM, ROOT, mapping_data

BASELINE = load_baseline(ROOT / "policies/platform-baseline.yaml")
CATALOG = "policies/catalogs/secpol.yaml"
SEEN: list[tuple[str, str | None]] = []


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


def list_policies(run: RunContext, args: BaseModel, credential: GatewayCredential | None) -> Any:
    SEEN.append(("list_task_policies", None if credential is None else credential.access_token))
    return ["SEC-1", "SEC-2"]


def read_page(run: RunContext, args: BaseModel, credential: GatewayCredential | None) -> Any:
    assert isinstance(args, ConfluenceReadPageArgs)
    SEEN.append(("read_page", None if credential is None else credential.access_token))
    return {"page_id": args.page_id}


def list_pages(run: RunContext, args: BaseModel, credential: GatewayCredential | None) -> Any:
    assert isinstance(args, ConfluenceListPagesArgs)
    return [{"id": "101"}]


def broken(run: RunContext, args: BaseModel, credential: GatewayCredential | None) -> Any:
    raise RuntimeError("upstream exploded with token-bob")


TOOLS = {
    "list_task_policies": ToolSpec("list_task_policies", "model_tool", NoArgs, list_policies),
    "route_to_expert": ToolSpec("route_to_expert", "model_tool", NoArgs, broken),
    "confluence.read_page": ToolSpec(
        "confluence.read_page", "operation", ConfluenceReadPageArgs, read_page
    ),
    "confluence.list_pages": ToolSpec(
        "confluence.list_pages", "operation", ConfluenceListPagesArgs, list_pages
    ),
}


def catalogs(ref: str) -> frozenset[str]:
    return frozenset({"101", "102"}) if ref == CATALOG else frozenset()


def policy_file(**policy_changes: Any) -> AgentPolicyFile:
    data = yaml.safe_load((ROOT / "policies/agents/questionnaire-agent.yaml").read_text())
    data["policy"].update(policy_changes)
    return AgentPolicyFile.model_validate(data)


def apply(world: Engine, log: AuditLog, **policy_changes: Any) -> None:
    registry.apply_policy(world, log, PLATFORM, policy_file(**policy_changes), BASELINE)


def without(op: str) -> dict[str, Any]:
    ops = policy_file().policy.model_dump(mode="json")["operations"]
    ops.pop(op)
    return {"operations": ops}


@pytest.fixture
def credentials() -> CredentialStore:
    store = CredentialStore()
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)
    for sub in (ALICE, BOB):
        store.put(f"s-{sub}", sub, GatewayCredential(f"token-{sub}", later))
    return store


@pytest.fixture
def executor(world: Engine, log: AuditLog, credentials: CredentialStore) -> Iterator[Executor]:
    SEEN.clear()
    yield Executor(world, log, credentials, BASELINE, TOOLS, catalogs)


def drafting_run(world: Engine, log: AuditLog, executor: Executor) -> RunContext:
    task_id = submit_task(world, log, ALICE, AGENT, LARKSPUR, "req-1", "SQR-1")
    authorize_stage(world, log, BOB, task_id)
    return executor.start_run(task_id, "drafting", "offline-replay")


def read(executor: Executor, run: RunContext, page: str = "101") -> Any:
    return executor.call(run, ToolCall("confluence.read_page", {"page_id": page}))


def run_status(world: Engine, run: RunContext) -> tuple[str, str]:
    with world.connect() as conn:
        run_row = conn.execute(select(runs.c.status).where(runs.c.run_id == run.run_id))
        task_row = conn.execute(select(tasks.c.status).where(tasks.c.task_id == run.task_id))
        return run_row.scalar_one(), task_row.scalar_one()


def counter(world: Engine, run: RunContext, name: str) -> int:
    with world.connect() as conn:
        value = conn.execute(
            select(run_counters.c.value).where(
                run_counters.c.run_id == run.run_id, run_counters.c.name == name
            )
        ).scalar()
    return int(value or 0)


# --------------------------------------------------------------- admission


def test_admission_binds_agent_identity_and_policy_from_records(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = drafting_run(world, log, executor)
    with world.connect() as conn:
        version = registry.current_policy_version(conn, AGENT)
        row = conn.execute(select(runs)).one()
    assert version is not None
    assert (run.agent_id, run.acting_sub, run.policy_version_id) == (AGENT, BOB, version.id)
    assert (row.status, row.stage, row.acting_sub, row.grant_id) == (
        "running",
        "drafting",
        BOB,
        run.grant_id,
    )
    with pytest.raises(ValueError, match="provenance"):
        executor.start_run(run.task_id, "drafting", "probably-connected")


def test_allowed_calls_run_with_the_acting_users_credential(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = drafting_run(world, log, executor)
    assert executor.call(run, ToolCall("list_task_policies", {})).content == ["SEC-1", "SEC-2"]
    assert read(executor, run).content == {"page_id": "101"}
    assert executor.call(run, ToolCall("confluence.list_pages", {"space": "SECPOL"})).ok
    # Model tools get no credential; operations get the acting user's (Bob's).
    assert [("list_task_policies", None), ("read_page", f"token-{BOB}")] == SEEN
    assert counter(world, run, "tool_calls") == 3
    assert counter(world, run, "op:confluence.read_page") == 1


# ---------------------------------------------- refused calls: run continues


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (ToolCall("list_task_policies", {"customer_id": "quillon-synthetic"}), "may not name"),
        (ToolCall("list_task_policies", {"sub": ALICE}), "may not name"),
        (ToolCall("confluence.read_page", {"page_id": "999"}), "not in the policy's catalog"),
        (ToolCall("confluence.list_pages", {"space": "HR"}), "space HR is out of scope"),
        (ToolCall("jira.get_request", {"issue_key": "SQR-1"}), "unknown tool"),
        (ToolCall("atlassian_executeRead", {"name": "getPage"}), "unknown tool"),
    ],
)
def test_model_shaped_requests_are_refused_and_the_run_continues(
    world: Engine, log: AuditLog, executor: Executor, call: ToolCall, message: str
) -> None:
    run = drafting_run(world, log, executor)
    result = executor.call(run, call)
    assert not result.ok and result.error is not None and message in result.error
    assert counter(world, run, "denied_calls") == 1
    assert run_status(world, run) == ("running", "drafting")
    assert executor.call(run, ToolCall("list_task_policies", {})).ok


def test_tool_failures_are_recorded_without_leaking_details(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = drafting_run(world, log, executor)
    result = executor.call(run, ToolCall("route_to_expert", {}))
    assert not result.ok and result.error == "tool route_to_expert failed"
    with world.connect() as conn:
        details = conn.execute(
            select(audit_events.c.details_json).where(audit_events.c.outcome == "failed")
        ).scalar_one()
    assert "RuntimeError" in details and "token-bob" not in details
    assert counter(world, run, "tool_calls") == 1


# --------------------------------------------- authority changes: run pauses


def test_pausing_the_agent_stops_the_next_call(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = drafting_run(world, log, executor)
    assert read(executor, run).ok
    registry.pause(world, log, PLATFORM, AGENT, "incident")
    with pytest.raises(RunStopped, match="paused") as stopped:
        read(executor, run)
    assert stopped.value.kind == "pause"
    assert run_status(world, run) == ("paused", "paused")
    with pytest.raises(RunStopped, match="run is paused") as again:
        read(executor, run)
    assert again.value.kind == "halt"


def test_revoking_the_grant_stops_the_next_call(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = drafting_run(world, log, executor)
    assert run.grant_id is not None
    revoke_grant(world, log, BOB, run.grant_id, "stop")
    with pytest.raises(RunStopped, match="revoked"):
        executor.call(run, ToolCall("list_task_policies", {}))


def test_expired_grant_stops_the_next_call(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = drafting_run(world, log, executor)
    past = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    with world.begin() as conn:
        conn.execute(
            update(task_grants).values(granted_at=past - dt.timedelta(hours=1), expires_at=past)
        )
    with pytest.raises(RunStopped, match="expired"):
        read(executor, run)


@pytest.mark.parametrize(
    ("change", "message"),
    [({"roles": ["sales"]}, "role for this stage"), ({"customers": []}, "customer")],
)
def test_losing_role_or_customer_stops_the_next_call(
    world: Engine, log: AuditLog, executor: Executor, change: dict[str, Any], message: str
) -> None:
    run = drafting_run(world, log, executor)
    data = mapping_data()
    next(u for u in data["users"] if u["username"] == "bob").update(change)
    apply_access_mapping(world, log, PLATFORM, AccessMapping.model_validate(data))
    with pytest.raises(RunStopped, match=message):
        executor.call(run, ToolCall("list_task_policies", {}))


def test_sign_out_stops_operations_but_not_local_model_tools(
    world: Engine, log: AuditLog, executor: Executor, credentials: CredentialStore
) -> None:
    run = drafting_run(world, log, executor)
    credentials.remove_session(f"s-{BOB}")
    assert executor.call(run, ToolCall("list_task_policies", {})).ok
    with pytest.raises(RunStopped, match="credential"):
        read(executor, run)
    assert run_status(world, run) == ("paused", "paused")


# -------------------------------------------------- policy changes mid-run


def test_a_permission_removed_mid_run_is_refused_at_the_next_call(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = drafting_run(world, log, executor)
    assert read(executor, run).ok
    apply(world, log, **without("confluence.read_page"))
    result = read(executor, run)
    assert not result.ok and "not allowed by the agent's policy" in str(result.error)


def test_a_permission_added_mid_run_is_not_used_by_that_run(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    apply(world, log, **without("confluence.read_page"))  # version 2, bound by the grant below
    run = drafting_run(world, log, executor)
    readded = policy_file().policy.model_dump(mode="json")["operations"]
    readded["confluence.read_page"]["max_calls_per_run"] = 250
    apply(world, log, operations=readded)  # version 3 adds read_page back
    result = read(executor, run)
    assert not result.ok and "not allowed by the agent's policy" in str(result.error)
    task_id = submit_task(world, log, ALICE, AGENT, LARKSPUR, "req-2")
    authorize_stage(world, log, BOB, task_id)
    fresh = executor.start_run(task_id, "drafting", "offline-replay")
    assert read(executor, fresh).ok  # new runs bind the new version


# ------------------------------------------------------------------ limits


def test_per_operation_limit_refuses_further_calls(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    ops = policy_file().policy.model_dump(mode="json")["operations"]
    ops["confluence.read_page"]["max_calls_per_run"] = 1
    apply(world, log, operations=ops)
    run = drafting_run(world, log, executor)
    assert read(executor, run).ok
    result = read(executor, run, "102")
    assert not result.ok and "per-run limit" in str(result.error)


def test_tool_call_budget_fails_the_run(world: Engine, log: AuditLog, executor: Executor) -> None:
    apply(world, log, budgets={"per_run_usd": 1, "per_run_model_calls": 5, "per_run_tool_calls": 2})
    run = drafting_run(world, log, executor)
    assert read(executor, run).ok and read(executor, run).ok
    with pytest.raises(RunStopped, match="budget exhausted") as stopped:
        read(executor, run)
    assert stopped.value.kind == "fail"
    assert run_status(world, run)[0] == "failed"


def test_repeated_refusals_fail_the_run(world: Engine, log: AuditLog, executor: Executor) -> None:
    run = drafting_run(world, log, executor)
    for _ in range(MAX_DENIED_CALLS):
        assert not executor.call(run, ToolCall("approve_answer", {})).ok
    with pytest.raises(RunStopped, match="too many refused calls") as stopped:
        executor.call(run, ToolCall("list_task_policies", {}))
    assert stopped.value.kind == "fail"


# ------------------------------------------------------------ bookkeeping


def test_finished_runs_take_no_calls_and_every_decision_is_audited(
    world: Engine, log: AuditLog, executor: Executor
) -> None:
    run = drafting_run(world, log, executor)
    read(executor, run)
    executor.call(run, ToolCall("approve_answer", {}))
    executor.finish_run(run, "succeeded")
    executor.finish_run(run, "failed")  # already finished: no change
    with pytest.raises(RunStopped, match="run is succeeded"):
        read(executor, run)
    with world.connect() as conn:
        rows = conn.execute(
            select(
                audit_events.c.event_type,
                audit_events.c.outcome,
                audit_events.c.run_id,
                audit_events.c.policy_hash,
            )
            .where(audit_events.c.run_id == run.run_id)
            .order_by(audit_events.c.seq)
        ).all()
        assert verify(conn).ok
    assert [(r.event_type, r.outcome) for r in rows] == [
        ("run.started", "succeeded"),
        ("tool.call", "succeeded"),
        ("tool.call", "denied"),
        ("run.finished", "succeeded"),
        ("tool.call", "denied"),
    ]
    assert {r.policy_hash for r in rows} == {run.policy_hash}
