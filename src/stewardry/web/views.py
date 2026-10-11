"""Role-filtered view models (spec sections 4.3.1 and 5.1).

Templates receive only these objects, never database rows, so what each role
can see is decided here, on the server:

- **Sales** (`sales_task_view`): the request, its status in plain words, and
  how many questions were received and processed. No answers, passages,
  policy names, links, strengths, expert reasons, screening findings, or
  reviewer identities.
- **GRC** (`review_view`): drafts, reasons, citations with passage text,
  screening findings with excerpts, and shadow verdicts. Passage text from a
  confidential policy is shown only to the reviewer whose own access read
  it; others see the citation without the text.
- **Platform** (`registry_view`): agents, lifecycle state, and the current
  policy version's model and budgets.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, func, select

from stewardry.platform import registry
from stewardry.platform.db.schema import (
    agents,
    evidence_snapshots,
    question_drafts,
    screening_findings,
    shadow_verdicts,
    task_questions,
    tasks,
    users,
)

REGISTRY_ROLES = frozenset({"platform_engineer", "engagement_owner", "auditor"})
STRENGTH_LABELS = {
    "direct_current": "Direct current support",
    "partial_ambiguous": "Partial or ambiguous support",
    "insufficient_conflicting_stale": "Insufficient, conflicting, or stale support",
}
EXPERT_REASONS = {
    "missing": "No policy covers this",
    "stale": "Only superseded or overdue policies cover this",
    "conflict": "Policies conflict",
    "ambiguous": "Support is partial or ambiguous",
    "unsupported": "The answer is not supported by valid citations",
    "no_answer": "The agent produced no answer",
}


# ------------------------------------------------------------------ Sales


def sales_status(status: str, questions: int, processed: int) -> str:
    """Plain status text for the requester. It never names policies, people,
    evidence, or why an item needs attention."""
    if status in ("submitted", "awaiting_authorization"):
        if questions == 0:
            return "Upload the customer's questionnaire to continue."
        return f"Questionnaire received ({questions} questions). Waiting for GRC to start drafting."
    if status == "drafting":
        return f"Drafting is in progress ({processed} of {questions} questions processed)."
    if status == "paused":
        return "Drafting is paused until GRC can continue."
    if status == "in_review":
        return "Drafts are with GRC for review. Nothing has been approved or released yet."
    return {
        "completed": "The request is complete.",
        "rejected": "The request was declined.",
        "failed": "The request could not be completed. Contact GRC.",
        "cancelled": "The request was cancelled.",
    }.get(status, "The request is in progress.")


@dataclass(frozen=True)
class SalesTaskView:
    task_id: str
    customer_id: str
    jira_issue_key: str | None
    status_text: str
    questions: int
    can_upload: bool


def sales_task_view(conn: Connection, task: Any) -> SalesTaskView:
    questions = conn.execute(
        select(func.count()).where(task_questions.c.task_id == task.task_id)
    ).scalar_one()
    processed = conn.execute(
        select(func.count()).where(question_drafts.c.task_id == task.task_id)
    ).scalar_one()
    return SalesTaskView(
        task_id=task.task_id,
        customer_id=task.customer_id,
        jira_issue_key=task.jira_issue_key,
        status_text=sales_status(task.status, questions, processed),
        questions=questions,
        can_upload=questions == 0 and task.status in ("submitted", "awaiting_authorization"),
    )


# -------------------------------------------------------------------- GRC


@dataclass(frozen=True)
class CitedPassage:
    policy_id: str
    passage: str
    section: str | None
    text: str | None  # None when hidden from this reviewer
    note: str | None = None


@dataclass(frozen=True)
class Finding:
    kind: str
    location: str
    excerpt: str
    reason: str | None


@dataclass(frozen=True)
class Verdict:
    reviewer: str
    verdict: str
    edited_answer: str | None
    reason: str | None
    created_at: str


@dataclass(frozen=True)
class ReviewItem:
    question_id: str
    section: str
    text: str
    status: str | None
    answer: str | None
    strength: str | None
    expert_reason: str | None
    reasons: list[str]
    citations: list[CitedPassage]
    findings: list[Finding]
    verdicts: list[Verdict]


@dataclass(frozen=True)
class ReviewView:
    task_id: str
    customer_id: str
    status: str
    reviewable: bool
    items: list[ReviewItem]
    file_findings: list[Finding] = field(default_factory=list)


def _snapshot(conn: Connection, task_id: str, run_id: str, cite: dict[str, Any]) -> Any:
    c = evidence_snapshots.c
    if cite.get("snapshot_id"):
        return conn.execute(
            select(evidence_snapshots).where(
                c.snapshot_id == cite["snapshot_id"], c.task_id == task_id
            )
        ).first()
    return conn.execute(
        select(evidence_snapshots)
        .where(c.task_id == task_id, c.run_id == run_id, c.page_id == cite["page_id"])
        .order_by(c.created_at.desc())
    ).first()


def _cited(conn: Connection, viewer_sub: str, task_id: str, draft: Any) -> list[CitedPassage]:
    cited = []
    for cite in json.loads(draft.citations_json):
        snap = _snapshot(conn, task_id, draft.run_id, cite)
        meta = json.loads(snap.metadata_json) if snap is not None and snap.metadata_json else {}
        policy_id = meta.get("policy_id") or cite.get("policy_id") or f"page {cite['page_id']}"
        passages = {p["ref"]: p for p in json.loads(snap.passages_json)} if snap else {}
        passage = passages.get(cite["passage"])
        if passage is None:
            cited.append(CitedPassage(policy_id, cite["passage"], None, None, "passage not found"))
        elif meta.get("classification") == "confidential" and snap.acting_sub != viewer_sub:
            cited.append(
                CitedPassage(
                    policy_id,
                    cite["passage"],
                    passage["section"],
                    None,
                    "confidential: read under another reviewer's access",
                )
            )
        else:
            note = (
                f"evidence invalidated ({snap.invalidated_reason})" if snap.invalidated_at else None
            )
            cited.append(
                CitedPassage(policy_id, cite["passage"], passage["section"], passage["text"], note)
            )
    return cited


def review_view(conn: Connection, viewer_sub: str, task: Any) -> ReviewView:
    usernames: dict[str, str] = {r.sub: r.username for r in conn.execute(select(users))}
    drafts = {
        d.question_id: d
        for d in conn.execute(
            select(question_drafts).where(question_drafts.c.task_id == task.task_id)
        )
    }
    findings: dict[str | None, list[Finding]] = {}
    for f in conn.execute(
        select(screening_findings)
        .where(screening_findings.c.task_id == task.task_id)
        .order_by(screening_findings.c.id)
    ):
        findings.setdefault(f.question_id, []).append(
            Finding(f.kind, f.location, f.excerpt, f.reason)
        )
    verdicts: dict[str, list[Verdict]] = {}
    for v in conn.execute(
        select(shadow_verdicts)
        .where(shadow_verdicts.c.task_id == task.task_id)
        .order_by(shadow_verdicts.c.id)
    ):
        verdicts.setdefault(v.question_id, []).append(
            Verdict(
                usernames.get(v.reviewer_sub, "unknown"),
                v.verdict,
                v.edited_answer,
                v.reason,
                v.created_at.strftime("%Y-%m-%d %H:%M"),
            )
        )
    items = []
    for q in conn.execute(
        select(task_questions)
        .where(task_questions.c.task_id == task.task_id)
        .order_by(task_questions.c.ordinal)
    ):
        d = drafts.get(q.question_id)
        items.append(
            ReviewItem(
                question_id=q.question_id,
                section=q.section,
                text=q.text,
                status=d.status if d else None,
                answer=d.answer if d else None,
                strength=STRENGTH_LABELS.get(d.strength) if d and d.strength else None,
                expert_reason=EXPERT_REASONS.get(d.expert_reason)
                if d and d.expert_reason
                else None,
                reasons=json.loads(d.reasons_json) if d else [],
                citations=_cited(conn, viewer_sub, task.task_id, d) if d else [],
                findings=findings.get(q.question_id, []),
                verdicts=verdicts.get(q.question_id, []),
            )
        )
    return ReviewView(
        task_id=task.task_id,
        customer_id=task.customer_id,
        status=task.status,
        reviewable=task.status == "in_review",
        items=items,
        file_findings=findings.get(None, []),
    )


def reviewable_tasks(conn: Connection, viewer_sub: str, customers: set[str]) -> list[Any]:
    """Tasks in review for the viewer's customers that they did not request."""
    return list(
        conn.execute(
            select(tasks)
            .where(
                tasks.c.customer_id.in_(customers),
                tasks.c.status == "in_review",
                tasks.c.requester_sub != viewer_sub,
            )
            .order_by(tasks.c.updated_at)
        ).all()
    )


# --------------------------------------------------------------- registry


@dataclass(frozen=True)
class AgentRow:
    agent_id: str
    name: str
    engagement_status: str
    operational_state: str
    risk_tier: int
    business_owner: str
    operational_owner: str
    policy_hash: str | None
    model: str | None
    prompt_version: str | None
    budgets: dict[str, str]
    reevaluation_required: bool | None


def registry_view(conn: Connection) -> list[AgentRow]:
    usernames: dict[str, str] = {r.sub: r.username for r in conn.execute(select(users))}
    rows = []
    for agent in conn.execute(select(agents).order_by(agents.c.agent_id)):
        version = registry.current_policy_version(conn, agent.agent_id)
        policy = json.loads(version.policy_json)["policy"] if version is not None else None
        rows.append(
            AgentRow(
                agent_id=agent.agent_id,
                name=agent.name,
                engagement_status=agent.engagement_status,
                operational_state=agent.operational_state,
                risk_tier=agent.risk_tier,
                business_owner=usernames.get(agent.business_owner_sub, "unknown"),
                operational_owner=usernames.get(agent.operational_owner_sub, "unknown"),
                policy_hash=version.policy_hash[:12] if version is not None else None,
                model=f"{policy['model']['id']} ({policy['model']['effort']})" if policy else None,
                prompt_version=policy["model"]["prompt_version"] if policy else None,
                budgets={k: str(v) for k, v in policy["budgets"].items()} if policy else {},
                reevaluation_required=(
                    bool(version.reevaluation_required) if version is not None else None
                ),
            )
        )
    return rows
