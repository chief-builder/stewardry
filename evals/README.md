# Evaluation data (never agent-visible)

Expected outcomes for the synthetic questionnaires in `fixtures/`. Runtime
code and the agent's tools must never read this directory (ADR 0002, spec
section 7.1). A test fails if anything under `src/` refers to it.

`datasets/questionnaire-v1.yaml` holds one item per question, plus one per
hidden adversarial location. Each item names its set (spec section 7.1):

| Set | Count | Expected outcome |
|---|---|---|
| `golden` | 75 | `answer`, with the key points and the policy passages that support it |
| `abstention` | 6 | `needs_expert`: no policy covers the question |
| `conflict_staleness` | 5 | `needs_expert`: conflicting, stale, or ambiguous evidence |
| `adversarial` | 9 | `flag` or `screening_finding`: never followed, never leaked |

Citations use `SEC-05#5.2` (policy and passage). `must_not_cite` lists
superseded passages that would make an answer wrong even if it reads well.

Grading follows `rubrics/answer-grading-v1.md`. Evidence strength follows
`rubrics/evidence-strength-v1.md`. The author grades the results. Any
automated assistance and every disagreement are recorded with the report
(spec section 7.2).

These are versioned fixtures, not measurements. No results exist until an
eval run records them, with its provenance label (ADR 0007).
