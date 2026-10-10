# Stewardry

Stewardry is a public portfolio project. It shows how a company puts Claude to
work inside business functions with user-scoped permissions, controls,
evaluations, and measured outcomes. A thin internal agent platform supports two
synthetic engagements: security questionnaire response (GRC, also helping
Sales) and NDA triage (Legal, shadow only).

The requirements live in [docs/stewardry-spec.md](docs/stewardry-spec.md).
Read it before changing behavior. The settled requirements in the final
paragraph of section 14 are fixed. Do not reopen them without the owner.

## Current phase

Phase 1 is planning only. Do not write application code until the owner has
reviewed the plan in [docs/plan/](docs/plan/) and the ADRs in
[docs/adr/](docs/adr/).

## Honesty rules

Accuracy and honest labeling matter more than features.

- Never invent results, measurements, sessions, or monitoring evidence.
- Keep four kinds of evidence separately labeled everywhere they appear
  (reports, dashboards, docs, recordings): **connected run**, **live-model
  eval**, **offline replay**, and **scripted example**. A replay shows that
  the workflow can be reproduced and that the application behaves as recorded.
  It does not prove live vendor access or current model quality.
- One person operates every role. Report the actual participant count of one
  next to any count of test identities. Do not claim team adoption or deal
  impact.
- Sparse data is labeled "insufficient data", never "healthy".
- Candidate targets (for example 95% citation faithfulness, 50% reviewer-time
  reduction) are not results until measured.
- Do not inherit a security or compliance claim from vendor-token-broker or
  any other reused project.

## Rules for the whole project

- **Synthetic data only.** Do not reference any current or past employer.
  Do not use Anthropic branding in the visual identity. Naming the model and
  the API provider as technical attribution is fine.
- **Secrets.** Never read, print, copy, or commit secrets, tokens, or client
  secrets. That includes `.env` files, `tests/stack/.env` in the broker
  checkout, custody contents, and token responses. When setup needs a secret,
  tell the owner what to configure and where. Check that a variable is set
  without printing its value.
- **Atlassian.** Do not re-register or reconfigure the existing Atlassian
  client registration. Never run `tools/register-mcp-client.py`, with or
  without `--force`. Do not widen vendor scopes. Do not call any Atlassian
  write operation (`executeWrite`, `executeDestructive`, or anything that
  changes Jira or Confluence). Re-registering can disconnect existing
  accounts.
- **vendor-token-broker** is a dependency pinned at
  `20d1426622a0c05478c4331b82ad8952411f9a4a` and cloned at
  `../vendor-token-broker`. Do not modify that repository.
- **Git.** Work on branches and open pull requests. Never push to `main`.
  Never force-push. Make small commits with conventional messages (`feat:`,
  `fix:`, `docs:`, `test:`, `chore:`). Keep the first line under 72
  characters. Do not add AI attribution lines or "Generated with" footers.
- **Versions.** Check today's date and current official sources (PyPI, the
  vendor's own docs) before choosing a library version. New pins must be at
  least 14 days old unless a security fix requires otherwise.
- **Names.** The package and distribution name is `stewardry`. Plain
  "steward" is taken on PyPI and npm.
- **Scope.** Build only what the spec and the reviewed plan require. Prefer
  boring, readable code. No speculative abstractions or extra providers.

## Engineering standards (spec section 11)

- One documented command from a clean clone (`make check`) runs lint,
  formatting, type checks, tests, the deterministic eval subset, and
  documentation checks. Prerequisites are declared. Offline checks are
  separate from connected checks.
- CI runs those checks plus build verification, dependency vulnerability
  scanning, and link and anchor checking. Connected and live-model tests need
  explicit credential setup and are reported separately.
- Pin GitHub Actions by commit SHA. Grant least privilege per job. Untrusted
  pull-request code must never reach service or model credentials. Use
  dependency locks, and pin container images by digest.
- Maintain SECURITY.md, THREAT_MODEL.md, dependency-update configuration,
  CodeQL for supported languages, and ADRs for major choices.
- Include contract tests for the broker integration, authorization-denial
  paths, token redaction, approval invalidation, source changes, duplicate
  actions, restart behavior, and supported export preservation.
- Document known limitations and test boundaries.

## Test tiers

Tests are grouped by what they need. Every report names its tier.

| Tier | Needs | Runs in `make check` and PR CI |
|---|---|---|
| offline | Nothing; network is blocked in tests | Yes |
| stack | Docker; the broker's stand-in stack from the pinned checkout, no real credentials | CI job without secrets; local opt-in |
| live-model | An Anthropic API key | No; manual, labeled live-model |
| connected | Real hub users, connected Atlassian test accounts, and an API key | No; manual, labeled connected |

## Security-critical invariants

- The model never sees credentials. Vendor tokens stay inside the broker and
  gateway boundary. Stewardry never calls the broker's `/v1/tokens/resolve`.
- Every tool execution rechecks the active user, agent policy, customer
  scope, source allow-list, task grant, and budget. Model-generated
  arguments, document content, and task fields cannot change the acting
  identity or the agent identity.
- `atlassian_executeRead` and `atlassian_discover` are never reachable from
  Stewardry.
- The model cannot approve, transfer authority, grant scopes, or change
  rollout status.
- Outages are never reported as missing consent.
- Audit events never contain tokens or secrets.
