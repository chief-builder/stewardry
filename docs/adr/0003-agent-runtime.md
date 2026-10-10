# ADR 0003: Agent runtime

Status: Accepted · 2026-10-10 (owner review of chief-builder/stewardry#1)

The owner accepted the Python Anthropic SDK "for now" and may move to the
Vercel AI SDK later. See [Possible future change](#possible-future-change).

## Context

Spec section 4.4 says unused runtime tools and alternate credential,
filesystem, or network paths must not bypass enforcement. Section 4.1 says
every tool execution rechecks user, agent, customer, source, approval, and
budget. Section 4.8 says Claude through the Anthropic API is the only
provider in iteration 1, with model, prompt version, settings, usage, and
latency recorded.

## Decision

Stewardry owns the agent loop. It calls the Anthropic Messages API through
the official `anthropic` Python SDK and writes the tool-use loop itself.

- **Model tools are Stewardry application operations only**, for example
  `list_task_policies`, `read_policy_passages(policy_id)`,
  `submit_answer(answer, citations, strength)`, and
  `route_to_expert(question_id, reason)`. Every call goes through the
  controlled executor (`platform/authz`). The executor uses the run's
  protected agent ID and authenticated user context, never values from tool
  arguments.
- **The model never talks MCP and never sees Atlassian tools.** Stewardry's
  Atlassian adapter is the MCP client (ADR 0006). The adapter fetches the
  allow-listed policy pages under the authorizing user's identity. The model
  receives only permitted passages.
- **Model settings:** `claude-opus-5-5` (the current Opus) with adaptive
  thinking, which cannot be disabled on this model. Effort is set
  explicitly; it starts at `medium`, the model's default, and is tuned with
  evals. Responses stream. Tool schemas use `strict: true`. `tool_choice` is
  `auto`, because forced tool choice returns an error on this model.
- **Citations:** policy passages are sent as document content blocks with
  citations enabled where that fits the tool flow. Character-range locations
  then come from the API instead of free-text quotes. Stewardry still
  verifies every citation against its own evidence snapshot. A citation from
  the model is never trusted by itself.
- **Limits:** each run has bounded tokens, model calls, tool calls, retries,
  and wall-clock time. There is also a daily spend ceiling. These are
  enforced in the model boundary (`platform/model`), not by prompts.
- **Refusals:** a `refusal` stop reason ends the item as a recorded, bounded
  failure. The server-side fallback parameter is **not** enabled by default.
  A fallback would silently change the model mid-run, which breaks the
  per-run model record and the eval gate in spec section 7.3. This is an
  open question for the owner.
- **Pricing reference:** cost is estimated from a checked-in pricing table
  that names its source and date. As of 2026-10-06, Anthropic lists
  `claude-opus-5-5` at $4 per million input tokens and $20 per million output
  tokens. That figure is re-checked against Anthropic's pricing page when the
  table is created. If usage is missing, it is recorded as unknown, not zero.
- **Background work:** jobs run in-process on a DB-backed queue with leases
  and idempotency keys (ADR 0004). The web app and the worker run in one
  process.

**Reason:** owning the loop makes every enforcement point visible and
testable. No runtime brings its own tools, filesystem, or network paths.

## Rejected alternatives

- **Claude Agent SDK:** it ships built-in file, shell, and web tools and its
  own permission model. That creates the alternate paths spec section 4.4
  forbids, and their absence would need constant proof.
- **Managed Agents:** tool execution and credentials move to a hosted
  sandbox, outside the local trust boundary the spec describes. It also adds
  a second agent registry next to Stewardry's own.
- **The Messages API MCP connector pointed at the gateway:** it would need
  to hand a user's gateway bearer token to the model provider. That breaks
  "tokens remain inside the existing MCP gateway and broker boundary."
- **The SDK Tool Runner (beta):** it would be workable, but it hides the loop
  behind a beta helper. The manual loop is small and keeps checks explicit.
- **LangGraph or LangChain:** framework weight and a provider abstraction
  that iteration 1 does not need.
- **A cheaper default model (Sonnet or Haiku):** not chosen up front. A
  model change is a gated change under spec section 7.3. It can be evaluated
  later against the accepted baseline.
- **Celery and Redis, or Temporal, for jobs:** extra services for one
  operator on one machine.

## Consequences

- Prompt-injection defense rests on authorization and data handling, not
  on screening. Screening only supplements it (spec section 4.4).
- Adaptive thinking cannot be turned off, so cost and latency include
  thinking tokens. Effort is the tuning lever.
- One process means a restart drops in-memory user credentials. Running
  tasks then pause until the user signs in again (ADR 0006, gap G6). This is
  deliberate.

## Possible future change

The owner may move the agent runtime to the Vercel AI SDK later. That would
be a new ADR that replaces this one. Nothing is built for it now. Things that
ADR would need to address:

- **Language.** The Vercel AI SDK is TypeScript. Moving to it reopens
  ADR 0001 (one language with the broker) and probably ADR 0005 (the web
  app).
- **Same controls.** Every tool call must still go through the controlled
  executor, and the model must still never see credentials or MCP tools.
  The model boundary must keep recording model ID, prompt version, usage,
  cost, and limits.
- **Provider.** Iteration 1 allows only Claude through the Anthropic API
  (spec section 4.8). A multi-provider SDK must stay configured for that
  single provider.
- **Evidence.** A runtime change is a gated change under spec section 7.3.
  It needs a full live-model eval compared with the last accepted baseline.
