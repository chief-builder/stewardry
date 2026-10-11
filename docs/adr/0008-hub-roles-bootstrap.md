# ADR 0008: Adding Stewardry's client and role users to the hub

Status: Accepted · 2026-10-10 (owner review of chief-builder/stewardry#1)

## Context

The hub at the pinned broker revision is the broker's test Keycloak. It runs
realm `mcp` in dev mode, re-imported from a file in the broker repository,
with users `alice` and `bob` only. The broker repository must not be
modified. Gap G1 was decided as "keep the in-memory test setup for now", so
anything added to the realm disappears whenever the stack is recreated.

## Decision

Stewardry owns an **idempotent hub bootstrap**: a command,
`stewardry hub bootstrap`, plus a declarative file,
`deploy/hub/stewardry-realm-additions.json`. It uses the Keycloak admin REST
API to add the following to realm `mcp`, and never removes or edits anything
the broker's realm file defines:

| Added | Settings |
|---|---|
| Client `stewardry` | Confidential; authorization code with PKCE (S256) required; redirect URI `http://localhost:<stewardry-port>/auth/callback` only; default scopes `basic`; optional scope `mcp-gateway`; **no** `offline_access` (G6); no direct-access grants; no service account; no token-exchange permission |
| Persona users | `alice` (Sales) and `bob` (GRC) are reused as they are. New: `legal`, `platform`, `auditor`, `owner`, `sme`. Optional: `sales2` |
| Nothing else | No changes to `mcp-gateway`, `vtb-broker`, `mcp-demo-cli`, client scopes, keys, token lifetimes, or registration policies |

### Roles live in Stewardry, not in the hub

The hub only authenticates people. Stewardry's role mapping (`sub` → role)
lives in Stewardry's database. The platform engineer administers it, and
every change is audited (spec section 3: "configure controls and access
mappings"). The bootstrap writes a *proposed* mapping file. A platform
engineer applies it with `stewardry roles apply`.

Hub groups are not used for authority. The broker already treats the
`groups` claim as admin authority (`ADMIN_GROUP`), so persona users get no
groups at all.

### Credentials

- The bootstrap reads the Keycloak admin credential from an environment
  variable that you set (`KEYCLOAK_ADMIN_USER` and
  `KEYCLOAK_ADMIN_PASSWORD`). It never prints it, logs it, or stores it.
- It generates a random password for each new persona user and the
  `stewardry` client secret. It writes them to `deploy/hub/.secrets.env`,
  which is git-ignored and created with mode 0600. It prints only the file
  path and the names of the entries written.
- `alice` and `bob` keep their documented test passwords from the broker's
  realm. Stewardry does not copy them.

### Stable subjects

The bootstrap first tries to create users with fixed IDs through partial
import, the way the broker's realm pins `alice` and `bob`. Keycloak's
documentation does not say whether partial import keeps a supplied `id`. The
stack tests verify this (Milestone 1 step S4b).

- **If fixed IDs work:** subjects survive a stack recreate, and history
  joins stay intact.
- **If they do not:** after each recreate, Stewardry detects a changed `sub`
  for a known username and refuses sign-in until a platform engineer re-maps
  it. The re-mapping is an audited event. Old records keep the old `sub`.

### When it runs

- After every stack (re)creation. The runbook records each run as an
  operational event (G1).
- In the `stack` test tier, against the stand-in stack.

**Reason:** this adds every role in spec section 4.3.2 without touching the
broker repository or any setting the broker or gateway depend on. Authority
stays in the application, as spec section 3 requires.

## Implementation note (S14, 2026-10-11)

`stewardry hub bootstrap` (`platform/hub_bootstrap.py`) implements this
decision. The stack tier checked it against a throwaway Keycloak started from
the broker's pinned image (26.7.4) with the broker's realm file mounted
read-only (`make test-stack`):

- **Fixed IDs survive partial import.** Keycloak 26.7.4 kept every supplied
  user ID. Subjects therefore survive a stack recreate. The re-mapping path
  stays in place, and is tested offline, in case a later version behaves
  differently.
- **The client carries its own username mapper.** The broker's realm has no
  `profile` scope, so ID tokens carried no `preferred_username`, and sign-in
  to Stewardry failed until this was added. The mapper is defined on the
  `stewardry` client and adds that claim to its ID tokens only. No realm or
  broker scope changes.
- **Gateway tokens work.** A bootstrapped persona signed in to Stewardry
  through the hub's real login form. The access token Stewardry holds has the
  gateway audience (`http://localhost:8500/mcp`), `azp` `stewardry`, and no
  groups.
- **The broker's configuration is unchanged.** Its clients, client scopes,
  and `alice` and `bob` are byte-identical before, between, and after two
  runs, and the second run writes nothing.
- **Not yet done:** running it against the real dev stack waits for setup
  verification and the owner's go-ahead. The gateway call itself is checked in
  S14b.

## Rejected alternatives

- **Editing or forking the broker's realm file:** the broker repository
  must not be modified, and a fork would drift.
- **Dynamic client registration for the `stewardry` client:** it creates a
  public-registration client with the realm's anonymous policy (consent
  screen, scope limits). It is acceptable for the stack tests, but too loose
  for the application's own client.
- **Roles carried as hub groups or claims:** this collides with the broker's
  admin-group semantics. It also moves authority to a system Stewardry does
  not administer.
- **Using `offline_access`:** rejected under G6.

## Consequences

- With in-memory custody, every recreate also drops every Atlassian
  connection. The bootstrap restores hub users, not vendor connections. Each
  user reconnects through consent; nothing is re-registered.
- The bootstrap must never be pointed at a hub other than the configured
  local one. It checks the issuer URL before it acts.
