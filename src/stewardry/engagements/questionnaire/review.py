"""Shadow review of drafted answers (spec section 5.1, Milestone 1 shadow status).

A GRC reviewer records a verdict on each draft: agree, edit (with their own
answer), or reject (with a reason). Verdicts are kept for comparison with the
agent's drafts. They are not approvals: nothing is released, exported, or
shown to Sales.

Who may review a task is decided here, from the database, on every request:
a GRC role, an assignment to the task's customer, and not being the person who
requested the task (the same separation as drafting authorization).
"""

from typing import Any

from sqlalchemy import Connection, Engine, Row, insert, select
from sqlalchemy.exc import IntegrityError

from stewardry.platform.access import assigned_customers
from stewardry.platform.audit import AuditEvent, AuditLog, record_now
from stewardry.platform.db.schema import (
    SHADOW_VERDICTS,
    question_drafts,
    shadow_verdicts,
    tasks,
    utcnow,
)
from stewardry.platform.roles import active_roles

REVIEW_ROLE = "grc"
REVIEWABLE_STATUSES = frozenset({"in_review"})


class ReviewRefused(Exception):
    """The review request was refused. The refusal has been audited."""


def review_problem(conn: Connection, viewer_sub: str, task: Row[Any] | None) -> str | None:
    """Why this user may not review this task, or None if they may."""
    if task is None:
        return "task does not exist"
    if REVIEW_ROLE not in active_roles(conn, viewer_sub):
        return "only GRC reviewers can review drafts"
    if task.customer_id not in assigned_customers(conn, viewer_sub):
        return "reviewer is not assigned to this customer"
    if task.requester_sub == viewer_sub:
        return "the requester cannot review their own task"
    return None


def record_shadow_verdict(
    engine: Engine,
    log: AuditLog,
    reviewer_sub: str,
    task_id: str,
    question_id: str,
    verdict: str,
    idempotency_key: str,
    edited_answer: str = "",
    reason: str = "",
) -> bool:
    """Store one verdict. Returns False for a repeated submission (no-op, audited)."""
    event = "review.shadow_verdict"
    with engine.connect() as conn:
        task = conn.execute(select(tasks).where(tasks.c.task_id == task_id)).first()
        problem = review_problem(conn, reviewer_sub, task)
        draft = conn.execute(
            select(question_drafts.c.status).where(
                question_drafts.c.task_id == task_id, question_drafts.c.question_id == question_id
            )
        ).first()
    ids = {"task_id": task_id, "question_id": question_id, "verdict": verdict}
    if problem is None and task is not None and task.status not in REVIEWABLE_STATUSES:
        problem = f"task is {task.status}; drafts are reviewed once drafting is complete"
    if problem is None and draft is None:
        problem = "this question has no draft"
    if problem is None and verdict not in SHADOW_VERDICTS:
        problem = "unknown verdict"
    if problem is None and verdict == "edit" and not edited_answer.strip():
        problem = "an edit needs the reviewer's answer"
    if problem is None and verdict == "reject" and not reason.strip():
        problem = "a rejection needs a reason"
    if problem is None and not idempotency_key.strip():
        problem = "a submission key is required"
    customer_id = task.customer_id if task is not None else None
    if problem is not None:
        record_now(
            engine,
            log,
            AuditEvent(
                event,
                "denied",
                actor_sub=reviewer_sub,
                reviewer_sub=reviewer_sub,
                task_id=task_id,
                customer_id=customer_id,
                details=ids | {"reason": problem},
            ),
        )
        raise ReviewRefused(problem)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(shadow_verdicts).values(
                    task_id=task_id,
                    question_id=question_id,
                    reviewer_sub=reviewer_sub,
                    verdict=verdict,
                    edited_answer=edited_answer.strip() or None if verdict == "edit" else None,
                    reason=reason.strip() or None,
                    idempotency_key=idempotency_key,
                    created_at=utcnow(),
                )
            )
            log.append(
                conn,
                AuditEvent(
                    event,
                    "succeeded",
                    actor_sub=reviewer_sub,
                    reviewer_sub=reviewer_sub,
                    task_id=task_id,
                    customer_id=customer_id,
                    details=ids | {"shadow": True},
                ),
            )
    except IntegrityError:
        record_now(
            engine,
            log,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=reviewer_sub,
                reviewer_sub=reviewer_sub,
                task_id=task_id,
                customer_id=customer_id,
                details=ids | {"duplicate": True},
            ),
        )
        return False
    return True
