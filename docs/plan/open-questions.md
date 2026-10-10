# Open questions for the owner

## Resolved on 2026-10-10

| Question | Decision |
|---|---|
| G1, persistence | Keep the in-memory test setup for now |
| G2, hub client and users | Add support for the other roles. Design in [ADR 0008](../adr/0008-hub-roles-bootstrap.md) |
| G3, per-agent policy | Brainstorm and add it. Design in [ADR 0009](../adr/0009-per-agent-policy.md) |
| G4, error contract | Brainstorm and fix it. Design in [ADR 0010](../adr/0010-gateway-error-classification.md), with no broker change |
| G6, credential lifetime | Memory only; drafting pauses when credentials lapse |
| Confluence plan | You are willing to move to Standard if needed |
| GitHub repository | Create `chief-builder/stewardry` |

## Still open

Offline Milestone 1 steps S0–S13 can start without these, except Q11 and
Q12, which S8 needs.

### Identity and the broker

1. **Hub user names.** ADR 0008 keeps `alice` (Sales) and `bob` (GRC) and
   adds `legal`, `platform`, `auditor`, `owner`, and `sme`, plus an optional
   `sales2`. Do you agree?
2. **G1 and hypercare.** With in-memory custody, the stack must stay up for
   the whole two-week hypercare window, or every reconnect is recorded as an
   event. Is that acceptable, or do you want to revisit G1 before hypercare?
3. **Upstream proposals.** Do you want to take
   [the broker proposals](../proposals/broker-upstream-proposals.md)
   (structured error codes, per-client policy, correlation IDs) to
   vendor-token-broker as separate work? Stewardry does not depend on them.

### Atlassian setup

4. **Restriction design.** Should Sales be excluded from the whole policy
   space by space permissions, with page restrictions on confidential
   pages? Or should only page restrictions be used? Page-only is closer to
   the spec's wording. Space plus page is easier to keep correct.
5. **Atlassian users.** Do Sales, GRC, Legal, and an expert-author account
   already exist on the site? Which existing account is the site admin, so
   that it is never connected to Stewardry?
6. **Registration age.** Did the Atlassian registration report a
   client-secret expiry date?

### Model and cost

7. **Refusal fallback.** Leave the Anthropic server-side refusal fallback
   off, so a run never silently changes model? This is proposed.
8. **Model.** Accept `claude-opus-5-5` as the iteration 1 model?
9. **Spend limits.** What daily and per-run spend ceilings should be the
   starting operational budgets?

### Process and scope

10. **Spec filename.** Keep `docs/stewardry-spec.md`, or rename it to
    `docs/SPEC.md`?
11. **Templates.** Is one `.xlsx` and one `.docx` template enough for
    iteration 1, and should I design both?
12. **Policy domain.** Should the synthetic policies follow a public control
    framework's topic list, or be fully invented?
13. **NDA timing.** May NDA shadow overlap hypercare?
14. **The stack tier in CI.** May CI run the stack tier on pull requests?
    It needs no secrets, and it uses a read-only checkout of
    vendor-token-broker at the pinned SHA. Locally, the stack tier cannot
    run while the real stack holds connections (G1), so CI is its main home.
