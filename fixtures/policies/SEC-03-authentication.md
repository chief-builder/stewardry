---
policy_id: SEC-03
title: Authentication
version: 2
status: current
classification: internal
owner: Security Engineering Lead
effective_date: "2026-08-01"
review_date: "2027-08-01"
confluence_page_id: "103"
---

# SEC-03 Authentication

## Workforce authentication

3.1 Multi-factor authentication is required for all workforce access to production systems, source code, and administrative consoles.

3.2 Workforce identities are managed centrally in a single sign-on provider.

3.3 Passwords, where used, are at least 14 characters long and are checked against lists of known breached passwords.

## Customer authentication and sessions

3.4 Customer accounts support SAML 2.0 single sign-on and time-based one-time password multi-factor authentication.

3.5 Sessions to administrative consoles expire after 12 hours.
