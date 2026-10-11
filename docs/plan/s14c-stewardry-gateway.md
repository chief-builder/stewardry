# S14c: Stewardry-only gateway instance (decision gate for Milestone 2)

Status: design check done in the stack tier, 2026-10-12. Decision: **owner's
call**. The recommendation is below.

## What was tried

ADR 0009's option 2: a second gateway, run from the broker's pinned image
(`vendor-token-broker@20d1426`), with all of its configuration in
Stewardry's repository. No broker code changed.

- **Upstreams.** `deploy/stack/stewardry-upstreams.stand-in.json` lists one
  upstream, Atlassian, with only the five reviewed tools: `getJiraIssue`,
  `searchConfluence`, `getConfluenceContent`, `atlassianUserInfo`, and
  `getAccessibleAtlassianResources`. Schemas come from the gateway's bundled
  `atlassian.json` snapshot.
- **Hub.** `stewardry hub bootstrap --with-stewardry-gateway` adds:
  - a `stewardry-gateway` client scope, whose audience is the second
    gateway's resource URL and its exchange client;
  - the `stewardry-gateway` confidential token-exchange client, with
    optional scope `hub-tier`, like the broker's own `mcp-gateway` client;
  - the scope as an optional scope of the `stewardry` client.

  As always, it only adds, and the broker's clients and scopes are
  byte-identical before and after.
- **Container.** The second gateway runs on port 8510, with
  `GATEWAY_SCOPE=stewardry-gateway` and its own client. It talks to the same
  broker and hub. It is an opt-in compose profile in
  `deploy/stack/compose.override.yml`.

## What the stack tests showed (`tests/stack/test_stewardry_gateway_stack.py`)

All passed locally against the broker's stand-in stack. The same tests run
in CI.

| Check | Result |
|---|---|
| Tools listed by the Stewardry gateway | exactly the five reviewed tools, plus the gateway's built-in `connect_atlassian` and `disconnect_atlassian`. The shared gateway lists `executeRead`, `discover`, and Jira search for comparison |
| Tool schemas | identical to the bundled snapshot |
| Token exchange for the new audience | works. A persona connected through the Stewardry gateway, and `atlassianUserInfo` succeeded |
| `executeRead`, `discover`, Jira search through the Stewardry gateway | refused by the gateway. None reached the upstream (mock-mcp call log) |
| A token for one gateway used on the other | rejected both ways (`needs_signin`) |

## What it buys

Today, nothing but Stewardry's own adapter stops a compromised or buggy
Stewardry from calling `executeRead` or `discover` through the shared gateway
(ADR 0009, THREAT_MODEL). With this instance, those tools do not exist on the
path Stewardry uses, and Stewardry's tokens are not accepted by the shared
gateway. The allowlist is enforced by a process Stewardry does not run.

## What it costs and does not cover

- **Costs:** one more container, one more hub client and scope, one more
  client secret to keep, and the upstreams file to keep in step with the
  pinned snapshot when the broker pin moves.
- **Still built in:** `connect_atlassian` and `disconnect_atlassian` are
  part of every gateway. Stewardry does not call `disconnect`. The
  allowlist does not remove it.
- **Per application, not per agent.** Narrowing per agent stays in
  Stewardry (ADR 0009).
- **Only the stand-in has been tested.** For the real stack, the upstreams
  file needs the real Atlassian URL and vendor `atlassian`, and Stewardry's
  sign-in would request `stewardry-gateway` instead of `mcp-gateway`. Not
  yet verified:
  - whether a connection made through one gateway serves the other (they
    share the broker's custody keyed by user and vendor);
  - the real Atlassian server's behavior;
  - how it combines with the runbook's reconnect steps.
- **Not used in Milestone 1.** No connected use, as planned.

## Recommendation

**Go for Milestone 2**, as the default path for Stewardry's Atlassian access.
Before switching, do these:

1. Verify it once against the real stack after S15. That covers:
   - sign-in with `stewardry-gateway`;
   - a connected read;
   - whether a connection made through the shared gateway is reused.
2. Add the real upstreams file next to the stand-in one.
3. Update ADR 0009 (option 2 accepted) and THREAT_MODEL.

If the owner prefers fewer moving parts, **no-go** is defensible. Then option
1 (adapter-only enforcement) stays, and THREAT_MODEL keeps stating that a
compromised Stewardry could call `executeRead`.
