"""Attaching an uploaded questionnaire to its task (spec section 5.1, step 1).

Only the task's requester may upload, once, before drafting is authorized.
The original file goes to the content-addressed artifact store; its questions
(with their template locations) and its screening findings go to the database.
Rejections and successful ingests are audited. The audit event carries counts
per finding kind, not the untrusted excerpts, which only reviewers may see.
"""

import json
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePath

from sqlalchemy import Engine, insert, select

from stewardry.engagements.questionnaire.parse import (
    UnsupportedQuestionnaire,
    parse_questionnaire,
)
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.audit import AuditEvent, AuditLog, record_now
from stewardry.platform.db.schema import (
    artifacts,
    screening_findings,
    task_questions,
    tasks,
    utcnow,
)

MEDIA_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
INGESTABLE_STATUSES = frozenset({"submitted", "awaiting_authorization"})


class IngestRefused(Exception):
    """The upload was refused. The refusal has been audited."""


@dataclass(frozen=True)
class IngestResult:
    artifact_id: str
    template_id: str
    question_count: int
    findings: dict[str, int]


def _safe_name(filename: str) -> str:
    name = PurePath(filename.replace("\\", "/")).name
    return "".join(c for c in name if c.isprintable())[:255] or "questionnaire"


def ingest_questionnaire(
    engine: Engine,
    log: AuditLog,
    store: ArtifactStore,
    actor_sub: str,
    task_id: str,
    filename: str,
    data: bytes,
) -> IngestResult:
    event = "questionnaire.ingested"
    with engine.connect() as conn:
        task = conn.execute(select(tasks).where(tasks.c.task_id == task_id)).first()
        already = conn.execute(
            select(task_questions.c.question_id).where(task_questions.c.task_id == task_id)
        ).first()

    def refuse(reason: str, outcome: str = "denied") -> IngestRefused:
        record_now(
            engine,
            log,
            AuditEvent(
                event,
                outcome,
                actor_sub=actor_sub,
                task_id=task_id,
                customer_id=task.customer_id if task else None,
                details={"reason": reason, "filename": _safe_name(filename)},
            ),
        )
        return IngestRefused(reason)

    if task is None:
        raise refuse("task does not exist")
    if actor_sub != task.requester_sub:
        raise refuse("only the task's requester can upload its questionnaire")
    if task.status not in INGESTABLE_STATUSES:
        raise refuse(f"task is {task.status}; questionnaires are uploaded before drafting")
    if already is not None:
        raise refuse("this task already has a questionnaire")
    try:
        parsed = parse_questionnaire(filename, data)
    except UnsupportedQuestionnaire as exc:
        raise refuse(str(exc), "failed") from exc

    sha256 = store.put(data)
    artifact_id = uuid.uuid4().hex
    now = utcnow()
    counts = dict(Counter[str](f.kind for f in parsed.findings))
    with engine.begin() as conn:
        conn.execute(
            insert(artifacts).values(
                artifact_id=artifact_id,
                sha256=sha256,
                size_bytes=len(data),
                media_type=MEDIA_TYPES[parsed.format],
                original_name=_safe_name(filename),
                kind="upload",
                task_id=task_id,
                customer_id=task.customer_id,
                created_by_sub=actor_sub,
                created_at=now,
            )
        )
        conn.execute(
            insert(task_questions),
            [
                {
                    "task_id": task_id,
                    "question_id": q.question_id,
                    "artifact_id": artifact_id,
                    "ordinal": q.ordinal,
                    "section": q.section,
                    "text": q.text,
                    "location_json": json.dumps(q.location, sort_keys=True),
                }
                for q in parsed.questions
            ],
        )
        if parsed.findings:
            conn.execute(
                insert(screening_findings),
                [
                    {
                        "task_id": task_id,
                        "artifact_id": artifact_id,
                        "kind": f.kind,
                        "location": f.location[:255],
                        "question_id": f.question_id,
                        "reason": f.reason[:255] if f.reason else None,
                        "excerpt": f.excerpt,
                        "created_at": now,
                    }
                    for f in parsed.findings
                ],
            )
        log.append(
            conn,
            AuditEvent(
                event,
                "succeeded",
                actor_sub=actor_sub,
                requester_sub=task.requester_sub,
                task_id=task_id,
                customer_id=task.customer_id,
                agent_id=task.agent_id,
                details={
                    "artifact_id": artifact_id,
                    "sha256": sha256,
                    "template": parsed.template_id,
                    "questions": len(parsed.questions),
                    "screening_findings": counts,
                },
            ),
        )
    return IngestResult(artifact_id, parsed.template_id, len(parsed.questions), counts)
