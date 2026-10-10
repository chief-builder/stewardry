# ADR 0004: Storage and audit integrity

Status: Proposed · 2026-10-10

## Decision

- **Database:** SQLite in WAL mode. Access goes through SQLAlchemy 2.x Core
  (no ORM session magic) with Alembic migrations. It holds the registry,
  tasks, runs, task grants, evidence snapshots, drafts, review actions,
  usage, jobs, and audit events.
- **Artifacts** (uploaded questionnaires, working drafts, approved files):
  stored on the local filesystem under a data directory outside the
  repository. They are content-addressed by SHA-256, with metadata and access
  rules in the database.
- **Audit log:**
  - One `audit_events` table. SQLite triggers reject `UPDATE` and `DELETE`.
  - Each event stores the SHA-256 of the previous event and of its own
    canonical JSON, which forms a hash chain.
  - A checkpoint command writes the chain head (sequence number and hash) to
    a separate anchor file. Each connected session and each weekly report
    records the head.
  - `stewardry audit verify` detects edited, deleted, reordered, and inserted
    events, and a truncated tail when compared with the last anchor.
  - Events pass through a redaction filter before they are written. Secret
    probes (JWT shapes, bearer headers, configured secret values) fail the
    write in tests.
- **Idempotency:** review actions, artifact returns, and job steps carry
  idempotency keys with unique constraints. A duplicate becomes a no-op that
  is still audited.

**Reason:** a single file runs from a clean clone with no services. That keeps
`make check` and offline replay free of Docker, and it is enough for one
operator.

## Trust boundary (stated honestly)

A local administrator controls the database file, the anchor file, and the
code. The chain detects the tested kinds of modification. It cannot prevent
an administrator from rewriting both the chain and the anchors. This claim is
written into THREAT_MODEL.md and the runbook, with the recovery procedure:
restore from a copy, then re-verify against the last anchor recorded
elsewhere.

## Rejected alternatives

- **PostgreSQL:** database roles could make the audit table append-only for
  the app user. But it adds Docker to the offline path, and a local
  administrator could still bypass it. Revisit this if concurrency or
  role-enforced storage becomes a requirement.
- **An external log service or object-lock storage:** stronger immutability,
  but it adds a cloud dependency the spec does not call for (local operation
  only).
- **An ORM with lazy loading:** it hides queries, and it makes
  per-user and per-customer filtering easy to forget.

## Consequences

- Only one writer process is allowed (consistent with ADR 0003).
- Retention and deletion rules must be written before connected sessions
  (spec section 4.5). Because the audit table is append-only, deletion means
  a documented export, then archive and re-anchor. It is never edited in
  place.
