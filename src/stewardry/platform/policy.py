"""Per-agent policy files and the platform baseline (ADR 0009).

A policy file has a ``registration`` section (who owns the agent; stored on
the agent record) and a ``policy`` section (what the agent may do). Only the
``policy`` section, with the agent ID and schema version, is hashed into an
immutable policy version.

The platform baseline lists the only operations, Jira projects, Confluence
spaces, and models any agent may be granted. Nothing an agent file says can
go outside it.
"""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from stewardry.platform.db.schema import ROLES

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Username = Annotated[str, Field(min_length=1, max_length=255)]


def _known_role(value: str) -> str:
    if value not in ROLES:
        raise ValueError(f"unknown role {value!r}; expected one of {ROLES}")
    return value


RoleName = Annotated[str, AfterValidator(_known_role)]

# The narrow adapter operations (docs/broker-contracts.md). Raw MCP tools such
# as executeRead or discover have no name here, so no file can grant them.
OPERATIONS = ("jira.get_request", "confluence.list_pages", "confluence.read_page")
# Policy fields whose change needs a new accepted eval (spec 4.8, ADR 0009).
GATED_FIELDS = ("model", "operations", "model_tools")


class PolicyError(ValueError):
    """A policy file is invalid or goes outside the platform baseline."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stage(_Strict):
    acting_roles: list[RoleName] = Field(min_length=1)
    requires_task_grant: bool = False


class Operation(_Strict):
    stage: Slug
    projects: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    spaces: list[str] = Field(default_factory=list)
    catalog: str | None = None
    detail: Literal["full"] | None = None
    max_calls_per_run: int | None = Field(default=None, gt=0)


class Model(_Strict):
    id: str = Field(min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    prompt_version: str = Field(pattern=r"^[a-z0-9-]+@[0-9]+$")


class Budgets(_Strict):
    per_run_usd: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    per_run_model_calls: int = Field(gt=0)
    per_run_tool_calls: int = Field(gt=0)
    per_question_model_calls: int = Field(gt=0)


class Policy(_Strict):
    stages: dict[Slug, Stage] = Field(min_length=1)
    operations: dict[str, Operation]
    model: Model
    model_tools: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]] = Field(
        min_length=1
    )
    budgets: Budgets
    customer_scope: Literal["per_task"]

    @model_validator(mode="after")
    def _operations_use_defined_stages(self) -> Policy:
        for name, op in self.operations.items():
            if op.stage not in self.stages:
                raise ValueError(f"operation {name} uses undefined stage {op.stage!r}")
        if len(set(self.model_tools)) != len(self.model_tools):
            raise ValueError("model_tools must not repeat")
        return self


class Registration(_Strict):
    name: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1)
    owning_team: str = Field(min_length=1, max_length=255)
    business_owner: Username
    operational_owner: Username
    risk_tier: Literal[1, 2, 3]
    runbook_url: str | None = None


class AgentPolicyFile(_Strict):
    schema_version: Literal[1]
    agent_id: Slug
    registration: Registration
    policy: Policy

    def policy_document(self) -> dict[str, Any]:
        """The hashed part: what the agent may do, not who owns it."""
        return {
            "agent_id": self.agent_id,
            "schema_version": self.schema_version,
            "policy": self.policy.model_dump(mode="json"),
        }

    def policy_json(self) -> str:
        return json.dumps(self.policy_document(), sort_keys=True, separators=(",", ":"))

    def policy_hash(self) -> str:
        return hashlib.sha256(self.policy_json().encode("utf-8")).hexdigest()


class PlatformBaseline(_Strict):
    schema_version: Literal[1]
    operations: list[str]
    jira_projects: list[str]
    confluence_spaces: list[str]
    models: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _only_known_operations(self) -> PlatformBaseline:
        unknown = set(self.operations) - set(OPERATIONS)
        if unknown:
            raise ValueError(f"baseline names unknown operations: {sorted(unknown)}")
        return self

    def check(self, agent: AgentPolicyFile) -> None:
        """Refuse any grant outside the baseline. Raises PolicyError."""
        policy = agent.policy
        problems = []
        for name, op in policy.operations.items():
            if name not in self.operations:
                problems.append(f"operation {name!r} is not allowed by the platform baseline")
            for project in set(op.projects) - set(self.jira_projects):
                problems.append(f"operation {name}: Jira project {project!r} is not allowed")
            for space in set(op.spaces) - set(self.confluence_spaces):
                problems.append(f"operation {name}: Confluence space {space!r} is not allowed")
        if policy.model.id not in self.models:
            problems.append(f"model {policy.model.id!r} is not allowed by the platform baseline")
        if problems:
            raise PolicyError("; ".join(problems))


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"{path}: cannot read YAML ({exc})") from exc


def load_agent_policy(path: Path) -> AgentPolicyFile:
    try:
        return AgentPolicyFile.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise PolicyError(f"{path}: {exc}") from exc


def load_baseline(path: Path) -> PlatformBaseline:
    try:
        return PlatformBaseline.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise PolicyError(f"{path}: {exc}") from exc


def gated_changes(old_policy_json: str, new: AgentPolicyFile) -> list[str]:
    """Which re-evaluation-gated fields differ from the previous version."""
    old = json.loads(old_policy_json)["policy"]
    current = new.policy_document()["policy"]
    return [name for name in GATED_FIELDS if old.get(name) != current.get(name)]


def json_schema() -> str:
    """The JSON Schema for agent policy files, as committed in policies/."""
    return json.dumps(AgentPolicyFile.model_json_schema(), indent=2, sort_keys=True) + "\n"


# ------------------------------------------------------- effective policy


@dataclass(frozen=True)
class EffectiveOperation:
    """What one operation may do in a run: the intersection of the baseline,
    the run's bound policy version, and the agent's current version."""

    name: str
    stage: str
    projects: frozenset[str]
    fields: frozenset[str]
    spaces: frozenset[str]
    catalogs: frozenset[str]
    max_calls_per_run: int | None


def _min_optional(a: int | None, b: int | None) -> int | None:
    values = [v for v in (a, b) if v is not None]
    return min(values) if values else None


def effective_operation(
    baseline: PlatformBaseline, bound: Policy, current: Policy, name: str
) -> EffectiveOperation | None:
    """None unless the operation is allowed by all three. A permission removed
    in the current version stops a running run; one added after the run was
    bound is not used by it (ADR 0009)."""
    if name not in baseline.operations:
        return None
    old, new = bound.operations.get(name), current.operations.get(name)
    if old is None or new is None or old.stage != new.stage:
        return None
    catalogs = {c for c in (old.catalog, new.catalog) if c is not None}
    return EffectiveOperation(
        name=name,
        stage=old.stage,
        projects=frozenset(old.projects) & frozenset(new.projects) & set(baseline.jira_projects),
        fields=frozenset(old.fields) & frozenset(new.fields),
        spaces=frozenset(old.spaces) & frozenset(new.spaces) & set(baseline.confluence_spaces),
        # Both versions must name the same catalog; a changed catalog means none.
        catalogs=frozenset(catalogs) if len(catalogs) <= 1 else frozenset(),
        max_calls_per_run=_min_optional(old.max_calls_per_run, new.max_calls_per_run),
    )


def effective_model_tools(bound: Policy, current: Policy) -> frozenset[str]:
    return frozenset(bound.model_tools) & frozenset(current.model_tools)


def effective_stage_roles(bound: Policy, current: Policy, stage: str) -> frozenset[str]:
    if stage not in bound.stages or stage not in current.stages:
        return frozenset()
    return frozenset(bound.stages[stage].acting_roles) & frozenset(
        current.stages[stage].acting_roles
    )


def effective_tool_call_budget(bound: Policy, current: Policy) -> int:
    return min(bound.budgets.per_run_tool_calls, current.budgets.per_run_tool_calls)


def effective_question_model_calls(bound: Policy, current: Policy) -> int:
    return min(bound.budgets.per_question_model_calls, current.budgets.per_question_model_calls)
