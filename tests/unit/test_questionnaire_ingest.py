"""Attaching an uploaded questionnaire to its task: who, when, and what is kept."""

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, update

from stewardry.engagements.questionnaire.ingest import IngestRefused, ingest_questionnaire
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.audit import AuditLog, verify
from stewardry.platform.db.schema import (
    artifacts,
    audit_events,
    screening_findings,
    task_questions,
    tasks,
)
from stewardry.platform.grants import submit_task
from tests.unit.conftest import AGENT, ALICE, BOB, LARKSPUR, ROOT

FIXTURE = ROOT / "fixtures" / "questionnaires" / "QN-LARKSPUR-01.xlsx"


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def task_id(world: Engine, log: AuditLog) -> str:
    return submit_task(world, log, ALICE, AGENT, LARKSPUR, "req-1", "SQR-1")


def ingest(
    world: Engine,
    log: AuditLog,
    store: ArtifactStore,
    task_id: str,
    actor: str = ALICE,
    filename: str = "QN-LARKSPUR-01.xlsx",
    data: bytes | None = None,
) -> object:
    return ingest_questionnaire(
        world, log, store, actor, task_id, filename, FIXTURE.read_bytes() if data is None else data
    )


def events(world: Engine, outcome: str) -> list[dict[str, object]]:
    with world.connect() as conn:
        rows = conn.execute(
            select(audit_events.c.details_json).where(
                audit_events.c.event_type == "questionnaire.ingested",
                audit_events.c.outcome == outcome,
            )
        ).scalars()
        return [json.loads(r) for r in rows]


def test_ingest_keeps_the_file_questions_and_findings(
    world: Engine, log: AuditLog, store: ArtifactStore, task_id: str
) -> None:
    result = ingest_questionnaire(
        world, log, store, ALICE, task_id, "../../QN-LARKSPUR-01.xlsx", FIXTURE.read_bytes()
    )
    assert result.question_count == 30
    assert result.template_id == "sqt-xlsx-v1"
    with world.connect() as conn:
        artifact = conn.execute(select(artifacts)).one()
        questions = conn.execute(select(task_questions).order_by(task_questions.c.ordinal)).all()
        findings = conn.execute(select(screening_findings)).all()
        assert verify(conn).ok
    assert artifact.kind == "upload"
    assert artifact.customer_id == LARKSPUR
    assert artifact.original_name == "QN-LARKSPUR-01.xlsx"
    assert store.get(artifact.sha256) == FIXTURE.read_bytes()
    assert [q.question_id for q in questions][:2] == ["Q01", "Q02"]
    assert json.loads(questions[0].location_json)["answer_cell"] == "D2"
    assert {f.kind for f in findings} >= {"hidden_column", "comment", "instruction_like"}
    assert len(findings) == sum(result.findings.values())


def test_audit_event_has_counts_but_no_untrusted_text(
    world: Engine, log: AuditLog, store: ArtifactStore, task_id: str
) -> None:
    ingest(world, log, store, task_id)
    (details,) = events(world, "succeeded")
    assert details["questions"] == 30
    assert details["template"] == "sqt-xlsx-v1"
    counts = details["screening_findings"]
    assert isinstance(counts, dict) and counts["hidden_column"] >= 1
    assert "Assistant" not in json.dumps(details)


def test_only_the_requester_can_upload(
    world: Engine, log: AuditLog, store: ArtifactStore, task_id: str
) -> None:
    with pytest.raises(IngestRefused, match="requester"):
        ingest(world, log, store, task_id, actor=BOB)
    assert events(world, "denied")


def test_a_task_takes_one_questionnaire(
    world: Engine, log: AuditLog, store: ArtifactStore, task_id: str
) -> None:
    ingest(world, log, store, task_id)
    with pytest.raises(IngestRefused, match="already"):
        ingest(world, log, store, task_id)


def test_unknown_task_is_refused(world: Engine, log: AuditLog, store: ArtifactStore) -> None:
    with pytest.raises(IngestRefused, match="does not exist"):
        ingest(world, log, store, "no-such-task")


def test_upload_after_drafting_starts_is_refused(
    world: Engine, log: AuditLog, store: ArtifactStore, task_id: str
) -> None:
    with world.begin() as conn:
        conn.execute(update(tasks).values(status="drafting"))
    with pytest.raises(IngestRefused, match="drafting"):
        ingest(world, log, store, task_id)


def test_unsupported_file_is_audited_as_failed_and_not_stored(
    world: Engine, log: AuditLog, store: ArtifactStore, task_id: str
) -> None:
    with pytest.raises(IngestRefused, match="Only"):
        ingest(world, log, store, task_id, filename="q.pdf", data=b"%PDF-1.7")
    (details,) = events(world, "failed")
    assert details["filename"] == "q.pdf"
    with world.connect() as conn:
        assert conn.execute(select(artifacts)).first() is None
        assert conn.execute(select(task_questions)).first() is None
