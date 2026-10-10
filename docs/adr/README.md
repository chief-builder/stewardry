# Architecture decision records

Each ADR records one major choice, the reason for it, and the alternatives
that were rejected. All ADRs below are **Proposed** and wait for owner review.
An ADR becomes Accepted only when the owner approves it. A later ADR replaces
an earlier one instead of rewriting it.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-language-and-toolchain.md) | Python 3.14 with uv, ruff, mypy, pytest, and Make | Proposed |
| [0002](0002-repository-structure.md) | One repository with one `stewardry` package; fixtures, evals, and replay data kept outside it | Proposed |
| [0003](0003-agent-runtime.md) | Application-owned tool loop over the Anthropic Messages API; no agent framework | Proposed |
| [0004](0004-storage-and-audit.md) | SQLite through SQLAlchemy and Alembic; hash-chained, append-only audit log | Proposed |
| [0005](0005-web-ui-and-dashboard.md) | Server-rendered FastAPI app with Jinja2 and HTMX; dashboard in the same app | Proposed |
| [0006](0006-atlassian-access-through-gateway.md) | Stewardry is an OIDC client of the hub and an MCP client of the existing gateway | Proposed (G1 and G6 decided) |
| [0007](0007-run-provenance-labels.md) | Every run, report, and dashboard figure carries a provenance label | Proposed |
| [0008](0008-hub-roles-bootstrap.md) | An idempotent Stewardry bootstrap adds its client and persona users to the hub; roles live in Stewardry | Proposed (G2 direction approved) |
| [0009](0009-per-agent-policy.md) | Versioned per-agent policy files enforced by the executor; a Stewardry-only gateway instance as later hardening | Proposed |
| [0010](0010-gateway-error-classification.md) | Consent detected only from structured elicitation; other errors from a pinned template catalog, failing closed | Proposed |

Template: Context, Decision, Reason, Rejected alternatives, Consequences.
