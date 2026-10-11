# Stewardry

Stewardry is a portfolio project about how a company puts Claude to work
inside business functions. It covers user-scoped permissions, controls,
evaluations, and measured outcomes. A thin internal agent platform supports
two synthetic business engagements:

- security questionnaire response (GRC, also helping Sales);
- NDA triage (Legal, shadow mode only).

All business data is synthetic. One person operates every role, and results
are reported as a controlled synthetic exercise. Connected runs, live-model
evals, offline replays, and scripted examples are always labeled separately.

## Status

Milestone 1 is in progress. Done so far:

- **S0:** the repository skeleton and quality gate.
- **S1:** storage: the SQLite schema, migrations, and a content-addressed
  artifact store.
- **S2:** an append-only, hash-chained audit log with secret redaction,
  through `stewardry audit verify` and `stewardry audit anchor`.
- **S3:** the agent registry and lifecycle. Per-agent policy files live in
  `policies/agents/`, checked against `policies/platform-baseline.yaml`, and
  are managed with `stewardry registry`.
- **S4a:** the access mapping, applied with `stewardry roles apply` (users,
  roles, and customer assignments; bootstrap and explicit remap), plus task
  submission, drafting grants, and identity resolution with no fallback
  identity.
- **S4b:** the web app (`stewardry serve`): hub sign-in with PKCE, in-memory
  sessions and credentials, CSRF protection, and the first task pages (Sales
  submits a request; GRC authorizes or revokes drafting under their own
  identity).
- **S5:** the controlled executor. Every model tool call and every Atlassian
  operation is checked against the platform baseline, the run's bound and
  current policy, the acting user's grant, role, customer, and credential,
  and the run's budget. Every decision is audited.
- **S6:** the model access boundary. Claude calls go through the same
  authority checks, with budgets, bounded retries, usage and cost under a
  dated pricing reference, and recording and offline replay.
- **S7:** the narrow Atlassian read adapter, with error classification that
  never takes text for consent (ADR 0010), contract-tested offline against
  unmodified pinned copies of the broker's gateway source and tool snapshot
  in `third_party/`.
- **S8:** synthetic data and eval separation. There are 21 policy pages for a
  fictional vendor, three 30-question questionnaires (`.xlsx` and `.docx`)
  with designed conflict, stale, ambiguous, and adversarial cases, and Jira
  requests in `fixtures/`. The expected outcomes and rubrics are in `evals/`,
  which runtime code can never reach.
- **S9:** questionnaire ingest. Uploads in the two supported templates are
  parsed into questions with stable cell locations; anything else is
  rejected with a reason. Hidden content, comments, tracked changes, and
  instruction-like text are recorded as screening findings for reviewers.
- **S10:** evidence snapshots. Each policy page read is recorded per run and
  user with its version, content hash, metadata, and numbered passages. A
  hash change invalidates earlier evidence, and the strength rubric labels
  stale, superseded, conflicting, and ineligible evidence as not approvable.
- **S11:** shadow drafting. For each question, Claude lists and reads
  policies through the controlled executor and either submits a cited answer
  or routes the item to an expert. Deterministic checks decide what reaches
  review. Drafting resumes after a restart without drafting anything twice.
  Nothing is exported or shown to Sales. `make record-drafting` records a
  small live-model scenario that the offline tests then replay.
- **S12:** role-restricted pages. Sales uploads the questionnaire and sees
  plain status only. GRC reviews drafts with citations, passage text, and
  screening findings, and records shadow verdicts (not approvals). Platform
  engineers, owners, and auditors see the agent registry.
- **S13:** baseline evals. `make check` replays and scores the recorded
  smoke scenario. `make eval-live` drafts all three questionnaires against
  the live model and writes a labeled report with every version that
  produced it (see [evals/README.md](evals/README.md)). The first live run
  (2026-10-11) found a conflict-handling gap, which is now fixed and checked
  by replaying its recordings. It is not graded yet and is not an accepted
  baseline.
- **S14:** `stewardry hub bootstrap` adds Stewardry's client and the legal,
  platform, auditor, owner, and sme personas to the hub realm. It only adds,
  never edits, and writes a proposed access mapping. `make test-stack` checks
  it against a throwaway Keycloak from the broker's pinned image (needs
  Docker).
- **S14b:** the real MCP client to the gateway, tested against the broker's
  stand-in stack from the pinned checkout. The tests cover consent, outages,
  disconnects, identity separation, and token hygiene. `make test-stack` and
  CI both run it. Locally it refuses to start while any `vtb-*` container
  exists.

There are no approvals and no export yet. See the
[Milestone 1 plan](docs/plan/milestone-1.md).

## Quickstart

You need Git, GNU Make, and [uv](https://docs.astral.sh/uv/). uv installs
Python 3.14 if you do not have it.

```bash
make check
```

`make check` runs everything offline: lint, format check, type checks,
tests with coverage (network blocked), Markdown link and anchor checks, a
secret-pattern scan, and workflow linting. `make help` lists the other
targets: `build`, `audit` (needs network), and `lock`.

The live-model smoke test makes one small, real API call. It needs your
Anthropic credentials (`ANTHROPIC_API_KEY` or an `ant auth login` profile)
and costs a fraction of a cent. It never runs in `make check` or CI:

```bash
make test-live-model
```

To create or migrate the local database, run `uv run stewardry db upgrade`.
The data directory defaults to `~/.local/share/stewardry`; set
`STEWARDRY_DATA_DIR` to choose another. It must be outside any git work
tree, and it is created owner-only.

## Adding Stewardry to the hub

`stewardry hub bootstrap` adds the `stewardry` client and the persona users
to the local hub realm (ADR 0008). Run it only against the local stack, and
for the real dev stack only after [setup verification](docs/setup-verification.md).
It needs these in your shell; their values are never printed:

| Variable | Meaning |
|---|---|
| `STEWARDRY_HUB_ISSUER` | The local hub realm, for example `http://localhost:8180/realms/mcp` |
| `KEYCLOAK_ADMIN_USER`, `KEYCLOAK_ADMIN_PASSWORD` | The hub's admin credential |

```bash
uv run stewardry hub bootstrap
```

It only adds, never edits, and running it again changes nothing. Generated
passwords and the client secret go to `deploy/hub/.secrets.env` (git-ignored,
0600); only the entry names are printed. It also writes
`deploy/hub/proposed-access-mapping.yaml` with the real subjects. Review that
file, then apply it with `stewardry roles apply`.

## Running the web app

The web app signs people in through the existing identity hub (see
[setup verification](docs/setup-verification.md)). Set these in your shell.
Never commit them:

| Variable | Meaning |
|---|---|
| `STEWARDRY_HUB_ISSUER` | The hub realm, for example `http://localhost:8180/realms/mcp` |
| `STEWARDRY_HUB_CLIENT_SECRET` | The `stewardry` client's secret, from `deploy/hub/.secrets.env` |
| `STEWARDRY_HUB_CLIENT_ID` | Optional; default `stewardry` |
| `STEWARDRY_PUBLIC_URL` | Optional; default `http://localhost:8700` |

```bash
uv run stewardry serve
```

It listens on `127.0.0.1:8700`. Sessions and gateway credentials live only
in memory, so a restart signs everyone out and paused work waits until the
authorizing user signs in again.

## Documentation

- [Project spec](docs/stewardry-spec.md)
- [Architecture decision records](docs/adr/README.md)
- [vendor-token-broker contracts and gaps](docs/broker-contracts.md)
- [Setup verification checklist](docs/setup-verification.md)
- [Milestone 1 plan](docs/plan/milestone-1.md),
  [earliest demo cut](docs/plan/early-demo-cut.md), and
  [open questions](docs/plan/open-questions.md)
- [Security policy](SECURITY.md) and [threat model](THREAT_MODEL.md)

## Dependencies

Stewardry depends on
[vendor-token-broker](https://github.com/chief-builder/vendor-token-broker)
at commit `20d1426622a0c05478c4331b82ad8952411f9a4a` for user-scoped
Atlassian access. Python dependencies are locked with hashes in `uv.lock`,
under a 14-day release cooldown (see ADR 0001).

## License

[MIT](LICENSE).
