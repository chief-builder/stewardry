# Setup verification checklist

Run this before Milestone 1 step S15 (connected compatibility). It records
what exists today, without exposing secrets and without changing the Atlassian
registration. Put the results in a dated copy of the tables below. Record
names, IDs, plan names, and yes/no answers only, never secret values.

## Ground rules

- **Never print a secret.** Where a command checks whether a secret is set,
  it prints the variable *name* or a count, never the value. Do not run
  `docker inspect` on a full environment, `cat` an `.env` file, or `env`.
  Don't paste a terminal that shows a token, a client secret, or a custody
  token into notes, issues, or chat.
- **Do not re-register.** Never run `tools/register-mcp-client.py` (with or
  without `--force`). Do not edit the Atlassian app's settings, the Rovo MCP
  domain settings, or the broker registry.
- **Do not stop the stack casually.** Custody in the broker's test stack is
  in memory. `make stack-down` or `docker compose ... down` deletes every
  stored Atlassian connection. The registration itself survives in
  `tests/stack/.env`, and users can reconnect through consent. That is not a
  re-registration, but it is an event to record.
- **Read only.** Every step below looks at existing state, except where it
  says "create". Creating synthetic content and test users is setup work.

## 1. Broker checkout

| Check | Command or place | Expected | Result |
|---|---|---|---|
| Pinned revision | `git -C ../vendor-token-broker rev-parse HEAD` | `20d1426622a0c05478c4331b82ad8952411f9a4a` | |
| No tracked changes | `git -C ../vendor-token-broker status --short --untracked-files=no` | empty | |
| Local env file exists | `test -f ../vendor-token-broker/tests/stack/.env && echo present` | `present` | |
| Which variables it sets (names only) | `sed -E 's/=.*//' ../vendor-token-broker/tests/stack/.env \| sort` | includes `ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET` | |
| Atlassian values are non-empty (count only) | `grep -cE '^ATLASSIAN_CLIENT_(ID\|SECRET)=.+' ../vendor-token-broker/tests/stack/.env` | `2` | |
| Secret expiry, if any | Your notes from the original registration run (the script printed a date if Atlassian set one) | a date, or "none reported" | |

Note: the checkout has untracked local files (`fix_plan.md`, `review*.md`).
Planning did not read them, and nothing here touches them.

**Recorded 2026-10-12 (names and counts only):**

- **Checkout:** at `20d1426…` with no tracked changes.
- **`.env`:** present. It sets `ATLASSIAN_CLIENT_ID` and
  `ATLASSIAN_CLIENT_SECRET` (both non-empty, by count), plus GitHub, Linear,
  and Cloudflare variables.
- **It does not set `GATEWAY_UPSTREAMS`.** The broker's compose file then
  uses the **stand-in** upstreams, so a plain `make stack-up` talks to the
  stand-in Atlassian, not the real one. For connected work, start the stack
  with the real upstreams, and only Atlassian. A longer consent wait leaves
  time for a person in a browser:

  Run this from the Stewardry checkout (`stack-up` is the broker's make target):

  ```bash
  GATEWAY_UPSTREAMS= GATEWAY_ENABLED_UPSTREAMS=atlassian GATEWAY_CONSENT_WAIT_S=120 make -C ../vendor-token-broker stack-up
  ```

  Section 5's last row then shows a real `cloudId`.

## 2. Atlassian site and plans

| Check | Where | Record |
|---|---|---|
| Site URL (`https://<name>.atlassian.net`) | Atlassian Administration → Products | |
| `cloudId` | From step 6 below (`getAccessibleAtlassianResources`) | |
| Confluence plan | Administration → Billing | Free / Standard / Premium / Enterprise |
| Jira plan | Administration → Billing | |
| Rovo MCP server enabled for the site | Administration → Rovo → Rovo MCP server | yes / no |
| Allowed callback domains include `http://localhost:*` | Same page → Domain settings (view only) | yes / no |
| Rovo MCP availability and usage limits on your plan | Same page and Atlassian docs | note any limit |
| Broker app appears under connected or authorized apps (registered as `vtb-mcp-gateway`) | Administration → Apps or connected apps (view only) | yes / no |
| Users currently licensed for Confluence and Jira | Administration → Users | count, and names of test users |

### Which Confluence plan supports page restrictions (spec 4.3.1)

- **Confluence Free does not.** Atlassian's page-permission documentation
  says: "You can't restrict access to content on the Free plan." On Free,
  everyone who can log in can view and edit all spaces and pages.
- **Standard is the lowest paid plan** that Atlassian describes as adding
  access control, including page restrictions and space permissions.
  Premium adds admin tooling for inspecting and bypassing restrictions, which
  Stewardry does not need.
- **Before you buy or upgrade:** confirm on Atlassian's current
  plan-comparison page that Standard includes page restrictions, and check
  the per-user price and trial terms. The pricing page could not be read in
  full during planning, so this statement relies on Atlassian's support
  documentation and its plans overview.
- Jira can stay on its current plan. The Jira demo needs only that Sales can
  read the synthetic request project.

## 3. Callbacks and client registration (view only)

| Check | How | Expected | Result |
|---|---|---|---|
| Broker callback used at registration | Your registration notes; the quickstart used `--redirect-base http://localhost:8600` | `http://localhost:8600/v1/callback/atlassian` | |
| Broker public URL in the running stack | Broker health answers on that base: `curl -fsS http://localhost:8600/healthz` | `{"ok":true,"custody":"ok"}` | |
| Hub sign-in callback for the broker | Keycloak admin console → realm `mcp` → Clients → `vtb-broker` → Valid redirect URIs (view only) | `http://localhost:8600/v1/callback/_hub` | |
| End-to-end consent still works | Step 6 with one test user | Connected without any re-registration | |

If the callback or public URL differs from the registration, **stop**. Do not
re-register. Record what differs and bring it to the owner review.

## 4. Identity realm

| Check | Command or place | Expected | Result |
|---|---|---|---|
| Running containers | `docker ps --filter name=vtb- --format '{{.Names}} {{.Status}}'` | `vtb-keycloak`, gateway, `vtb-broker-kc`, `vtb-openbao-kc`, … | |
| Keycloak mode | `docker ps --filter name=vtb-keycloak --format '{{.Command}}'` | shows `start-dev --import-realm` (dev, re-imported at start) or a persistent setup | |
| Discovery document | `curl -fsS http://localhost:8180/realms/mcp/.well-known/openid-configuration \| python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["issuer"], d["id_token_signing_alg_values_supported"][:4], "registration_endpoint" in d)'` | issuer `http://localhost:8180/realms/mcp`, PS256 available | |
| Clients present | Admin console → realm `mcp` → Clients (sign in with the test admin credential documented in the broker's compose file; don't copy it into notes) | `mcp-demo-cli`, `mcp-gateway`, `vtb-broker`, plus any client registered dynamically by Claude Code | |
| Users present | Admin console → Users | `alice`, `bob` (fixed IDs `a11ce000-…-001`, `b0b00000-…-002`) | |
| Token-exchange setting on `mcp-gateway` (view only) | Clients → `mcp-gateway` | enabled | |
| Access-token lifespan | Realm settings → Tokens | 5 minutes in the test realm | |

## 5. Custody configuration

| Check | Command | Expected | Result |
|---|---|---|---|
| Custody mode | `docker ps --filter name=vtb-openbao-kc --format '{{.Command}}'` | `server -dev` means in memory: connections are lost on restart | |
| Broker sees custody | `curl -fsS http://localhost:8600/healthz` | `"custody":"ok"` | |
| Coordination backend | `docker ps --filter name=vtb-redis --format '{{.Names}}'` | empty for `memory` (one replica), present for `redis` | |
| Gateway is pointed at the real Atlassian | Run step 6. A real `cloudId` and your site URL mean real. The stand-in returns test data | real | |

## 6. Test accounts and role mapping (spec 4.3.2)

For each hub user, use a **separate private browser window** so hub sessions
do not mix. Then run, from the broker checkout:

```bash
.venv/bin/python tools/mcp-demo-client.py atlassian_getAccessibleAtlassianResources
```

```bash
.venv/bin/python tools/mcp-demo-client.py atlassian_atlassianUserInfo
```

Sign in as the intended hub user, and connect Atlassian as the intended
Atlassian account when asked. The output contains the site list and an
`accountId`, not tokens. Record only the `accountId` and `cloudId`.

### How many accounts the role mapping needs

| Persona | Hub account | Connected Atlassian account | Why |
|---|---|---|---|
| Sales (Alice) | **required** | **required**: Jira read on the request project; *no* access to the restricted policy pages | Intake reads, and the vendor-side denial demo |
| GRC analyst (Bob) | **required** | **required**: read on the policy space and restricted pages | Evidence retrieval under GRC's own identity |
| Legal counsel | **required** | **required**: read on the playbook space | NDA shadow (Milestone 3) |
| Platform engineer | **required** | **none** | Shows that admin status grants no source access |
| Auditor | **required** | **none** | Sees audit metadata only |
| Engagement owner | recommended | none | Gate decisions and handoff acceptance under a separate identity |
| Subject-matter expert | recommended | none through Stewardry. A separate Atlassian user edits policy pages in Confluence's UI | Expert updates come from a different identity than the reviewer |
| Second Sales, other customer | optional | optional, Jira read only | Connected cross-customer check (offline evals cover it otherwise) |

**Minimum:** 5 distinct hub accounts and 3 distinct connected Atlassian
accounts (Sales, GRC, Legal). **Recommended:** 7 hub accounts, 3 connected
Atlassian accounts, plus 1 Atlassian author account for the expert that is
never connected to Stewardry.

The existing Atlassian site-admin account must not be connected to any
Stewardry hub user. Every connected `accountId` must differ from every other
connected `accountId` (spec 4.3.2: distinct hub users on one privileged
account prove nothing).

| Hub user | Hub `sub` | Atlassian account (display name) | `accountId` | `cloudId` matches site | Distinct from all others |
|---|---|---|---|---|---|
| | | | | | |

The realm currently has only `alice` and `bob`. The other hub users and the
`stewardry` client are added by `stewardry hub bootstrap` (Milestone 1 step
S14, [ADR 0008](adr/0008-hub-roles-bootstrap.md)). It is run against this
stack only after this checklist is complete and you give the go-ahead. You
supply the Keycloak admin credential as `KEYCLOAK_ADMIN_USER` and
`KEYCLOAK_ADMIN_PASSWORD` in your shell. Generated passwords go to
`deploy/hub/.secrets.env` (git-ignored) and are never printed.

## 7. Synthetic content and restrictions (create)

Decided 2026-10-12: **page restrictions only** (open question 4). Sales can
see the policy space, but the confidential pages are restricted to GRC. You
create the Atlassian users and the content yourself. Stewardry never writes
to Atlassian.

1. **Users.** Create separate Atlassian users for Sales, GRC, and Legal,
   plus an expert author account. None of them is the site admin. Give each a
   Jira and Confluence license.
2. **Jira.** Create a project with key `SQR`, and give Sales browse
   permission. Create request `SQR-1` from `fixtures/jira/SQR-1.json`
   (summary, reporter, and the customer field if you add one; leave the
   internal notes in the description). Record the customer field's ID
   (`customfield_…`) if you create one.
3. **Policy pages.** Run `make confluence-pages` and open
   `build/confluence-pages/index.html`.
   1. Create a space with key `SECPOL`, and give Sales, GRC, and the expert
      view access (the expert also needs edit).
   2. For each of the 21 policies, create a page with the title shown. Open
      the policy's file in the browser, select all, copy, and paste. Plain
      content only: no macros, panels, or other formatting.
   3. On SEC-04, SEC-09, and SEC-17, add a view restriction for the GRC user
      only (and the expert, if they need to edit those pages).
4. **Check the restriction in Atlassian's UI.** Signed in as the Sales
   Atlassian user, confirm SEC-04 cannot be opened. Record the date.
5. **Record the page IDs.** Take each page's ID from its URL
   (`…/pages/<id>/…`) and record it here. `policies/catalogs/secpol.yaml` is
   updated from this list in the S15 pull request.

| Policy | Page ID | Restricted to GRC |
|---|---|---|
| SEC-01 … SEC-21 | | SEC-04, SEC-09, SEC-17 |

The legal playbook space (`LEGALPB`) is needed only for Milestone 3. It can
wait.

## 8. Model access (configure; do not share)

| Item | Where you configure it | Check without exposing it |
|---|---|---|
| Anthropic API key for Stewardry | Stewardry's `.env` (git-ignored) as `ANTHROPIC_API_KEY`, or an `ant auth login` profile | `test -n "$ANTHROPIC_API_KEY" && echo set` |
| Spend limit | Anthropic Console → the workspace used for Stewardry → limits | Record the limit amount, not the key |
| Hub client secret for the `stewardry` client (after G2) | Stewardry's `.env` as `STEWARDRY_HUB_CLIENT_SECRET` | `grep -c '^STEWARDRY_HUB_CLIENT_SECRET=.\+' .env` |

## 9. The connected run (S15)

Do this after sections 1–7 and your go-ahead.

1. **Bootstrap the real hub.** Start the stack with the real upstreams
   (section 1), then run `stewardry hub bootstrap` with
   `KEYCLOAK_ADMIN_USER` and `KEYCLOAK_ADMIN_PASSWORD` set in your shell.
   Review `deploy/hub/proposed-access-mapping.yaml`.
2. **Fill in the settings file.** Copy
   `deploy/connected/connected.example.yaml` to
   `deploy/connected/connected.yaml` (git-ignored). Fill in the `cloudId`,
   the request key, and three page IDs: SEC-05 (readable), SEC-04
   (restricted), and SEC-07 (to edit).
3. **Set the settings in your shell** (values never printed):
   `STEWARDRY_HUB_ISSUER`; `STEWARDRY_HUB_CLIENT_SECRET` (from
   `deploy/hub/.secrets.env`); and `STEWARDRY_ATLASSIAN_CLOUD_ID`.
4. **Run `make test-connected`.** It starts Stewardry on
   `http://localhost:8700` and prompts you as it goes:
   1. Sign in as each of alice (Sales), bob (GRC), and legal, each in its
      own private window. Connect Atlassian as that person's own Atlassian
      account.
   2. When asked, edit SEC-07 in Confluence's UI as the expert.
   3. Optionally allow the outage and disconnect checks.
5. **Results.** The report, labeled `connected`, and the recorded response
   shapes (G7, G8) are written to `docs/connected/<date>/`. Check them for
   anything that should not be published before committing.

## Sign-off

| Item | Done | Date | Notes |
|---|---|---|---|
| Sections 1–5 recorded without secrets | | | |
| Confluence plan supports restrictions | | | |
| Account mapping complete and distinct | | | |
| Restricted page denied to Sales in Atlassian's UI | | | |
| Gap decisions G1, G2, G3, G4, G6 | yes | 2026-10-10 | See broker-contracts.md |
| Hub bootstrap run against this stack (after go-ahead) | | | |
