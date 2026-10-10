# Architecture decision records

Each ADR records one major choice, the reason for it, and the alternatives
that were rejected. ADRs 0001–0010 were accepted on 2026-10-10, when the
owner reviewed and merged chief-builder/stewardry#1. An ADR becomes Accepted
only when the owner approves it. A later ADR replaces
an earlier one instead of rewriting it.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-language-and-toolchain.md) | Python 3.14 with uv, ruff, mypy, pytest, and Make | Accepted |
| [0002](0002-repository-structure.md) | One repository with one `stewardry` package; fixtures, evals, and replay data kept outside it | Accepted |
| [0003](0003-agent-runtime.md) | Application-owned tool loop over the Anthropic Messages API; no agent framework | Accepted (for now; a later move to the Vercel AI SDK is possible) |
| [0004](0004-storage-and-audit.md) | SQLite through SQLAlchemy and Alembic; hash-chained, append-only audit log | Accepted |
| [0005](0005-web-ui-and-dashboard.md) | Server-rendered FastAPI app with Jinja2 and HTMX; dashboard in the same app | Accepted |
| [0006](0006-atlassian-access-through-gateway.md) | Stewardry is an OIDC client of the hub and an MCP client of the existing gateway | Accepted |
| [0007](0007-run-provenance-labels.md) | Every run, report, and dashboard figure carries a provenance label | Accepted |
| [0008](0008-hub-roles-bootstrap.md) | An idempotent Stewardry bootstrap adds its client and persona users to the hub; roles live in Stewardry | Accepted |
| [0009](0009-per-agent-policy.md) | Versioned per-agent policy files enforced by the executor; a Stewardry-only gateway instance as later hardening | Accepted (option 2 decided at S14c) |
| [0010](0010-gateway-error-classification.md) | Consent detected only from structured elicitation; other errors from a pinned template catalog, failing closed | Accepted |

Template: Context, Decision, Reason, Rejected alternatives, Consequences.
