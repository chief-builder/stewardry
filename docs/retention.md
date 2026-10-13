# Retention, access, and deletion

Status: recorded before the first connected session (spec section 4.5).
Periods marked **proposed** become agreed when the owner merges the pull
request that adds them. This describes what Stewardry does today. Where it
has no automatic deletion yet, this document says so.

## Where data lives

| Data | Where | Contents |
|---|---|---|
| Application database | the data directory (`stewardry serve`), or `build/shadow/<date>/db.sqlite` for a shadow run | tasks, questions, runs, drafts, verdicts, evidence snapshots, usage, audit events |
| Uploaded questionnaires | `artifacts/` next to the database, by SHA-256 | the uploaded files, unchanged |
| Model recordings | `build/shadow/<date>/model/` | redacted model requests and responses, for replay |
| Published reports | `docs/connected/`, `evals/reports/` (committed) | sanitized results only: no tokens, no account IDs, no email addresses |
| Credentials | process memory only (ADR 0006) | hub tokens; gone when Stewardry stops |
| Vendor tokens | the broker's custody, never in Stewardry | not held by Stewardry |

`build/` is git-ignored. Everything above is synthetic except the hub and
Atlassian account identifiers, which stay out of committed files.

## Retention periods (proposed)

| Data | Kept | Then |
|---|---|---|
| Audit events | for the life of the project | never deleted. The chain is append-only, and anchors (`stewardry audit anchor`) detect changes |
| Runs, drafts, verdicts, usage, evidence snapshots | for the life of the project | kept, so nightly evals and the weekly control report can reread them |
| Uploaded questionnaires | 90 days after the task's last activity | deleted by the operator (see below) |
| Model recordings | until the report that uses them is accepted, then 90 days | deleted by the operator |
| Published reports | for the life of the repository | not deleted; corrections are new commits |

## Authorized access

- **In the app:**
  - Sales sees only its own task's status.
  - GRC reviewers see drafts for their assigned customers.
  - A confidential passage is shown only to the reviewer whose own access
    read it.
  - Every view refusal is audited.
- **On disk:** the database, artifacts, and recordings are readable by the
  local OS user who runs Stewardry. That administrator can read or change
  anything. The audit chain detects changes; it cannot prevent them (spec
  4.5). Recovery is to restore from a copy and check it against the last
  anchor.

## Deletion behavior

- **No automatic deletion** exists in Milestone 1. The operator deletes
  expired uploads and recordings by hand, and records each deletion (what,
  when, why) in `docs/operations-log.md`, which is created with its first
  entry. A deletion tool is later scope.
- **Deleting an upload keeps its database rows.** The task, questions, and
  audit events remain. The file can no longer be read back.
- **Deleting the whole data directory** erases the audit log too. That is
  recorded in the operations log, and the last anchor is kept outside the
  directory.

## Superseded sources

- **Evidence snapshots are kept as recorded:** the page version, content
  hash, and passages a draft cited.
- **When a policy page changes,** the next read records a new snapshot. The
  old snapshot is marked invalidated, with the reason ("content changed" or
  "policy metadata changed"). A citation of invalidated evidence counts as
  insufficient support, and the review view flags it.
- **Nothing is rewritten or deleted,** so a past decision can still be checked
  against what it saw.
