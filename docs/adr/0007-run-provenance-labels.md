# ADR 0007: Run provenance labels

Status: Accepted · 2026-10-10 (owner review of chief-builder/stewardry#1)

## Decision

Every run record has exactly one `provenance` value, set by the runtime from
its actual configuration and never from user input:

| Label | Means | Model | Atlassian |
|---|---|---|---|
| `connected` | A real session with real hub users and connected Atlassian test accounts | Live Anthropic API | Live, through the gateway |
| `live-model-eval` | An eval run against fixtures | Live Anthropic API | Fixture adapter |
| `offline-replay` | A replay of recorded responses; outbound network blocked | Recorded | Recorded |
| `scripted-example` | A hand-written or simulated example for docs or UI | None or simulated | None or simulated |

- Reports, dashboard figures, and exported evidence show the label, group
  results by it, and never sum across labels.
- Replay mode installs a network guard that fails on any outbound socket.
  A test proves that replay needs no credentials.
- Connected runs record the broker revision, gateway upstream mode (real or
  stand-in), model ID, prompt version, dataset version, and control
  configuration hash.
- Monitoring and hypercare claims count only `connected` sessions with real
  dates.

**Reason:** spec section 1 says these kinds of evidence must be labeled
separately. A label set by the runtime is harder to get wrong than a
convention in documents.

## Implementation note (S11, 2026-10-11)

- **Model transports have a kind:** `live`, `replay`, or `scripted`. The
  model boundary refuses a transport that does not match the run's label.
  `scripted` (test doubles and docs examples) serves only
  `scripted-example` runs. The offline drafting tests use it, and they are
  not evidence.
- **The drafting replay** (`replay/drafting-smoke/`) is recorded by
  `make record-drafting` as a `live-model-eval` run against the fixture
  adapter. Its replay therefore uses the same fixture adapter for Atlassian,
  not recorded Atlassian responses. Recorded Atlassian responses arrive with
  connected runs.

## Rejected alternatives

- **Labels only in documentation:** easy to drift and impossible to test.
- **Separate databases per mode:** it hides comparisons and still does not
  stop mislabeled exports.
