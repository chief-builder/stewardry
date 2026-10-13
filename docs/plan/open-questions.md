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
| Q4, restriction design (2026-10-12) | Page restrictions only: Sales sees the policy space; SEC-04, SEC-09, and SEC-17 are restricted to GRC |
| Q5, Atlassian users (2026-10-12) | None exist yet. The owner creates Sales, GRC, Legal, and an expert author, none of them the site admin |
| Publishing the synthetic pages (2026-10-12) | Stewardry renders them (`make confluence-pages`); the owner pastes them into Confluence. Stewardry never writes to Atlassian |
| Q7, refusal fallback (2026-10-13) | Off: a run never silently changes model |
| Q8, model (2026-10-13) | `claude-opus-5-5` for iteration 1 |
| Q9, spend limits (2026-10-13) | $5 and 400 model calls per run, 8 per question, $10 per installation per UTC day ([brief](../brief.md)) |

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

4. *Resolved 2026-10-12 (see above).*
5. *Resolved 2026-10-12 (see above).* Still to note: which account is the
   site admin, so that it is never connected to Stewardry.
6. **Registration age.** Did the Atlassian registration report a
   client-secret expiry date?

### Model and cost

7. *Resolved 2026-10-13 (see above).*
8. *Resolved 2026-10-13 (see above).*
9. *Resolved 2026-10-13 (see above).*

### Process and scope

10. **Spec filename.** Keep `docs/stewardry-spec.md`, or rename it to
    `docs/SPEC.md`?
11. **Templates.** *Default applied in S8:* I designed one `.xlsx` and one
    `.docx` template (`fixtures/templates/`). Keep them, or supply your own?
12. **Policy domain.** *Default applied in S8:* the policies are fully
    invented, on the topics spec section 5.2 lists, with no framework text.
    Keep this, or align the topics to a public framework's list?
13. **NDA timing.** May NDA shadow overlap hypercare?
14. **The stack tier in CI.** May CI run the stack tier on pull requests?
    It needs no secrets, and it uses a read-only checkout of
    vendor-token-broker at the pinned SHA. Locally, the stack tier cannot
    run while the real stack holds connections (G1), so CI is its main home.
