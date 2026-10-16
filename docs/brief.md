# Engagement brief: security questionnaire response

Status: recorded before the first shadow session (spec sections 4.6, 4.7,
5.4 and 9.1). Owner decisions are dated. Values marked **proposed** become
agreed when the owner merges the pull request that adds them; change them
here, not in code.

This is a controlled local exercise. One person operates every role
(participant count: 1). Nothing here is a production SLO, a measured
result, or a claim of adoption.

## Workflow and roles

The workflow follows spec section 5.1. In Milestone 1 the engagement is in
**shadow**:

- Drafts stay in the restricted GRC review surface.
- Nothing is exported or returned to Sales.

| Role | Persona | Authority |
|---|---|---|
| Sales | alice | Submits requests and uploads questionnaires for assigned customers |
| GRC | bob | Authorizes drafting under their own identity, and reviews drafts |
| Policy expert | (an Atlassian account, never connected to Stewardry) | Edits policy pages in Confluence |
| Engagement owner | owner | Accepts eval reports, and moves the agent through rollout gates |
| Platform engineer | platform | Applies agent policies and the access mapping |

Roles come from Stewardry's audited role mapping (ADR 0008).

## Owner decisions (2026-10-13)

- **Model:** `claude-opus-5-5` (Q8), the model of the
  [2026-10-11 live eval](../evals/reports/2026-10-11-full-live-model-eval.md).
- **Refusal fallback:** off (Q7). A run never silently changes model. A
  refusal routes the question to an expert.
- **Budgets (Q9):**

  | Limit | Value | Set in |
  |---|---|---|
  | Spend per run | $5.00 | `policies/agents/questionnaire-agent.yaml` |
  | Model calls per run | 400 | same file |
  | Model calls per question | 8 | same file |
  | Tool calls per run | 1200 | same file |
  | Spend per installation per UTC day | $10.00 | `policies/model-limits.yaml` |

  The live eval cost $3.45 for about 90 drafted items ($0.038 each), so a
  30-question run is expected to cost about $1.15. The ceilings leave about
  4 times that.
- **Shadow design:** QN-LARKSPUR-02 is completed by hand first, then drafted
  by Stewardry and reviewed. See [Reviewer-time baseline](#reviewer-time-baseline).
- **Grading:** the owner grades the
  [2026-10-11 eval items](../evals/grades/full-2026-10-11.yaml) before the
  shadow session, so the grades are not influenced by the shadow drafts.
  Done on 2026-10-16. See the
  [graded replay](../evals/reports/2026-10-16-full-offline-replay.md) and its
  [limits](../evals/reports/README.md#2026-10-16-graded-replay-of-the-same-baseline).
  The shadow gate still names the live report: the grades judge the live
  answers, and a replay is not a live-model check (spec 7.3).

## Golden set and rubrics

- **Golden set:** `evals/datasets/questionnaire-v1.yaml`
  (`questionnaire-evals@1`), with 95 items over three questionnaires.
- **Rubrics:**
  - [answer grading](../evals/rubrics/answer-grading-v1.md);
  - [evidence strength](../evals/rubrics/evidence-strength-v1.md).
- **Release gate for shadow:**
  - the live eval report covers the current agent policy hash;
  - the owner accepts it in the registry (`registry.evaluation_accepted`).
- **Retrieval changes since that report:** S15 changed how the adapter reads
  Atlassian's response envelope and Confluence's table HTML. A replay of
  the full suite on the 2026-10-13 code gave the same items, metrics, and
  zero-tolerance counts as the 2026-10-11 replay. The control-config hash
  differs only because of description and comment text in the catalog and
  in `model-limits.yaml`. No values changed.

## Thresholds

These are **gates**: any failure blocks moving beyond shadow, regardless of
earlier results:
- no uncited factual answer reaches review;
- no unauthorized access;
- no cross-customer leakage;
- no injection compliance;
- abstention on every designated no-coverage item.

**Proposed** quality thresholds for leaving shadow. They are judged on
graded items, with sample sizes reported:

| Metric | Threshold | Status |
|---|---|---|
| Correctness of ready-for-review drafts | at least 90% graded correct | proposed |
| Citation faithfulness | at least 95% | candidate target (spec 5.4), not a result |
| Needs-expert recall on no-coverage items | 100% | gate |
| Reviewer-time reduction | at least 50% | candidate target (spec 5.4), not a result |

Below about 20 graded items, a metric is reported as "insufficient data".

## Reviewer-time baseline

Method (spec 4.7), agreed before measurement:

1. **Manual completion:**
   - The operator copies `fixtures/questionnaires/QN-LARKSPUR-02.xlsx` to
     `build/shadow/manual-QN-LARKSPUR-02.xlsx`.
   - They fill in the Answer column by reading the SECPOL pages in
     Confluence as GRC.
   - They time active minutes with a stopwatch, pausing for breaks.
   - Golden answers are not opened during the completion.
2. **Assisted review:** in the same session or the next one, Stewardry
   drafts the same questionnaire. The operator records a verdict on every
   draft in the review surface, timing active minutes the same way.
3. **What's recorded:**
   - Task size: 30 questions.
   - The order: manual first.
   - Both self-reported times.
   - The span between the first and last verdict, from Stewardry's
     timestamps.

**Known limitations:**
- **Learning effects:** the operator wrote the questionnaire and its golden
  answers, and completes it by hand before reviewing the drafts. The
  assisted review benefits from that.
- **One sample:** it says nothing about the 50% candidate target ("insufficient
  data").

## Numerical operating objectives (proposed)

These are objectives for this local exercise. Provider outages, consent
failures, and authorization denials are reported separately from
application errors (spec 4.6).

| Objective | Target | Measured from |
|---|---|---|
| Gateway availability during operating windows | at least 99% of gateway calls not `unavailable` | audit events |
| Latency to first draft | at most 2 minutes from drafting start | run and draft timestamps |
| Full 30-question completion | at most 15 minutes from drafting start | run and draft timestamps |
| Questions ending in a drafting error | at most 5% | draft outcomes |

The live eval's model-call latency was p50 2.4 s and p95 4.8 s.

## Operating windows (proposed)

- **When:** shadow sessions run only while the operator is at the machine,
  on weekdays between 09:00 and 18:00 local time.
- **Outside windows:** the stack may stay up. Outages there are not counted
  against availability.
- **Restarts:** with in-memory custody (G1), every stack restart disconnects
  all Atlassian accounts. Each reconnect is recorded as an operational
  event.

## Retention

See [retention.md](retention.md).
