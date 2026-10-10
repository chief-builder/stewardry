# Proposed upstream changes to vendor-token-broker

Status: **Draft, not submitted.** These would change
`chief-builder/vendor-token-broker`, which this project must not modify.
Nothing here is done unless the owner decides to take it to that repository
as separate work. Stewardry does not depend on any of it (ADRs 0009 and
0010 work at the pinned revision).

## P1. Machine-readable gateway errors (closes G4 upstream)

- **Change:** every gateway `ToolError` also sets `structuredContent` (or
  `_meta`) to `{"code": "<stable code>", "upstream": "<name>"}`. The codes
  would be `needs_consent`, `consent_declined`, `consent_timeout`,
  `unavailable`, `disconnect_pending`, `vendor_unusable`, `config_error`,
  `upstream_rejected`, and `unauthenticated`. The human text stays as it is.
- **Freeze:** add the codes to the wire-compatibility test, as is already
  done for the broker's problem titles.
- **Stewardry effect:** the template catalog in ADR 0010 becomes a fallback
  for older pins.

## P2. Per-client tool and argument policy (closes G3 upstream)

- **Change:** an optional `clients` section in the upstreams file, keyed by
  `azp`. It holds a per-client tool subset and simple argument constraints
  (for example a fixed `cloudId`). The gateway already logs `azp`, so it
  knows the client.
- **Stewardry effect:** the vendor-facing allow-list moves outside the
  application process. Per-agent policy stays in Stewardry, because the
  gateway cannot know agents.

## P3. Correlation ID pass-through (G3 audit joins)

- **Change:** accept an opaque correlation ID from the client (for example
  in an MCP `_meta` field on the tool call) and include it in `gateway.call`
  and `gateway.consent` log lines. The gateway would validate its length and
  characters, and it would never be used for authorization.
- **Stewardry effect:** gateway logs join to Stewardry runs by ID instead of
  by `sub` and time.
