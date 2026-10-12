# ADR 0009: Per-agent policy

Status: Accepted · 2026-10-10 (owner review of chief-builder/stewardry#1)

Option 1 is accepted. Option 2 (a Stewardry-only gateway instance) is not
accepted yet. It is decided at Milestone 1 step S14c.

## Context

The gateway has no per-client or per-agent policy (gap G3). Any valid gateway
token can call every allow-listed Atlassian tool with any arguments,
including `executeRead` and `discover`. Spec section 4.2 requires each
registered agent to have allowed tools and sources, model configuration,
budgets, and a versioned effective policy. Spec section 4.3 says effective
access is the intersection of user authority, the agent's allow-lists,
customer scope, and the vendor's permissions.

## Options considered

| # | Where it is enforced | Per agent? | Needs a broker change? | Verdict |
|---|---|---|---|---|
| 1 | **Stewardry registry policy, checked by the controlled executor on every call** | Yes | No | **Adopt now.** The authoritative layer |
| 2 | **A Stewardry-only gateway instance** with a narrowed upstreams file (no `executeRead`, `discover`, or Jira search), run from the pinned image with config in the Stewardry repo | Per application, not per agent | No code change. Needs one more hub client and scope (through ADR 0008's bootstrap) | **Proposed as Milestone 2 hardening**, after the stack tests verify it. It closes the "compromised Stewardry calls `executeRead`" path |
| 3 | Atlassian permissions on each role's own account (spaces, page restrictions) | Per user | No | Already required. It is the vendor-side layer |
| 4 | A per-client (`azp`) tool and argument allow-list inside the gateway | Per client | **Yes** | Written up as an upstream proposal ([broker-upstream-proposals.md](../proposals/broker-upstream-proposals.md)), not done here |
| 5 | One gateway instance per agent | Yes | No | Rejected: duplicated infrastructure for two agents |

## Decision (option 1)

### Policy files

Each agent has a policy file in the repository at `policies/agents/<agent-id>.yaml`.
A JSON Schema validates it. Changes go through a pull request, the same way
the broker's vendor registry is treated as reviewed config. Applying a file
with `stewardry registry apply` (platform-engineer role, audited) stores an
immutable **policy version**, identified by the SHA-256 of its canonical
form.

Illustrative shape (the values are placeholders, not agreed budgets):

```yaml
agent_id: questionnaire-agent
schema_version: 1
risk_tier: 2                      # drafts need human approval before release
stages:
  intake:   {acting_roles: [sales]}
  drafting: {acting_roles: [grc], requires_task_grant: true}
operations:                       # narrow adapter operations, never raw MCP tools
  jira.get_request:
    stage: intake
    projects: [SQR]
    fields: [summary, reporter, created, customer]   # mapped to site field IDs in config
  confluence.list_pages:
    stage: drafting
    spaces: [SECPOL]
  confluence.read_page:
    stage: drafting
    catalog: policies/catalogs/secpol.yaml   # page IDs and expected classification
    detail: full
    max_calls_per_run: 300
model:
  id: claude-opus-5-5
  effort: medium
  prompt_version: questionnaire-draft@1
model_tools: [list_task_policies, read_policy_passages, submit_answer, route_to_expert]
budgets:                          # placeholders until set before connected sessions
  per_run_usd: 0
  per_run_model_calls: 0
  per_run_tool_calls: 0
customer_scope: per_task          # a run sees only its task's customer
```

*Implementation note (S3, 2026-10-10):* the implemented file shape is in
[`policies/agents/questionnaire-agent.yaml`](../../policies/agents/questionnaire-agent.yaml)
and is validated by
[`policies/agent-policy.schema.json`](../../policies/agent-policy.schema.json).
It differs from the sketch above in one way. Owners and the risk tier sit in
a `registration` section stored on the agent record. Only the `policy`
section (with the agent ID and schema version) is hashed into a version, so
changing an owner does not create a new policy version. The platform
baseline is
[`policies/platform-baseline.yaml`](../../policies/platform-baseline.yaml).

### Effective policy, checked on every call

The executor allows a call only when all of these allow it:

1. the **platform baseline**: global denies that no agent file can override,
   namely `executeRead`, `discover`, any write or destructive operation, any
   `cloudId` other than the configured site, and any space not configured for
   the platform;
2. the run's **bound policy version**, fixed at admission;
3. the agent's **current policy version**. If a permission is removed, it
   stops running work at the next controlled boundary. Additions apply only
   to new runs;
4. the **acting user's role** for that stage, and their **task grant**;
5. the task's **customer scope**;
6. the remaining **budget**.

Atlassian's own permission check on the user's account then applies on top
of this. Every decision is audited with the policy version hash.

### Change control

- A change to `model`, `prompt_version`, `operations`, or `model_tools`
  marks the agent **re-evaluation required**. Its engagement status cannot
  advance until an accepted eval report names the new policy hash (spec
  sections 4.8 and 7.3).
- The model, user input, document content, and task fields cannot change a
  policy or select a version.

**Reason:** this is the one place that knows the agent, the user, the task,
and the customer at the same time. It is per agent by construction, and it
is testable offline.

## Consequences

- The vendor-facing boundary is still "Stewardry adapter plus Atlassian
  permissions" until option 2 lands. THREAT_MODEL.md states this.
- Option 2 needs a new gateway audience. That means another client scope
  added by the bootstrap, and a client for the second gateway's token
  exchange. Its design is verified in the stack tier before it is proposed
  for connected use.

*Implementation note (S14c, 2026-10-12):* option 2 was checked in the stack
tier (Milestone 1 step S14c). The second gateway listed only the five
reviewed tools, with schemas from the bundled snapshot. It refused
`executeRead`, `discover`, and Jira search without reaching the upstream.
Token exchange worked for its own audience, and tokens did not cross
between the two gateways. The findings and the recommendation (go for
Milestone 2, after one verification on the real stack) are in
[s14c-stewardry-gateway.md](../plan/s14c-stewardry-gateway.md). Option 1
stays in force for Milestone 1.
