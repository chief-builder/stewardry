---
policy_id: SEC-04
title: Privileged Access
version: 2
status: current
classification: confidential
owner: Security Engineering Lead
effective_date: "2026-08-01"
review_date: "2027-08-01"
confluence_page_id: "104"
---

# SEC-04 Privileged Access

## Just-in-time administration

4.1 Production administrative access is granted just in time for at most 4 hours and requires a linked change or incident ticket.

4.2 Privileged sessions are recorded, and recordings are retained for 1 year.

4.3 No more than six engineers hold standing eligibility for production administration.

## Credentials and review

4.4 Root and break-glass credentials are stored in a hardware-backed vault that requires two people to retrieve them.

4.5 Eligibility for privileged access is reviewed every month.
