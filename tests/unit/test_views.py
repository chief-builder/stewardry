"""Role-restricted views (S12): what Sales, GRC, and platform users can see,
leak tests on the rendered HTML, shadow verdicts, and audited refusals."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from stewardry.engagements.questionnaire.review import ReviewRefused, record_shadow_verdict
from stewardry.platform.artifacts import ArtifactStore
from stewardry.platform.audit import AuditLog
from stewardry.platform.credentials import CredentialStore
from stewardry.platform.db.schema import (
    audit_events,
    evidence_snapshots,
    question_drafts,
    screening_findings,
    shadow_verdicts,
)
from stewardry.web.app import Services, create_app
from stewardry.web.hub import HubClient
from stewardry.web.sessions import SessionStore
from stewardry.web.views import EXPERT_REASONS, STRENGTH_LABELS, review_view, sales_status
from tests.fakes.drafting_setup import Setup, make_setup
from tests.fakes.hub import CLIENT_ID, CLIENT_SECRET, ISSUER, FakeHub
from tests.fakes.scripted_model import ScriptedModel
from tests.unit.conftest import ALICE, BOB, CAROL, OWNER, PLATFORM, ROOT
from tests.unit.test_drafting import PLANS, read, submit
from tests.unit.test_web import BASE, SETTINGS

QUESTIONNAIRE = ROOT / "fixtures" / "questionnaires" / "QN-LARKSPUR-01.xlsx"
VIEW_PLANS = PLANS | {
    "Q05": [read("SEC-04"), submit("Admin access is just in time.", "direct", ("SEC-04", "4.1"))]
}
PERSONAS = {
    ALICE: "alice",
    BOB: "bob",
    CAROL: "carol",
    PLATFORM: "platform",
    OWNER: "owner",
}


@pytest.fixture
def drafted(world: Engine, log: AuditLog, tmp_path: Path) -> Setup:
    setup = make_setup(world, log, tmp_path, QUESTIONNAIRE.name, QUESTIONNAIRE.read_bytes())
    assert setup.drafting(ScriptedModel(VIEW_PLANS)).run(setup.start()) is True
    return setup


@pytest.fixture
def services(world: Engine, log: AuditLog, tmp_path: Path) -> Iterator[Services]:
    hub = FakeHub()
    http = httpx2.Client(transport=hub.transport())
    client = HubClient(ISSUER, CLIENT_ID, CLIENT_SECRET, SETTINGS.redirect_uri, http)
    services = Services(
        SETTINGS,
        world,
        client,
        log,
        SessionStore(),
        CredentialStore(),
        ArtifactStore(tmp_path / "web-artifacts"),
    )
    services.hub_fake = hub  # type: ignore[attr-defined]
    yield services
    http.close()


def as_user(services: Services, sub: str) -> TestClient:
    client = TestClient(create_app(services), base_url=BASE, follow_redirects=False)
    start = client.get("/login")
    hub: FakeHub = services.hub_fake  # type: ignore[attr-defined]
    code, state = hub.approve(start.headers["location"], sub, PERSONAS[sub])
    assert client.get("/auth/callback", params={"code": code, "state": state}).status_code == 303
    return client


def csrf(client: TestClient, path: str = "/") -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match
    return match[1]


def denials(world: Engine, event_type: str = "view.denied") -> list[dict[str, Any]]:
    with world.connect() as conn:
        rows = conn.execute(
            select(audit_events.c.details_json).where(
                audit_events.c.event_type == event_type, audit_events.c.outcome == "denied"
            )
        ).scalars()
        return [json.loads(r) for r in rows]


def reviewer_only_strings(world: Engine) -> set[str]:
    """Everything Sales must never see, taken from what drafting stored."""
    found: set[str] = set()
    with world.connect() as conn:
        for draft in conn.execute(select(question_drafts)):
            found |= {draft.answer} if draft.answer else set()
            found |= set(json.loads(draft.reasons_json))
        for snap in conn.execute(select(evidence_snapshots)):
            found |= {p["text"] for p in json.loads(snap.passages_json)}
            found |= {p["section"] for p in json.loads(snap.passages_json)}
        for finding in conn.execute(select(screening_findings)):
            found.add(finding.excerpt)
    catalog = yaml.safe_load((ROOT / "policies/catalogs/secpol.yaml").read_text())
    found |= {p["title"] for p in catalog["pages"]}
    found |= set(STRENGTH_LABELS.values()) | set(EXPERT_REASONS.values())
    found |= {"ready_for_review", "needs_expert", "direct_current", "bob", BOB}
    return {s for s in found if s and len(s) > 3}


# ------------------------------------------------------------ Sales


def test_sales_sees_status_without_reviewer_content(drafted: Setup, services: Services) -> None:
    alice = as_user(services, ALICE)
    page = alice.get(f"/tasks/{drafted.task_id}")
    assert page.status_code == 200
    assert "Drafts are with GRC for review. Nothing has been approved or released yet." in page.text
    forbidden = reviewer_only_strings(drafted.world)
    assert len(forbidden) > 80  # answers, reasons, passages, findings, titles, labels
    review_html = as_user(services, BOB).get(f"/review/{drafted.task_id}").text
    assert sum(s in review_html for s in forbidden) > 30  # the probe finds real content
    for path in ("/", f"/tasks/{drafted.task_id}"):
        html = alice.get(path).text
        leaked = [s for s in forbidden if s in html]
        assert leaked == []
        assert not re.search(r"SEC-\d", html)
        assert not re.search(r'href="(https?:)?//', html)  # no links off the app
        assert "/review/" not in html and "/registry" not in html


@pytest.mark.parametrize(
    ("status", "questions", "processed", "text"),
    [
        ("awaiting_authorization", 0, 0, "Upload the customer's questionnaire"),
        ("awaiting_authorization", 30, 0, "Questionnaire received (30 questions)"),
        ("drafting", 30, 12, "12 of 30 questions processed"),
        ("paused", 30, 12, "paused until GRC can continue"),
        ("in_review", 30, 30, "Nothing has been approved"),
        ("failed", 30, 3, "could not be completed"),
        ("mystery", 30, 3, "in progress"),
    ],
)
def test_sales_status_text(status: str, questions: int, processed: int, text: str) -> None:
    message = sales_status(status, questions, processed)
    assert text in message
    assert not re.search(r"SEC-|polic|conflict|stale|expert|bob", message, re.IGNORECASE)


def test_only_the_requester_sees_a_task_page(drafted: Setup, services: Services) -> None:
    for sub in (BOB, CAROL, PLATFORM):
        response = as_user(services, sub).get(f"/tasks/{drafted.task_id}")
        assert response.status_code == 404
    assert as_user(services, ALICE).get("/tasks/no-such-task").status_code == 404
    reasons = [d["reason"] for d in denials(drafted.world) if d["view"] == "task_status"]
    assert reasons.count("not the requester") == 3 and "no such task" in reasons


def test_sales_uploads_a_questionnaire(services: Services) -> None:
    alice = as_user(services, ALICE)
    token = csrf(alice)
    form = {"csrf_token": token, "customer_id": "larkspur-synthetic", "submission_key": "k1"}
    assert alice.post("/tasks", data=form).status_code == 303
    task_id = re.search(r'href="/tasks/([0-9a-f]{32})"', alice.get("/").text)
    assert task_id
    path = f"/tasks/{task_id[1]}"
    assert 'type="file"' in alice.get(path).text

    bad = alice.post(
        f"{path}/questionnaire",
        data={"csrf_token": token},
        files={"file": ("q.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert bad.status_code == 400 and "Only .xlsx and .docx" in bad.text

    bob = as_user(services, BOB)
    stranger = bob.post(
        f"{path}/questionnaire",
        data={"csrf_token": csrf(bob)},
        files={"file": (QUESTIONNAIRE.name, QUESTIONNAIRE.read_bytes())},
    )
    assert stranger.status_code == 404

    good = alice.post(
        f"{path}/questionnaire",
        data={"csrf_token": token},
        files={"file": (QUESTIONNAIRE.name, QUESTIONNAIRE.read_bytes())},
    )
    assert good.status_code == 303 and good.headers["location"] == path
    page = alice.get(path).text
    assert "Questionnaire received (30 questions)" in page
    assert 'type="file"' not in page
    for finding_text in ("Assistant:", "Reviewer note"):
        assert finding_text not in page  # screening findings are reviewer-only


def test_an_upload_needs_the_csrf_token(drafted: Setup, services: Services) -> None:
    alice = as_user(services, ALICE)
    response = alice.post(
        f"/tasks/{drafted.task_id}/questionnaire",
        data={"csrf_token": "forged"},
        files={"file": (QUESTIONNAIRE.name, QUESTIONNAIRE.read_bytes())},
    )
    assert response.status_code == 403


# ------------------------------------------------------------ GRC


def test_grc_sees_drafts_citations_passages_and_findings(
    drafted: Setup, services: Services
) -> None:
    bob = as_user(services, BOB)
    assert f'href="/review/{drafted.task_id}"' in bob.get("/").text
    page = bob.get(f"/review/{drafted.task_id}")
    assert page.status_code == 200
    html = page.text
    assert "Shadow mode" in html and "do not approve anything" in html
    assert "We encrypt data at rest." in html
    assert "SEC-05 §5.1" in html and "AES-256" in html
    assert "Policies conflict" in html and "SEC-15 §15.3" in html
    assert "hidden_column" in html and "Assistant:" in html  # findings with excerpts
    assert "SEC-04 §4.1" in html and "just in time for at most 4 hours" in html
    with drafted.world.connect() as conn:
        views = conn.execute(
            select(audit_events).where(audit_events.c.event_type == "view.review")
        ).all()
    assert len(views) == 1 and views[0].reviewer_sub == BOB


def test_confidential_passages_are_hidden_from_other_reviewers(drafted: Setup) -> None:
    from stewardry.platform.db.schema import tasks

    with drafted.world.connect() as conn:
        task = conn.execute(select(tasks)).one()
        mine = review_view(conn, BOB, task)
        theirs = review_view(conn, "someone-else", task)

    def cited(view: Any, question_id: str) -> Any:
        return next(i for i in view.items if i.question_id == question_id).citations[0]

    assert cited(mine, "Q05").text is not None
    hidden = cited(theirs, "Q05")
    assert hidden.text is None and "confidential" in (hidden.note or "")
    assert cited(theirs, "Q09").text is not None  # SEC-05 is internal


@pytest.mark.parametrize(
    ("sub", "reason"),
    [
        (ALICE, "only GRC reviewers can review drafts"),
        (CAROL, "reviewer is not assigned to this customer"),
        (PLATFORM, "only GRC reviewers can review drafts"),
    ],
)
def test_others_cannot_open_the_review(
    drafted: Setup, services: Services, sub: str, reason: str
) -> None:
    response = as_user(services, sub).get(f"/review/{drafted.task_id}")
    assert response.status_code == 403
    assert "AES-256" not in response.text
    assert {"view": "review", "reason": reason} in denials(drafted.world)


def test_shadow_verdicts_are_recorded_not_approvals(drafted: Setup, services: Services) -> None:
    bob = as_user(services, BOB)
    path = f"/review/{drafted.task_id}"
    token = csrf(bob, path)
    form = {"csrf_token": token, "verdict": "agree", "idempotency_key": "v1"}
    assert bob.post(f"{path}/Q09/verdict", data=form).status_code == 303
    assert bob.post(f"{path}/Q09/verdict", data=form).status_code == 303  # repeat: no-op
    edit = {"csrf_token": token, "verdict": "edit", "idempotency_key": "v2"}
    assert bob.post(f"{path}/Q14/verdict", data=edit).status_code == 403  # no answer given
    reject = edit | {"verdict": "reject", "reason": "<script>alert(1)</script>"}
    assert bob.post(f"{path}/Q14/verdict", data=reject).status_code == 303
    with drafted.world.connect() as conn:
        rows = conn.execute(select(shadow_verdicts).order_by(shadow_verdicts.c.id)).all()
        drafts = {d.question_id: d.status for d in conn.execute(select(question_drafts))}
    assert [(r.question_id, r.verdict) for r in rows] == [("Q09", "agree"), ("Q14", "reject")]
    assert drafts["Q14"] == "needs_expert"  # a verdict changes no draft
    html = bob.get(path).text
    assert "bob · agree" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_sales_cannot_record_a_verdict(drafted: Setup, services: Services) -> None:
    alice = as_user(services, ALICE)
    form = {"csrf_token": csrf(alice), "verdict": "agree", "idempotency_key": "x"}
    response = alice.post(f"/review/{drafted.task_id}/Q09/verdict", data=form)
    assert response.status_code == 403
    assert denials(drafted.world, "review.shadow_verdict")[0]["reason"] == (
        "only GRC reviewers can review drafts"
    )


@pytest.mark.parametrize(
    ("question_id", "verdict", "kwargs", "message"),
    [
        ("Q99", "agree", {}, "no draft"),
        ("Q09", "approve", {}, "unknown verdict"),
        ("Q09", "reject", {}, "needs a reason"),
        ("Q09", "agree", {"key": " "}, "submission key"),
    ],
)
def test_bad_verdicts_are_refused(
    drafted: Setup, question_id: str, verdict: str, kwargs: dict[str, str], message: str
) -> None:
    with pytest.raises(ReviewRefused, match=message):
        record_shadow_verdict(
            drafted.world,
            drafted.log,
            BOB,
            drafted.task_id,
            question_id,
            verdict,
            kwargs.get("key", "k"),
        )


def test_verdicts_wait_for_drafting_to_finish(world: Engine, log: AuditLog, tmp_path: Path) -> None:
    setup = make_setup(world, log, tmp_path, QUESTIONNAIRE.name, QUESTIONNAIRE.read_bytes())
    with pytest.raises(ReviewRefused, match="task is drafting"):
        record_shadow_verdict(world, log, BOB, setup.task_id, "Q09", "agree", "k")


# ------------------------------------------------------------ registry


@pytest.mark.parametrize("sub", [PLATFORM, OWNER])
def test_platform_and_owner_see_the_registry(world: Engine, services: Services, sub: str) -> None:
    client = as_user(services, sub)
    assert 'href="/registry"' in client.get("/").text
    html = client.get("/registry").text
    assert "questionnaire-agent" in html and "claude-opus-5-5 (medium)" in html
    assert "questionnaire-draft@1" in html and "per_question_model_calls: 8" in html


@pytest.mark.parametrize("sub", [ALICE, BOB])
def test_others_cannot_see_the_registry(world: Engine, services: Services, sub: str) -> None:
    client = as_user(services, sub)
    assert 'href="/registry"' not in client.get("/").text
    assert client.get("/registry").status_code == 403
    assert {"view": "registry", "reason": "no platform, owner, or auditor role"} in denials(world)


def test_a_requester_cannot_review_their_own_task(drafted: Setup) -> None:
    from types import SimpleNamespace

    from stewardry.engagements.questionnaire.review import review_problem

    own = SimpleNamespace(customer_id="larkspur-synthetic", requester_sub=BOB)
    with drafted.world.connect() as conn:
        assert review_problem(conn, BOB, own) == "the requester cannot review their own task"  # type: ignore[arg-type]
        assert review_problem(conn, BOB, None) == "task does not exist"
