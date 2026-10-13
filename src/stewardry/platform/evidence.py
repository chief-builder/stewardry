"""Evidence snapshots, freshness, and the strength rubric (spec sections 4.3.1, 5.1, 5.2).

A snapshot is what one acting user read from one policy page during one run:
page ID, the returned version (nullable until gap G7 is verified), a SHA-256
of the returned body, the policy metadata, and numbered passages with stable
locations. The content hash, not the version, is the authoritative change
detector.

Supported page layout (plain content only; spec section 4.3.1):

    <h1>SEC-07 Data Retention and Disposal</h1>
    <table>
      <tr><th>Policy ID</th><td>SEC-07</td></tr>
      <tr><th>Version</th><td>2</td></tr>
      <tr><th>Status</th><td>current</td></tr>          current or superseded
      <tr><th>Classification</th><td>internal</td></tr>
      <tr><th>Owner</th><td>...</td></tr>
      <tr><th>Effective date</th><td>2026-08-01</td></tr>
      <tr><th>Review date</th><td>2027-08-01</td></tr>
      <tr><th>Superseded by</th><td>SEC-05</td></tr>   optional
    </table>
    <h2>Retention</h2>
    <p>7.1 Customer data is retained ...</p>

A missing or empty body, any element outside that plain set (Confluence
macros arrive as `ac:` elements or macro wrappers), missing metadata, or no
numbered passages make the snapshot ineligible: it can never support an
answer. Ineligible snapshots are still stored, so reviewers can see why.

Snapshots and the cache never cross users, customers, tasks, or runs: every
lookup filters on all of them, taken from the run's protected context.
"""

import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Literal

from sqlalchemy import Engine, Row, insert, select, update

from stewardry.platform.audit import AuditEvent, AuditLog
from stewardry.platform.db.schema import evidence_snapshots, utcnow
from stewardry.platform.execution import RunContext

Freshness = Literal["current", "overdue", "superseded"]
Strength = Literal["direct_current", "partial_ambiguous", "insufficient_conflicting_stale"]
Claimed = Literal["direct", "partial"]

PASSAGE = re.compile(r"^(\d{1,3}\.\d{1,3})\s+(\S.*)$", re.DOTALL)
POLICY_ID = re.compile(r"^[A-Z]{2,8}-\d{1,4}$")
STATUSES = frozenset({"current", "superseded"})
BLOCKS = frozenset({"h1", "h2", "h3", "h4", "p", "li", "th", "td"})
INLINE = frozenset({"strong", "em", "b", "i", "u", "code", "br", "span"})
CONTAINERS = frozenset({"table", "thead", "tbody", "tr", "ul", "ol"})
METADATA_FIELDS = {
    "policy id": "policy_id",
    "version": "policy_version",
    "status": "status",
    "classification": "classification",
    "owner": "owner",
    "effective date": "effective_date",
    "review date": "review_date",
    "superseded by": "superseded_by",
}
REQUIRED_METADATA = frozenset(METADATA_FIELDS.values()) - {"superseded_by"}


# ------------------------------------------------------------------ parsing


@dataclass(frozen=True)
class Passage:
    ref: str  # "7.2": stable within a policy version
    section: str
    text: str


@dataclass(frozen=True)
class PolicyMetadata:
    policy_id: str
    policy_version: int
    status: str
    classification: str
    owner: str
    effective_date: dt.date
    review_date: dt.date
    superseded_by: str | None = None


@dataclass(frozen=True)
class ParsedPage:
    metadata: PolicyMetadata | None
    passages: list[Passage]
    problems: list[str]


class _Blocks(HTMLParser):
    """Collects (tag, text) for each block element, and any unsupported tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.rows: list[list[str]] = []
        self.unsupported: list[str] = []
        self._tag: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "p" and self._tag in {"th", "td"}:
            self._text.append(" ")  # Confluence wraps cell text in <p>; it stays in the cell
        elif tag in BLOCKS:
            self._flush()
            self._tag = tag
        elif tag == "tr":
            self.rows.append([])
        elif tag == "br":
            self._text.append(" ")
        elif tag not in INLINE | CONTAINERS:
            self.unsupported.append(tag)
        if any(name.startswith("data-macro") for name, _ in attrs):
            self.unsupported.append(f"{tag} (macro wrapper)")

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCKS and tag == self._tag:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._tag is not None:
            self._text.append(data)

    def _flush(self) -> None:
        if self._tag is None:
            return
        text = " ".join("".join(self._text).split())
        if self._tag in {"th", "td"} and self.rows:
            self.rows[-1].append(text)
        else:
            self.blocks.append((self._tag, text))
        self._tag, self._text = None, []


def _metadata(rows: list[list[str]], problems: list[str]) -> PolicyMetadata | None:
    values: dict[str, str] = {}
    for row in rows:
        if len(row) == 2 and row[0].strip().lower() in METADATA_FIELDS:
            values[METADATA_FIELDS[row[0].strip().lower()]] = row[1].strip()
    missing = sorted(REQUIRED_METADATA - {k for k, v in values.items() if v})
    if missing:
        problems.append("missing policy metadata: " + ", ".join(missing))
        return None
    try:
        meta = PolicyMetadata(
            policy_id=values["policy_id"],
            policy_version=int(values["policy_version"]),
            status=values["status"].lower(),
            classification=values["classification"].lower(),
            owner=values["owner"],
            effective_date=dt.date.fromisoformat(values["effective_date"]),
            review_date=dt.date.fromisoformat(values["review_date"]),
            superseded_by=values.get("superseded_by") or None,
        )
    except ValueError:
        problems.append("invalid policy metadata (version must be a number, dates YYYY-MM-DD)")
        return None
    if not POLICY_ID.fullmatch(meta.policy_id):
        problems.append(f"invalid policy ID {meta.policy_id!r}")
    if meta.status not in STATUSES:
        problems.append(f"unknown policy status {meta.status!r}")
    return meta


def parse_page(body_html: str | None) -> ParsedPage:
    """Metadata and numbered passages from a page body, with any problems that
    make it ineligible as evidence."""
    if body_html is None or not body_html.strip():
        return ParsedPage(None, [], ["incomplete body: the page returned no content"])
    parser = _Blocks()
    parser.feed(body_html)
    parser.close()
    parser._flush()
    problems = [f"unsupported content: <{tag}>" for tag in dict.fromkeys(parser.unsupported)]
    metadata = _metadata(parser.rows, problems)
    passages: list[Passage] = []
    section = ""
    for tag, text in parser.blocks:
        if tag in {"h2", "h3", "h4"}:
            section = text
        elif tag in {"p", "li"} and (match := PASSAGE.match(text)):
            passages.append(Passage(match[1], section, match[2]))
    refs = [p.ref for p in passages]
    if not passages:
        problems.append("no numbered passages")
    elif len(set(refs)) != len(refs):
        problems.append("a passage number is used more than once")
    return ParsedPage(metadata, passages, problems)


# ------------------------------------------------------------------ freshness


def freshness(meta: PolicyMetadata, as_of: dt.date) -> Freshness:
    """Superseded wins over overdue: a superseded policy never counts."""
    if meta.status == "superseded" or meta.superseded_by:
        return "superseded"
    if meta.review_date < as_of:
        return "overdue"
    return "current"


# ------------------------------------------------------------------ snapshots


@dataclass(frozen=True)
class EvidenceSnapshot:
    snapshot_id: str
    run_id: str
    acting_sub: str
    task_id: str
    customer_id: str
    page_id: str
    page_version: int | None
    content_sha256: str
    metadata: PolicyMetadata | None
    passages: list[Passage]
    problems: list[str]
    invalidated_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return not self.problems and self.metadata is not None and not self.invalidated_reason

    def passage(self, ref: str) -> Passage | None:
        return next((p for p in self.passages if p.ref == ref), None)


def _meta_json(meta: PolicyMetadata | None) -> str | None:
    if meta is None:
        return None
    data = {k: str(v) if isinstance(v, dt.date) else v for k, v in meta.__dict__.items()}
    return json.dumps(data, sort_keys=True)


def _meta_from_json(text: str | None) -> PolicyMetadata | None:
    if text is None:
        return None
    data: dict[str, Any] = json.loads(text)
    for key in ("effective_date", "review_date"):
        data[key] = dt.date.fromisoformat(data[key])
    return PolicyMetadata(**data)


def _from_row(row: Row[Any]) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        snapshot_id=row.snapshot_id,
        run_id=row.run_id,
        acting_sub=row.acting_sub,
        task_id=row.task_id,
        customer_id=row.customer_id,
        page_id=row.page_id,
        page_version=row.page_version,
        content_sha256=row.content_sha256,
        metadata=_meta_from_json(row.metadata_json),
        passages=[Passage(**p) for p in json.loads(row.passages_json)],
        problems=json.loads(row.problems_json),
        invalidated_reason=row.invalidated_reason,
    )


def _version(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class EvidenceCache:
    """One run's snapshots, keyed by acting user, task, page, and version.

    Every query also filters on the run and its customer, all taken from the
    run's protected context, so nothing is shared across users, customers,
    tasks, or runs.
    """

    def __init__(self, engine: Engine, log: AuditLog, run: RunContext) -> None:
        self.engine = engine
        self.log = log
        self.run = run

    def _scope(self) -> Any:
        c = evidence_snapshots.c
        return (
            (c.run_id == self.run.run_id)
            & (c.acting_sub == self.run.acting_sub)
            & (c.task_id == self.run.task_id)
            & (c.customer_id == self.run.customer_id)
        )

    def lookup(self, page_id: str, version: int | None) -> EvidenceSnapshot | None:
        """A valid snapshot of exactly this page version, if this run has one.
        Without a version number (gap G7) the page must be read again."""
        if version is None:
            return None
        c = evidence_snapshots.c
        with self.engine.connect() as conn:
            row = conn.execute(
                select(evidence_snapshots)
                .where(
                    self._scope(),
                    c.page_id == page_id,
                    c.page_version == version,
                    c.invalidated_at.is_(None),
                )
                .order_by(c.created_at.desc())
            ).first()
        return _from_row(row) if row is not None else None

    def latest(self, page_id: str) -> EvidenceSnapshot | None:
        """This run's newest valid snapshot of a page, whatever its version."""
        c = evidence_snapshots.c
        with self.engine.connect() as conn:
            row = conn.execute(
                select(evidence_snapshots)
                .where(self._scope(), c.page_id == page_id, c.invalidated_at.is_(None))
                .order_by(c.created_at.desc())
            ).first()
        return _from_row(row) if row is not None else None

    def get(self, snapshot_id: str) -> EvidenceSnapshot | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(evidence_snapshots).where(
                    self._scope(), evidence_snapshots.c.snapshot_id == snapshot_id
                )
            ).first()
        return _from_row(row) if row is not None else None

    def record(self, page: Mapping[str, Any]) -> EvidenceSnapshot:
        """Snapshot a page read by the adapter (`confluence_read_page`).

        If this run already holds a valid snapshot of the page with the same
        content hash and metadata, that snapshot is returned. If either
        changed, the old snapshot is invalidated (and audited) and the new one
        is stored.
        """
        page_id = str(page["page_id"])
        body = page.get("body_html")
        body = body if isinstance(body, str) else None
        sha256 = hashlib.sha256((body or "").encode("utf-8")).hexdigest()
        parsed = parse_page(body)
        c = evidence_snapshots.c
        with self.engine.begin() as conn:
            previous = [
                _from_row(r)
                for r in conn.execute(
                    select(evidence_snapshots).where(
                        self._scope(), c.page_id == page_id, c.invalidated_at.is_(None)
                    )
                )
            ]
            for old in previous:
                if old.content_sha256 == sha256 and old.metadata == parsed.metadata:
                    return old
            now = utcnow()
            for old in previous:
                reason = (
                    "content changed" if old.content_sha256 != sha256 else "policy metadata changed"
                )
                self._invalidate(conn, old, reason, now)
            snapshot = EvidenceSnapshot(
                snapshot_id=uuid.uuid4().hex,
                run_id=self.run.run_id,
                acting_sub=self.run.acting_sub,
                task_id=self.run.task_id,
                customer_id=self.run.customer_id,
                page_id=page_id,
                page_version=_version(page.get("version")),
                content_sha256=sha256,
                metadata=parsed.metadata,
                passages=parsed.passages,
                problems=parsed.problems,
            )
            conn.execute(
                insert(evidence_snapshots).values(
                    snapshot_id=snapshot.snapshot_id,
                    run_id=snapshot.run_id,
                    acting_sub=snapshot.acting_sub,
                    task_id=snapshot.task_id,
                    customer_id=snapshot.customer_id,
                    page_id=page_id,
                    page_version=snapshot.page_version,
                    content_sha256=sha256,
                    metadata_json=_meta_json(parsed.metadata),
                    passages_json=json.dumps([p.__dict__ for p in parsed.passages]),
                    problems_json=json.dumps(parsed.problems),
                    eligible=snapshot.eligible,
                    created_at=now,
                )
            )
        return snapshot

    def revalidate(self, snapshot: EvidenceSnapshot, page: Mapping[str, Any]) -> bool:
        """Compare a fresh read with a snapshot (before an artifact is
        returned). Returns True if unchanged; otherwise the snapshot is
        invalidated and the fresh read is recorded."""
        if snapshot.run_id != self.run.run_id or self.get(snapshot.snapshot_id) is None:
            raise ValueError("snapshot does not belong to this run")
        return self.record(page).snapshot_id == snapshot.snapshot_id

    def _invalidate(self, conn: Any, old: EvidenceSnapshot, reason: str, now: dt.datetime) -> None:
        conn.execute(
            update(evidence_snapshots)
            .where(evidence_snapshots.c.snapshot_id == old.snapshot_id)
            .values(invalidated_at=now, invalidated_reason=reason)
        )
        self.log.append(
            conn,
            AuditEvent(
                "evidence.invalidated",
                "succeeded",
                actor_sub=self.run.acting_sub,
                task_id=self.run.task_id,
                customer_id=self.run.customer_id,
                agent_id=self.run.agent_id,
                run_id=self.run.run_id,
                policy_hash=self.run.policy_hash,
                details={"snapshot_id": old.snapshot_id, "page_id": old.page_id, "reason": reason},
            ),
        )


# ------------------------------------------------------------------ strength


@dataclass(frozen=True)
class Citation:
    snapshot: EvidenceSnapshot
    passage_ref: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.snapshot.page_id, self.passage_ref)


@dataclass(frozen=True)
class ConflictMarker:
    """Two or more passages that disagree (for example backup retention of 35
    days in one policy and 90 in another). Recorded by the drafting stage or a
    reviewer; the agent never resolves a conflict itself (spec section 5.1)."""

    passages: frozenset[tuple[str, str]]  # (page_id, passage_ref)
    note: str

    def __post_init__(self) -> None:
        if len(self.passages) < 2:
            raise ValueError("a conflict involves at least two passages")


@dataclass(frozen=True)
class Assessment:
    label: Strength
    reasons: list[str] = field(default_factory=list)

    @property
    def approvable(self) -> bool:
        return self.label == "direct_current"


def assess(
    claimed: Claimed,
    citations: Sequence[Citation],
    conflicts: Sequence[ConflictMarker],
    as_of: dt.date,
) -> Assessment:
    """The rubric label for a drafted answer (evals/rubrics/evidence-strength-v1.md
    documents the same three labels).

    The drafting stage claims direct or partial support. These deterministic
    rules can only lower that claim, never raise it: no citations, a citation
    to a missing passage, an ineligible or invalidated snapshot, a superseded
    or overdue policy, or a recorded conflict all give
    insufficient/conflicting/stale support.
    """
    reasons: list[str] = []
    if not citations:
        reasons.append("no citations")
    for cite in citations:
        where = f"page {cite.snapshot.page_id} passage {cite.passage_ref}"
        meta = cite.snapshot.metadata
        if cite.snapshot.invalidated_reason:
            reasons.append(f"{where}: evidence invalidated ({cite.snapshot.invalidated_reason})")
        elif not cite.snapshot.eligible or meta is None:
            reasons.append(f"{where}: page is not eligible evidence")
        elif cite.snapshot.passage(cite.passage_ref) is None:
            reasons.append(f"{where}: no such passage")
        elif (state := freshness(meta, as_of)) != "current":
            reasons.append(f"{where}: policy {meta.policy_id} is {state}")
    cited = {c.key for c in citations}
    for conflict in conflicts:
        if conflict.passages & cited:
            reasons.append(f"conflicting evidence: {conflict.note}")
    if reasons:
        return Assessment("insufficient_conflicting_stale", reasons)
    if claimed == "partial":
        return Assessment("partial_ambiguous", ["support claimed as partial or ambiguous"])
    return Assessment("direct_current")
