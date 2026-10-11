"""Controlled execution (spec sections 4.1 and 4.4, ADR 0009).

Every model tool call and every Atlassian operation goes through `Executor.call`:

1. **Gather facts** from protected records: the run, its task, the bound and
   current policy versions, the agent's state, the grant, the acting user's
   roles and customers, the credential, and the run's counters.
2. **Decide** with the pure function `decide`, in this order:

   | # | Check                                  | On failure                  |
   |---|----------------------------------------|-----------------------------|
   | 1 | run is running                         | stop (no state change)      |
   | 2 | agent registered, past discovery, enabled, policy version its own | pause run and task |
   | 3 | grant active (stages that need one)    | pause run and task          |
   | 4 | acting user still holds a stage role   | pause run and task          |
   | 5 | acting user still assigned to customer | pause run and task          |
   | 6 | fewer than MAX_DENIED_CALLS refusals   | fail the run                |
   | 7 | the request itself (see `check_request`) | refuse; the run continues |
   | 8 | tool-call budget left                  | fail the run                |
   | 9 | credential available (operations)     | pause run and task          |

3. **Record** the decision in the audit log, with the bound policy hash, and
   update the run's counters in the same transaction.

Identity, agent, customer, and scope come only from the run record. Tool
arguments are validated against a strict schema and may not name a user, an
agent, a customer, or a credential.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, Engine, insert, select, update

from stewardry.platform import grants, registry
from stewardry.platform.access import assigned_customers
from stewardry.platform.audit import AuditEvent, AuditLog
from stewardry.platform.credentials import CredentialStore, GatewayCredential
from stewardry.platform.db.schema import (
    PROVENANCE,
    agent_policy_versions,
    run_counters,
    runs,
    task_grants,
    tasks,
    utcnow,
)
from stewardry.platform.policy import (
    EffectiveOperation,
    PlatformBaseline,
    Policy,
    effective_model_tools,
    effective_operation,
    effective_stage_roles,
    effective_tool_call_budget,
)
from stewardry.platform.roles import active_roles

MAX_DENIED_CALLS = 20
RESERVED_ARGUMENT_KEYS = frozenset(
    {
        "sub",
        "user",
        "user_id",
        "username",
        "acting_sub",
        "actor_sub",
        "requester_sub",
        "authorizing_sub",
        "reviewer_sub",
        "agent",
        "agent_id",
        "customer",
        "customer_id",
        "task_id",
        "run_id",
        "grant_id",
        "policy_version",
        "policy_version_id",
        "cloud_id",
        "cloudid",
        "token",
        "access_token",
        "refresh_token",
        "credential",
        "authorization",
    }
)
Stop = Literal["pause", "fail", "halt"]


# ------------------------------------------------------------------ types


class RunStopped(Exception):
    """The run cannot continue. `kind` says whether it was paused or failed."""

    def __init__(self, reason: str, kind: Stop) -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


@dataclass(frozen=True)
class RunContext:
    """A run's protected identity. Nothing a tool receives can change it."""

    run_id: str
    task_id: str
    agent_id: str
    customer_id: str
    stage: str
    acting_sub: str
    grant_id: str | None
    policy_version_id: int
    policy_hash: str
    provenance: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: Any = None
    error: str | None = None


Handler = Callable[[RunContext, BaseModel, GatewayCredential | None], Any]


@dataclass(frozen=True)
class ToolSpec:
    """A tool the executor knows. `kind` decides which policy section governs it."""

    name: str
    kind: Literal["model_tool", "operation"]
    arguments: type[BaseModel]
    handler: Handler


# ---------------------------------------------- operation argument schemas


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JiraGetRequestArgs(_Args):
    issue_key: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,9}-[0-9]{1,9}$")


class ConfluenceListPagesArgs(_Args):
    space: str = Field(pattern=r"^[A-Z][A-Z0-9]{1,31}$")


class ConfluenceReadPageArgs(_Args):
    page_id: str = Field(pattern=r"^[0-9]{1,20}$")


OPERATION_ARGUMENTS: dict[str, type[_Args]] = {
    "jira.get_request": JiraGetRequestArgs,
    "confluence.list_pages": ConfluenceListPagesArgs,
    "confluence.read_page": ConfluenceReadPageArgs,
}

# A catalog reference (from policy) -> the page IDs it allows.
CatalogLookup = Callable[[str], frozenset[str]]


def operation_scope_problem(
    op: EffectiveOperation, args: BaseModel, catalogs: CatalogLookup
) -> str | None:
    if isinstance(args, JiraGetRequestArgs):
        project = args.issue_key.split("-", 1)[0]
        return None if project in op.projects else f"Jira project {project} is out of scope"
    if isinstance(args, ConfluenceListPagesArgs):
        return None if args.space in op.spaces else f"space {args.space} is out of scope"
    if isinstance(args, ConfluenceReadPageArgs):
        allowed = frozenset().union(*(catalogs(c) for c in op.catalogs))
        return None if args.page_id in allowed else "page is not in the policy's catalog"
    return "operation has no scope rule"


# ----------------------------------------------------------- the decision


@dataclass(frozen=True)
class CallFacts:
    """What the protected records say at the moment of the call."""

    run_status: str
    agent_problem: str | None
    grant_problem: str | None
    has_stage_role: bool
    has_customer: bool
    has_credential: bool
    denied_calls: int
    tool_calls_used: int
    tool_call_budget: int


@dataclass(frozen=True)
class RequestCheck:
    """What the requested call itself asks for, judged against policy."""

    problem: str | None
    needs_credential: bool
    arguments: BaseModel | None = None


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str | None = None
    stop: Stop | None = None


def decide(facts: CallFacts, request: RequestCheck) -> Verdict:
    """Pure. The order is the table in the module docstring."""
    if facts.run_status != "running":
        return Verdict(False, f"run is {facts.run_status}", "halt")
    if facts.agent_problem:
        return Verdict(False, facts.agent_problem, "pause")
    if facts.grant_problem:
        return Verdict(False, facts.grant_problem, "pause")
    if not facts.has_stage_role:
        return Verdict(False, "acting user no longer holds a role for this stage", "pause")
    if not facts.has_customer:
        return Verdict(False, "acting user is no longer assigned to this customer", "pause")
    if facts.denied_calls >= MAX_DENIED_CALLS:
        return Verdict(False, "too many refused calls in this run", "fail")
    if request.problem:
        return Verdict(False, request.problem)
    if facts.tool_calls_used >= facts.tool_call_budget:
        return Verdict(False, "tool-call budget exhausted", "fail")
    if request.needs_credential and not facts.has_credential:
        return Verdict(False, "the acting user's credential is not available", "pause")
    return Verdict(True)


def _reserved_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        found = {str(k) for k in value if str(k).lower() in RESERVED_ARGUMENT_KEYS}
        for item in value.values():
            found |= _reserved_keys(item)
        return found
    if isinstance(value, list | tuple):
        return set().union(*(_reserved_keys(v) for v in value)) if value else set()
    return set()


def check_request(
    call: ToolCall,
    tools: Mapping[str, ToolSpec],
    baseline: PlatformBaseline,
    bound: Policy,
    current: Policy,
    stage: str,
    catalogs: CatalogLookup,
    op_calls_used: int,
) -> RequestCheck:
    """Judge the requested call against the effective policy. Pure."""
    spec = tools.get(call.name)
    if spec is None:
        return RequestCheck(f"unknown tool {call.name!r}", False)
    reserved = _reserved_keys(call.arguments)
    if reserved:
        return RequestCheck(
            f"arguments may not name users, agents, customers, or credentials: {sorted(reserved)}",
            False,
        )
    try:
        args = spec.arguments.model_validate(dict(call.arguments))
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "(root)" for e in exc.errors()})
        return RequestCheck(f"invalid arguments: {fields}", False)

    if spec.kind == "model_tool":
        if call.name not in effective_model_tools(bound, current):
            return RequestCheck(f"tool {call.name} is not allowed by the agent's policy", False)
        return RequestCheck(None, False, args)

    op = effective_operation(baseline, bound, current, call.name)
    if op is None:
        return RequestCheck(f"operation {call.name} is not allowed by the agent's policy", True)
    if op.stage != stage:
        return RequestCheck(f"operation {call.name} belongs to stage {op.stage}", True)
    scope = operation_scope_problem(op, args, catalogs)
    if scope:
        return RequestCheck(scope, True)
    if op.max_calls_per_run is not None and op_calls_used >= op.max_calls_per_run:
        return RequestCheck(f"operation {call.name} reached its per-run limit", True)
    return RequestCheck(None, True, args)


# ---------------------------------------------------------------- executor


def _policy(policy_json: str) -> Policy:
    return Policy.model_validate(json.loads(policy_json)["policy"])


def _counter(conn: Connection, run_id: str, name: str) -> int:
    value = conn.execute(
        select(run_counters.c.value).where(
            run_counters.c.run_id == run_id, run_counters.c.name == name
        )
    ).scalar()
    return int(value or 0)


def _increment(conn: Connection, run_id: str, *names: str) -> None:
    for name in names:
        current = _counter(conn, run_id, name)
        if current == 0:
            conn.execute(insert(run_counters).values(run_id=run_id, name=name, value=1))
        else:
            conn.execute(
                update(run_counters)
                .where(run_counters.c.run_id == run_id, run_counters.c.name == name)
                .values(value=current + 1)
            )


def _arguments_digest(arguments: Mapping[str, Any]) -> str:
    text = json.dumps(arguments, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Executor:
    def __init__(
        self,
        engine: Engine,
        log: AuditLog,
        credentials: CredentialStore,
        baseline: PlatformBaseline,
        tools: Mapping[str, ToolSpec],
        catalogs: CatalogLookup,
    ) -> None:
        self.engine = engine
        self.log = log
        self.credentials = credentials
        self.baseline = baseline
        self.tools = dict(tools)
        self.catalogs = catalogs

    # ------------------------------------------------------------ admission

    def start_run(self, task_id: str, stage: str, provenance: str) -> RunContext:
        """Admit a run. The agent comes from the task record (server-side
        routing); the acting identity and policy version from the grant."""
        if provenance not in PROVENANCE:
            raise ValueError(f"unknown provenance {provenance!r}")
        execution = grants.acquire_execution(
            self.engine, self.log, self.credentials, task_id, stage
        )
        with self.engine.connect() as conn:
            version: str = conn.execute(
                select(agent_policy_versions.c.policy_hash).where(
                    agent_policy_versions.c.id == execution.policy_version_id
                )
            ).scalar_one()
        run = RunContext(
            run_id=uuid.uuid4().hex,
            task_id=task_id,
            agent_id=execution.agent_id,
            customer_id=execution.customer_id,
            stage=stage,
            acting_sub=execution.acting_sub,
            grant_id=execution.grant_id,
            policy_version_id=execution.policy_version_id,
            policy_hash=version,
            provenance=provenance,
        )
        with self.engine.begin() as conn:
            conn.execute(
                insert(runs).values(
                    run_id=run.run_id,
                    task_id=task_id,
                    agent_id=run.agent_id,
                    policy_version_id=run.policy_version_id,
                    acting_sub=run.acting_sub,
                    grant_id=run.grant_id,
                    stage=stage,
                    provenance=provenance,
                    status="running",
                    started_at=utcnow(),
                )
            )
            self.log.append(
                conn, self._event(run, "run.started", "succeeded", {"provenance": provenance})
            )
        return run

    def finish_run(
        self, run: RunContext, status: Literal["succeeded", "failed", "cancelled"], reason: str = ""
    ) -> None:
        with self.engine.begin() as conn:
            current = conn.execute(select(runs.c.status).where(runs.c.run_id == run.run_id))
            if current.scalar() != "running":
                return
            conn.execute(
                update(runs)
                .where(runs.c.run_id == run.run_id)
                .values(status=status, ended_at=utcnow())
            )
            self.log.append(
                conn,
                self._event(run, "run.finished", "succeeded", {"status": status, "reason": reason}),
            )

    # ---------------------------------------------------------------- calls

    def gather(
        self, conn: Connection, run: RunContext, call: ToolCall
    ) -> tuple[CallFacts, Policy, Policy, int]:
        """Read every fact the decision needs from protected records."""
        now = utcnow()
        row = conn.execute(select(runs).where(runs.c.run_id == run.run_id)).one()
        task = conn.execute(select(tasks).where(tasks.c.task_id == row.task_id)).one()
        bound_row = conn.execute(
            select(agent_policy_versions).where(agent_policy_versions.c.id == row.policy_version_id)
        ).one()
        current = registry.current_policy(conn, row.agent_id)
        bound = _policy(bound_row.policy_json)
        current_policy = current[1] if current else bound

        agent_problem = None
        try:
            registry.check_runnable(conn, row.agent_id, row.policy_version_id)
        except registry.AgentNotRunnable as exc:
            agent_problem = str(exc)

        grant_problem = None
        stage = row.stage or run.stage
        if bound.stages.get(stage) and bound.stages[stage].requires_task_grant:
            grant = conn.execute(
                select(task_grants).where(task_grants.c.grant_id == row.grant_id)
            ).first()
            if grant is None or grant.authorizing_sub != row.acting_sub:
                grant_problem = "the run has no grant for its acting user"
            elif grant.revoked_at is not None:
                grant_problem = "the grant was revoked"
            elif grant.expires_at <= now:
                grant_problem = "the grant has expired"

        roles = active_roles(conn, row.acting_sub)
        facts = CallFacts(
            run_status=row.status,
            agent_problem=agent_problem,
            grant_problem=grant_problem,
            has_stage_role=bool(roles & effective_stage_roles(bound, current_policy, stage)),
            has_customer=task.customer_id in assigned_customers(conn, row.acting_sub),
            has_credential=self.credentials.get(row.acting_sub, now) is not None,
            denied_calls=_counter(conn, run.run_id, "denied_calls"),
            tool_calls_used=_counter(conn, run.run_id, "tool_calls"),
            tool_call_budget=effective_tool_call_budget(bound, current_policy),
        )
        return facts, bound, current_policy, _counter(conn, run.run_id, f"op:{call.name}")

    def call(self, run: RunContext, call: ToolCall) -> ToolResult:
        with self.engine.connect() as conn:
            facts, bound, current, op_calls = self.gather(conn, run, call)
        request = check_request(
            call, self.tools, self.baseline, bound, current, run.stage, self.catalogs, op_calls
        )
        verdict = decide(facts, request)
        details: dict[str, Any] = {
            "tool": call.name,
            "argument_keys": sorted(str(k) for k in call.arguments),
            "arguments_sha256": _arguments_digest(call.arguments),
        }

        if verdict.stop is not None:
            self._stop(run, verdict, details)
        if not verdict.allowed:
            with self.engine.begin() as conn:
                _increment(conn, run.run_id, "denied_calls")
                self.log.append(
                    conn,
                    self._event(run, "tool.call", "denied", details | {"reason": verdict.reason}),
                )
            return ToolResult(False, error=verdict.reason)

        spec = self.tools[call.name]
        credential = self.credentials.get(run.acting_sub, utcnow())
        assert request.arguments is not None  # noqa: S101 - allowed implies parsed
        try:
            content = spec.handler(
                run, request.arguments, credential if spec.kind == "operation" else None
            )
            outcome, result = "succeeded", ToolResult(True, content)
        except Exception as exc:  # a tool failure is recorded, never fatal to the executor
            outcome = "failed"
            result = ToolResult(False, error=f"tool {call.name} failed")
            details["error_type"] = type(exc).__name__
        with self.engine.begin() as conn:
            names = ["tool_calls"] + ([f"op:{call.name}"] if spec.kind == "operation" else [])
            _increment(conn, run.run_id, *names)
            self.log.append(conn, self._event(run, "tool.call", outcome, details))
        return result

    def _stop(self, run: RunContext, verdict: Verdict, details: dict[str, Any]) -> None:
        assert verdict.stop is not None and verdict.reason is not None  # noqa: S101
        with self.engine.begin() as conn:
            if verdict.stop in ("pause", "fail"):
                status = "paused" if verdict.stop == "pause" else "failed"
                values: dict[str, Any] = {"status": status}
                if status == "failed":
                    values["ended_at"] = utcnow()
                conn.execute(
                    update(runs)
                    .where(runs.c.run_id == run.run_id, runs.c.status == "running")
                    .values(**values)
                )
                if verdict.stop == "pause":
                    conn.execute(
                        update(tasks)
                        .where(tasks.c.task_id == run.task_id)
                        .values(status="paused", updated_at=utcnow())
                    )
            self.log.append(
                conn,
                self._event(
                    run,
                    "tool.call",
                    "denied",
                    details | {"reason": verdict.reason, "run_stopped": verdict.stop},
                ),
            )
        raise RunStopped(verdict.reason, verdict.stop)

    def _event(
        self, run: RunContext, event_type: str, outcome: str, details: dict[str, Any]
    ) -> AuditEvent:
        return AuditEvent(
            event_type,
            outcome,
            actor_sub=run.acting_sub,
            authorizing_sub=run.acting_sub if run.grant_id else None,
            agent_id=run.agent_id,
            policy_hash=run.policy_hash,
            customer_id=run.customer_id,
            task_id=run.task_id,
            run_id=run.run_id,
            grant_id=run.grant_id,
            details={"stage": run.stage, "provenance": run.provenance} | details,
        )
