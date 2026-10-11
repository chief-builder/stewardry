# ADR 0006: Atlassian access through the existing gateway

Status: Accepted · 2026-10-10 (owner review of chief-builder/stewardry#1)

## Decision

1. **Stewardry is an OIDC client of the existing hub** (Keycloak realm
   `mcp`). It is a confidential client with the authorization-code flow and
   PKCE. It requests `openid` and `mcp-gateway`. The ID token sets up the
   Stewardry session. The access token, whose audience is the gateway's
   `/mcp` URL, is the user's gateway-scoped credential.
2. **Stewardry is an MCP client of the existing gateway.** The Atlassian
   adapter (`platform/atlassian`) calls `atlassian_*` tools with the
   *authorizing* user's gateway access token. The gateway then:
   - exchanges that token at the hub (RFC 8693), keeping the user's subject;
   - resolves the user's Atlassian token at the broker;
   - calls Atlassian's MCP server in a fresh session.
3. **Stewardry never calls the broker directly and never holds vendor
   tokens.** It has no token-exchange permission, so it cannot obtain a hub
   JWT for the broker.
4. **The adapter exposes narrow operations only**, each with argument
   allow-lists and result filtering (mapping in
   [broker-contracts.md](../broker-contracts.md#which-read-tools-back-each-operation)).
   `executeRead` and `discover` are never called.
5. **User credentials are held in memory only**, in the one Stewardry
   process (ADR 0003). They are keyed by Stewardry session and never written
   to disk, logs, the model, or the browser. A task grant records which
   user's credential a run may use. When that credential is missing or
   expired, the run pauses with "awaiting authorization". It never falls back
   to another identity. The owner confirmed this answer to gap G6 on
   2026-10-10.

### Task grants, lifetime, and revocation (implemented in S4a)

Spec section 4.3.2 asks for these to be documented:

- **Who authorizes.** A user whose role is listed for the stage in the
  agent's policy (GRC for drafting) authorizes work under their own hub
  subject. They must be assigned to the task's customer, and they cannot be
  the task's requester. Sales submission authorizes only intake reads under
  the requester's own identity.
- **What a grant binds.** A grant ties the task, the stage, the authorizing
  subject, the agent's policy version at the time of authorization, a
  granted time, and an expiry. A run's acting identity comes only from this
  grant (`grants.acquire_execution`), never from task fields or caller
  arguments.
- **Lifetime.** 8 hours by default, at most 24 hours. Grants do not renew
  themselves. When a grant expires, the task pauses until someone authorizes
  it again.
- **Credential renewal.** This is separate from the grant. The authorizing
  user's gateway credential is held in memory only, and refreshing it within
  the hub session is S4b work. A restart, a sign-out, or an expired
  credential pauses the task.
- **Cancellation and revocation.** The authorizing user or a platform
  engineer can revoke a grant, with a reason. The task pauses. Re-authorizing
  creates a new grant.
- **No fallback.** If the grant is missing, expired, or revoked; the
  authorizer's credential is unavailable; the agent is paused; or the
  authorizer has lost the customer assignment, the task pauses and the
  refusal is audited. Another user's credential is never used.

**Reason:** this is the documented gateway contract, unchanged. Stewardry
adds application authorization in front of it and never moves vendor tokens
across the trust boundary.

## Rejected alternatives

- **Calling the broker's `/v1/tokens/resolve` from Stewardry:** vendor tokens
  would enter Stewardry. The broker docs say to keep them inside the trusted
  gateway.
- **Giving Stewardry token-exchange permission at the hub:** this
  reconfigures the identity setup and gives Stewardry the gateway's
  privilege.
- **A Stewardry service account at Atlassian:** the spec forbids shared
  service accounts.
- **Storing hub refresh tokens with `offline_access`:** Stewardry would
  become a long-term credential custodian. The owner rejected it for G6.

## Consequences

- Connected runs need the hub, gateway, broker, and custody running, plus
  the user's active Stewardry sign-in during drafting.
- Error mapping follows ADR 0010, the client and role users follow
  ADR 0008, and per-agent limits follow ADR 0009.
- With in-memory custody (G1), recreating the stack disconnects every
  Atlassian account. Users reconnect through consent. That is never a
  re-registration, and each occurrence is recorded as an operational event.
