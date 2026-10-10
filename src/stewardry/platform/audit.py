"""Append-only, hash-chained audit log (ADR 0004).

Each event stores the hash of the previous event and the SHA-256 of its own
canonical JSON. ``verify`` recomputes the chain and compares it with anchors
(chain heads written to a separate file). The database rejects UPDATE and
DELETE and any insert that does not extend the head.

Trust boundary: an administrator who controls both the database and every
anchor copy can rewrite history undetected. Keep anchor copies elsewhere.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, insert, select, text

from stewardry.platform.db.schema import (
    AUDIT_OUTCOMES,
    GENESIS_HASH,
    TIMESTAMP_FORMAT,
    audit_events,
    utcnow,
)
from stewardry.platform.redaction import Redactor

EVENT_TYPE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
IDENTITY_FIELDS = (
    "actor_sub",
    "requester_sub",
    "authorizing_sub",
    "reviewer_sub",
    "agent_id",
    "policy_hash",
    "customer_id",
    "task_id",
    "run_id",
    "grant_id",
    "correlation_id",
)
# Every stored column except event_hash, in a fixed order, is hashed.
HASHED_FIELDS = (
    "seq",
    "event_id",
    "occurred_at",
    "event_type",
    "outcome",
    *IDENTITY_FIELDS,
    "details_json",
    "prev_hash",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(row: Mapping[str, Any]) -> str:
    payload = {name: row[name] for name in HASHED_FIELDS}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    """One auditable decision or action. ``outcome`` separates denied attempts
    (blocked by a control) from actions that succeeded or failed."""

    event_type: str
    outcome: str
    actor_sub: str | None = None
    requester_sub: str | None = None
    authorizing_sub: str | None = None
    reviewer_sub: str | None = None
    agent_id: str | None = None
    policy_hash: str | None = None
    customer_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    grant_id: str | None = None
    correlation_id: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not EVENT_TYPE.fullmatch(self.event_type) or len(self.event_type) > 64:
            raise ValueError(f"event_type must be dotted lowercase, got {self.event_type!r}")
        if self.outcome not in AUDIT_OUTCOMES:
            raise ValueError(f"outcome must be one of {AUDIT_OUTCOMES}, got {self.outcome!r}")
        if not isinstance(self.details, Mapping):
            raise TypeError("details must be a mapping")


@dataclass(frozen=True)
class Appended:
    seq: int
    event_id: str
    event_hash: str
    redacted: frozenset[str]


class AuditLog:
    def __init__(self, redactor: Redactor | None = None) -> None:
        self.redactor = redactor or Redactor.from_environment()

    def append(self, conn: Connection, event: AuditEvent) -> Appended:
        """Append within the caller's transaction, so the event commits or
        rolls back together with the action it records."""
        found: set[str] = set()
        identities: dict[str, str | None] = {}
        for name in IDENTITY_FIELDS:
            value = getattr(event, name)
            if value is not None:
                value, kinds = self.redactor.redact_text(value)
                found |= kinds
            identities[name] = value
        details, kinds = self.redactor.redact(dict(event.details))
        found |= kinds
        if found:
            details["redactions"] = sorted(found)

        head = conn.execute(
            select(audit_events.c.seq, audit_events.c.event_hash)
            .order_by(audit_events.c.seq.desc())
            .limit(1)
        ).first()
        occurred_at = utcnow()
        row: dict[str, Any] = {
            "seq": head.seq + 1 if head else 1,
            "event_id": uuid.uuid4().hex,
            # The hash covers the exact text the column stores.
            "occurred_at": occurred_at.strftime(TIMESTAMP_FORMAT),
            "event_type": event.event_type,
            "outcome": event.outcome,
            **identities,
            "details_json": canonical_json(details),
            "prev_hash": head.event_hash if head else GENESIS_HASH,
        }
        row["event_hash"] = event_hash(row)
        conn.execute(insert(audit_events).values({**row, "occurred_at": occurred_at}))
        return Appended(row["seq"], row["event_id"], row["event_hash"], frozenset(found))


# ------------------------------------------------------------------ anchors


@dataclass(frozen=True)
class Anchor:
    seq: int
    event_hash: str
    anchored_at: str


def write_anchor(conn: Connection, anchor_file: Path) -> Anchor:
    """Append the current chain head to the anchor file (JSON lines, 0600)."""
    head = conn.execute(
        select(audit_events.c.seq, audit_events.c.event_hash)
        .order_by(audit_events.c.seq.desc())
        .limit(1)
    ).first()
    if head is None:
        raise ValueError("the audit log is empty; there is no head to anchor")
    anchor = Anchor(head.seq, head.event_hash, utcnow().strftime(TIMESTAMP_FORMAT))
    if not anchor_file.exists():
        anchor_file.touch(mode=0o600)
        anchor_file.chmod(0o600)
    with anchor_file.open("a", encoding="utf-8") as f:
        f.write(canonical_json(anchor.__dict__) + "\n")
    return anchor


def read_anchors(anchor_file: Path) -> list[Anchor]:
    if not anchor_file.exists():
        return []
    anchors = []
    for line in anchor_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = json.loads(line)
            anchors.append(Anchor(int(data["seq"]), str(data["event_hash"]), data["anchored_at"]))
    return anchors


# ------------------------------------------------------------------- verify


@dataclass
class VerifyResult:
    events: int = 0
    head_seq: int = 0
    anchors_checked: int = 0
    unanchored_tail: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(conn: Connection, anchors: list[Anchor] | None = None) -> VerifyResult:
    """Recompute the chain from the stored text and check it against anchors.

    Detects edited, deleted, reordered, and inserted events. A truncated tail
    or a fully recomputed chain is detected only where an anchor covers it.
    """
    result = VerifyResult()
    rows = [
        dict(r) for r in conn.execute(text("SELECT * FROM audit_events ORDER BY seq")).mappings()
    ]
    hashes: dict[int, str] = {}
    expected_seq, expected_prev = 1, GENESIS_HASH
    for row in rows:
        seq = row["seq"]
        if seq != expected_seq:
            result.problems.append(
                f"seq {seq}: expected seq {expected_seq} (events missing or out of order)"
            )
        if row["prev_hash"] != expected_prev:
            result.problems.append(f"seq {seq}: does not link to the previous event")
        if event_hash(row) != row["event_hash"]:
            result.problems.append(f"seq {seq}: content does not match its hash")
        hashes[seq] = row["event_hash"]
        expected_seq, expected_prev = seq + 1, row["event_hash"]
    result.events = len(rows)
    result.head_seq = rows[-1]["seq"] if rows else 0

    for anchor in anchors or []:
        result.anchors_checked += 1
        stored = hashes.get(anchor.seq)
        if stored is None:
            result.problems.append(
                f"anchor seq {anchor.seq} ({anchor.anchored_at}): event missing (log truncated?)"
            )
        elif stored != anchor.event_hash:
            result.problems.append(
                f"anchor seq {anchor.seq} ({anchor.anchored_at}): hash differs (history rewritten?)"
            )
    last_anchor = max((a.seq for a in anchors or []), default=0)
    result.unanchored_tail = max(result.head_seq - last_anchor, 0)
    return result
