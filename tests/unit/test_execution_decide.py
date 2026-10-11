"""Exhaustive checks of the executor's pure decision and policy intersection.

Every combination of the factors is enumerated, not sampled: a call is
allowed exactly when every check passes, and otherwise it is refused for the
first failing check in the documented order.
"""

import itertools
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from stewardry.platform.execution import (
    MAX_DENIED_CALLS,
    CallFacts,
    ConfluenceReadPageArgs,
    RequestCheck,
    ToolCall,
    ToolSpec,
    check_request,
    decide,
)
from stewardry.platform.policy import (
    PlatformBaseline,
    Policy,
    load_agent_policy,
    load_baseline,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY = load_agent_policy(ROOT / "policies/agents/questionnaire-agent.yaml").policy
BASELINE = load_baseline(ROOT / "policies/platform-baseline.yaml")
CATALOG = "policies/catalogs/secpol.yaml"


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PassageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_id: str


def _handler(*_: Any) -> None:
    return None


TOOLS = {
    "list_task_policies": ToolSpec("list_task_policies", "model_tool", NoArgs, _handler),
    "read_policy_passages": ToolSpec("read_policy_passages", "model_tool", PassageArgs, _handler),
    "confluence.read_page": ToolSpec(
        "confluence.read_page", "operation", ConfluenceReadPageArgs, _handler
    ),
}


def catalogs(ref: str) -> frozenset[str]:
    return frozenset({"101", "102"}) if ref == CATALOG else frozenset()


# ------------------------------------------------------------- decide()

ORDER = [
    ("run_running", "run is", "halt"),
    ("agent_ok", "agent", "pause"),
    ("grant_ok", "grant", "pause"),
    ("role_ok", "role for this stage", "pause"),
    ("customer_ok", "customer", "pause"),
    ("denied_ok", "too many refused", "fail"),
    ("request_ok", "request problem", None),
    ("budget_ok", "budget exhausted", "fail"),
    ("credential_ok", "credential", "pause"),
]


@pytest.mark.parametrize(
    "flags",
    [
        dict(zip([n for n, _, _ in ORDER] + ["needs_credential"], combo, strict=True))
        for combo in itertools.product([True, False], repeat=len(ORDER) + 1)
    ],
)
def test_decision_matrix(flags: dict[str, bool]) -> None:
    has_credential = flags["credential_ok"]
    facts = CallFacts(
        run_status="running" if flags["run_running"] else "paused",
        agent_problem=None if flags["agent_ok"] else "agent questionnaire-agent is paused",
        grant_problem=None if flags["grant_ok"] else "the grant was revoked",
        has_stage_role=flags["role_ok"],
        has_customer=flags["customer_ok"],
        has_credential=has_credential,
        denied_calls=0 if flags["denied_ok"] else MAX_DENIED_CALLS,
        tool_calls_used=0 if flags["budget_ok"] else 5,
        tool_call_budget=5,
    )
    request = RequestCheck(
        None if flags["request_ok"] else "request problem", flags["needs_credential"]
    )
    verdict = decide(facts, request)

    effective = dict(flags)
    effective["credential_ok"] = has_credential or not flags["needs_credential"]
    failing = [(name, text, stop) for name, text, stop in ORDER if not effective[name]]
    if not failing:
        assert verdict.allowed and verdict.reason is None and verdict.stop is None
    else:
        _, text, stop = failing[0]
        assert not verdict.allowed
        assert verdict.reason is not None and text in verdict.reason
        assert verdict.stop == stop


# -------------------------------------------------------- check_request()


def policy_with(**changes: Any) -> Policy:
    data = POLICY.model_dump(mode="json")
    for key, value in changes.items():
        data[key] = value
    return Policy.model_validate(data)


def without_operation(name: str) -> Policy:
    ops = POLICY.model_dump(mode="json")["operations"]
    ops.pop(name)
    return policy_with(operations=ops)


def baseline_without(name: str) -> PlatformBaseline:
    data = BASELINE.model_dump(mode="json")
    data["operations"] = [op for op in data["operations"] if op != name]
    return PlatformBaseline.model_validate(data)


@pytest.mark.parametrize(
    ("in_baseline", "in_bound", "in_current", "page", "used"),
    list(itertools.product([True, False], [True, False], [True, False], ["101", "999"], [0, 300])),
)
def test_operation_intersection(
    in_baseline: bool, in_bound: bool, in_current: bool, page: str, used: int
) -> None:
    name = "confluence.read_page"
    baseline = BASELINE if in_baseline else baseline_without(name)
    bound = POLICY if in_bound else without_operation(name)
    current = POLICY if in_current else without_operation(name)
    result = check_request(
        ToolCall(name, {"page_id": page}),
        TOOLS,
        baseline,
        bound,
        current,
        "drafting",
        catalogs,
        used,
    )
    should_allow = in_baseline and in_bound and in_current and page == "101" and used < 300
    assert (result.problem is None) == should_allow
    assert result.needs_credential  # operations always need the acting user's credential
    if not (in_baseline and in_bound and in_current):
        assert result.problem == f"operation {name} is not allowed by the agent's policy"


@pytest.mark.parametrize(
    ("in_bound", "in_current"), list(itertools.product([True, False], repeat=2))
)
def test_model_tool_intersection(in_bound: bool, in_current: bool) -> None:
    tools = POLICY.model_tools
    without = [t for t in tools if t != "list_task_policies"]
    bound = POLICY if in_bound else policy_with(model_tools=without)
    current = POLICY if in_current else policy_with(model_tools=without)
    result = check_request(
        ToolCall("list_task_policies", {}), TOOLS, BASELINE, bound, current, "drafting", catalogs, 0
    )
    assert (result.problem is None) == (in_bound and in_current)
    assert not result.needs_credential


@pytest.mark.parametrize(
    "arguments",
    [
        {"policy_id": "p1", "customer_id": "quillon-synthetic"},
        {"policy_id": "p1", "sub": "sub-alice"},
        {"policy_id": "p1", "Agent_ID": "other-agent"},
        {"policy_id": "p1", "filter": {"acting_sub": "sub-carol"}},
        {"policy_id": "p1", "items": [{"access_token": "x"}]},
        {"policy_id": "p1", "cloudId": "another-site"},
    ],
)
def test_arguments_cannot_name_identity_customers_or_credentials(
    arguments: dict[str, Any],
) -> None:
    result = check_request(
        ToolCall("read_policy_passages", arguments),
        TOOLS,
        BASELINE,
        POLICY,
        POLICY,
        "drafting",
        catalogs,
        0,
    )
    assert result.problem is not None and "may not name" in result.problem


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (ToolCall("atlassian_executeRead", {}), "unknown tool"),
        (ToolCall("approve_answer", {}), "unknown tool"),
        (ToolCall("read_policy_passages", {"policy_id": 3}), "invalid arguments"),
        (ToolCall("read_policy_passages", {"policy_id": "p", "extra": 1}), "invalid arguments"),
        (ToolCall("confluence.read_page", {"page_id": "../101"}), "invalid arguments"),
    ],
)
def test_unknown_tools_and_bad_arguments(call: ToolCall, message: str) -> None:
    result = check_request(call, TOOLS, BASELINE, POLICY, POLICY, "drafting", catalogs, 0)
    assert result.problem is not None and message in result.problem


def test_operations_are_tied_to_their_stage() -> None:
    result = check_request(
        ToolCall("confluence.read_page", {"page_id": "101"}),
        TOOLS,
        BASELINE,
        POLICY,
        POLICY,
        "intake",
        catalogs,
        0,
    )
    assert result.problem == "operation confluence.read_page belongs to stage drafting"


def test_changed_catalog_between_versions_allows_no_pages() -> None:
    ops = POLICY.model_dump(mode="json")["operations"]
    ops["confluence.read_page"]["catalog"] = "policies/catalogs/other.yaml"
    result = check_request(
        ToolCall("confluence.read_page", {"page_id": "101"}),
        TOOLS,
        BASELINE,
        POLICY,
        policy_with(operations=ops),
        "drafting",
        catalogs,
        0,
    )
    assert result.problem == "page is not in the policy's catalog"
