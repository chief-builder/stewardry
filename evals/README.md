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

## Running evals

The harness is in `tools/` (`run_evals.py` and `eval_report.py`). It is not
runtime code, which is why it may read this directory.

| Command | Provenance | What it does |
|---|---|---|
| `make check` | `offline-replay` | Replays the recorded four-item smoke scenario (`replay/drafting-smoke/`) and scores it. This is the deterministic subset |
| `make eval-live` | `live-model-eval` | Drafts all three questionnaires against the live model, with the fixture adapter for Atlassian. Records every call to `replay/full-<date>/` and writes a report and a grading file. Costs money |
| `make eval-replay RECORDINGS=replay/full-<date>` | `offline-replay` | Replays a recorded baseline offline |

Reports go to `reports/<date>-<suite>-<provenance>.json` and `.md`. Each one
records:

- the model, effort, and prompt version;
- the dataset and its SHA-256;
- the agent policy hash and a control configuration hash (baseline, limits,
  pricing, catalog);
- the pricing reference, the code revision, and the `as_of` date.

Rules:

- **Raw and valid completion are reported separately.** A run with a boundary
  violation is not valid.
- **Zero-tolerance counts include failed runs.**
- **Small samples are labeled.** Any rate with fewer than 20 items reads
  "insufficient data (n=...)".
- **Automatic checks are labeled as automatic.** These are citation presence
  and overlap with the expected citations; they are not faithfulness grades.
- **Correctness is "not graded"** until the author fills in
  `grades/<report>.yaml`.
- **Replays spend nothing.** Replay reports show the recorded calls' usage,
  say that no money was spent, and do not report latency.
