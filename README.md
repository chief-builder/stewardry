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

Milestone 1 is in progress. Step S0 is the repository skeleton and quality
gate. There is no application behavior yet. See the
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
