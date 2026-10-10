# ADR 0002: Repository structure

Status: Accepted · 2026-10-10 (owner review of chief-builder/stewardry#1)

## Decision

One repository. It holds one installable package, `stewardry`, in a `src/`
layout. The platform and the two engagements are subpackages. Agent-visible
synthetic data, evaluation answers, and replay recordings live outside the
package, in separate top-level folders.

```
stewardry/
├── src/stewardry/
│   ├── platform/
│   │   ├── registry/      agent records, versioned effective policy, lifecycle
│   │   ├── identity/      hub sign-in, sessions, role mapping, task grants
│   │   ├── authz/         controlled tool executor and access intersection
│   │   ├── audit/         append-only events, hash chain, redaction
│   │   ├── budget/        usage, cost, and limits
│   │   ├── model/         Anthropic access boundary, recording and replay
│   │   ├── atlassian/     narrow read adapter (MCP client to the gateway)
│   │   ├── evidence/      page snapshots, hashes, passages, staleness
│   │   └── jobs/          DB-backed jobs with leases and idempotency keys
│   ├── engagements/
│   │   ├── questionnaire/ parsing, drafting, review, export
│   │   └── nda/           shadow triage (Milestone 3)
│   ├── web/               FastAPI routes, templates, role-restricted views
│   └── cli.py             platform-engineer and evaluation commands
├── fixtures/              synthetic, agent-visible: policies, questionnaires, NDAs, Jira requests
├── evals/                 datasets with expected answers, rubrics, reports (never agent-visible)
├── replay/                recorded model and service responses for offline replay
├── tests/                 unit/, contract/, stack/, live/, connected/
├── deploy/connected/      local connected-stack configuration (see ADR 0006)
└── docs/                  spec, ADRs, plan, runbook, threat model
```

**Reason:** the spec says the platform should be only as large as the two
engagements need. One package keeps refactoring cheap. Folder boundaries keep
the data separations that the spec makes mandatory.

## Enforced boundaries

- `evals/` is not importable or readable from runtime code. A test fails if
  the runtime package or the agent's tool surface can reach it (spec section
  7.1: keep evaluation answers separate from agent-visible data).
- `engagements/*` may import `platform/*`, but not each other. NDA reuse is
  then a measurable fact, not an assumption.
- Replay data is used only when the run mode is `replay`. A replay run blocks
  outbound network calls (ADR 0007).

## Rejected alternatives

- **Several packages in a monorepo** (platform, each engagement): this adds
  packaging and versioning overhead before there is a second consumer.
- **A separate repository per engagement:** this breaks the requirement that
  one repository shows the whole cycle (spec section 1).
- **Fixtures inside the package:** this makes it too easy for runtime code
  to read expected answers.
