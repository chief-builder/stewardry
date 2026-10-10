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
| [0006](0006-atlassian-access-through-gateway.md) | Stewardry is an OIDC client of the hub and an MCP client of the existing gateway | Proposed, blocked on gap decisions |
| [0007](0007-run-provenance-labels.md) | Every run, report, and dashboard figure carries a provenance label | Proposed |

Template: Context, Decision, Reason, Rejected alternatives, Consequences.
