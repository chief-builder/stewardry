from pathlib import Path
from typing import Any

import pytest
import yaml

from stewardry.platform.policy import (
    AgentPolicyFile,
    PlatformBaseline,
    PolicyError,
    gated_changes,
    json_schema,
    load_agent_policy,
    load_baseline,
)

ROOT = Path(__file__).resolve().parents[2]
AGENT_FILE = ROOT / "policies" / "agents" / "questionnaire-agent.yaml"
BASELINE_FILE = ROOT / "policies" / "platform-baseline.yaml"


def raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(AGENT_FILE.read_text())
    return data


def build(data: dict[str, Any]) -> AgentPolicyFile:
    return AgentPolicyFile.model_validate(data)


def test_committed_files_load_and_fit_the_baseline() -> None:
    agent = load_agent_policy(AGENT_FILE)
    load_baseline(BASELINE_FILE).check(agent)
    assert agent.agent_id == "questionnaire-agent"
    assert "platform_engineer" not in {
        role for stage in agent.policy.stages.values() for role in stage.acting_roles
    }


def test_committed_json_schema_is_current() -> None:
    assert (ROOT / "policies" / "agent-policy.schema.json").read_text() == json_schema()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["policy"].update(surprise=True), "Extra inputs"),
        (lambda d: d["policy"]["stages"]["intake"].update(acting_roles=["root"]), "unknown role"),
        (lambda d: d["policy"]["operations"]["jira.get_request"].update(stage="x"), "undefined"),
        (lambda d: d["policy"]["model"].update(effort="extreme"), "effort"),
        (lambda d: d["policy"]["budgets"].update(per_run_usd=-1), "per_run_usd"),
        (lambda d: d["policy"]["model_tools"].append("submit_answer"), "must not repeat"),
        (lambda d: d["policy"].update(customer_scope="all_customers"), "customer_scope"),
        (lambda d: d["registration"].update(risk_tier=4), "risk_tier"),
        (lambda d: d.update(agent_id="Questionnaire Agent"), "agent_id"),
    ],
)
def test_invalid_policies_are_refused(change: Any, message: str) -> None:
    data = raw()
    change(data)
    with pytest.raises(ValueError, match=message):
        build(data)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda d: d["policy"]["operations"].update(
                {"atlassian.execute_read": {"stage": "drafting"}}
            ),
            "'atlassian.execute_read' is not allowed",
        ),
        (
            lambda d: d["policy"]["operations"].update({"executeRead": {"stage": "drafting"}}),
            "'executeRead' is not allowed",
        ),
        (
            lambda d: d["policy"]["operations"]["confluence.list_pages"].update(spaces=["HR"]),
            "space 'HR' is not allowed",
        ),
        (
            lambda d: d["policy"]["operations"]["jira.get_request"].update(projects=["FIN"]),
            "project 'FIN' is not allowed",
        ),
        (lambda d: d["policy"]["model"].update(id="some-other-model"), "model"),
    ],
)
def test_baseline_denies_cannot_be_overridden(change: Any, message: str) -> None:
    data = raw()
    change(data)
    with pytest.raises(PolicyError, match=message):
        load_baseline(BASELINE_FILE).check(build(data))


def test_baseline_rejects_unknown_operation_names() -> None:
    with pytest.raises(ValueError, match="unknown operations"):
        PlatformBaseline.model_validate(
            {
                "schema_version": 1,
                "operations": ["atlassian.discover"],
                "jira_projects": [],
                "confluence_spaces": [],
                "models": ["claude-opus-5-5"],
            }
        )


def test_hash_covers_policy_but_not_registration() -> None:
    base = build(raw())
    renamed = raw()
    renamed["registration"]["name"] = "Renamed"
    tighter = raw()
    tighter["policy"]["budgets"]["per_run_model_calls"] = 10
    assert build(renamed).policy_hash() == base.policy_hash()
    assert build(tighter).policy_hash() != base.policy_hash()


def test_gated_changes_ignore_budgets() -> None:
    base = build(raw())
    budget = raw()
    budget["policy"]["budgets"]["per_run_tool_calls"] = 5
    model = raw()
    model["policy"]["model"]["prompt_version"] = "questionnaire-draft@2"
    tools = raw()
    tools["policy"]["model_tools"].pop()
    assert gated_changes(base.policy_json(), build(budget)) == []
    assert gated_changes(base.policy_json(), build(model)) == ["model"]
    assert gated_changes(base.policy_json(), build(tools)) == ["model_tools"]


def test_unreadable_files_raise_policy_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("policy: [unclosed")
    with pytest.raises(PolicyError, match="cannot read YAML"):
        load_agent_policy(broken)
    with pytest.raises(PolicyError, match="cannot read YAML"):
        load_baseline(tmp_path / "missing.yaml")
    with pytest.raises(PolicyError, match="schema_version"):
        load_baseline(AGENT_FILE)
