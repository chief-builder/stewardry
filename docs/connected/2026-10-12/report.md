# Connected verification, 2026-10-12: **connected**

One person operates every role (participant count: 1).

| Check | Result |
|---|---|
| site and account mapping | pass |
| Jira field read as Sales | pass |
| full page read as GRC | pass |
| restricted page denied to Sales | pass |
| expert edit detected | pass |
| outage handling | pass |
| disconnect then consent | pass |
| no fallback identity | pass |

## Review notes (added after the run, 2026-10-12)

These notes were written while reviewing the recorded files. They are not
part of the harness output.

- **This is the second run of the day.** The first run found four failures,
  all caused by response shapes the adapter did not yet read: the `{"data": …}`
  envelope, `metadata.version`, `resources[].cloudId`, and `<p>` inside
  Confluence table cells. Those were fixed and covered by offline contract
  tests (`tests/unit/test_atlassian_rovo_shapes.py`) before this run.
- **The Jira check passed, but only partly.** `getJiraIssue` was asked for
  `created`, `reporter`, and `summary` (by key). Only `summary` came back. The
  site has no `customer` field, so it was not requested. The harness has
  since been changed to report this case as "partial", not "pass". A rerun
  of this one check, the same day and also connected, asked for
  `view: "full"`. `summary`, `reporter`, and `created` came back. Only
  `customer` was missing, as expected. That rerun's raw output stays out of
  the repo because it holds the reporter's email address; the adapter now
  keeps only the display name. See
  [broker-contracts.md](../../broker-contracts.md#observed-on-the-real-atlassian-site-s15-2026-10-12).
- **Sanitized for the public repo.** The site's cloud ID, site name, and
  Atlassian account IDs were replaced with placeholders. The placeholders
  match the ones in `tests/fixtures/atlassian-rovo-2026-10-12.json`, with
  1 = sales, 2 = grc, 3 = legal, and 4 = the policy expert (page author).
  Content hashes were computed on the raw bodies; `page-body.html` holds no
  replaced value and still matches its hash. The consent link's transaction
  value was removed by the harness.
- **One label was corrected.** In `responses.json`, the
  `disconnect_atlassian` call was recorded under `grc`. The call used Legal's
  credential (`tests/connected/test_connected.py`). The recorder had kept the
  previous call's label. The label now reads `legal`, and the harness sets
  the label for direct calls.
