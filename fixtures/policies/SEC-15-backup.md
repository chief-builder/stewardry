---
policy_id: SEC-15
title: Backup
version: 2
status: current
classification: internal
owner: Security Engineering Lead
effective_date: "2026-09-01"
review_date: "2027-09-01"
confluence_page_id: "115"
---

# SEC-15 Backup

## Backups

15.1 Production databases are backed up continuously with point-in-time recovery.

15.2 Backups are encrypted at rest as described in SEC-05.

15.3 Backups are retained for 90 days.

## Protection and testing

15.4 Backups are stored in a cloud account separate from production, with restricted deletion rights.

15.5 Restores from backup are tested every quarter.
