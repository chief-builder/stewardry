# Milestone 1 plan

Spec section 13, Milestone 1: gateway, registry, and audit foundation;
Anthropic model access; minimum identity, source, secret, and budget
controls; baseline evals; supported questionnaire drafting in shadow mode.
Compatibility with the existing Atlassian setup, full evidence retrieval,
and role-specific vendor access are verified before connected access is
claimed.

The steps are ordered so that offline work comes first and nothing waits on
Atlassian setup. Each step is one or a few small pull requests. Controls land
in the same step as the behavior they protect.

**Tiers** (see CLAUDE.md): **offline** needs nothing. **stack** needs Docker
and the broker's stand-in stack, with no real credentials. **live-model**
needs an Anthropic key. **connected** needs real hub users and connected
Atlassian accounts.

## Phase A: offline foundation

### S0. Repository skeleton and quality gate

- **Delivers:**
  - `pyproject.toml`, `uv.lock` (14-day cooldown), the `src/stewardry`
    layout, and a Makefile with `make check`.
  - CI workflow; Dependabot; CodeQL; SECURITY.md; THREAT_MODEL.md (initial
    trust boundaries and the inherited broker limitations from G12);
    README with prerequisites.
- **Controls:** GitHub Actions pinned by SHA with `permissions: contents:
  read` and `persist-credentials: false`. PR jobs get no secrets. actionlint
  and zizmor in CI. pip-audit over the lock. lychee link check. A
  secret-pattern scan over tracked files.
- **Tests:** `make check` passes on a clean clone. A CI job proves that
  nothing needs network access beyond the package index.
- **Tier:** offline.

### S1. Storage and domain schema

- **Delivers:** SQLite and Alembic migrations for agents, policy versions,
  users and role mapping, customers, tasks, runs, task grants, jobs, usage,
  and artifacts metadata. Also a content-addressed artifact store.
- **Controls:** unique constraints for idempotency keys. Foreign keys on.
  Data directory outside the repo, created with owner-only permissions.
- **Tests:** migration up and down, constraint violations, and artifact
  hash round-trip.
- **Tier:** offline.

### S2. Append-only audit log

- **Delivers:**
  - An event schema (requester, acting or authorizing user, reviewer,
    agent, policy version, customer, task, timestamp, result,
    correlation ID).
  - A hash chain, `stewardry audit verify`, and an anchor checkpoint
    command.
- **Controls:** triggers reject `UPDATE` and `DELETE`. A redaction filter
  blocks JWT shapes, bearer headers, and configured secret values.
- **Tests:** tamper fixtures (edit, delete, reorder, insert, truncate after
  an anchor) are detected. Secret probes never reach stored events or logs.
  Blocked attempts are recorded separately from successful actions.
- **Tier:** offline.

### S3. Agent registry and lifecycle

- **Delivers:**
  - Registry records with every field in spec section 4.2.
  - Per-agent policy files (`policies/agents/*.yaml`) with a JSON Schema,
    applied by `stewardry registry apply` as immutable versions identified
    by a content hash (ADR 0009). Also the platform baseline of global
    denies.
  - "Re-evaluation required" is set when model, prompt, operations, or
    model tools change.
  - Engagement status and operational state (enabled or paused).
  - A platform-engineer CLI to register, pause, resume (with a reason), and
    retire agents.
  - `questionnaire-agent` registered in `build`.
- **Controls:** unregistered, paused, and retired agents cannot start.
  Pausing stops work at the next controlled boundary. Status changes need an
  authorized role and the gate evidence. Platform-engineer status grants no
  source access.
- **Tests:**
  - A lifecycle state-machine table.
  - Unauthorized transitions rejected and audited.
  - Pause during a run stops the next tool or model call.
  - A schema-invalid policy is refused.
  - A baseline deny cannot be overridden by an agent file.
  - Status cannot advance while re-evaluation is required.
- **Tier:** offline.

### S4. Identity, sessions, roles, and task grants (with a local test issuer)

*Delivered in two pull requests:* **S4a** covers the access mapping, task
submission, grants, and identity resolution, with no web code. **S4b** covers
the FastAPI app, OIDC sign-in against the test issuer, sessions, CSRF, and
the web routes on top of S4a.

- **Delivers:**
  - A FastAPI app shell with OIDC sign-in (Authlib), tested against an
    in-process test issuer that signs with PS256.
  - Server-side sessions, a `sub` → role mapping stored in Stewardry and
    applied by a platform engineer (`stewardry roles apply`, audited;
    ADR 0008), and the
    task-grant model: requester and authorizing user, task, agent, policy
    version, scope, expiry, and revocation.
  - An in-memory credential holder keyed by session (ADR 0006).
- **Controls:**
  - CSRF on forms.
  - Grants bind to the authenticated `sub`. Changing a task field cannot
    change whose credential is used.
  - Sales cannot authorize drafting.
  - A known username arriving with a changed `sub` is refused until a
    platform engineer re-maps it.
  - Hub `groups` claims are ignored for authority.
  - An expired or revoked grant, or a missing credential, pauses the run.
    It never falls back to another identity.
- **Tests:** identity-swap attempts through form fields, task fields, and
  replayed requests. Wrong-role actions. Grant expiry and cancellation.
  Restart drops credentials and the tasks pause.
- **Tier:** offline.

### S5. Controlled execution core

- **Delivers:**
  - Request admission: resolve the registered agent through server-side
    routing and bind it to a protected run record.
  - A tool executor that, on every call, computes platform baseline ∩ the
    run's bound policy version ∩ the agent's current policy version ∩ user
    role for the stage ∩ task grant ∩ customer scope ∩ budget (ADR 0009).
- **Controls:** tool arguments cannot carry identity, agent ID, customer,
  or source outside the run's scope. Unknown tools are denied. Every
  decision is audited.
- **Tests:**
  - A property-based authorization-denial matrix.
  - Model-shaped arguments that try to name another user, customer, or page.
  - Paused-agent rechecks.
  - A permission removed mid-run stops the next call.
  - A permission added mid-run is not used by runs already in progress.
- **Tier:** offline.

### S6. Model access boundary, recording, and replay

- **Delivers:**
  - An Anthropic client wrapper that records model ID, prompt version,
    settings, usage, latency, and estimated cost from a dated pricing table.
  - Budgets for tokens, calls, retries, timeouts, and daily spend.
  - Refusal handling as a bounded failure.
  - A recorder and a replay player keyed by a canonical request hash.
- **Controls:** keys come only from the environment and are never logged.
  Replay mode installs a network guard and refuses to start if model or
  service credentials are configured. Missing usage is recorded as unknown.
- **Tests:** budget exhaustion stops a run cleanly. Retries are bounded.
  Replay with no network and no credentials. A test that a key is never
  written to logs or audit.
- **Tier:** offline. One labeled `live-model` smoke test is run manually.

### S7. Atlassian read adapter (offline contract)

- **Delivers:**
  - The `AtlassianReads` interface with the narrow operations in
    [broker-contracts.md](../broker-contracts.md#which-read-tools-back-each-operation).
  - A fixture-backed fake that matches the pinned snapshot's argument
    shapes.
  - The ADR 0010 classifier. Consent comes only from a structured URL
    elicitation whose URL matches the configured broker authorize path. All
    other errors are matched against the pinned template catalog
    (`gateway_errors.py`), and anything unmatched becomes `unexpected`,
    handled as `unavailable`.
  - Two elicitation modes: interactive connect (accept and relay to that
    user only) and background (record `needs_consent`, cancel, pause).
- **Controls:** a fixed `cloudId`; project-key and catalog allow-lists;
  CQL built only from templates; Jira field allow-list filtering;
  `executeRead` and `discover` unreachable; unknown errors become
  `unavailable`, never consent.
- **Tests:**
  - Allow-list enforcement.
  - A snapshot-drift test reads the pinned `atlassian.json` and checks the
    tool names and argument fields used.
  - A catalog-drift test parses the pinned `src/mcp_gateway/server.py` and
    fails if any `ToolError` template is missing from the catalog or stale.
  - "Connect … first" text never yields consent.
  - A foreign-host elicitation URL is rejected.
  - No tokens in results.
- **Tier:** offline.

### S8. Synthetic data v0 and eval separation

- **Delivers:**
  - About 20 synthetic policies (plain content plus owner, dates,
    supersession, classification, version).
  - One `.xlsx` and one `.docx` questionnaire template with defined
    question and answer locations.
  - Three questionnaires of about 30 questions each, covering covered,
    uncovered, conflicting, stale, and adversarial cases.
  - Synthetic Jira request fixtures.
  - A golden set, abstention set, and rubric in `evals/`.
- **Controls:** a test fails if runtime code or the agent's tool surface
  can reach `evals/`. A data review confirms no employer references and no
  real company data.
- **Tests:** fixture schema validation and the separation test.
- **Tier:** offline.

### S9. Questionnaire ingest and input screening

- **Delivers:** parsing of the supported templates into questions with
  stable locations. Unsupported layouts are rejected with an actionable
  explanation. Screening of untrusted text, including comments and hidden
  cells or text in the supported locations, records findings for review.
- **Controls:** document text is treated as data. Screening findings are
  audited. Screening is not relied on as the only injection defense.
- **Tests:** template parsing, rejection messages, and planted hidden-text
  fixtures detected.
- **Tier:** offline.

### S10. Evidence snapshots and the strength rubric

- **Delivers:**
  - Evidence snapshots: page ID, version metadata (nullable until G7 is
    verified), content hash, and passages with stable locations.
  - Staleness rules from review dates and supersession, a conflict marker,
    and the qualitative strength labels from spec section 5.1.
  - A per-run evidence cache keyed by acting user, task, page, and version.
- **Controls:** snapshots and caches never cross users or customers. An
  incomplete body or unresolved macros makes evidence ineligible.
- **Tests:** hash changes invalidate the evidence. Cache isolation between
  users and customers. Stale or conflicting evidence is labeled correctly.
- **Tier:** offline.

### S11. Shadow drafting workflow

- **Delivers:**
  - A per-question tool loop (ADR 0003) with the tools
    `list_task_policies`, `read_policy_passages`, `submit_answer`, and
    `route_to_expert`.
  - Deterministic citation-presence and citation-location validation.
  - Resumable progress through jobs with leases.
- **Controls:**
  - A factual answer without valid citations cannot reach review; it routes
    to an expert.
  - Missing, stale, conflicting, or ambiguous evidence is marked "needs an
    expert".
  - Budgets apply per question and per run.
  - Shadow status: nothing is exported or released to Sales.
- **Tests:**
  - Replayed runs across the covered, uncovered, conflict, and stale cases.
  - Restart mid-run resumes without duplicate drafts.
  - A duplicate submission is idempotent.
  - A model attempt to self-approve or name another identity is denied.
- **Tier:** offline (replay). A `live-model` run is labeled separately.

### S12. Minimal role-restricted views

- **Delivers:** a Sales status view, and a GRC shadow-review view showing
  drafts, citations, and passages. The GRC view records a shadow verdict
  (agree, edit, or reject) for later comparison; this is not approval.
  There is also a platform registry view.
- **Controls:** view models filtered on the server per role. Sales views
  and status messages contain no passages, titles, links, or reviewer-only
  metadata.
- **Tests:** leak tests on the rendered HTML and status text per role.
  Unauthorized role access is denied and audited.
- **Tier:** offline.

### S13. Baseline evals

- **Delivers:**
  - A deterministic eval subset in `make check` that runs on replay.
  - A `live-model` eval command that writes a report with model, prompt,
    dataset, policy, and control versions.
  - Metrics: citation presence, abstention precision and recall on the
    no-coverage set, correctness against the author-graded rubric, cost,
    and latency.
  - Raw completion and valid completion reported separately.
- **Controls:** provenance labels (ADR 0007). Zero-tolerance counts include
  failed and excluded runs. Small samples are labeled insufficient.
- **Tests:** the report generator on fixed inputs. The label is always
  present.
- **Tier:** offline for the subset. `live-model` for the full baseline.

## Phase B: stack (Docker, no real credentials)

### S14. Hub bootstrap for Stewardry's client and role users

- **Delivers:** `stewardry hub bootstrap` and
  `deploy/hub/stewardry-realm-additions.json` (ADR 0008). The command
  idempotently adds the `stewardry` client and the `legal`, `platform`,
  `auditor`, `owner`, and `sme` users (optionally `sales2`) to realm `mcp`.
  It also writes a proposed role-mapping file.
- **Controls:**
  - It checks the issuer before acting and only adds to the realm, never
    edits or removes.
  - The admin credential comes from environment variables and is never
    printed.
  - Generated passwords and the client secret go to a git-ignored 0600
    file, and only the entry names are printed.
  - No `offline_access`, no groups, and no changes to broker-owned clients
    or scopes.
- **Tests:**
  - Running it twice changes nothing the second time.
  - Broker-owned clients and scopes are byte-identical before and after.
  - Whether fixed user IDs survive partial import is verified (ADR 0008),
    and the fallback re-mapping path is tested.
  - Alice signs in to Stewardry and her gateway call works.
  - The `platform` user has no Atlassian connection, and Stewardry denies
    the platform role source access.
- **Tier:** stack. It is then run once against the real dev stack, after
  setup verification and with your go-ahead.

### S14b. Gateway contract tests against the broker's stand-in stack

- **Delivers:**
  - A `make test-stack` target. It starts the broker's gateway profile from
    the pinned checkout (read-only use) and runs Stewardry's adapter as an
    MCP client for the stand-in users `alice` and `bob`.
  - Stewardry's client and users come from the S14 bootstrap, run against
    the ephemeral stand-in stack. Nothing in the broker repository changes.
- **Controls:**
  - `make test-stack` refuses to start if any `vtb-*` container is already
    running. The stand-in stack uses the same container names and ports as
    the real one, and recreating it would wipe in-memory custody, which
    disconnects every real Atlassian account (G1, G10).
    In practice, run the stack tier in CI, or locally only when no real
    connections need to be kept.
  - The consent link is relayed only to the requesting user.
  - An outage is never shown as consent.
- **Tests:**
  - Sign-in, call, interactive consent accept, decline, and timeout.
  - A background run gets a structured elicitation, records
    `needs_consent`, and pauses without exposing the link.
  - Each ADR 0010 class is triggered for real where the stand-ins allow it:
    broker stopped and hub stopped → `unavailable`; a declined or cancelled
    elicitation; revoke-pending if it can be produced.
  - Disconnect → `needs_consent`.
  - Alice's credential is never used for Bob's run.
  - No token material in Stewardry's logs or audit (mirroring the broker's
    container-log test).
- **Tier:** stack. The CI job has no secrets.

### S14c. Stewardry-only gateway instance (decision gate for Milestone 2)

- **Delivers:** a design check in the stack tier only. A second gateway
  container is started from the pinned image with a Stewardry upstreams
  file that lists only `getJiraIssue`, `searchConfluence`,
  `getConfluenceContent`, `atlassianUserInfo`, and
  `getAccessibleAtlassianResources`. It has its own audience scope and
  exchange client, added by the bootstrap (ADR 0009, option 2).
- **Tests:** `executeRead` and `discover` are not listed and cannot be
  called through it. Token exchange works for the new audience. Tools load
  from the bundled snapshot subset.
- **Outcome:** a short report, and a go or no-go decision on adopting it in
  Milestone 2. No connected use in Milestone 1.
- **Tier:** stack.

## Phase C: connected (needs setup verification and gap decisions)

### S15. Connected compatibility verification

- **Delivers:** a dated report, labeled `connected`, showing:
  - per-user site and account mapping, with distinct `accountId`s;
  - a Jira field read as Sales;
  - a full-page read as GRC with version metadata and a hash;
  - a restricted page denied to Sales by Atlassian **and** by Stewardry;
  - a manual Confluence edit by the expert detected as a version and hash
    change;
  - consent, disconnect, and outage behavior on the real stack.
- **Controls:** read-only tools only. No write operations exist in the
  adapter. Hub users and the `stewardry` client come from the S14
  bootstrap. In-memory custody (G1) means the stack must not be recreated
  during the session; if it is, reconnects are recorded.
- **Tests:** the connected suite (`pytest -m connected`), run manually. It
  closes G7 and G8 with recorded contract fixtures.
- **Tier:** connected.

### S16. First connected shadow run

- **Delivers:** one synthetic questionnaire drafted under GRC's identity
  from real Confluence evidence, compared with a manual completion. This
  records the first actual shadow session.
- **Controls:** everything from S2–S13 in force, a budget cap, and no
  release to Sales.
- **Tests:** the run record shows `connected` provenance, the broker
  revision, model and prompt versions, and a valid audit chain.
- **Tier:** connected and live-model.

## Before the first connected session (spec 4.5 and 4.6)

Write these down before S16. They are documents, not code:

- retention periods and deletion behavior;
- operating windows;
- operational budgets;
- the reviewer-time baseline method;
- the engagement brief's numerical objectives.
