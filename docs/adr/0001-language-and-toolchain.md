# ADR 0001: Python 3.14 and toolchain

Status: Accepted · 2026-10-10 (owner review of chief-builder/stewardry#1)

## Context

Stewardry needs an Anthropic SDK, an MCP client for the existing gateway, an
OIDC client, `.xlsx` and `.docx` processing that keeps formatting, and a
small web app. vendor-token-broker, the dependency it is built around, is a
Python 3.14 project that uses uv-generated hash-pinned locks, ruff, mypy, and
pytest behind `make check`.

## Decision

- **Language:** Python 3.14 (latest 3.14 patch at lock time; 3.14.8 as of
  this ADR).
- **Environment and lock:** uv with a single `uv.lock`. Installs use the
  lock. Resolution uses a 14-day cooldown (`exclude-newer`) so no dependency
  is newer than two weeks unless an ADR amendment records a security
  exception.
- **Quality gate:** ruff (lint and format), mypy in strict mode for
  `src/`, pytest with coverage, and a Makefile whose `make check` runs every
  offline check.
- **CI extras** (mirroring the broker's CI): pip-audit over the lock, lychee
  link checking, CodeQL, Dependabot for `uv` and GitHub Actions, actionlint
  and zizmor for workflow hygiene.

**Reason:** one language across Stewardry and the broker, with the
first-party Anthropic and MCP SDKs and mature Office-format libraries.

## Candidate libraries (checked on PyPI, 2026-10-10)

Exact pins are written to `uv.lock` in Milestone 1 step S0, under the
cooldown rule. Several latest releases are too new today and will resolve to
an earlier version.

| Purpose | Package | Latest on PyPI (upload date) | Passes 14-day cooldown today |
|---|---|---|---|
| Model access | `anthropic` | 1.13.0 (2026-10-09) | No; resolve to the newest release on or before 2026-09-26 |
| MCP client to the gateway | `mcp` | 2.3.0 (2026-10-02) | No; same rule |
| Web app | `fastapi` | 0.143.0 (2026-10-08) | No; same rule |
| ASGI server | `uvicorn` | 0.54.0 (2026-09-25) | Yes |
| Storage | `sqlalchemy` | 2.1.4 (2026-10-07) | No; same rule |
| Migrations | `alembic` | 1.20.0 (2026-09-11) | Yes |
| Templates | `jinja2` | 3.1.6 (2025-03-05) | Yes |
| Validation and settings | `pydantic`, `pydantic-settings` | 2.14.0 (2026-10-08), 2.15.0 (2026-08-07) | No / Yes |
| OIDC client | `authlib` | 1.8.0 (2026-08-30) | Yes |
| `.xlsx` | `openpyxl` | 3.1.5 (2024-06-28) | Yes |
| `.docx` | `python-docx` | 1.2.0 (2025-06-16) | Yes |
| Tests | `pytest` | 9.1.1 (2026-06-19) | Yes |
| Lint and format | `ruff` | 0.17.0 (2026-10-09) | No; same rule |
| Types | `mypy` | 2.4.0 (2026-10-01) | No; same rule |
| Environment | `uv` | 0.13.0 (2026-10-09) | Tool, not locked; use the newest release that passes the cooldown |

Python 3.15.0 was released on 2026-10-09. It is rejected for now because it is
one day old and the broker targets 3.14.

## Rejected alternatives

- **TypeScript or Node:** it would add a second language next to the
  Python broker, and its `.docx` round-trip libraries are weaker than
  `python-docx` and `openpyxl`.
- **Go:** it has no first-party MCP SDK to match the gateway's tested
  clients, and Office-format support is weak.
- **Poetry or pip-tools:** a second lock tool for no gain. The broker
  already uses uv.
- **Python 3.15:** too new (see above).

## Consequences

- Office-format fidelity is limited to what openpyxl and python-docx
  preserve. This is why iteration 1 supports only a published template set
  (spec section 5.2). Round-trip tests decide what "preserved" means.
- openpyxl does not evaluate formulas. Preservation means the formula text is
  kept, not that values are recalculated.
