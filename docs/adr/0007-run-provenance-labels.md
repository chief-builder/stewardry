# ADR 0007: Run provenance labels

Status: Proposed · 2026-10-10

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

## Rejected alternatives

- **Labels only in documentation:** easy to drift and impossible to test.
- **Separate databases per mode:** it hides comparisons and still does not
  stop mislabeled exports.
