# Open questions for the owner

The first group blocks connected work. Offline Milestone 1 steps S0–S13 can
start without any of these answers, except Q16 and Q17.

## Identity and the broker (blocks S14–S16)

1. **G1, persistence.** Keep the broker's dev stack (in-memory custody,
   test passwords, connections lost on every restart)? Or set up a
   persistent local stack from the pinned images, with configuration kept in
   the Stewardry repo? This decides whether two weeks of hypercare are
   practical.
2. **G2, the hub client and users.** May Stewardry add a `stewardry` client
   and persona users to the `mcp` realm? If yes, which way: a Stewardry-owned
   realm overlay, an admin-API bootstrap script, or dynamic registration
   plus admin-created users? This reconfigures the identity realm, not the
   Atlassian registration, but you asked that nothing be reconfigured without
   approval.
3. **G4, error contract.** Is it acceptable to classify gateway errors by
   pinned message shapes, with unknown errors failing closed to
   `unavailable`? Or do you want to change the broker later to add
   structured error codes (outside this project)?
4. **G6, credential lifetime.** Should Stewardry hold reviewer credentials
   in memory only (drafting pauses when the hub session ends or Stewardry
   restarts)? Or store an `offline_access` refresh token encrypted and bound
   to the task grant? Memory only is proposed.
5. **Hub user names.** The realm's `alice` and `bob` match the spec's Alice
   (Sales) and Bob (GRC). Should we keep those two and add
   `legal`, `platform`, `auditor`, `owner`, and `sme`, or use another naming
   scheme?

## Atlassian setup

6. **Plan.** Is the existing site on Confluence Free today? Are you willing
   to move Confluence to Standard (or use a Standard trial) for the page
   restrictions in spec section 4.3.1?
7. **Restriction design.** Should Sales be excluded from the whole policy
   space by space permissions, with page restrictions on confidential
   pages? Or should only page restrictions be used? Page-only is closer to
   the spec's wording. Space plus page is easier to keep correct.
8. **Atlassian users.** Do Sales, GRC, Legal, and an expert-author account
   already exist on the site? Which existing account is the site admin, so
   that it is never connected to Stewardry?
9. **Registration age.** Did the Atlassian registration report a
   client-secret expiry date? (Linear and Cloudflare expire about 2026-12-26
   and 2026-12-27; Atlassian's is unknown.)

## Model and cost

10. **Refusal fallback.** Leave the Anthropic server-side refusal fallback
    off, so a run never silently changes model? This is proposed. Or enable
    it and record the fallback model per call?
11. **Model.** Accept `claude-opus-5-5` as the iteration 1 model? A cheaper
    model would be a later, eval-gated change.
12. **Spend limits.** What daily and per-run spend ceilings should be the
    starting operational budgets? They can be changed later, but each change
    is recorded.

## Process and scope

13. **Spec filename.** You referred to `docs/SPEC.md`, but the file is
    `docs/stewardry-spec.md`. Keep the current name, or rename it?
14. **GitHub repository.** Should I create `chief-builder/stewardry` as a
    public repository and open this plan as a pull request? Nothing has been
    pushed. The repository exists only locally, with the spec on `main` and
    this plan on `docs/phase-1-plan`.
15. **NDA timing.** Must NDA shadow wait until hypercare ends (playbook step
    8), or may it overlap hypercare? Overlap would let the full section 12
    demo happen sooner.
16. **Templates.** Is one `.xlsx` and one `.docx` template enough for
    iteration 1? Do you want to supply a layout you prefer, or should I
    design both? This is needed for S8.
17. **Policy domain.** Should the roughly 20 synthetic policies follow a
    recognizable public control framework's topic list, for realism, while
    staying synthetic? Or should they be fully invented? This is needed for
    S8.
18. **Hosting and tests in CI.** May CI run the `stack` tier (Docker, the
    broker's stand-ins, no secrets) on pull requests? It needs a read-only
    checkout of `chief-builder/vendor-token-broker` at the pinned SHA.
