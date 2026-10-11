---
policy_id: SEC-08
title: Logging and Monitoring
version: 1
status: current
classification: internal
owner: Security Engineering Lead
effective_date: "2026-08-01"
review_date: "2027-08-01"
confluence_page_id: "108"
---

# SEC-08 Logging and Monitoring

## Logging

8.1 Security-relevant events from production systems, the identity provider, and the cloud control plane are logged centrally.

8.2 Logs are protected from modification, and access to them is restricted to the security team.

8.3 Security logs are retained for 1 year.

8.4 Clocks on production systems are synchronized to a common time source.

## Monitoring

8.5 Alerts for suspicious activity are triaged by an on-call engineer 24 hours a day, 7 days a week.
