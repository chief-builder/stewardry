# ADR 0010: Classifying gateway errors without guessing

Status: Proposed · 2026-10-10

## Context

Gap G4 is that gateway errors over MCP are human-readable text. Reading the
pinned gateway source (`src/mcp_gateway/server.py` at `20d1426`) confirms
two things:

- **Every gateway-side failure is a FastMCP `ToolError`.** It reaches the
  client as a `CallToolResult` with `isError: true` and one text item.
- **An error from Atlassian's own tool** is forwarded as-is, also with
  `isError: true`. So the flag alone cannot tell "the gateway failed" from
  "Atlassian answered with an error".

Consent is the exception. When the client advertises URL elicitation, the
gateway sends a **structured** request:

- on 2026-07-28, an `InputRequiredResult` carrying an `ElicitRequest` with
  mode `url`;
- on 2025-11-25, an `elicitation/create` request with mode `url`.

The spec requirement is that an outage must never look like missing consent.

## Options considered

| # | Approach | Broker change? | Verdict |
|---|---|---|---|
| A | Match gateway messages by text | No | Brittle on its own |
| B | **Detect consent only from the structured elicitation**; never infer it from text | No | **Adopt** |
| C | **A pinned catalog of the gateway's message templates**, generated from the pinned source and checked by contract tests; anything unmatched fails closed | No | **Adopt**, together with B |
| D | Probe `/healthz` on the broker and hub discovery after a failure, to diagnose | No | Optional operator diagnostics only. It never decides consent |
| E | The gateway adds a stable machine-readable `code` to every error result | **Yes** | Upstream proposal ([broker-upstream-proposals.md](../proposals/broker-upstream-proposals.md)). When adopted, it replaces C |

## Decision (B + C, failing closed)

### 1. Consent only from the structured elicitation

Stewardry's MCP client **always advertises `elicitation.url`**.

- **Interactive "Connect Atlassian" action by the signed-in user:** the
  elicitation URL is accepted only if it is under the configured broker
  authorize path (`<broker>/v1/authorize/atlassian?txn=`). It is shown only
  to that user. Stewardry answers `accept` and lets the gateway's bounded
  wait run.
- **Background run:** an elicitation means `needs_consent`. Stewardry
  records it, answers `cancel` (2025-11-25) or does not re-call (2026-07-28),
  and pauses the run. It never opens or stores the link. The user later
  starts the interactive connect.

Text that merely mentions connecting (for example "Connect Atlassian first:
open … ") is **never** treated as consent. With elicitation advertised, the
gateway does not send that text. If it appears anyway, it is classified as
`unexpected`.

### 2. A pinned template catalog for everything else

`platform/atlassian/gateway_errors.py` holds the gateway's message templates
from the pinned revision, each mapped to a class:

| Gateway message (template at `20d1426`) | Stewardry class | Run behavior |
|---|---|---|
| `a dependency is unavailable (…); retry shortly` | `unavailable` | Bounded retry, then pause as unavailable |
| `{name}'s MCP server is unavailable; retry shortly` | `unavailable` | Same |
| `{name} connection is not usable (…); retry` | `unavailable` | Same |
| `{name} is not usable right now (revoke-pending); try again later` | `disconnect_pending` | Pause, no link |
| `{name} is not usable right now (<other title>); try again later` | `vendor_unusable` | Pause and alert the platform engineer |
| `the gateway could not establish your identity with the token broker; …` | `config_error` | Stop and alert the platform engineer |
| `{name} rejected the connection; reconnect {name} and retry` | `needs_reconnect` | Pause and prompt that user to reconnect interactively |
| `{name} was not connected; the request was cancelled` | `consent_declined` | Expected after a background `cancel`, so it confirms `needs_consent` |
| `{name} is not connected yet; finish connecting in the browser, then retry` | `consent_timeout` | Pause |
| `not authenticated`, or HTTP 401 with `WWW-Authenticate` | `needs_signin` | Pause until that user signs in to Stewardry again |
| Any other `isError` result | `upstream_tool_error` | Classified per operation after G8 is verified (denied or not found). Never consent, never success |
| No match, malformed result, or transport error | `unexpected` → treated as `unavailable` | Fail closed and keep the raw text, redacted, in the audit |

### 3. Contract tests keep the catalog honest

- **Offline:** a test parses the pinned `server.py` from the broker checkout
  at the pinned SHA. It fails if any `ToolError(...)` template there is
  missing from the catalog, or if the catalog contains a template that is no
  longer there. Moving the broker pin therefore forces a catalog review.
- **Stack:** each class is triggered for real against the stand-in stack.
  The tests cover broker stopped, hub stopped, a declined or cancelled
  elicitation, a disconnect, and a revoke-pending state if the stand-ins can
  produce one.
- **Spoofing:** an upstream tool error whose text copies a gateway template
  could be misread. For consent this is harmless, because consent is never
  derived from text. For the other classes, the worst case is a pause, never
  an approval or a fallback identity.

**Reason:** consent detection uses a structured protocol signal, and
everything ambiguous fails toward "unavailable". The spec's rule holds
without changing the broker.

## Consequences

- Bumping the broker pin needs a catalog review. The offline test enforces
  it.
- If option E lands upstream, the catalog shrinks to a compatibility shim
  for older revisions.
