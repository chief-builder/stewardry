# Earliest demo-ready cut of Iteration 1

## Timing

Spec section 9 orders the playbook as:

1. shadow;
2. assisted;
3. go-live gate;
4. handoff;
5. two weeks of hypercare;
6. NDA reuse.

The Sales file return in demo step 5 is allowed only from **assisted** onward.

The earliest point for a credible five-minute connected walkthrough
(spec section 12) is therefore **after the assisted gate is recorded and at
least one actual assisted session has returned an approved file**. That is
before the go-live gate, and well before hypercare finishes.

## What the cut contains

It covers all of Milestone 1 and Milestone 2, plus this narrow slice of
Milestone 3:

- the connected Atlassian and Stewardry workflow for **one `.xlsx`
  template** only;
- approve, edit, and reject, with revision-bound approvals and revalidation;
- expert routing, resolved by a manual Confluence update that Stewardry
  detects;
- approved-file return to Sales, with sanitization checks for that template
  (comments, hidden cells, links, metadata);
- a minimal dashboard with actual figures from connected sessions;
- an offline replay of the same walkthrough.

## The walkthrough in that cut

| Spec step | Shown in this cut | Evidence kind |
|---|---|---|
| 1. Sales creates a Jira request, submits the key and file, sees status | Yes | connected |
| 2. GRC authorizes under its own identity; drafts appear; two items go to an expert | Yes | connected (live model) |
| 3. GRC edits and approves; missing evidence is resolved by an authoritative update | Yes, through a manual Confluence edit by the expert account | connected |
| 4. A planted injection is flagged or denied and appears in the audit trail | Yes (Milestone 2 adversarial controls) | connected, plus live-model eval results shown separately |
| 5. The approved file returns to Sales without confidential content | Yes, `.xlsx` only | connected |
| 6. The dashboard shows cost, turnaround, edits, and session counts with the single-participant caveat | Yes, from the actual sessions to date, with sample sizes | connected |
| 7. The registry shows the handoff, NDA in shadow, and monitoring evidence | **Partly.** The registry shows `questionnaire-agent` at `assisted`, with its gate records. `nda-triage-agent` is registered in `discovery` or `build`. Monitoring covers the dated sessions so far | connected |

## What this cut does not yet show

State these plainly in the recording and the README:

- **No handoff and no hypercare.** The go-live gate, the handoff
  acceptance, and the two-week monitoring record do not exist yet. Nothing
  is shown as "live" or "handed off".
- **No NDA triage in shadow.** The NDA agent is registered only. There are
  no NDA findings, no playbook retrieval under Legal's identity, and no
  reuse-effort measurement.
- **No `.docx` export.** Only one `.xlsx` template is supported for
  return. Multi-sheet and formula preservation are shown only for that
  template.
- **No reviewed-answer library.** Promotion, sanitization for reuse, and
  invalidation of library entries are not built.
- **No weekly control report over a full week** unless a full week of
  sessions has actually happened. Otherwise the report is labeled
  "insufficient data".
- **No measured reviewer-time reduction claim.** The figures shown are the
  sessions measured so far against the agreed baseline method. The 50%
  target stays a candidate.
- **No production or availability claims.** Operating-window measurements
  cover only the recorded windows.
- **Persistent custody depends on gap G1.** If the dev stack is still in
  use, the cut states that Atlassian connections reset on restart.

## The next cut (optional)

A second recording, made after the handoff and partway through hypercare,
can replace step 7 with:

- the recorded handoff;
- the hypercare day count so far, labeled as in progress;
- the NDA agent in shadow, but only if NDA shadow may overlap hypercare
  ([open question 15](open-questions.md)).

The complete section 12 demo needs Milestone 3 to be finished.
