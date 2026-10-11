"""Evidence snapshots, freshness, the per-run cache, and the strength rubric."""

import datetime as dt
import json
import re
import uuid

import pytest
from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

from stewardry.platform.audit import AuditLog, verify
from stewardry.platform.db.schema import (
    agent_policy_versions,
    audit_events,
    evidence_snapshots,
    runs,
    tasks,
    utcnow,
)
from stewardry.platform.evidence import (
    Citation,
    ConflictMarker,
    EvidenceCache,
    assess,
    freshness,
    parse_page,
)
from stewardry.platform.execution import RunContext
from tests.fakes.policy_pages import POLICIES, page, policy_html
from tests.unit.conftest import AGENT, ALICE, BOB, CAROL, LARKSPUR, QUILLON

AS_OF = dt.date(2026, 10, 10)  # the eval dataset's as_of date
ALL_POLICIES = sorted(p.name[:6] for p in POLICIES.glob("SEC-*.md"))


def new_run(
    world: Engine, acting_sub: str, customer_id: str, task_id: str | None = None
) -> RunContext:
    """A run row for these tests; admission itself is tested in test_execution."""
    now = utcnow()
    with world.begin() as conn:
        version_id, policy_hash = conn.execute(
            select(agent_policy_versions.c.id, agent_policy_versions.c.policy_hash)
        ).one()
        if task_id is None:
            task_id = uuid.uuid4().hex
            conn.execute(
                insert(tasks).values(
                    task_id=task_id,
                    agent_id=AGENT,
                    customer_id=customer_id,
                    requester_sub=ALICE,
                    submission_key=task_id,
                    status="drafting",
                    created_at=now,
                    updated_at=now,
                )
            )
        run_id = uuid.uuid4().hex
        conn.execute(
            insert(runs).values(
                run_id=run_id,
                task_id=task_id,
                agent_id=AGENT,
                policy_version_id=version_id,
                acting_sub=acting_sub,
                stage="drafting",
                provenance="offline-replay",
                status="running",
                started_at=now,
            )
        )
    return RunContext(
        run_id,
        task_id,
        AGENT,
        customer_id,
        "drafting",
        acting_sub,
        None,
        version_id,
        policy_hash,
        "offline-replay",
    )


@pytest.fixture
def cache(world: Engine, log: AuditLog) -> EvidenceCache:
    return EvidenceCache(world, log, new_run(world, BOB, LARKSPUR))


def invalidations(world: Engine) -> list[dict[str, str]]:
    with world.connect() as conn:
        rows = conn.execute(
            select(audit_events.c.details_json).where(
                audit_events.c.event_type == "evidence.invalidated"
            )
        ).scalars()
        return [json.loads(r) for r in rows]


# ------------------------------------------------------------ parsing


@pytest.mark.parametrize("policy_id", ALL_POLICIES)
def test_every_policy_fixture_is_eligible_with_its_passages(policy_id: str) -> None:
    meta, body = policy_html(policy_id)
    parsed = parse_page(body)
    assert parsed.problems == []
    assert parsed.metadata is not None
    assert parsed.metadata.policy_id == policy_id
    assert parsed.metadata.policy_version == meta["version"]
    (path,) = POLICIES.glob(f"{policy_id}-*.md")
    expected = re.findall(r"^(\d+\.\d+) ", path.read_text(), re.MULTILINE)
    assert [p.ref for p in parsed.passages] == expected


def test_passages_keep_their_section_and_text() -> None:
    parsed = parse_page(policy_html("SEC-07")[1])
    backup = next(p for p in parsed.passages if p.ref == "7.2")
    assert backup.section == "Retention"
    assert backup.text == "Backups are retained for 35 days and then deleted automatically."


def _with(body: str, extra: str) -> str:
    return body.replace("</h2>", "</h2>" + extra, 1)


BODY = policy_html("SEC-05")[1]


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        (None, "incomplete body"),
        ("   ", "incomplete body"),
        (
            _with(BODY, '<ac:structured-macro ac:name="excerpt-include"/>'),
            "unsupported content: <ac:structured-macro>",
        ),
        (_with(BODY, '<div class="conf-macro">x</div>'), "unsupported content: <div>"),
        (_with(BODY, '<span data-macro-name="jira">x</span>'), "macro wrapper"),
        (_with(BODY, "<script>x</script>"), "unsupported content: <script>"),
        (re.sub(r"<table>.*</table>", "", BODY), "missing policy metadata"),
        (BODY.replace("<td>current</td>", "<td>draft</td>"), "unknown policy status"),
        (BODY.replace("<td>SEC-05</td>", "<td>sec five</td>"), "invalid policy ID"),
        (BODY.replace("2026-", "26-"), "invalid policy metadata"),
        (re.sub(r"<p>\d+\.\d+ ", "<p>", BODY), "no numbered passages"),
        (BODY.replace("<p>5.2 ", "<p>5.1 "), "used more than once"),
    ],
)
def test_incomplete_or_unsupported_pages_are_ineligible(body: str | None, problem: str) -> None:
    assert any(problem in p for p in parse_page(body).problems)


# ------------------------------------------------------------ freshness


@pytest.mark.parametrize(
    ("policy_id", "expected"),
    [("SEC-05", "current"), ("SEC-19", "overdue"), ("SEC-21", "superseded")],
)
def test_freshness_follows_review_dates_and_supersession(policy_id: str, expected: str) -> None:
    meta = parse_page(policy_html(policy_id)[1]).metadata
    assert meta is not None
    assert freshness(meta, AS_OF) == expected


def test_superseded_wins_over_overdue() -> None:
    meta = parse_page(policy_html("SEC-21")[1]).metadata
    assert meta is not None and meta.review_date < AS_OF
    assert freshness(meta, AS_OF) == "superseded"


# ------------------------------------------------------------ cache


def test_snapshot_records_identity_version_and_hash(cache: EvidenceCache) -> None:
    snap = cache.record(page("SEC-05", version=4))
    assert (snap.page_id, snap.page_version, snap.acting_sub, snap.customer_id) == (
        "105",
        4,
        BOB,
        LARKSPUR,
    )
    assert re.fullmatch(r"[0-9a-f]{64}", snap.content_sha256)
    assert snap.eligible
    assert cache.lookup("105", 4) == snap
    assert cache.get(snap.snapshot_id) == snap


def test_lookup_needs_a_version(cache: EvidenceCache) -> None:
    cache.record(page("SEC-05", version=None))
    assert cache.lookup("105", None) is None
    assert cache.lookup("105", 3) is None


def test_rereading_the_same_content_reuses_the_snapshot(cache: EvidenceCache) -> None:
    first = cache.record(page("SEC-05"))
    assert cache.record(page("SEC-05")) == first
    assert cache.revalidate(first, page("SEC-05"))


def test_a_content_change_invalidates_the_evidence(world: Engine, cache: EvidenceCache) -> None:
    old = cache.record(page("SEC-05"))
    changed = page("SEC-05", body=policy_html("SEC-05")[1].replace("AES-256", "AES-128"))
    assert changed["body_html"] != page("SEC-05")["body_html"]
    assert not cache.revalidate(old, changed)
    stale = cache.get(old.snapshot_id)
    assert stale is not None and stale.invalidated_reason == "content changed"
    assert not stale.eligible
    assert cache.lookup("105", 3) != stale
    assert invalidations(world) == [
        {"snapshot_id": old.snapshot_id, "page_id": "105", "reason": "content changed"}
    ]
    with world.connect() as conn:
        assert verify(conn).ok


def test_a_hash_change_invalidates_even_with_the_same_version(cache: EvidenceCache) -> None:
    old = cache.record(page("SEC-05", version=3))
    new = cache.record(page("SEC-05", version=3, body=policy_html("SEC-05")[1] + "<p>x</p>"))
    assert new.snapshot_id != old.snapshot_id
    assert cache.lookup("105", 3) == new


def test_an_ineligible_read_still_invalidates_earlier_evidence(cache: EvidenceCache) -> None:
    old = cache.record(page("SEC-05"))
    new = cache.record(page("SEC-05", body=""))
    assert not new.eligible and new.problems
    stale = cache.get(old.snapshot_id)
    assert stale is not None and stale.invalidated_reason


def test_revalidate_refuses_another_runs_snapshot(world: Engine, log: AuditLog) -> None:
    mine = EvidenceCache(world, log, new_run(world, BOB, LARKSPUR))
    theirs = EvidenceCache(world, log, new_run(world, BOB, LARKSPUR))
    snap = theirs.record(page("SEC-05"))
    with pytest.raises(ValueError, match="this run"):
        mine.revalidate(snap, page("SEC-05"))


# ------------------------------------------------------------ isolation


def test_snapshots_never_cross_users_customers_tasks_or_runs(world: Engine, log: AuditLog) -> None:
    bob = new_run(world, BOB, LARKSPUR)
    snap = EvidenceCache(world, log, bob).record(page("SEC-05"))
    others = [
        new_run(world, CAROL, QUILLON),  # another user and customer
        new_run(world, BOB, QUILLON),  # same user, another customer
        new_run(world, BOB, LARKSPUR),  # same user and customer, another task
        new_run(world, BOB, LARKSPUR, task_id=bob.task_id),  # same task, another run
    ]
    for run in others:
        cache = EvidenceCache(world, log, run)
        assert cache.lookup("105", 3) is None
        assert cache.get(snap.snapshot_id) is None
        assert cache.record(page("SEC-05")).snapshot_id != snap.snapshot_id
    still = EvidenceCache(world, log, bob).get(snap.snapshot_id)
    assert still is not None and still.invalidated_reason is None


def test_a_forged_run_context_cannot_read_another_users_snapshot(
    world: Engine, log: AuditLog
) -> None:
    bob = new_run(world, BOB, LARKSPUR)
    snap = EvidenceCache(world, log, bob).record(page("SEC-05"))
    forged = RunContext(**{**bob.__dict__, "acting_sub": CAROL})
    assert EvidenceCache(world, log, forged).get(snap.snapshot_id) is None
    forged = RunContext(**{**bob.__dict__, "customer_id": QUILLON})
    assert EvidenceCache(world, log, forged).lookup("105", 3) is None


# ------------------------------------------------------------ strength


def cite(cache: EvidenceCache, policy_id: str, ref: str) -> Citation:
    return Citation(cache.record(page(policy_id)), ref)


def test_direct_current_support_is_approvable(cache: EvidenceCache) -> None:
    result = assess("direct", [cite(cache, "SEC-05", "5.1")], [], AS_OF)
    assert result.label == "direct_current" and result.approvable


def test_claimed_partial_support_stays_partial(cache: EvidenceCache) -> None:
    result = assess("partial", [cite(cache, "SEC-05", "5.1")], [], AS_OF)
    assert result.label == "partial_ambiguous" and not result.approvable


@pytest.mark.parametrize(
    ("policy_id", "ref", "reason"),
    [
        ("SEC-21", "21.1", "is superseded"),
        ("SEC-19", "19.1", "is overdue"),
        ("SEC-05", "5.99", "no such passage"),
    ],
)
def test_stale_or_missing_evidence_is_insufficient(
    cache: EvidenceCache, policy_id: str, ref: str, reason: str
) -> None:
    result = assess("direct", [cite(cache, policy_id, ref)], [], AS_OF)
    assert result.label == "insufficient_conflicting_stale"
    assert not result.approvable
    assert reason in result.reasons[0]


def test_no_citations_is_insufficient() -> None:
    assert assess("direct", [], [], AS_OF).reasons == ["no citations"]


def test_ineligible_or_invalidated_evidence_is_insufficient(cache: EvidenceCache) -> None:
    broken = Citation(cache.record(page("SEC-06", body="")), "6.1")
    assert "not eligible" in assess("direct", [broken], [], AS_OF).reasons[0]
    old = cache.record(page("SEC-05"))
    cache.record(page("SEC-05", body=policy_html("SEC-05")[1] + "<p>5.99 New.</p>"))
    stale = cache.get(old.snapshot_id)
    assert stale is not None
    assert "invalidated" in assess("direct", [Citation(stale, "5.1")], [], AS_OF).reasons[0]


def test_a_recorded_conflict_blocks_approval(cache: EvidenceCache) -> None:
    backup_35 = cite(cache, "SEC-07", "7.2")
    backup_90 = cite(cache, "SEC-15", "15.3")
    conflict = ConflictMarker(
        frozenset({backup_35.key, backup_90.key}), "backup retention: 35 days vs 90 days"
    )
    for citations in ([backup_35], [backup_90], [backup_35, backup_90]):
        result = assess("direct", citations, [conflict], AS_OF)
        assert result.label == "insufficient_conflicting_stale"
        assert "35 days vs 90 days" in result.reasons[-1]
    unrelated = assess("direct", [cite(cache, "SEC-05", "5.1")], [conflict], AS_OF)
    assert unrelated.approvable


def test_a_conflict_needs_two_passages() -> None:
    with pytest.raises(ValueError, match="two passages"):
        ConflictMarker(frozenset({("107", "7.2")}), "alone")


# ------------------------------------------------------------ constraints


def _row(run: RunContext, **changes: object) -> dict[str, object]:
    return {
        "snapshot_id": uuid.uuid4().hex,
        "run_id": run.run_id,
        "acting_sub": run.acting_sub,
        "task_id": run.task_id,
        "customer_id": run.customer_id,
        "page_id": "105",
        "content_sha256": "0" * 64,
        "passages_json": "[]",
        "problems_json": "[]",
        "eligible": False,
        "created_at": utcnow(),
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"eligible": True, "metadata_json": None},
        {"invalidated_at": utcnow()},
        {"invalidated_reason": "changed"},
        {"content_sha256": "not-a-hash"},
        {"page_id": ""},
    ],
)
def test_database_rejects_inconsistent_snapshots(world: Engine, changes: dict[str, object]) -> None:
    run = new_run(world, BOB, LARKSPUR)
    with pytest.raises(IntegrityError), world.begin() as conn:
        conn.execute(insert(evidence_snapshots).values(_row(run, **changes)))
